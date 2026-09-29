from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from terminalweave.billing.credit import CreditAccount
from terminalweave.billing.discounts import DiscountBook, DiscountRule
from terminalweave.billing.settlement import (
    PaymentInput,
    RateSchedule,
    SettlementItem,
    SettlementLedger,
    SettlementRequest,
    SettlementService,
)
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.core.errors import CapacityError, ConflictError, NotFoundError, ValidationError
from terminalweave.core.money import Money

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)


def tariff(operation: str, base: str, size: int = 40) -> HandlingTariff:
    return HandlingTariff(operation, size, Money(Decimal(base), 'USD'))


def request(occurred_at=T1, payments=(), codes=frozenset(), key='KEY-1', customer='CUST-1', ref='BIZ-1', items=None):
    return SettlementRequest(
        idempotency_key=key,
        customer_id=customer,
        occurred_at=occurred_at,
        business_ref=ref,
        items=items or (SettlementItem('MOVE', 40, 2),),
        payments=tuple(payments),
        discount_codes=codes,
    )


def make_service() -> SettlementService:
    schedule = RateSchedule()
    schedule.publish(tariff('MOVE', '100'), T0)
    schedule.publish(tariff('MOVE', '120'), T2)
    discounts = DiscountBook([
        DiscountRule('LOYALTY', Money(Decimal('150'), 'USD'), Decimal('0.10')),
        DiscountRule('SMALL', Money(Decimal('50'), 'USD'), Decimal('0.05')),
    ])
    return SettlementService(schedule=schedule, discounts=discounts)


def payment(pid: str, amount: str, reference='BIZ-1') -> PaymentInput:
    return PaymentInput(pid, reference, Money(Decimal(amount), 'USD'))


def test_rating_uses_rate_at_occurrence_and_freezes_lines():
    service = make_service()
    # Business in February rates at 100 even though a 120 rate exists from March.
    settled = service.settle(request(occurred_at=T1, payments=(payment('P1', '200.00'),)))
    assert settled.subtotal.amount == Decimal('200.00')
    line = settled.lines[0]
    assert line.unit_price.amount == Decimal('100.00')
    assert line.tariff_effective_at == T0
    assert service.verify_detail(settled)


def test_rating_refuses_when_no_rate_was_effective():
    service = make_service()
    with pytest.raises(NotFoundError):
        service.settle(request(occurred_at=datetime(2025, 12, 31, tzinfo=UTC)))


def test_new_rate_only_affects_later_business():
    service = make_service()
    before = service.settle(request(occurred_at=T1, key='K1', payments=(payment('P1', '200'),)))
    after = service.settle(request(occurred_at=T2, key='K2', payments=(payment('P2', '240'),)))
    assert before.subtotal.amount == Decimal('200.00')
    assert after.subtotal.amount == Decimal('240.00')


def test_best_discount_is_applied_once():
    service = make_service()
    settled = service.settle(request(payments=(payment('P1', '180.00'),), codes=frozenset({'LOYALTY', 'SMALL'})))
    assert settled.discount_code == 'LOYALTY'
    assert settled.discount_amount.amount == Decimal('20.00')
    assert settled.discounted_total.amount == Decimal('180.00')
    assert settled.amount_due_after.amount == Decimal('0.00')


def test_credit_covers_discounted_balance_without_payment():
    service = make_service()
    service.open_credit_account('CUST-1', Money(Decimal('500'), 'USD'))
    settled = service.settle(request(codes=frozenset({'LOYALTY'})))
    assert settled.discounted_total.amount == Decimal('180.00')
    assert settled.credit_used.amount == Decimal('180.00')
    assert settled.credit_outstanding_after.amount == Decimal('180.00')
    assert service.ledger.account('CUST-1').outstanding.amount == Decimal('180.00')


def test_credit_and_payments_apply_in_order():
    service = make_service()
    service.open_credit_account('CUST-1', Money(Decimal('100'), 'USD'))
    settled = service.settle(request(payments=(payment('P1', '100.00'),)))
    assert settled.subtotal.amount == Decimal('200.00')
    assert settled.credit_used.amount == Decimal('100.00')
    assert settled.payments[0].applied.amount == Decimal('100.00')
    assert settled.amount_due_after.amount == Decimal('0.00')


