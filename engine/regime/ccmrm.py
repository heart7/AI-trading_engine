"""Regime layer (CCMRM, spec §7.6, §7.6a). Default authority T0: claims are displayed, sizing sees m_regime = 1 and
no stress cut. Nothing here can raise size: m_regime is clipped to [0, 1] and the schema rejects m > 1.

State rule (the spec leaves it open; ASSUMED, A-REGIME-STATES), per 4h bar on completed daily bars:
  S  realised 20-day daily vol above the 90th percentile of its trailing 365-day history, or a 5-day drop > 15%
  U  close above EMA60 and EMA20 above EMA60
  D  close below EMA60 and EMA20 below EMA60
  R  otherwise
A state change is confirmed after `confirm_bars` consecutive bars (2, D). Transitions between confirmed states are
counted with exponential decay over H days. Rows are Dirichlet(1) posteriors; intervals are 90% Beta marginals.
Copy rule: "probability the next 4h state is U", never "prediction".
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from engine.common.canonical import content_hash
from engine.common.schemas import validate
from engine.common.special import beta_ppf, chi2_sf

STATES = ("U", "D", "R", "S")
IDX = {s: i for i, s in enumerate(STATES)}
BPD = 6
CI_TOO_WIDE = 0.50  # ASSUMED: a 90% interval wider than this cannot throttle anything


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    a, out, acc = 2.0 / (n + 1), np.empty(len(x)), x[0]
    for k, v in enumerate(x):
        acc = v if k == 0 else a * v + (1 - a) * acc
        out[k] = acc
    return out


def raw_states(c: np.ndarray) -> list[str | None]:
    """Raw state per 4h bar, causal: uses the bar's close and daily closes of completed days only."""
    nd = len(c) // BPD
    dc = c[BPD - 1::BPD][:nd]
    e20, e60 = _ema(dc, 20), _ema(dc, 60)
    lr = np.diff(np.log(dc), prepend=np.log(dc[0]))
    vol20 = np.array([lr[max(0, d - 19):d + 1].std() if d >= 19 else np.nan for d in range(nd)])
    out: list[str | None] = []
    for i in range(len(c)):
        d = i // BPD - 1  # last completed day
        if d < 365:
            out.append(None)
            continue
        hist = vol20[d - 364:d + 1]
        thr = np.nanpercentile(hist, 90)
        drop5 = c[i] / dc[d - 4] - 1 if d >= 4 else 0.0
        if vol20[d] > thr or drop5 < -0.15:
            out.append("S")
        elif c[i] > e60[d] and e20[d] > e60[d]:
            out.append("U")
        elif c[i] < e60[d] and e20[d] < e60[d]:
            out.append("D")
        else:
            out.append("R")
    return out


def confirm(raw: Sequence[str | None], dwell: int) -> list[str | None]:
    out: list[str | None] = []
    cur, cand, run = None, None, 0
    for s in raw:
        if s is None:
            out.append(cur)
            continue
        if cur is None:
            cur = s
        elif s != cur:
            run = run + 1 if s == cand else 1
            cand = s
            if run >= dwell:
                cur, cand, run = s, None, 0
        else:
            cand, run = None, 0
        out.append(cur)
    return out


