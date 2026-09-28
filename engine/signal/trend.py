"""Shared trend signal T = (B + M + Z) / 3 (spec §4).

Deterministic and causal: the value at 4h bar i uses only bars 0..i. Two implementations exist:
`signal_at` (scalar, one bar, readable) and `signal_series` (vectorised, for replay). Tests
require them to agree exactly; the verifier (P3.5) gets a third, independently written path.

Interpretations recorded here (spec is silent):
- B compares close_t with the max/min of the 4h closes of the prior L days (L*6 bars, current bar
  excluded). Closes only, never highs or lows (v8 regression).
- M uses log returns over l months = 30*l days to close_t, scaled by the EWMA daily vol
  (lambda from policy) of completed daily bars times sqrt(days), divided by clip_t, clipped to [-1, 1].
- Z uses EMA_fast/EMA_slow and ATR_n on completed daily bars (the day containing bar t is excluded
  until it completes). ATR is the simple mean of the last n true ranges.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

BARS_PER_DAY = 6


@dataclass(frozen=True)
class SignalParams:
    breakout_lookbacks_d: tuple[int, ...]
    momentum_lookbacks_m: tuple[int, ...]
    clip_t: float
    ema_fast: int
    ema_slow: int
    atr_n: int
    ewma_lambda: float
    T_entry: float
    components: tuple[str, ...] = ("B", "M", "Z")  # ablation (§9.3 step 3) removes one; B>0 entry gate stays

    @classmethod
    def from_policy(cls, doc: Mapping) -> SignalParams:
        s = doc["signal"]
        return cls(tuple(s["breakout_lookbacks_d"]), tuple(s["momentum_lookbacks_m"]), float(s["clip_t"]),
                   int(s["ema_fast"]), int(s["ema_slow"]), int(s["atr_n"]), float(doc["sizing"]["ewma_lambda"]),
                   float(s["T_entry"]))

    @property
    def min_history_bars(self) -> int:
        need_days = max(max(self.breakout_lookbacks_d), 30 * max(self.momentum_lookbacks_m))
        return need_days * BARS_PER_DAY


@dataclass(frozen=True)
class Signal:
    B: float | None
    M: float | None
    Z: float | None
    T: float | None
    abstain: str | None
    atr_daily: float | None
    sigma_daily: float | None


def _clip(x: float) -> float:
    return float(min(1.0, max(-1.0, x)))


def daily_from_arrays(h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:  # noqa: E741
    """Completed daily bars strictly before the day containing bar i. Bar 0 must open at 00:00 UTC."""
    ndays = i // BARS_PER_DAY  # days fully before bar i's day
    if ndays == 0:
        return np.empty(0), np.empty(0), np.empty(0)
    n = ndays * BARS_PER_DAY
    hh = h[:n].reshape(ndays, BARS_PER_DAY).max(axis=1)
    ll = l[:n].reshape(ndays, BARS_PER_DAY).min(axis=1)
    cc = c[:n].reshape(ndays, BARS_PER_DAY)[:, -1]
    return hh, ll, cc


def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1)
    out = np.empty_like(x, dtype=float)
    acc = x[0]
    for k in range(len(x)):
        acc = x[k] if k == 0 else a * x[k] + (1 - a) * acc
        out[k] = acc
    return out


def atr(hh: np.ndarray, ll: np.ndarray, cc: np.ndarray, n: int) -> float:
    prev = np.concatenate(([cc[0]], cc[:-1]))
    tr = np.maximum(hh - ll, np.maximum(np.abs(hh - prev), np.abs(ll - prev)))
    return float(tr[-n:].mean())


def ewma_vol(cc: np.ndarray, lam: float) -> float:
    r = np.diff(np.log(cc))
    var = r[0] ** 2
    for x in r[1:]:
        var = lam * var + (1 - lam) * x * x
    return float(np.sqrt(var))


def signal_at(h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int, p: SignalParams) -> Signal:  # noqa: E741
    if i + 1 < p.min_history_bars + 1:
        return Signal(None, None, None, None, "INSUFFICIENT_HISTORY", None, None)
    ct = c[i]
    votes = []
    for L in p.breakout_lookbacks_d:
        w = c[i - L * BARS_PER_DAY:i]
        votes.append(1.0 if ct > w.max() else (-1.0 if ct < w.min() else 0.0))
    B = float(np.mean(votes))

    hh, ll, cc = daily_from_arrays(h, l, c, i)
    if len(cc) < max(p.ema_slow, p.atr_n + 1, 30):
        return Signal(B, None, None, None, "INSUFFICIENT_HISTORY", None, None)
    sig = ewma_vol(cc, p.ewma_lambda)
    ms = []
    for m in p.momentum_lookbacks_m:
        days = 30 * m
        r = float(np.log(ct / c[i - days * BARS_PER_DAY]))
        ms.append(_clip(r / (sig * np.sqrt(days)) / p.clip_t) if sig > 0 else 0.0)
    M = float(np.mean(ms))
    a = atr(hh, ll, cc, p.atr_n)
    ef, es = ema(cc, p.ema_fast)[-1], ema(cc, p.ema_slow)[-1]
    Z = _clip((ef - es) / a / p.clip_t) if a > 0 else 0.0
    vals = {"B": B, "M": M, "Z": Z}
    T = sum(vals[k] for k in p.components) / len(p.components)
    return Signal(B, M, Z, T, None, a, sig)


def signal_series(h: np.ndarray, l: np.ndarray, c: np.ndarray, p: SignalParams) -> dict[str, np.ndarray]:  # noqa: E741
    """Vectorised replay path: same numbers as signal_at for every bar (NaN where it abstains)."""
    n = len(c)
    out = {k: np.full(n, np.nan) for k in ("B", "M", "Z", "T", "atr_daily", "sigma_daily")}
    ndays = n // BARS_PER_DAY
    hh = h[:ndays * BARS_PER_DAY].reshape(ndays, BARS_PER_DAY).max(axis=1)
    ll = l[:ndays * BARS_PER_DAY].reshape(ndays, BARS_PER_DAY).min(axis=1)
    cc = c[:ndays * BARS_PER_DAY].reshape(ndays, BARS_PER_DAY)[:, -1]
    # per-day indicator values computed over days [0..d] (inclusive)
    ef, es = ema(cc, p.ema_fast), ema(cc, p.ema_slow)
    prev = np.concatenate(([cc[0]], cc[:-1]))
    tr = np.maximum(hh - ll, np.maximum(np.abs(hh - prev), np.abs(ll - prev)))
    csum = np.concatenate(([0.0], np.cumsum(tr)))
    r = np.diff(np.log(cc))
    var = np.empty(max(len(r), 0))
    for k in range(len(r)):
        var[k] = r[0] ** 2 if k == 0 else p.ewma_lambda * var[k - 1] + (1 - p.ewma_lambda) * r[k] ** 2
    min_days = max(p.ema_slow, p.atr_n + 1, 30)
    for i in range(p.min_history_bars, n):
        ct = c[i]
        votes = []
        for L in p.breakout_lookbacks_d:
            w = c[i - L * BARS_PER_DAY:i]
            votes.append(1.0 if ct > w.max() else (-1.0 if ct < w.min() else 0.0))
        B = float(np.mean(votes))
        out["B"][i] = B
        d = i // BARS_PER_DAY  # number of completed days
        if d < min_days:
            continue
        sig = float(np.sqrt(var[d - 2]))
        a = float((csum[d] - csum[d - p.atr_n]) / p.atr_n)
        ms = []
        for m in p.momentum_lookbacks_m:
            days = 30 * m
            rr = float(np.log(ct / c[i - days * BARS_PER_DAY]))
            ms.append(_clip(rr / (sig * np.sqrt(days)) / p.clip_t) if sig > 0 else 0.0)
        M = float(np.mean(ms))
        Z = _clip((ef[d - 1] - es[d - 1]) / a / p.clip_t) if a > 0 else 0.0
        vals = {"B": B, "M": M, "Z": Z}
        out["M"][i], out["Z"][i] = M, Z
        out["T"][i] = sum(vals[k] for k in p.components) / len(p.components)
        out["atr_daily"][i], out["sigma_daily"][i] = a, sig
    return out
