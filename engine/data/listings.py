"""Point-in-time listing table (spec §6.2, §9.2, INV-21).

Backtests read the tradable universe at each date from listing records, never a constant list.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Listing:
    instrument_id: str  # "<venue>:<symbol>", e.g. "kraken-spot:XBT/USD"
    venue: str
    symbol: str
    kind: str  # spot | perp
    base: str
    quote: str
    listed_at: datetime
    delisted_at: datetime | None = None
    delisting_notice_at: datetime | None = None
    source: str = "FIXTURE"

    def live_at(self, t: datetime) -> bool:
        return self.listed_at <= t and (self.delisted_at is None or t < self.delisted_at)

    def under_notice_at(self, t: datetime) -> bool:
        return self.delisting_notice_at is not None and self.delisting_notice_at <= t


class ConstantUniverseRejected(Exception):
    pass


class ListingTable:
    def __init__(self, listings: list[Listing]):
        ids = [x.instrument_id for x in listings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate instrument_id in listing table")
        self._by_id = {x.instrument_id: x for x in listings}

    def get(self, instrument_id: str) -> Listing:
        return self._by_id[instrument_id]

    def universe_at(self, t: datetime, *, kind: str | None = None, bases: set[str] | None = None) -> list[Listing]:
        return sorted((x for x in self._by_id.values() if x.live_at(t) and (kind is None or x.kind == kind)
                       and (bases is None or x.base in bases)), key=lambda x: x.instrument_id)

    def history_days_at(self, instrument_id: str, t: datetime) -> float:
        x = self._by_id[instrument_id]
        return max(0.0, (t - x.listed_at).total_seconds() / 86400)

    def complete(self) -> bool:
        """Every record states where it came from and when it listed."""
        return all(x.listed_at is not None and x.source for x in self._by_id.values())


def require_point_in_time(universe: object) -> ListingTable:
    """Backtests must pass a ListingTable; a plain list of symbols is a constant universe (INV-21)."""
    if not isinstance(universe, ListingTable):
        raise ConstantUniverseRejected("backtest universe must be point-in-time listing records")
    return universe
