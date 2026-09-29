"""Unified settlement pipeline.

One business event is settled in a single, ordered pass:

1. rate every consumed item with the tariff *as of the business occurrence*
   and freeze the rated lines (immutable, checksummed);
2. apply the single best eligible discount;
3. authorize the discounted balance against credit (up to the available credit);
4. cover the remainder with payments.

A settlement either commits every side effect (credit authorization,
payment application, persisted record) or none of them. Repeated requests
carrying the same idempotency key return the identical settlement, even when
their payload differs.
"""
from __future__ import annotations
from copy import copy
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from threading import RLock

from terminalweave.billing.credit import CreditAccount, CreditController
from terminalweave.billing.discounts import DiscountBook
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.billing.reconciliation import PaymentReconciler, PaymentRecord, Receivable
from terminalweave.core.checksum import PayloadChecksum
from terminalweave.core.clock import ensure_utc, utc_now
from terminalweave.core.errors import ConflictError, NotFoundError, ValidationError
from terminalweave.core.money import Money
from terminalweave.core.serialization import to_primitive

PAYMENT_TOLERANCE = Decimal('0.01')


@dataclass(frozen=True)
class EffectiveRate:
    effective_at: object
    tariff: HandlingTariff

    def __post_init__(self):
        object.__setattr__(self, 'effective_at', ensure_utc(self.effective_at))


class RateSchedule:
    """Append-only, effective-dated handling tariffs.

    Rates are looked up with ``tariff_at`` as of a business occurrence time.
    Publishing a rate never removes an earlier one, so historical settlements
    keep rating against the rate they were originally produced with.
    """

    def __init__(self, tariffs: list[HandlingTariff] | None = None):
        self._versions: dict[tuple[str, int], list[EffectiveRate]] = {}
        self._lock = RLock()
        for tariff in tariffs or []:
            self.publish(tariff, utc_now())

    def publish(self, tariff: HandlingTariff, effective_at: object) -> EffectiveRate:
        effective_at = ensure_utc(effective_at)
        key = (tariff.operation, tariff.container_size)
        with self._lock:
            versions = self._versions.setdefault(key, [])
            if versions and effective_at < versions[-1].effective_at:
                raise ConflictError('cannot backdate a rate past the latest version')
            if versions and effective_at == versions[-1].effective_at:
                raise ConflictError('a rate is already effective at that time')
            version = EffectiveRate(effective_at, tariff)
            versions.append(version)
            return version

    def tariff_at(self, operation: str, size: int, when: object) -> HandlingTariff:
        when = ensure_utc(when)
        effective_at, tariff = self._active(operation, size, when)
        return tariff

    def effective_at_for(self, operation: str, size: int, when: object):
        return self._active(operation, size, ensure_utc(when))[0]

    def _active(self, operation: str, size: int, when: object) -> tuple[object, HandlingTariff]:
        with self._lock:
            versions = self._versions.get((operation, size), [])
            active = [v for v in versions if v.effective_at <= when]
        if not active:
            raise NotFoundError(f'no rate for {operation}/{size} effective at that time')
        return active[-1].effective_at, active[-1].tariff


@dataclass(frozen=True)
class SettlementItem:
    operation: str
    container_size: int
    quantity: int
    hazardous: bool = False
    reefer: bool = False
    description: str = ''

    def __post_init__(self):
        if self.quantity < 0:
            raise ValidationError('item quantity cannot be negative')


@dataclass(frozen=True)
class PaymentInput:
    payment_id: str
    reference: str
    amount: Money

    def __post_init__(self):
        if not self.payment_id or not self.reference:
            raise ValidationError('payment id and reference required')
        if self.amount.amount < 0:
            raise ValidationError('payment amount cannot be negative')


@dataclass(frozen=True)
class SettlementRequest:
    idempotency_key: str
    customer_id: str
    occurred_at: object
    business_ref: str
    items: tuple[SettlementItem, ...]
    payments: tuple[PaymentInput, ...] = ()
    discount_codes: frozenset[str] = frozenset()
    currency: str = 'USD'

    def __post_init__(self):
        if not self.idempotency_key or not self.customer_id or not self.business_ref:
            raise ValidationError('idempotency key, customer and business reference required')
        object.__setattr__(self, 'occurred_at', ensure_utc(self.occurred_at))
        if not self.items:
            raise ValidationError('settlement must contain at least one item')
        if any(payment.amount.currency != self.currency for payment in self.payments):
            raise ValidationError('payment currency does not match settlement currency')


@dataclass(frozen=True)
class RatedLine:
    ref: str
    operation: str
    container_size: int
    quantity: int
    unit_price: Money
    amount: Money
    tariff_effective_at: object
    hazardous: bool = False
    reefer: bool = False
    description: str = ''