def test_short_payment_fails_without_half_bill():
    service = make_service()
    account = service.open_credit_account('CUST-1', Money(Decimal('50'), 'USD'))
    with pytest.raises(ValidationError):
        service.settle(request(payments=(payment('P1', '100.00'),)))
    # Nothing committed: no settlement, credit untouched.
    assert service.ledger._records == {}
    assert account.outstanding.amount == Decimal('0.00')


def test_suspended_credit_fails_atomically():
    service = make_service()
    account = CreditAccount('CUST-1', Money(Decimal('500'), 'USD'), Money.zero('USD'), suspended=True)
    service.ledger.open_account(account)
    with pytest.raises(CapacityError):
        service.settle(request())
    assert service.ledger._records == {}


def test_overpayment_is_rejected():
    service = make_service()
    with pytest.raises(ValidationError):
        service.settle(request(payments=(payment('P1', '210.00'),)))
    assert service.ledger._records == {}


def test_repeated_key_returns_identical_settlement_even_when_payload_changes():
    service = make_service()
    service.open_credit_account('CUST-1', Money(Decimal('500'), 'USD'))
    first = service.settle(request(key='DUP'))
    # Same key, different business content and no credit configured to cover:
    # the stored result must be returned, not recomputed.
    second_req = request(key='DUP', items=(SettlementItem('MOVE', 40, 5),))
    second = service.settle(second_req)
    assert second is first
    assert second.settlement_id == first.settlement_id
    assert second.request_digest == first.request_digest
    # The mutated request was never charged to credit.
    assert service.ledger.account('CUST-1').outstanding.amount == Decimal('200.00')


def test_concurrent_duplicates_commit_once():
    service = make_service()
    req = request(key='RACE', payments=(payment('P1', '200.00'),))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.settle(req), range(8)))
    assert all(r.settlement_id == results[0].settlement_id for r in results)
    assert len(service.ledger._records) == 1


def test_concurrent_settlements_serialize_against_one_credit_account():
    service = make_service()
    service.open_credit_account('CUST-1', Money(Decimal('300'), 'USD'))
    outcomes = []

    def run(key):
        try:
            outcomes.append(('ok', service.settle(request(key=key, payments=()))))
        except ValidationError:
            outcomes.append(('short', None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ['A', 'B']))
    assert sorted(kind for kind, _ in outcomes) == ['ok', 'short']
    assert service.ledger.account('CUST-1').outstanding.amount == Decimal('200.00')
    assert len(service.ledger._records) == 1


def test_committed_lines_are_immutable_and_checksummed():
    import dataclasses
    service = make_service()
    settled = service.settle(request(payments=(payment('P1', '200.00'),)))
    with pytest.raises(Exception):
        settled.lines[0].amount = Money(Decimal('999'), 'USD')
    with pytest.raises(Exception):
        settled.lines = ()
    assert service.verify_detail(settled)
    tampered_line = dataclasses.replace(settled.lines[0], quantity=3, amount=Money(Decimal('300'), 'USD'))
    forged = dataclasses.replace(settled, lines=(tampered_line,))
    assert not service.verify_detail(forged)
    assert service.verify_detail(settled)


def test_payment_referencing_business_is_preferred():
    service = make_service()
    # Total 200: the referenced payment plus the unrelated one exactly cover
    # it, but the referenced one must be consumed first.
    settled = service.settle(request(payments=(
        payment('P-OTHER', '5.00', reference='OTHER-BIZ'),
        payment('P-MATCH', '195.00', reference='BIZ-1'),
    )))
    assert settled.payments[0].payment_id == 'P-MATCH'
    assert settled.payments[0].applied.amount == Decimal('195.00')
    assert settled.payments[1].payment_id == 'P-OTHER'
    assert settled.payments[1].applied.amount == Decimal('5.00')
    assert settled.amount_due_after.amount == Decimal('0.00')


def test_rate_schedule_cannot_be_backdated():
    schedule = RateSchedule()
    schedule.publish(tariff('MOVE', '100'), T0)
    with pytest.raises(ConflictError):
        schedule.publish(tariff('MOVE', '90'), datetime(2025, 1, 1, tzinfo=UTC))


def test_settlement_lookup_and_unknown_key():
    service = make_service()
    settled = service.settle(request(key='LOOKUP', payments=(payment('P1', '200.00'),)))
    assert service.get_settlement(settled.settlement_id).settlement_id == settled.settlement_id
    assert service.ledger.get_by_key('LOOKUP') is settled
    with pytest.raises(NotFoundError):
        service.ledger.get_by_key('MISSING')
