from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock
from terminalweave.billing.credit import CreditAccount, CreditController
from terminalweave.billing.discounts import DiscountBook
from terminalweave.billing.invoice import Invoice, InvoiceLine
from terminalweave.billing.rate_book import HistoricalTariffBook
from terminalweave.billing.reconciliation import PaymentRecord
from terminalweave.core.checksum import PayloadChecksum
from terminalweave.core.clock import ensure_utc, utc_now
from terminalweave.core.errors import ConflictError, NotFoundError, ValidationError
from terminalweave.core.ids import new_id
from terminalweave.core.money import Money
from terminalweave.core.serialization import to_primitive

STATUS_OPEN = 'OPEN'
STATUS_PAID = 'PAID'

@dataclass(frozen=True)
class ChargeItem:
    """A business event to be charged.

    ``occurred_at`` pins the rate version: the charge is priced with the
    tariff that was in effect when the business happened, never the
    current one.
    """

    reference: str
    operation: str
    container_size: int
    quantity: Decimal
    occurred_at: datetime
    hazardous: bool = False
    reefer: bool = False

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValidationError('charge item reference required')
        if self.quantity < 0:
            raise ValidationError('quantity cannot be negative')
        object.__setattr__(self, 'quantity', Decimal(str(self.quantity)))
        object.__setattr__(self, 'occurred_at', ensure_utc(self.occurred_at))

@dataclass(frozen=True)
class ChargeLine:
    """Immutable priced detail. The unit price is a snapshot of the
    point-in-time tariff and must never be recomputed later."""

    line_id: str
    reference: str
    operation: str
    container_size: int
    quantity: Decimal
    unit_price: Money
    amount: Money
    rate_effective_from: datetime
    hazardous: bool = False
    reefer: bool = False

    @property
    def description(self) -> str:
        flags = []
        if self.hazardous:
            flags.append('hazmat')
        if self.reefer:
            flags.append('reefer')
        suffix = f' ({",".join(flags)})' if flags else ''
        return f'{self.operation} {self.container_size}{suffix}'

class SettlementRequest:

    def __init__(self, request_id: str, customer_id: str, items: list[ChargeItem],
                 currency: str='USD', discount_codes: set[str] | None=None,
                 payments: list[PaymentRecord] | None=None):
        if not request_id.strip():
            raise ValidationError('request id required')
        if not customer_id.strip():
            raise ValidationError('customer id required')
        if not items:
            raise ValidationError('settlement requires at least one charge item')
        self.request_id = request_id.strip()
        self.customer_id = customer_id.strip()
        self.currency = currency
        self.items = list(items)
        self.discount_codes = set(discount_codes or set())
        self.payments = list(payments or [])

    def fingerprint(self) -> str:
        payload = {
            'request_id': self.request_id,
            'customer_id': self.customer_id,
            'currency': self.currency,
            'discount_codes': sorted(self.discount_codes),
            'items': [to_primitive({
                'reference': i.reference,
                'operation': i.operation,
                'container_size': i.container_size,
                'quantity': i.quantity,
                'occurred_at': i.occurred_at,
                'hazardous': i.hazardous,
                'reefer': i.reefer,
            }) for i in self.items],
            'payments': [to_primitive(p) for p in self.payments],
        }
        return PayloadChecksum.digest(payload)

