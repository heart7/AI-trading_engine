"""sigma* calibration by Monte Carlo (spec §9.3 step 8, §9.9).

Simulates one-year daily paths of the sleeve at a target volatility sigma*, with:
- a parameter posterior for the mean that includes a null-edge mass (mu = 0 with probability null_mass),
- Student-t shocks (nu = 4) scaled to unit variance,
- the §7.3 loss ladder active at daily resolution (daily stop, 5-day suspend, DD 8% halve, DD 12% suspend,
  DD 16% flatten via the 48h dead-man, DD 20% terminate).
sigma* is the largest grid value with P(DD >= 20% within 1y) <= p_max. A control run with the ladder off must
give a larger P(DD >= 20%); if it does not, the job is defective (§9.9).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DAYS = 365


@dataclass(frozen=True)
class MCResult:
    sigma_star: float | None
    grid: list[tuple[float, float, float, float]]  # (sigma, P(DD>=20%) ladder on, ladder off, P(FLATTEN rung reached) ladder on)
    control_ok: bool
    null_mass: float
    nu: float
    paths: int
    seed: int


EXIT_DECAY = 0.8  # ASSUMED: with entries blocked, open exposure decays 20%/day as positions exit on their own rules
REBUILD_STEP = 0.2  # ASSUMED: exposure rebuilds by at most 20% of target per day through new entries
DEADMAN_DAYS = 2  # DD 16%: FLATTEN may wait for the 48h dead-man
REARM_DAYS = 5  # ASSUMED: the principal re-arms a SUSPEND (5-day or DD 12% rung) after 5 days


def simulate(sigma_ann: float, mu_ann_mean: float, mu_ann_se: float, *, ladder: dict | None, paths: int, seed: int,
             null_mass: float, nu: float, dd_limit: float, return_flatten: bool = False):
    """Vectorised across paths. Returns P(drawdown >= dd_limit within one year).

    STOP and SUSPEND block new entries but do not close positions (spec §7.3), so they make exposure decay
    rather than vanish; FLATTEN takes effect only after the 48h dead-man in the worst case."""
    rng = np.random.default_rng(seed)
    sd = sigma_ann / np.sqrt(DAYS)
    shocks = rng.standard_t(nu, size=(paths, DAYS)) / np.sqrt(nu / (nu - 2))
    null = rng.random(paths) < null_mass
    mu = np.where(null, 0.0, rng.normal(mu_ann_mean, mu_ann_se, size=paths)) / DAYS
    nav = np.ones(paths)
    hwm = np.ones(paths)
    expo = np.ones(paths)
    halve = np.zeros(paths, bool)
    flatten_at = np.full(paths, 10 ** 9)
    blocked_until = np.full(paths, -1)
    suspended_until = np.full(paths, -1)
    hit = np.zeros(paths, bool)
    hist = np.ones((DAYS + 1, paths))
    for d in range(DAYS):
        live = ~hit & (d < flatten_at)
        r = np.where(live, expo * (mu + sd * shocks[:, d]), 0.0)
        nav = nav * (1 + r)
        hwm = np.maximum(hwm, nav)
        hist[d + 1] = nav
        dd = 1 - nav / hwm
        hit |= dd >= dd_limit
        if ladder is None:
            continue
        c = ladder["dd"]
        blocked_until = np.where(-r >= ladder["daily_stop"], np.maximum(blocked_until, d + 1), blocked_until)
        fire = dd >= c["suspend_downgrade"]
        if d >= 5:
            fire |= (1 - nav / hist[d - 4]) >= ladder["rolling_5d_suspend"]
        suspended_until = np.where(fire & (suspended_until < d), d + 1 + REARM_DAYS, suspended_until)
        suspended = d < suspended_until
        flatten_at = np.where((dd >= c["flatten_decision"]) & (flatten_at > d + DEADMAN_DAYS), d + 1 + DEADMAN_DAYS, flatten_at)
        halve = np.where(dd >= c["halve"], True, np.where(dd < 0.05, False, halve))
        target = np.where(halve, 0.5, 1.0)
        blocked = suspended | (d < blocked_until)
        expo = np.where(blocked, expo * EXIT_DECAY,
                        np.where(expo < target, np.minimum(target, expo + REBUILD_STEP * target),
                                 np.maximum(target, expo * EXIT_DECAY)))
    if return_flatten:
        return float(hit.mean()), float((flatten_at < 10 ** 9).mean())
    return float(hit.mean())


def calibrate(oos_daily: np.ndarray, ladder: dict, *, p_max: float, dd_limit: float, grid: tuple[float, ...] | None = None,
              paths: int = 2000, seed: int = 20260928, null_mass: float = 0.5, nu: float = 4.0) -> MCResult:
    r = np.asarray(oos_daily, float)
    vol = float(r.std(ddof=1) * np.sqrt(DAYS)) if len(r) > 2 else 0.0
    sharpe = float(r.mean() * DAYS / vol) if vol > 0 else 0.0
    se_sharpe = np.sqrt(DAYS / max(len(r), 1))  # s.e. of annual Sharpe ~ sqrt(1/years)
    grid = grid or tuple(round(float(x), 3) for x in np.arange(0.02, 0.305, 0.02))
    rows = []
    best = None
    control_ok = True
    for g in grid:
        on, flat = simulate(g, sharpe * g, se_sharpe * g, ladder=ladder, paths=paths, seed=seed, null_mass=null_mass, nu=nu,
                            dd_limit=dd_limit, return_flatten=True)
        off = simulate(g, sharpe * g, se_sharpe * g, ladder=None, paths=paths, seed=seed, null_mass=null_mass, nu=nu, dd_limit=dd_limit)
        rows.append((g, on, off, flat))
        if on <= p_max:
            best = g
        if off > 0.001 and off <= on:
            control_ok = False
    if not any(off > on for _, on, off, _f in rows):
        control_ok = False
    return MCResult(best, rows, control_ok, null_mass, nu, paths, seed)
