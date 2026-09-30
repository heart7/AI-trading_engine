"""Bar primitives: 4h OHLCV, UTC boundary alignment and daily aggregation (spec §4.1, §11)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

H4 = timedelta(hours=4)
D1 = timedelta(days=1)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Bar:
    open_time: datetime  # UTC; a 4h bar opens at 00/04/08/12/16/20 UTC
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float

    @property
    def close_time(self) -> datetime:
        return self.open_time + H4

    def valid(self) -> bool:
        vals = (self.o, self.h, self.l, self.c, self.v)
        return (all(np.isfinite(x) for x in vals) and self.v >= 0 and self.l > 0
                and self.l <= min(self.o, self.c) and self.h >= max(self.o, self.c))


def aligned(ts: datetime, step: timedelta = H4) -> bool:
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        return False
    return (ts - EPOCH) % step == timedelta(0)


def to_arrays(bars: list[Bar]) -> dict[str, np.ndarray]:
    return {
        "open_time": np.array([int(b.open_time.timestamp()) for b in bars], dtype=np.int64),
        "o": np.array([b.o for b in bars]), "h": np.array([b.h for b in bars]),
        "l": np.array([b.l for b in bars]), "c": np.array([b.c for b in bars]), "v": np.array([b.v for b in bars]),
    }


def daily_from_4h(bars: list[Bar]) -> list[Bar]:
    """Aggregate complete UTC days of six 4h bars into daily bars (DERIVED, never separately sourced).

    A day with any missing 4h bar is omitted rather than filled, so a gap stays a gap.
    """
    days: dict[datetime, list[Bar]] = {}
    for b in bars:
        day = b.open_time.replace(hour=0, minute=0, second=0, microsecond=0)
        days.setdefault(day, []).append(b)
    out = []
    for day in sorted(days):
        bs = sorted(days[day], key=lambda x: x.open_time)
        if len(bs) != 6 or [x.open_time.hour for x in bs] != [0, 4, 8, 12, 16, 20]:
            continue
        out.append(Bar(day, bs[0].o, max(x.h for x in bs), min(x.l for x in bs), bs[-1].c, sum(x.v for x in bs)))
    return out
