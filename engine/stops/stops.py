"""Stops (spec §5.6, §6.4, INV-04). Stops only ratchet in the position's favour and are never widened."""
from __future__ import annotations

import math
from dataclasses import dataclass


class StopWidenAttempt(Exception):
    """Raised on any attempt to move a stop against the position. Callers open a STOP_WIDEN_ATTEMPT incident."""


def initial_stop(entry: float, atr_daily: float, k_stop: float, side: int = 1) -> float:
    return entry - side * k_stop * atr_daily


def trail_candidate(extreme_close: float, atr_daily: float, k_trail: float, side: int = 1) -> float:
    """Long: highest close since entry - k*ATR. Short: lowest close since entry + k*ATR."""
    return extreme_close - side * k_trail * atr_daily


@dataclass
class StopState:
    side: int  # +1 long, -1 short
    entry: float
    initial: float
    trail: float
    extreme_close: float

    @property
    def active(self) -> float:
        return max(self.initial, self.trail) if self.side > 0 else min(self.initial, self.trail)

    def on_close(self, close: float, atr_daily: float, k_trail: float) -> float:
        """Ratchet on a 4h close; returns the active stop."""
        self.extreme_close = max(self.extreme_close, close) if self.side > 0 else min(self.extreme_close, close)
        cand = trail_candidate(self.extreme_close, atr_daily, k_trail, self.side)
        self.trail = max(self.trail, cand) if self.side > 0 else min(self.trail, cand)
        return self.active

    def request(self, new_stop: float) -> float:
        """External stop change (API/UI). Only moves in the position's favour; widening raises."""
        widens = new_stop < self.active if self.side > 0 else new_stop > self.active
        if widens:
            raise StopWidenAttempt(f"stop {self.active} -> {new_stop} widens against a {'long' if self.side > 0 else 'short'}")
        self.trail = new_stop
        return self.active

    def hit(self, low: float, high: float) -> bool:
        return low <= self.active if self.side > 0 else high >= self.active


def open_stop(entry: float, atr_daily: float, k_stop: float, k_trail: float, side: int = 1) -> StopState:
    ini = initial_stop(entry, atr_daily, k_stop, side)
    return StopState(side, entry, ini, trail_candidate(entry, atr_daily, k_trail, side), entry)


def round_order(qty: float, stop_px: float, entry_px: float, *, lot: float, tick: float, side: int,
                max_risk_usd: float) -> tuple[float, float, float]:
    """Spec §8.7 rounding: qty down to lot; stop to the tick on the protective side (never wider than one tick);
    R recomputed after both roundings; if R exceeds the limit, drop one more lot."""
    q = math.floor(qty / lot + 1e-9) * lot
    s = (math.floor(stop_px / tick + 1e-9) * tick) if side > 0 else (math.ceil(stop_px / tick - 1e-9) * tick)
    risk = q * abs(entry_px - s)
    while q > 0 and risk > max_risk_usd + 1e-9:
        q = round(q - lot, 12)
        risk = q * abs(entry_px - s)
    return max(q, 0.0), s, risk


@dataclass(frozen=True)
class TimeStop:
    flag_x: float
    reduce_x: float
    expected_dwell_bars: int

    def state(self, bars_held: int) -> str:
        if bars_held >= self.reduce_x * self.expected_dwell_bars:
            return "REDUCE_ONLY"
        if bars_held >= self.flag_x * self.expected_dwell_bars:
            return "FLAGGED"
        return "OK"