@dataclass(frozen=True)
class SettlementBill:
    bill_id: str
    request_id: str
    customer_id: str
    currency: str
    lines: tuple[ChargeLine, ...]
    subtotal: Money
    discount_code: str | None
    discount_amount: Money
    receivable: Money
    payments: tuple[PaymentRecord, ...]
    paid: Money
    due: Money
    status: str
    created_at: datetime
    checksum: str

    def payload(self) -> dict[str, object]:
        return {
            'bill_id': self.bill_id,
            'request_id': self.request_id,
            'customer_id': self.customer_id,
            'currency': self.currency,
            'lines': [to_primitive({
                'line_id': ln.line_id,
                'reference': ln.reference,
                'operation': ln.operation,
                'container_size': ln.container_size,
                'quantity': ln.quantity,
                'unit_price': ln.unit_price,
                'amount': ln.amount,
                'rate_effective_from': ln.rate_effective_from,
                'hazardous': ln.hazardous,
                'reefer': ln.reefer,
            }) for ln in self.lines],
            'subtotal': self.subtotal,
            'discount_code': self.discount_code,
            'discount_amount': self.discount_amount,
            'receivable': self.receivable,
            'payments': [to_primitive(p) for p in self.payments],
            'paid': self.paid,
            'due': self.due,
            'status': self.status,
            'created_at': self.created_at,
        }

    @classmethod
    def seal(cls, **values) -> 'SettlementBill':
        created_at = ensure_utc(values['created_at'])
        values['created_at'] = created_at
        bill = cls(checksum='', **values)
        object.__setattr__(bill, 'checksum', PayloadChecksum.digest(bill.payload()))
        return bill

    def verify(self) -> bool:
        return PayloadChecksum.verify(self.payload(), self.checksum)

@dataclass(frozen=True)
class SettlementResult:
    bill: SettlementBill
    invoice: Invoice

