"""Statistics for the validation harness (spec §9.2, §9.3, Appendix B.4).

All resampling is seeded; results are reproducible for a given (data, seed).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

N = NormalDist()
DAYS = 365  # crypto trades every day


def sharpe_ann(r: np.ndarray) -> float:
    r = np.asarray(r, dtype=float)
    if len(r) < 3:
        return 0.0
    sd = r.std(ddof=1)
    return 0.0 if sd == 0 else float(r.mean() / sd * math.sqrt(DAYS))


def max_drawdown(r: np.ndarray) -> float:
    eq = np.cumprod(1 + np.asarray(r, dtype=float))
    if not len(eq):
        return 0.0
    peak = np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:]
    return float(np.max(1 - eq / peak))


def stationary_bootstrap_indices(n: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap: geometric block lengths with the given mean, wrapping around."""
    idx = np.empty(n, dtype=np.int64)
    p = 1.0 / mean_block
    t = int(rng.integers(n))
    for k in range(n):
        idx[k] = t
        t = int(rng.integers(n)) if rng.random() < p else (t + 1) % n
    return idx


def bootstrap_stat(series: list[np.ndarray], stat, *, mean_block: float, reps: int, seed: int) -> np.ndarray:
    """Resample aligned series jointly (paired tests keep days together)."""
    rng = np.random.default_rng(seed)
    n = len(series[0])
    out = np.empty(reps)
    for b in range(reps):
        ix = stationary_bootstrap_indices(n, mean_block, rng)
        out[b] = stat(*[s[ix] for s in series])
    return out


def sharpe_lower_bound(r: np.ndarray, *, mean_block: float = 20, reps: int = 1000, seed: int = 7, alpha: float = 0.05) -> float:
    """Lower bound of the one-sided (1 - alpha) bootstrap CI of annualised Sharpe."""
    bs = bootstrap_stat([np.asarray(r, dtype=float)], sharpe_ann, mean_block=mean_block, reps=reps, seed=seed)
    return float(np.quantile(bs, alpha))


def moments(r: np.ndarray) -> tuple[float, float, float]:
    r = np.asarray(r, dtype=float)
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0, 0.0, 3.0
    z = (r - r.mean()) / sd
    return float(r.mean() / sd), float(np.mean(z ** 3)), float(np.mean(z ** 4))


def deflated_sharpe(r: np.ndarray, n_trials: int) -> float:
    """Bailey & Lopez de Prado DSR: P(true SR > SR0), SR0 the expected max SR of n_trials null strategies.
    Per-period SR; the variance of the SR estimator across trials is approximated by its null value 1/T."""
    sr, g3, g4 = moments(r)
    t = len(r)
    if t < 3:
        return 0.0
    gamma = 0.5772156649
    v = 1.0 / t
    if n_trials <= 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(v) * ((1 - gamma) * N.inv_cdf(1 - 1 / n_trials) + gamma * N.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    return float(N.cdf((sr - sr0) * math.sqrt(t - 1) / denom))


def min_trl(r: np.ndarray, *, z: float = 1.645) -> float:
    """Minimum track-record length in periods to show SR > 0 (Appendix B.4). Infinite when SR <= 0."""
    sr, g3, g4 = moments(r)
    if sr <= 0:
        return math.inf
    return 1 + (1 - g3 * sr + (g4 - 1) / 4 * sr * sr) * (z / sr) ** 2


def paired_sharpe_diff_ci(a: np.ndarray, b: np.ndarray, *, mean_block: float = 20, reps: int = 1000, seed: int = 11,
                          alpha: float = 0.05) -> tuple[float, float, float]:
    """(point, lower, upper) of Sharpe(a) - Sharpe(b), jointly resampled, two-sided (1 - alpha)."""
    bs = bootstrap_stat([np.asarray(a, float), np.asarray(b, float)], lambda x, y: sharpe_ann(x) - sharpe_ann(y),
                        mean_block=mean_block, reps=reps, seed=seed)
    return sharpe_ann(a) - sharpe_ann(b), float(np.quantile(bs, alpha / 2)), float(np.quantile(bs, 1 - alpha / 2))


def mean_return_ci(r: np.ndarray, *, mean_block: float = 20, reps: int = 1000, seed: int = 13, alpha: float = 0.05) -> tuple[float, float]:
    bs = bootstrap_stat([np.asarray(r, float)], lambda x: float(x.mean() * DAYS), mean_block=mean_block, reps=reps, seed=seed)
    return float(np.quantile(bs, alpha / 2)), float(np.quantile(bs, 1 - alpha / 2))


def n_eff(n_raw: int, m: int, rho_bar: float) -> float:
    """Spec §9.2: n_eff = n_raw / [1 + (m - 1) rho_bar]."""
    return n_raw / (1 + (m - 1) * rho_bar)
