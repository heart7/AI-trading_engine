from datetime import datetime, timezone
from functools import cache

from engine.data.bars import to_arrays
from engine.data.fixtures import fixture_bars
from engine.policy.loader import load_policy
from engine.replay.paper import Series

START = datetime(2019, 1, 1, tzinfo=timezone.utc)
NAMES = ["FIXTURE_BTC", "FIXTURE_ETH", "FIXTURE_XRP", "FIXTURE_SOL"]


@cache
def arrays(name: str, n: int, seed: int, vol: float = 0.035):
    return to_arrays(fixture_bars(name, START, n, seed=seed, vol_daily=vol))


def universe(n: int = 6 * 365 * 3):
    out = []
    for j, name in enumerate(NAMES):
        a = arrays(name, n, 100 + j, 0.03 + 0.01 * j)
        out.append(Series(name, a["open_time"], a["o"], a["h"], a["l"], a["c"]))
    return out


def policy():
    return load_policy().doc


def contract_specs():
    """FIXTURE contract specs, loaded like a capability snapshot (INV-24: intervals are data, never code)."""
    import json
    from pathlib import Path

    from engine.strategy_b.contracts import ContractSpec
    raw = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "contract_specs.json").read_text())
    return {d["instrument_id"]: ContractSpec(**{**d, "mm_schedule": tuple(tuple(x) for x in d["mm_schedule"])})
            for d in raw["specs"]}


def fixture_funding(series, specs, seed: int = 7):
    """FIXTURE funding events: a small positive base, a trend-following component (longs pay more in uptrends,
    which is what makes carry conditional) and noise, clipped at the contract's cap. ASSUMED shape."""
    import numpy as np
    rng = np.random.default_rng(seed)
    out = {}
    for s in series:
        sp = specs[s.instrument_id]
        iv = int(sp.funding_interval_h * 3600)
        t0, t1 = int(s.open_time[0]), int(s.open_time[-1]) + 4 * 3600
        ts = np.arange(t0 + iv, t1 + 1, iv)
        bar = np.minimum((ts - t0 - 1) // (4 * 3600), len(s.c) - 1)
        ret30 = np.log(s.c[bar] / s.c[np.maximum(bar - 180, 0)])
        scale = sp.funding_interval_h / 8.0
        rate = scale * (0.0001 + 0.0003 * np.tanh(ret30 / 0.2) + rng.normal(0, 0.0001, len(ts)))
        out[s.instrument_id] = (ts, np.clip(rate, -sp.funding_cap, sp.funding_cap))
    return out
