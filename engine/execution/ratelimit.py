"""Per-venue token bucket with priority lanes (spec §8.7): protective exits > cancels > entries > data backfill.

A lower lane may only take a token if the bucket keeps a reserve for the lanes above it, so a burst of
entries or backfill can never starve a protective stop. Venue-reported usage overrides the local estimate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Lane(IntEnum):
    PROTECT = 0
    CANCEL = 1
    ENTRY = 2
    DATA = 3


# Tokens that must remain after a take, as a share of capacity (A-RATE-LANES). CANCEL keeps one token.
RESERVE_SHARE = {Lane.PROTECT: 0.0, Lane.ENTRY: 0.25, Lane.DATA: 0.50}


class RateLimited(Exception):
    def __init__(self, lane: Lane):
        self.lane = lane
        super().__init__(f"rate budget exhausted for lane {lane.name}")


@dataclass
class TokenBucket:
    capacity: float
    refill_per_s: float
    tokens: float = field(default=-1.0)
    t: float = 0.0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            self.tokens = self.capacity

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.t) * self.refill_per_s)
        self.t = max(self.t, now)

    def reserve(self, lane: Lane) -> float:
        return 1.0 if lane == Lane.CANCEL else RESERVE_SHARE[lane] * self.capacity

    def try_take(self, lane: Lane, now: float, cost: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens - cost < self.reserve(lane) - 1e-12:
            return False
        self.tokens -= cost
        return True

    def take(self, lane: Lane, now: float, cost: float = 1.0) -> None:
        if not self.try_take(lane, now, cost):
            raise RateLimited(lane)

    def update_from_venue(self, used: float, limit: float, now: float) -> None:
        """Venue usage headers win over the local estimate."""
        self._refill(now)
        self.tokens = min(self.tokens, max(0.0, self.capacity * (1 - used / limit)))