@dataclass(frozen=True)
class PaymentApplication:
    payment_id: str
    reference: str
    offered: Money
    applied: Money


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    idempotency_key: str
    customer_id: str
    business_ref: str
    currency: str
    occurred_at: object
    settled_at: object
    lines: tuple[RatedLine, ...]
    subtotal: Money
    discount_code: str | None
    discount_amount: Money
    discounted_total: Money
    credit_used: Money
    credit_outstanding_after: Money
    payments: tuple[PaymentApplication, ...]
    amount_due_after: Money
    request_digest: str
    detail_checksum: str


def _digest(value: object) -> str:
    return PayloadChecksum.digest(to_primitive(value))


def _settlement_id(idempotency_key: str) -> str:
    digest = sha256(idempotency_key.encode('utf-8')).hexdigest()[:16].upper()
    return f'STL-{digest}'


@dataclass
class SettlementLedger:
    """In-process ledger and credit registry used by the settlement service.

    The whole object is guarded by one lock so a settlement commits under a
    single critical section: a failing pipeline never leaves a half-written
    bill or a partially authorized credit account.
    """

    _records: dict[str, Settlement] = field(default_factory=dict)
    _requests: dict[str, str] = field(default_factory=dict)
    _accounts: dict[str, CreditAccount] = field(default_factory=dict)

    def __post_init__(self):
        self._lock = RLock()

    def open_account(self, account: CreditAccount) -> None:
        with self._lock:
            if account.customer_id in self._accounts:
                raise ConflictError('credit account already registered')
            self._accounts[account.customer_id] = account

    def account(self, customer_id: str) -> CreditAccount | None:
        with self._lock:
            return self._accounts.get(customer_id)

    def _peek(self, idempotency_key: str) -> Settlement | None:
        with self._lock:
            settlement_id = self._requests.get(idempotency_key)
            if settlement_id is None:
                return None
            return self._records[settlement_id]

    def get(self, settlement_id: str) -> Settlement:
        with self._lock:
            record = self._records.get(settlement_id)
        if record is None:
            raise NotFoundError(f'unknown settlement {settlement_id}')
        return record

    def get_by_key(self, idempotency_key: str) -> Settlement:
        record = self._peek(idempotency_key)
        if record is None:
            raise NotFoundError('no settlement for that idempotency key')
        return record


