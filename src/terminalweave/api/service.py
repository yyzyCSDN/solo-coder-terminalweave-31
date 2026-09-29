from __future__ import annotations
from datetime import datetime
from terminalweave.marine.tide import TideCurve, TideSample, UnderKeelRequirement
from terminalweave.billing.demurrage import BusinessCalendar, DemurrageTariff, TariffBand, DemurrageCalculator
from terminalweave.billing.discounts import DiscountBook, DiscountRule
from terminalweave.billing.settlement import (
    PaymentInput,
    SettlementItem,
    SettlementRequest,
    SettlementService,
)
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.core.clock import parse_iso_z
from terminalweave.core.money import Money
from terminalweave.core.serialization import to_primitive
from decimal import Decimal

class TerminalService:

    def __init__(self):
        self.settlement = SettlementService()

    def health(self) -> dict[str, object]:
        return {'status': 'ok', 'service': 'terminalweave'}

    def tide_clearance(self, payload: dict[str, object]) -> dict[str, object]:
        samples = [TideSample(datetime.fromisoformat(x['at'].replace('Z', '+00:00')), float(x['height_m'])) for x in payload['samples']]
        curve = TideCurve(samples)
        req = UnderKeelRequirement(float(payload['chart_depth_m']), float(payload['draft_m']), float(payload['minimum_clearance_m']), float(payload.get('squat_allowance_m', 0)), float(payload.get('wave_allowance_m', 0)))
        at = datetime.fromisoformat(str(payload['at']).replace('Z', '+00:00'))
        return {'safe': curve.is_safe(at, req), 'clearance_m': curve.clearance_m(at, req)}

    def demurrage_quote(self, payload: dict[str, object]) -> dict[str, object]:
        currency = str(payload.get('currency', 'USD'))
        rate = Money(Decimal(str(payload['daily_rate'])), currency)
        tariff = DemurrageTariff(int(payload.get('free_days', 0)), (TariffBand(1, None, rate),))
        calc = DemurrageCalculator(BusinessCalendar(), tariff)
        start = datetime.fromisoformat(str(payload['discharge_at']).replace('Z', '+00:00'))
        end = datetime.fromisoformat(str(payload['gate_out_at']).replace('Z', '+00:00'))
        total = calc.total(start, end)
        return {'amount': str(total.amount), 'currency': total.currency}

    # -- unified settlement ---------------------------------------------

    def publish_rate(self, payload: dict[str, object]) -> dict[str, object]:
        currency = str(payload.get('currency', 'USD'))
        tariff = HandlingTariff(
            str(payload['operation']),
            int(payload['container_size']),
            Money(Decimal(str(payload['base'])), currency),
            Money(Decimal(str(payload['hazardous_surcharge'])), currency) if payload.get('hazardous_surcharge') is not None else None,
            Money(Decimal(str(payload['reefer_surcharge'])), currency) if payload.get('reefer_surcharge') is not None else None,
        )
        version = self.settlement.schedule.publish(tariff, parse_iso_z(str(payload['effective_at'])))
        return {'operation': tariff.operation, 'container_size': tariff.container_size, 'effective_at': str(to_primitive(version.effective_at))}

    def add_discount_rule(self, payload: dict[str, object]) -> dict[str, object]:
        currency = str(payload.get('currency', 'USD'))
        cap = payload.get('cap')
        rule = DiscountRule(
            str(payload['code']),
            Money(Decimal(str(payload['threshold'])), currency),
            Decimal(str(payload['percent'])),
            Money(Decimal(str(cap)), currency) if cap is not None else None,
        )
        self.settlement.discounts.rules[rule.code] = rule
        return {'code': rule.code}

    def open_credit_account(self, payload: dict[str, object]) -> dict[str, object]:
        currency = str(payload.get('currency', 'USD'))
        account = self.settlement.open_credit_account(
            str(payload['customer_id']),
            Money(Decimal(str(payload['limit'])), currency),
            Money(Decimal(str(payload.get('outstanding', '0'))), currency),
        )
        return {'customer_id': account.customer_id, 'available': str(account.available.amount), 'currency': currency}

    def settle_business(self, payload: dict[str, object]) -> dict[str, object]:
        currency = str(payload.get('currency', 'USD'))
        request = SettlementRequest(
            idempotency_key=str(payload['idempotency_key']),
            customer_id=str(payload['customer_id']),
            occurred_at=parse_iso_z(str(payload['occurred_at'])),
            business_ref=str(payload['business_ref']),
            items=tuple(
                SettlementItem(
                    operation=str(item['operation']),
                    container_size=int(item['container_size']),
                    quantity=int(item.get('quantity', 1)),
                    hazardous=bool(item.get('hazardous', False)),
                    reefer=bool(item.get('reefer', False)),
                    description=str(item.get('description', '')),
                )
                for item in payload['items']
            ),
            payments=tuple(
                PaymentInput(
                    payment_id=str(payment['payment_id']),
                    reference=str(payment['reference']),
                    amount=Money(Decimal(str(payment['amount'])), currency),
                )
                for payment in payload.get('payments', [])
            ),
            discount_codes=frozenset(str(code) for code in payload.get('discount_codes', [])),
            currency=currency,
        )
        record = self.settlement.settle(request)
        return to_primitive(record)
