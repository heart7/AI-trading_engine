"""Admission by portfolio time-to-flatten (spec §5.1, §6.2, §7.5, INV-23).

Time to close every position at <= 10% participation of stressed depth (depth x 0.3). An instrument whose
addition pushes the portfolio past the ceiling is not admissible, however liquid it is on its own.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PARTICIPATION = 0.10
STRESS_DEPTH = 0.30


def instrument_ttf_min(notional_usd: float, volume_usd_per_min: float) -> float:
    if notional_usd <= 0:
        return 0.0
    cap = volume_usd_per_min * STRESS_DEPTH * PARTICIPATION
    return float("inf") if cap <= 0 else notional_usd / cap


def portfolio_ttf_min(positions_usd: Mapping[str, float], volume_usd_per_min: Mapping[str, float]) -> float:
    """Instruments are flattened in parallel, each at its own participation cap; the slowest one sets the time."""
    return max((instrument_ttf_min(n, volume_usd_per_min.get(k, 0.0)) for k, n in positions_usd.items()), default=0.0)


@dataclass(frozen=True)
class Admission:
    admissible: bool
    code: str | None
    ttf_min: float
    detail: str


def admit_by_ttf(positions_usd: Mapping[str, float], volume_usd_per_min: Mapping[str, float], instrument: str,
                 add_usd: float, ceiling_min: float) -> Admission:
    after = dict(positions_usd)
    after[instrument] = after.get(instrument, 0.0) + add_usd
    ttf = portfolio_ttf_min(after, volume_usd_per_min)
    if ttf > ceiling_min:
        return Admission(False, "NOT_ADMISSIBLE", ttf, f"time-to-flatten {ttf:.1f} min > ceiling {ceiling_min} min")
    return Admission(True, None, ttf, "ok")


def admit_b_universe(policy: Mapping, *, vol_usd_30d_median: float, oi_usd: float, history_days: int,
                     spec_approved: bool) -> Admission:
    u = policy["universe"]["B"]
    fails = [n for n, ok in (("volume", vol_usd_30d_median >= u["min_vol_usd_30d_median"]),
                              ("open_interest", oi_usd >= u["min_oi_usd"]),
                              ("history", history_days >= u["min_history_days"]),
                              ("contract_spec", spec_approved)) if not ok]
    return Admission(not fails, "NOT_ADMISSIBLE" if fails else None, 0.0, ", ".join(fails) or "ok")