@dataclass
class Matrix:
    counts: np.ndarray  # 4x4 decayed counts
    ess: np.ndarray  # per row

    def posterior(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        a = self.counts + 1.0
        a0 = a.sum(axis=1, keepdims=True)
        mean = a / a0
        lo, hi = np.empty_like(mean), np.empty_like(mean)
        for i in range(4):
            for j in range(4):
                lo[i, j] = beta_ppf(0.05, a[i, j], a0[i, 0] - a[i, j])
                hi[i, j] = beta_ppf(0.95, a[i, j], a0[i, 0] - a[i, j])
        return mean, lo, hi


def decayed_counts(states: Sequence[str | None], upto: int, H_days: int) -> Matrix:
    lam = 1.0 / (H_days * BPD)
    w_sum, w_sq = np.zeros((4, 4)), np.zeros(4)
    row_w = np.zeros(4)
    for t in range(1, upto + 1):
        a, b = states[t - 1], states[t]
        if a is None or b is None:
            continue
        w = math.exp(-(upto - t) * lam)
        w_sum[IDX[a], IDX[b]] += w
        row_w[IDX[a]] += w
        w_sq[IDX[a]] += w * w
    ess = np.where(w_sq > 0, row_w ** 2 / np.where(w_sq > 0, w_sq, 1), 0.0)
    return Matrix(w_sum, ess)


def m_regime(f_l: float, theta0: float, theta1: float) -> float:
    return float(min(1.0, max(0.0, (f_l - theta0) / (theta1 - theta0))))


@dataclass
class RegimeLayer:
    policy: Mapping[str, Any]
    policy_hash: str
    ece: float | None = None  # rolling calibration error from `calibration()`; None = not yet measured
    drift: str | None = None  # input drift status from research/learner/drift.py; FAIL renders ABSTAIN (§10.2 L3)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def authority(self) -> str:
        return self.policy["regime"]["authority"]

    def claim(self, instrument_id: str, c: np.ndarray, i: int, bar_close: datetime, *, side: int = 1,
              now: datetime | None = None, ttl_s: int = 4 * 3600) -> dict[str, Any] | None:
        rg = self.policy["regime"]
        states = confirm(raw_states(c[:i + 1]), rg["confirm_bars"])
        cur = states[i]
        if cur is None:
            return None
        mat = decayed_counts(states, i, rg["H_days"])
        mean, lo, hi = mat.posterior()
        r = IDX[cur]
        fav = "U" if side > 0 else "D"
        f_l = float(lo[r, IDX[fav]])
        reasons = []
        if mat.ess[r] < rg["ess_min"]:
            reasons.append("ESS")
        if hi[r, IDX[fav]] - lo[r, IDX[fav]] > CI_TOO_WIDE:
            reasons.append("CI_TOO_WIDE")
        if f_l <= rg["theta0"]:
            reasons.append("THETA0")
        if cur == "S":
            reasons.append("STRESS")
        if self.ece is not None and self.ece >= rg["ece_fail"]:
            reasons.append("N1")
        if self.drift == "FAIL":
            reasons.append("DRIFT")
        now = now or datetime.now(timezone.utc)
        if (now - bar_close).total_seconds() > ttl_s:
            reasons.append("STALE")
        m = 0.0 if reasons and reasons != ["THETA0"] else m_regime(f_l, rg["theta0"], rg["theta1"])
        payload = {"instrument_id": instrument_id, "bar_close": bar_close.isoformat(), "state": cur,
                   "P_next": {s: {"mean": float(mean[r, IDX[s]]), "lo90": float(lo[r, IDX[s]]),
                                  "hi90": float(hi[r, IDX[s]])} for s in STATES},
                   "counts_decayed": [[float(x) for x in row] for row in mat.counts],
                   "ESS": [float(x) for x in mat.ess], "m_regime": m, "authority": self.authority,
                   "binding_reasons": reasons}
        claim = {"claim_id": "rc-" + content_hash(payload)[:16], "kind": "regime_claim", "class": "ESTIMATED",
                 "snapshot_hash": content_hash([float(x) for x in c[:i + 1][-10:]]), "policy_hash": self.policy_hash,
                 "code_version": "0.1.0", "created_at": now.isoformat(), "ttl_s": ttl_s, "certified": False,
                 "payload": payload}
        validate("regime_claim", claim)
        self.history.append(claim)
        return claim

    def sizing_multiplier(self, claim: Mapping[str, Any] | None) -> float:
        """What sizing may use. At T0 the answer is always 1 (display only, no stress cut)."""
        if self.authority != "T1" or claim is None:
            return 1.0
        return min(1.0, float(claim["payload"]["m_regime"]))


def homogeneity(states: Sequence[str | None], boundaries: Sequence[int]) -> dict[str, Any]:
    """Chi-square test that per-era transition counts share one matrix (row by row, pooled statistic)."""
    edges = [0, *boundaries, len(states)]
    eras = []
    for a, b in zip(edges, edges[1:], strict=False):
        m = np.zeros((4, 4))
        for t in range(max(a, 1), b):
            if states[t - 1] and states[t]:
                m[IDX[states[t - 1]], IDX[states[t]]] += 1
        eras.append(m)
    stat, dof = 0.0, 0
    for r in range(4):
        tab = np.array([e[r] for e in eras])
        tab = tab[:, tab.sum(axis=0) > 0]
        tab = tab[tab.sum(axis=1) > 0]
        if tab.shape[0] < 2 or tab.shape[1] < 2:
            continue
        exp = tab.sum(axis=1, keepdims=True) * tab.sum(axis=0, keepdims=True) / tab.sum()
        stat += float(((tab - exp) ** 2 / exp).sum())
        dof += (tab.shape[0] - 1) * (tab.shape[1] - 1)
    p = chi2_sf(stat, dof) if dof else 1.0
    return {"chi2": stat, "dof": dof, "p_value": p, "homogeneous": p >= 0.05}


def information_horizon(P: np.ndarray, eps: float = 0.02, k_max: int = 500) -> int:
    """k*: bars after which every row of P^k is within `eps` total variation of the stationary distribution."""
    w, v = np.linalg.eig(P.T)
    pi = np.real(v[:, np.argmin(np.abs(w - 1))])
    pi = pi / pi.sum()
    Pk = np.eye(4)
    for k in range(1, k_max + 1):
        Pk = Pk @ P
        if 0.5 * np.abs(Pk - pi).sum(axis=1).max() < eps:
            return k
    return k_max


def calibration(pred: Sequence[float], hit: Sequence[bool], bins: int = 10) -> float:
    """Expected calibration error of probabilities `pred` against outcomes `hit`."""
    p, y = np.asarray(pred, float), np.asarray(hit, float)
    if not len(p):
        return float("nan")
    idx = np.minimum((p * bins).astype(int), bins - 1)
    ece = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            ece += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(ece)
