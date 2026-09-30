"""FIXTURE market data (conduct rule 0.2.2): synthetic, named FIXTURE_*, certified:false, rendered hatched.

Regime-switching random walk so trend logic has something to find and something to lose on.
Deterministic for a given seed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from engine.data.bars import H4, Bar

FIXTURE_PREFIX = "FIXTURE_"


def fixture_bars(name: str, start: datetime, n: int, *, seed: int, p0: float = 100.0, vol_daily: float = 0.035,
                 drift_daily: float = 0.004) -> list[Bar]:
    if not name.startswith(FIXTURE_PREFIX):
        raise ValueError("fixture instruments must be named FIXTURE_*")
    rng = np.random.default_rng(seed)
    sig = vol_daily / np.sqrt(6)
    regime = 0
    px = p0
    bars = []
    for i in range(n):
        if rng.random() < 1 / 240:  # ~40-day average regime length
            regime = int(rng.integers(0, 3))
        mu = {0: drift_daily, 1: -drift_daily, 2: 0.0}[regime] / 6
        r = rng.standard_t(4) * sig / np.sqrt(2) + mu
        o = px
        c = o * float(np.exp(r))
        wick = abs(rng.normal(0, sig * 0.6))
        h = max(o, c) * float(np.exp(wick))
        lo = min(o, c) * float(np.exp(-abs(rng.normal(0, sig * 0.6))))
        v = float(rng.lognormal(10, 0.5))
        bars.append(Bar(start + i * H4, o, h, lo, c, v))
        px = c
    return bars


def second_source(bars: list[Bar], *, seed: int, noise_bp: float = 3.0, drop: float = 0.0) -> list[Bar]:
    """A second venue's view of the same fixture: small independent noise, optional missing bars."""
    rng = np.random.default_rng(seed)
    out = []
    for b in bars:
        if drop and rng.random() < drop:
            continue
        k = float(np.exp(rng.normal(0, noise_bp / 1e4)))
        out.append(Bar(b.open_time, b.o * k, b.h * k, b.l * k, b.c * k, b.v * float(rng.uniform(0.3, 0.9))))
    return out


def fixture_start(years: float, end: datetime) -> datetime:
    start = end - timedelta(days=int(365 * years))
    return start.replace(hour=0, minute=0, second=0, microsecond=0)