class SettlementService:
    """Runs the unified discount -> credit -> payment pipeline atomically."""

    def __init__(self,
                 schedule: RateSchedule | None = None,
                 discounts: DiscountBook | None = None,
                 ledger: SettlementLedger | None = None,
                 credit: CreditController | None = None,
                 reconciler: PaymentReconciler | None = None,
                 clock=utc_now):
        self.schedule = schedule or RateSchedule()
        self.discounts = discounts or DiscountBook([])
        self.ledger = ledger or SettlementLedger()
        self.credit = credit or CreditController()
        self.reconciler = reconciler or PaymentReconciler()
        self._clock = clock

    # -- accounts --------------------------------------------------------

    def open_credit_account(self, customer_id: str, limit: Money, outstanding: Money | None = None) -> CreditAccount:
        account = CreditAccount(customer_id, limit, outstanding or Money.zero(limit.currency))
        self.ledger.open_account(account)
        return account

    # -- pipeline --------------------------------------------------------

    def settle(self, request: SettlementRequest) -> Settlement:
        """Settle one business event; repeat keys return the same record."""
        # The digest only covers the business request, never wall-clock data,
        # so identical requests hash identically.
        request_digest = _digest(request)
        existing = self.ledger._peek(request.idempotency_key)
        if existing is not None:
            return existing

        # The whole pipeline runs in one critical section. All staging is
        # pure/in-memory, so either every side effect commits or none does;
        # concurrent settlements for one customer cannot both authorize
        # against the same credit snapshot.
        with self.ledger._lock:
            # Re-check after acquiring: a concurrent identical request may have
            # committed while we waited.
            existing = self.ledger._requests.get(request.idempotency_key)
            if existing is not None:
                return self.ledger._records[existing]

            lines = self._rate(request)
            subtotal = self._sum((line.amount for line in lines), request.currency)
            detail_checksum = self._lines_checksum(lines)

            discount_code, discount_amount = self.discounts.best(subtotal, set(request.discount_codes))
            discounted_total = subtotal - discount_amount

            credit_used, candidate_account = self._authorize_credit(request, discounted_total)
            remaining = discounted_total - credit_used

            payment_applications, amount_due_after = self._apply_payments(request, remaining)

            settled_at = ensure_utc(self._clock())
            settlement = Settlement(
                settlement_id=_settlement_id(request.idempotency_key),
                idempotency_key=request.idempotency_key,
                customer_id=request.customer_id,
                business_ref=request.business_ref,
                currency=request.currency,
                occurred_at=request.occurred_at,
                settled_at=settled_at,
                lines=tuple(lines),
                subtotal=subtotal,
                discount_code=discount_code,
                discount_amount=discount_amount,
                discounted_total=discounted_total,
                credit_used=credit_used,
                credit_outstanding_after=(candidate_account.outstanding if candidate_account else Money.zero(request.currency)),
                payments=tuple(payment_applications),
                amount_due_after=amount_due_after,
                request_digest=request_digest,
                detail_checksum=detail_checksum,
            )
            if candidate_account is not None:
                self.ledger._accounts[candidate_account.customer_id] = candidate_account
            self.ledger._records[settlement.settlement_id] = settlement
            self.ledger._requests[settlement.idempotency_key] = settlement.settlement_id
            return settlement

    def get_settlement(self, settlement_id: str) -> Settlement:
        return self.ledger.get(settlement_id)

    # -- stages ----------------------------------------------------------

    def _rate(self, request: SettlementRequest) -> list[RatedLine]:
        lines = []
        for index, item in enumerate(request.items, start=1):
            tariff = self.schedule.tariff_at(item.operation, item.container_size, request.occurred_at)
            unit_price = tariff.price(item.hazardous, item.reefer)
            if unit_price.currency != request.currency:
                raise ValidationError('rated line currency does not match settlement currency')
            lines.append(RatedLine(
                ref=f'{request.business_ref}-L{index}',
                operation=item.operation,
                container_size=item.container_size,
                quantity=item.quantity,
                unit_price=unit_price,
                amount=unit_price.scale(item.quantity),
                tariff_effective_at=self.schedule.effective_at_for(item.operation, item.container_size, request.occurred_at),
                hazardous=item.hazardous,
                reefer=item.reefer,
                description=item.description,
            ))
        return lines

    def _authorize_credit(self, request: SettlementRequest, amount: Money) -> tuple[Money, CreditAccount | None]:
        account = self.ledger.account(request.customer_id)
        if account is None or amount.amount == 0:
            return Money.zero(request.currency), account
        candidate = copy(account)
        # Use only as much credit as the account can bear; the remainder is
        # expected from payments.
        draw = min(amount.amount, candidate.available.amount)
        self.credit.authorize(candidate, Money(draw, request.currency))
        return Money(draw, request.currency), candidate

    def _apply_payments(self, request: SettlementRequest, remaining: Money) -> tuple[list[PaymentApplication], Money]:
        offered = list(request.payments)
        if remaining.amount == 0 and not offered:
            return [], Money.zero(request.currency)

        ordering = self._payment_order(offered, request.business_ref, remaining)
        applications = []
        left = remaining
        for payment in ordering:
            applied_amount = min(left.amount, payment.amount.amount)
            applications.append(PaymentApplication(
                payment_id=payment.payment_id,
                reference=payment.reference,
                offered=payment.amount,
                applied=Money(applied_amount, request.currency),
            ))
            left = Money(left.amount - applied_amount, request.currency)

        offered_total = self._sum((p.amount for p in offered), request.currency)
        delta = offered_total.amount - remaining.amount
        if left.amount > PAYMENT_TOLERANCE:
            raise ValidationError(f'payments short by {left.amount} {request.currency}')
        if delta > PAYMENT_TOLERANCE:
            raise ValidationError(f'payments exceed amount due by {delta} {request.currency}')
        # A sub-cent rounding residue is absorbed rather than left dangling.
        return applications, Money.zero(request.currency)

    def _payment_order(self, payments: list[PaymentInput], business_ref: str, remaining: Money) -> list[PaymentInput]:
        # Reuse the reconciliation convention: a payment matching the
        # receivable's reference and value is applied first. Any other payment
        # referencing this business event follows; unrelated payments are
        # consumed last so they never get swallowed ahead of the real ones.
        records = [PaymentRecord(p.payment_id, p.reference, p.amount) for p in payments]
        receivable = Receivable(business_ref, business_ref, remaining)
        matched = set(self.reconciler.match(records, [receivable], tolerance=PAYMENT_TOLERANCE))
        primary = [p for p in payments if p.payment_id in matched]
        referenced = sorted(
            (p for p in payments if p.payment_id not in matched and p.reference == business_ref),
            key=lambda p: p.payment_id,
        )
        rest = sorted(
            (p for p in payments if p.payment_id not in matched and p.reference != business_ref),
            key=lambda p: p.payment_id,
        )
        return primary + referenced + rest

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _sum(values, currency: str) -> Money:
        total = Money.zero(currency)
        for value in values:
            total = total + value
        return total

    @staticmethod
    def _lines_checksum(lines: list[RatedLine]) -> str:
        payload = [
            {
                'ref': line.ref,
                'operation': line.operation,
                'container_size': line.container_size,
                'quantity': line.quantity,
                'unit_price': to_primitive(line.unit_price),
                'amount': to_primitive(line.amount),
                'tariff_effective_at': to_primitive(line.tariff_effective_at),
                'hazardous': line.hazardous,
                'reefer': line.reefer,
            }
            for line in lines
        ]
        return PayloadChecksum.digest({'lines': payload})

    def verify_detail(self, settlement: Settlement) -> bool:
        """Recompute the immutable line checksum; false if details were altered."""
        return self._lines_checksum(list(settlement.lines)) == settlement.detail_checksum
