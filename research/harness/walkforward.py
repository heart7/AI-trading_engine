"""Anchored walk-forward estimation of mu_q (spec §5.5, §9.1.3).

mu_q is the 25th-percentile posterior mean of the instrument's next-day log return, conditional on the
entry state (T >= T_entry and B > 0), with a zero-mean prior worth n0 pseudo-days:

    mu_post = sum(r) / (n + n0),   sd_post = sigma / sqrt(n + n0),   mu_q = mu_post + z_q * sd_post

It uses market data only (no trades), so walk-forward has no bootstrap circularity. Each test window
reads only training data that ends an embargo (>= longest lookback, 180 days) before the window starts;
the one-day forward label is purged at the training edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

BARS_PER_DAY = 6


@dataclass(frozen=True)
class Fold:
    train_end_bar: int  # exclusive; labels need bar + 6 < train_end_bar
    test_start_bar: int
    test_end_bar: int


def anchored_folds(n_bars: int, *, first_test_bar: int, test_bars: int, embargo_bars: int) -> list[Fold]:
    folds = []
    s = first_test_bar
    while s < n_bars:
        folds.append(Fold(max(0, s - embargo_bars), s, min(n_bars, s + test_bars)))
        s += test_bars
    return folds


def mu_q_estimate(c: np.ndarray, T: np.ndarray, B: np.ndarray, end_bar: int, *, T_entry: float, n0: int, q: float) -> float | None:
    idx = np.arange(0, max(0, end_bar - BARS_PER_DAY))
    ok = idx[(~np.isnan(T[idx])) & (T[idx] >= T_entry) & (B[idx] > 0)]
    all_r = np.diff(np.log(c[:end_bar]))
    if len(all_r) < 30:
        return None
    sigma = float(np.std(all_r) * np.sqrt(BARS_PER_DAY))  # daily
    r = np.log(c[ok + BARS_PER_DAY] / c[ok]) if len(ok) else np.array([])
    # overlapping 4h-sampled daily labels: count independent days, not bars
    n = len(r) / BARS_PER_DAY
    mu_post = float(r.mean()) * n / (n + n0) if len(r) else 0.0
    sd_post = sigma / np.sqrt(n + n0)
    return mu_post + NormalDist().inv_cdf(q) * sd_post


def mu_q_schedule(series_signals: dict[str, dict[str, np.ndarray]], closes: dict[str, np.ndarray], folds: list[Fold], *,
                  T_entry: float, n0: int, q: float) -> dict[str, np.ndarray]:
    out = {}
    for k, sig in series_signals.items():
        arr = np.full(len(closes[k]), np.nan)
        for f in folds:
            v = mu_q_estimate(closes[k], sig["T"], sig["B"], f.train_end_bar, T_entry=T_entry, n0=n0, q=q)
            if v is not None:
                arr[f.test_start_bar:f.test_end_bar] = v
        out[k] = arr
    return out
