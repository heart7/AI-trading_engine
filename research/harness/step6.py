"""§9.3 step 6: regime layer validation N1-N4 (spec §7.6, §9.3, §10.3b P6).

Step 6 gates only the regime layer's move from T0 to T1; it never gates SHADOW (INV-29). It runs during SHADOW
because N4 needs a shadow record. Part 6A, which the spec says defines N1-N4 in detail, was not supplied, so the
tests below are the build's reading of the one-line definitions in §9.3 (A-STEP6-N1-N4, decision 0006):

  N1 calibration      walk-forward one-bar-ahead state probabilities; ECE (one-vs-rest, 10 bins) <= ece_warn
  N2 label-shuffle    skill (Brier skill vs walk-forward climatology) must beat the 95th percentile of the skill on
                      permuted outcomes, and permuted outcomes must show no skill (leakage check)
  N3 context          a context M1-M7 is eligible for non-zero pool weight only if its out-of-sample Brier
                      uplift over the base matrix, on the same bars, has a block-bootstrap 5th percentile > 0;
                      N3 fails when any context carries weight without being eligible (none supplied: all 0)
  N4 shadow uplift    daily net Sharpe of the regime-throttled shadow book minus the unthrottled book, circular
                      block bootstrap (block = null_test.block_days), 5th percentile > 0, on an OBSERVED shadow
                      record of at least validation.shadow_days days
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from engine.common.canonical import content_hash
from engine.regime.ccmrm import BPD, IDX, STATES, calibration
from research.harness.verdicts import StepRecord, record_verdict


def walk_forward_probs(states: Sequence[str | None], H_days: int, *, with_index: bool = False):
    """One-bar-ahead P(next state) from decayed counts up to each bar (no look-ahead).

    Returns (probs[k, 4], outcome index[k], climatology[k, 4]) for bars where both state and next state exist,
    plus the bar indices when `with_index`."""
    lam = math.exp(-1.0 / (H_days * BPD))
    W = np.zeros((4, 4))
    visits = np.zeros(4)
    P, Y, C, T = [], [], [], []
    for t in range(1, len(states)):
        W *= lam
        visits *= lam
        a, b = states[t - 1], states[t]
        if a is not None and b is not None:
            W[IDX[a], IDX[b]] += 1.0
            visits[IDX[b]] += 1.0
        cur = states[t]
        nxt = states[t + 1] if t + 1 < len(states) else None
        if cur is None or nxt is None:
            continue
        row = W[IDX[cur]] + 1.0
        P.append(row / row.sum())
        Y.append(IDX[nxt])
        C.append((visits + 1.0) / (visits + 1.0).sum())
        T.append(t)
    out = np.array(P).reshape(-1, 4), np.array(Y, dtype=int), np.array(C).reshape(-1, 4)
    return (*out, np.array(T, dtype=int)) if with_index else out


def brier(P: np.ndarray, Y: np.ndarray) -> float:
    onehot = np.eye(4)[Y]
    return float(np.mean(np.sum((P - onehot) ** 2, axis=1)))


def skill(P: np.ndarray, Y: np.ndarray, C: np.ndarray) -> float:
    b0 = brier(C, Y)
    return 1.0 - brier(P, Y) / b0 if b0 > 0 else 0.0


def n1_calibration(P: np.ndarray, Y: np.ndarray, ece_max: float) -> dict[str, Any]:
    hit = (np.eye(4)[Y] > 0).ravel()
    ece = calibration(P.ravel(), hit)
    return {"test": "N1", "metric": "ECE", "value": ece, "limit": ece_max, "passed": bool(ece <= ece_max), "n": int(len(Y))}


def n2_label_shuffle(P: np.ndarray, Y: np.ndarray, C: np.ndarray, *, n_perm: int = 200, seed: int = 6) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    real = skill(P, Y, C)
    null = np.array([skill(P, rng.permutation(Y), C) for _ in range(n_perm)])
    q95 = float(np.quantile(null, 0.95))
    leak = float(np.mean(null))
    return {"test": "N2", "metric": "Brier skill", "value": real, "null_q95": q95, "null_mean": leak,
            "passed": bool(real > q95 and leak <= 0.0), "n_perm": n_perm}


def _block_bootstrap_q05(x: np.ndarray, stat, block: int, n: int, seed: int) -> float:
    rng = np.random.default_rng(seed)
    m = len(x)
    k = math.ceil(m / block)
    out = []
    for _ in range(n):
        starts = rng.integers(0, m, size=k)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:m] % m
        out.append(stat(x[idx]))
    return float(np.quantile(out, 0.05))


def n3_contexts(states: Sequence[str | None], contexts: Mapping[str, Sequence[bool]], H_days: int, *,
                block: int, pool_weights: Mapping[str, float] | None = None, n_boot: int = 500,
                seed: int = 7) -> dict[str, Any]:
    weights = dict(pool_weights or {})
    if not contexts:
        if any(w > 0 for w in weights.values()):
            return {"test": "N3", "metric": "context uplift", "value": None, "passed": False, "contexts": {},
                    "note": "a context carries pool weight without any N3 evidence"}
        return {"test": "N3", "metric": "context uplift", "value": None, "passed": True, "contexts": {},
                "note": "no context sources supplied (Part 6A absent): every context pool weight is 0"}
    P, Y, _, T = walk_forward_probs(states, H_days, with_index=True)
    base_err = dict(zip(T.tolist(), np.sum((P - np.eye(4)[Y]) ** 2, axis=1), strict=True))
    res = {}
    for name, flags in contexts.items():
        f = np.asarray(flags, bool)
        on = [s if (s is not None and f[t]) else None for t, s in enumerate(states)]
        Pc, Yc, _, Tc = walk_forward_probs(on, H_days, with_index=True)
        err_c = np.sum((Pc - np.eye(4)[Yc]) ** 2, axis=1)
        diff = np.array([base_err[t] - e for t, e in zip(Tc.tolist(), err_c, strict=True) if t in base_err])
        n = len(diff)  # > 0 means the context-conditioned matrix predicts better on the same bars
        if n < 2 * block * BPD:
            res[name] = {"eligible": False, "reason": "too few context bars", "n": int(n)}
            continue
        half = diff[n // 2:]  # out-of-sample half
        q05 = _block_bootstrap_q05(half, np.mean, block * BPD, n_boot, seed)
        res[name] = {"eligible": bool(q05 > 0), "uplift_q05": q05, "n": int(n)}
    bad = sorted(k for k, w in weights.items() if w > 0 and not res.get(k, {}).get("eligible"))
    return {"test": "N3", "metric": "context uplift", "value": None, "passed": not bad, "contexts": res,
            "weighted_but_ineligible": bad,
            "note": "contexts failing N3 keep pool weight 0; N3 fails only if an ineligible context carries weight"}


def sharpe(r: np.ndarray) -> float:
    sd = float(np.std(r, ddof=1)) if len(r) > 2 else 0.0
    return float(np.mean(r) / sd * math.sqrt(365)) if sd > 0 else 0.0


def throttled_returns(daily_returns: Sequence[float], daily_m: Sequence[float]) -> np.ndarray:
    """ASSUMED (A-STEP6-N1-N4): the throttled book's daily return is the base return scaled by that day's m <= 1."""
    m = np.clip(np.asarray(daily_m, float), 0.0, 1.0)
    return np.asarray(daily_returns, float) * m


def n4_shadow_uplift(base: Sequence[float], throttled: Sequence[float], *, record_class: str, min_days: int,
                     block_days: int, n_boot: int = 1000, seed: int = 8) -> dict[str, Any]:
    b, t = np.asarray(base, float), np.asarray(throttled, float)
    out: dict[str, Any] = {"test": "N4", "metric": "Sharpe uplift", "days": int(len(b)), "class": record_class}
    if record_class != "OBSERVED":
        return dict(out, value=None, passed=False, runnable=False, note="needs an OBSERVED shadow record, not FIXTURE")
    if len(b) < min_days or len(b) != len(t):
        return dict(out, value=None, passed=False, runnable=False, note=f"needs {min_days} shadow days, has {len(b)}")
    pair = np.stack([b, t], axis=1)
    up = sharpe(t) - sharpe(b)
    q05 = _block_bootstrap_q05(pair, lambda x: sharpe(x[:, 1]) - sharpe(x[:, 0]), block_days, n_boot, seed)
    return dict(out, value=up, ci_lo=q05, passed=bool(q05 > 0), runnable=True)


def run_step6(states: Sequence[str | None], policy: Mapping[str, Any], *, shadow_base: Sequence[float] = (),
              shadow_throttled: Sequence[float] = (), shadow_class: str = "FIXTURE",
              contexts: Mapping[str, Sequence[bool]] | None = None, pool_weights: Mapping[str, float] | None = None,
              data_hash: str = "") -> tuple[StepRecord, list[dict]]:
    rg, val = policy["regime"], policy["validation"]
    P, Y, C = walk_forward_probs(states, rg["H_days"])
    tests = [n1_calibration(P, Y, rg["ece_warn"]), n2_label_shuffle(P, Y, C),
             n3_contexts(states, contexts or {}, rg["H_days"], block=val["null_test"]["block_days"], pool_weights=pool_weights),
             n4_shadow_uplift(shadow_base, shadow_throttled, record_class=shadow_class, min_days=val["shadow_days"],
                              block_days=val["null_test"]["block_days"])]
    runnable = tests[3].get("runnable", True)
    passed = all(t["passed"] for t in tests)
    run_id = None if not runnable else "s6-" + content_hash({"data": data_hash, "states": [s or "" for s in states][-10:],
                                                             "tests": tests, "policy_regime": dict(rg)})[:16]
    rec = record_verdict(6, "regime", "PASS" if passed else "FAIL", run_id, "N1-N4", tests[0]["value"],
                         (None, None), details={"tests": tests, "states": list(STATES)})
    return rec, tests
