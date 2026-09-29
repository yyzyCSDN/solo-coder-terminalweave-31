from datetime import datetime, timezone
from decimal import Decimal
import pytest

from terminalweave.billing.credit import CreditAccount
from terminalweave.billing.discounts import DiscountBook, DiscountRule
from terminalweave.billing.invoice import InvoiceLine
from terminalweave.billing.rate_book import HistoricalTariffBook, TariffVersion
from terminalweave.billing.reconciliation import PaymentRecord
from terminalweave.billing.settlement import (
    ChargeItem, SettlementRequest, SettlementService, STATUS_OPEN, STATUS_PAID,
)
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.core.errors import (
    CapacityError, ConflictError, NotFoundError, StateTransitionError, ValidationError,
)
from terminalweave.core.money import Money

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 3, 1, tzinfo=UTC)
T2 = datetime(2026, 6, 1, tzinfo=UTC)


def tariff(base: str, operation: str = 'LIFT', size: int = 40) -> HandlingTariff:
    return HandlingTariff(operation, size, Money(Decimal(base), 'USD'))


def book_with_rates() -> HistoricalTariffBook:
    return HistoricalTariffBook([
        TariffVersion(T0, tariff('100')),
        TariffVersion(T1, tariff('120')),
    ])


def make_service(**overrides) -> SettlementService:
    accounts = overrides.pop('accounts', {'CUST': CreditAccount('CUST', Money(Decimal('10000'), 'USD'), Money.zero('USD'))})
    discounts = overrides.pop('discount_book', DiscountBook([
        DiscountRule('LOYALTY', Money(Decimal('50'), 'USD'), Decimal('0.10')),
        DiscountRule('SMALL', Money(Decimal('10'), 'USD'), Decimal('0.05')),
    ]))
    clock = overrides.pop('clock', lambda: T2)
    return SettlementService(book_with_rates(), discounts, accounts, clock=clock, **overrides)


def req(request_id='R-1', occurred_at=datetime(2026, 2, 15, tzinfo=UTC), payments=None, discount_codes=None, quantity=Decimal('2')):
    return SettlementRequest(
        request_id, 'CUST',
        [ChargeItem('MOV-1', 'LIFT', 40, quantity, occurred_at)],
        discount_codes=discount_codes,
        payments=payments,
    )


def test_lines_priced_at_rate_in_effect_when_business_occurred():
    svc = make_service()
    # Business happened in February: January rate (100) applies even
    # though the March rate (120) is current when settlement runs.
    result = svc.settle(req(occurred_at=datetime(2026, 2, 15, tzinfo=UTC)))
    line = result.bill.lines[0]
    assert line.unit_price == Money(Decimal('100'), 'USD')
    assert line.amount == Money(Decimal('200'), 'USD')
    assert line.rate_effective_from == T0


def test_later_rate_change_does_not_rewrite_existing_bill():
    book = book_with_rates()
    svc = make_service()
    svc.tariff_book = book
    first = svc.settle(req('R-1', occurred_at=datetime(2026, 2, 15, tzinfo=UTC)))
    # Register a June rate change, then replay the same request.
    book.register(tariff('150'), T2)
    replay = svc.settle(req('R-1', occurred_at=datetime(2026, 2, 15, tzinfo=UTC)))
    assert replay is first
    assert replay.bill.lines[0].unit_price == Money(Decimal('100'), 'USD')
    assert replay.bill.verify()


def test_pipeline_applies_discount_then_credit_then_payment():
    account = CreditAccount('CUST', Money(Decimal('10000'), 'USD'), Money.zero('USD'))
    svc = make_service(accounts={'CUST': account})
    payments = [PaymentRecord('P-1', 'STL', Money(Decimal('80'), 'USD'))]
    result = svc.settle(req(payments=payments, discount_codes={'LOYALTY'}))
    bill = result.bill
    assert bill.subtotal == Money(Decimal('200'), 'USD')
    assert bill.discount_code == 'LOYALTY'
    assert bill.discount_amount == Money(Decimal('20'), 'USD')
    assert bill.receivable == Money(Decimal('180'), 'USD')
    assert bill.paid == Money(Decimal('80'), 'USD')
    assert bill.due == Money(Decimal('100'), 'USD')
    assert bill.status == STATUS_OPEN
    # Credit ledger shows the receivable authorized and the payment posted.
    assert account.outstanding == Money(Decimal('100'), 'USD')


def test_full_payment_within_tolerance_marks_paid():
    svc = make_service()
    # Receivable 180; pay one cent under and tolerance writes it off.
    payments = [PaymentRecord('P-1', 'STL', Money(Decimal('179.99'), 'USD'))]
    result = svc.settle(req(payments=payments, discount_codes={'LOYALTY'}), )
    assert result.bill.status == STATUS_PAID
    assert result.bill.due == Money.zero('USD')
    assert result.bill.paid == Money(Decimal('180'), 'USD')


