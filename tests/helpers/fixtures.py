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
