"""Decision clock (spec §13.4, INV-09). Decisions happen only at 4h bar closes; there is no intraday path."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DECISION_HOURS = (0, 4, 8, 12, 16, 20)
CYCLE_DEADLINES_S = {"certified": 60, "claims": 90, "intents": 120, "orders": 150, "entry_ladder": 900}


def last_bar_close(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    base = ts.replace(minute=0, second=0, microsecond=0)
    return base - timedelta(hours=base.hour % 4)


def is_decision_time(ts: datetime, window_s: int = CYCLE_DEADLINES_S["intents"]) -> bool:
    """True only within the intents deadline after a 4h close."""
    return (ts.astimezone(timezone.utc) - last_bar_close(ts)).total_seconds() <= window_s


def step_late(step: str, t_offset_ms: int) -> bool:
    return t_offset_ms > CYCLE_DEADLINES_S[step] * 1000


class IntradayTriggerIgnored(Exception):
    pass


def decision_cycle_guard(ts: datetime) -> datetime:
    """Returns the bar close the cycle belongs to, or raises: an intraday trigger creates no signal_intent."""
    if not is_decision_time(ts):
        raise IntradayTriggerIgnored(f"{ts.isoformat()} is not within a 4h decision window")
    return last_bar_close(ts)