def test_overpayment_fails_without_creating_bill_or_touching_credit():
    account = CreditAccount('CUST', Money(Decimal('10000'), 'USD'), Money.zero('USD'))
    svc = make_service(accounts={'CUST': account})
    payments = [PaymentRecord('P-1', 'STL', Money(Decimal('500'), 'USD'))]
    with pytest.raises(ValidationError):
        svc.settle(req(request_id='R-BAD', payments=payments, discount_codes={'LOYALTY'}))
    # No half bill, no reservation, no ledger movement.
    assert account.outstanding == Money.zero('USD')
    with pytest.raises(NotFoundError):
        svc.result_for('R-BAD')


def test_credit_exceeded_fails_atomically_and_request_can_be_retried():
    account = CreditAccount('CUST', Money(Decimal('150'), 'USD'), Money.zero('USD'))
    svc = make_service(accounts={'CUST': account})
    payments = [PaymentRecord('P-1', 'STL', Money(Decimal('20'), 'USD'))]
    # Receivable 180 exceeds the 150 limit -> authorization stage rejects.
    with pytest.raises(CapacityError):
        svc.settle(req(request_id='R-CAP', payments=payments, discount_codes={'LOYALTY'}))
    assert account.outstanding == Money.zero('USD')
    with pytest.raises(NotFoundError):
        svc.result_for('R-CAP')
    # No bill and no ledger movement; the same request can be retried
    # once the headroom exists, using the same request id.
    account.limit = Money(Decimal('10000'), 'USD')
    result = svc.settle(req(request_id='R-CAP', payments=payments, discount_codes={'LOYALTY'}))
    assert result.bill.status == STATUS_OPEN
    assert account.outstanding == Money(Decimal('160'), 'USD')


def test_duplicate_request_returns_identical_result():
    svc = make_service()
    first = svc.settle(req(payments=[PaymentRecord('P-1', 'STL', Money(Decimal('180'), 'USD'))],
                           discount_codes={'LOYALTY'}))
    second = svc.settle(req(payments=[PaymentRecord('P-1', 'STL', Money(Decimal('180'), 'USD'))],
                            discount_codes={'LOYALTY'}))
    assert first is second
    assert svc.result_for('R-1') is first
    assert first.bill.status == STATUS_PAID


def test_same_request_id_with_different_content_is_conflict():
    svc = make_service()
    svc.settle(req(discount_codes={'LOYALTY'}))
    with pytest.raises(ConflictError):
        svc.settle(req(discount_codes={'SMALL'}))
    with pytest.raises(ConflictError):
        svc.settle(req(quantity=Decimal('3')))
    # Original result is unchanged.
    assert svc.result_for('R-1').bill.discount_code == 'LOYALTY'


def test_bill_details_are_immutable_and_checksum_guards_tampering():
    svc = make_service()
    result = svc.settle(req(discount_codes={'LOYALTY'}))
    bill = result.bill
    assert bill.verify()
    with pytest.raises(Exception):
        bill.status = STATUS_PAID  # type: ignore[misc]
    with pytest.raises(Exception):
        bill.lines[0].unit_price = Money(Decimal('1'), 'USD')  # type: ignore[misc]
    # The generated invoice was finalized as part of the commit.
    assert result.invoice.finalized is True
    with pytest.raises(StateTransitionError):
        result.invoice.add(InvoiceLine('X', 'x', Decimal('1'), Money(Decimal('1'), 'USD')))


def test_missing_tariff_for_business_time_fails_cleanly():
    svc = make_service()
    early = req('R-EARLY', occurred_at=datetime(2025, 12, 31, tzinfo=UTC))
    with pytest.raises(NotFoundError):
        svc.settle(early)
    with pytest.raises(NotFoundError):
        svc.result_for('R-EARLY')


def test_fully_discounted_bill_needs_no_credit_movement():
    free_book = DiscountBook([
        DiscountRule('FREE', Money(Decimal('0'), 'USD'), Decimal('1.0')),
    ])
    svc = make_service(discount_book=free_book)
    result = svc.settle(req(discount_codes={'FREE'}))
    assert result.bill.receivable == Money.zero('USD')
    assert result.bill.status == STATUS_PAID
    assert svc.accounts['CUST'].outstanding == Money.zero('USD')


def test_multiple_payments_are_summed_in_order():
    svc = make_service()
    payments = [
        PaymentRecord('P-1', 'STL', Money(Decimal('100'), 'USD')),
        PaymentRecord('P-2', 'STL', Money(Decimal('80'), 'USD')),
    ]
    result = svc.settle(req(payments=payments, discount_codes={'LOYALTY'}))
    assert result.bill.paid == Money(Decimal('180'), 'USD')
    assert result.bill.status == STATUS_PAID