class SettlementService:
    """Unified settlement pipeline.

    Charge lines are priced from the historical tariff book at each item's
    occurrence time, then discount, credit authorization and payment are
    applied in that order. All work is staged against local copies; state
    is committed in a single critical section, so a failure at any stage
    leaves no partial bill. A repeated request id replays the identical
    stored result.
    """

    def __init__(self, tariff_book: HistoricalTariffBook, discount_book: DiscountBook | None=None,
                 accounts: dict[str, CreditAccount] | None=None,
                 credit_controller: CreditController | None=None,
                 payment_tolerance: Decimal=Decimal('0.01'), clock=utc_now):
        self.tariff_book = tariff_book
        self.discount_book = discount_book
        self.accounts = accounts or {}
        self.credit = credit_controller or CreditController()
        self.payment_tolerance = Decimal(str(payment_tolerance))
        self._clock = clock
        self._bills: dict[str, SettlementResult] = {}
        self._by_request: dict[str, tuple[str, str]] = {}
        self._lock = RLock()

    # ---- lookups -------------------------------------------------------

    def result_for(self, request_id: str) -> SettlementResult:
        entry = self._by_request.get(request_id)
        if entry is None:
            raise NotFoundError(f'no settlement for request {request_id}')
        return self._bills[entry[0]]

    def bill(self, bill_id: str) -> SettlementResult:
        result = self._bills.get(bill_id)
        if result is None:
            raise NotFoundError(f'no settlement bill {bill_id}')
        return result

    # ---- pipeline ------------------------------------------------------

    def settle(self, request: SettlementRequest) -> SettlementResult:
        fingerprint = request.fingerprint()
        with self._lock:
            entry = self._by_request.get(request.request_id)
            if entry is not None:
                bill_id, original_fingerprint = entry
                # Replays must be byte-identical; a reused id with a
                # different request is a conflict, never a second bill.
                if original_fingerprint != fingerprint:
                    raise ConflictError(
                        f'request {request.request_id} was already settled with different content')
                return self._bills[bill_id]

            bill_id = str(new_id('STL'))
            created_at = ensure_utc(self._clock())
            # Stage the whole pipeline on copies. Nothing here touches
            # shared state, so any exception simply unwinds with no bill
            # created and no idempotency reservation left behind.
            staged = self._stage(request, bill_id, created_at)
            # Commit point: publish the immutable bill and post the
            # account movements that were already proven on a probe copy.
            account = self.accounts.get(request.customer_id)
            if staged.bill.receivable.amount > 0:
                self.credit.authorize(account, staged.bill.receivable)
                self.credit.payment(account, staged.bill.paid)
            self._bills[bill_id] = staged
            self._by_request[request.request_id] = (bill_id, fingerprint)
            return staged

    def _stage(self, request: SettlementRequest, bill_id: str, created_at: datetime) -> SettlementResult:
        lines = self._price_lines(request, bill_id)
        subtotal = self._sum(lines, request.currency)
        discount_code, discount_amount = self._discount(request, subtotal)
        receivable = subtotal - discount_amount

        paid = self._sum_payments(request)
        if paid.amount > receivable.amount + self.payment_tolerance:
            raise ValidationError(
                f'payments {paid.amount} exceed receivable {receivable.amount}')
        if (receivable.amount - paid.amount) <= self.payment_tolerance:
            due = Money.zero(request.currency)
            paid = receivable
        else:
            due = receivable - paid
        status = STATUS_PAID if due.amount == 0 else STATUS_OPEN

        account = self.accounts.get(request.customer_id)
        if receivable.amount > 0 and account is None:
            raise NotFoundError(f'no credit account for customer {request.customer_id}')
        # Prove the postings on a private copy before the commit section.
        if account is not None and receivable.amount > 0:
            probe = deepcopy(account)
            self.credit.authorize(probe, receivable)
            self.credit.payment(probe, paid)

        bill = SettlementBill.seal(
            bill_id=bill_id,
            request_id=request.request_id,
            customer_id=request.customer_id,
            currency=request.currency,
            lines=tuple(lines),
            subtotal=subtotal,
            discount_code=discount_code,
            discount_amount=discount_amount,
            receivable=receivable,
            payments=tuple(request.payments),
            paid=paid,
            due=due,
            status=status,
            created_at=created_at,
        )
        invoice = self._invoice(bill)
        return SettlementResult(bill, invoice)

    def _price_lines(self, request: SettlementRequest, bill_id: str) -> list[ChargeLine]:
        lines = []
        for index, item in enumerate(request.items, start=1):
            tariff = self.tariff_book.tariff_at(item.operation, item.container_size, item.occurred_at)
            unit_price = tariff.price(item.hazardous, item.reefer)
            if unit_price.currency != request.currency:
                raise ValidationError(
                    f'tariff currency {unit_price.currency} does not match settlement {request.currency}')
            lines.append(ChargeLine(
                line_id=f'{bill_id}-L{index:03d}',
                reference=item.reference,
                operation=item.operation,
                container_size=item.container_size,
                quantity=item.quantity,
                unit_price=unit_price,
                amount=unit_price.scale(item.quantity),
                rate_effective_from=self.tariff_book.version_at(
                    item.operation, item.container_size, item.occurred_at).effective_from,
                hazardous=item.hazardous,
                reefer=item.reefer,
            ))
        return lines

    def _discount(self, request: SettlementRequest, subtotal: Money) -> tuple[str | None, Money]:
        if not request.discount_codes or self.discount_book is None:
            return None, Money.zero(request.currency)
        code, amount = self.discount_book.best(subtotal, request.discount_codes)
        return code, amount

    def _sum_payments(self, request: SettlementRequest) -> Money:
        total = Money.zero(request.currency)
        for payment in request.payments:
            if payment.amount.currency != request.currency:
                raise ValidationError(
                    f'payment {payment.payment_id} currency {payment.amount.currency} does not match settlement {request.currency}')
            if payment.amount.amount < 0:
                raise ValidationError('payment cannot be negative')
            total = total + payment.amount
        return total

    @staticmethod
    def _sum(lines: list[ChargeLine], currency: str) -> Money:
        total = Money.zero(currency)
        for line in lines:
            total = total + line.amount
        return total

    @staticmethod
    def _invoice(bill: SettlementBill) -> Invoice:
        invoice = Invoice(bill.bill_id, bill.customer_id, bill.currency)
        for line in bill.lines:
            invoice.add(InvoiceLine(line.line_id, line.description, line.quantity, line.unit_price))
        invoice.finalize()
        return invoice
