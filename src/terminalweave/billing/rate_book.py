from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from terminalweave.billing.tariffs import HandlingTariff
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import NotFoundError, ValidationError
from terminalweave.core.money import Money

@dataclass(frozen=True)
class TariffVersion:
    effective_from: datetime
    tariff: HandlingTariff

    def __post_init__(self) -> None:
        object.__setattr__(self, 'effective_from', ensure_utc(self.effective_from))

    @property
    def key(self) -> tuple[str, int]:
        return (self.tariff.operation, self.tariff.container_size)

class HistoricalTariffBook:
    """Tariffs keyed by operation and container size with effective dating.

    Quotes always resolve against the version in effect at the business
    event time, so a later price change can never rewrite an older charge.
    """

    def __init__(self, versions: list[TariffVersion] | None=None):
        self._versions: dict[tuple[str, int], list[TariffVersion]] = {}
        self._lock = RLock()
        for version in versions or []:
            self.register(version.tariff, version.effective_from)

    def register(self, tariff: HandlingTariff, effective_from: datetime) -> None:
        effective_from = ensure_utc(effective_from)
        with self._lock:
            chain = self._versions.setdefault((tariff.operation, tariff.container_size), [])
            if any(v.effective_from == effective_from for v in chain):
                raise ValidationError('duplicate effective-from for tariff')
            chain.append(TariffVersion(effective_from, tariff))
            chain.sort(key=lambda v: v.effective_from)

    def version_at(self, operation: str, size: int, occurred_at: datetime) -> TariffVersion:
        occurred_at = ensure_utc(occurred_at)
        with self._lock:
            chain = self._versions.get((operation, size), [])
            effective = [v for v in chain if v.effective_from <= occurred_at]
        if not effective:
            raise NotFoundError(f'no tariff in effect for {operation}/{size} at {occurred_at.isoformat()}')
        return effective[-1]

    def tariff_at(self, operation: str, size: int, occurred_at: datetime) -> HandlingTariff:
        return self.version_at(operation, size, occurred_at).tariff

    def quote_at(self, operation: str, size: int, occurred_at: datetime, count: int=1, **flags) -> Money:
        if count < 0:
            raise ValidationError('count cannot be negative')
        return self.tariff_at(operation, size, occurred_at).price(**flags).scale(count)
