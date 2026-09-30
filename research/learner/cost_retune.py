"""Cost-model retune from realised slippage (spec §10.3 allowed target "cost-model quantiles vs realised slippage").

The live cost model charges each taker side half_spread + slippage_q75 of the arrival price (A-FEES-KRAKEN, policy
cost.slippage_quantile = 0.75). This module compares that charge with what was actually paid and, when the evidence
is clear, writes a proposal. It never changes the cost model:

- Inputs are taker sides of OBSERVED episodes (real fills, CANARY onward) and OBSERVED shadow quotes (the order book
  at each would-be entry, decision 0006). FIXTURE inputs are reported and never fitted.
- The fitted value is the policy quantile of the realised adverse cost, with a seeded bootstrap interval.
- RAISE when the whole interval sits above the model's charge (costs are worse than assumed: the conservative
  direction). LOWER only when the interval sits below it AND enough real fills back it: quotes carry an ASSUMED
  impact model, so they can make the model stricter but never looser. Anything else is NO_CHANGE.
- A LOWER loosens the model, which lets more trades through the cost_R budget, so the proposal says so and requires
  the §9.3 step 4 cost tornado to be re-run on the new value. cost_R_max and N_max are never targets (§10.3c).
- Output is HYPOTHESIS class with applies: false. Applying it is a code or policy change that goes through the
  harness, a stress PASS and a signed approval like any other.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from engine.common.canonical import content_hash
from engine.replay.paper import CostModel, ReplayConfig, Series, replay
from engine.router.router import StrategyRouter

HYPOTHESIS_ID = "H-A-COST-QUANTILE"
TARGET = "CostModel.slippage_q75"
# ASSUMED (A-COST-RETUNE): evidence needed before any proposal, bootstrap size and interval.
MIN_FILLS = 50
MIN_QUOTES = 100
N_BOOT = 2000
CI = 0.90
FORBIDDEN_TARGETS = ("cost.cost_R_max", "portfolio.N_max")


def observations(episodes: Iterable[Mapping[str, Any]] = (), journal: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    out = []
    for ep in episodes:
        if ep["excluded_from_training"]:
            continue
        for side in ("entry", "exit"):
            s = ep[side]
            if s["liquidity"] != "taker":
                continue  # a resting limit pays no spread or slippage; it says nothing about the taker charge
            out.append({"source": "FILL", "class": ep["class"], "instrument_id": ep["instrument_id"],
                        "at": ep[f"{side}_intent"]["bar_close"], "cost_predicted": s["cost_predicted"],
                        "cost_realised": s["cost_realised"]})
    for rec in journal:
        for row in rec.get("instruments", []):
            if row.get("cost_observed") is None or "cost_predicted" not in row:
                continue
            out.append({"source": "SHADOW_QUOTE", "class": rec["class"], "instrument_id": row["instrument_id"],
                        "at": rec["bar_close"], "cost_predicted": float(row["cost_predicted"]),
                        "cost_realised": float(row["cost_observed"])})
    return out


def _bootstrap_q(x: np.ndarray, q: float, n_boot: int, ci: float, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    qs = np.concatenate([np.quantile(x[rng.integers(0, len(x), size=(min(200, n_boot - k), len(x)))], q, axis=1)
                         for k in range(0, n_boot, 200)])  # chunked so a long record stays small in memory
    a = (1 - ci) / 2
    return float(np.quantile(qs, a)), float(np.quantile(qs, 1 - a))


def fit(obs: Sequence[Mapping[str, Any]], *, costs: CostModel | None = None, quantile: float = 0.75,
        min_fills: int = MIN_FILLS, min_quotes: int = MIN_QUOTES, n_boot: int = N_BOOT, ci: float = CI,
        seed: int = 0) -> dict[str, Any]:
    costs = costs or CostModel()
    current = costs.half_spread + costs.slippage_q75
    real = [o for o in obs if o["class"] == "OBSERVED"]
    n_fix = len(obs) - len(real)
    fills = sum(o["source"] == "FILL" for o in real)
    quotes = len(real) - fills
    base = {"class": "HYPOTHESIS", "hypothesis_id": HYPOTHESIS_ID, "target": TARGET, "quantile": quantile,
            "current": {"half_spread": costs.half_spread, "slippage_q75": costs.slippage_q75, "side_charge": current},
            "n": {"fills": fills, "quotes": quotes, "fixture_ignored": n_fix}, "applies": False}
    if not real:
        return {**base, "evidence_class": "FIXTURE" if n_fix else "NONE", "verdict": "NO_EVIDENCE",
                "detail": "no OBSERVED fills or shadow quotes; FIXTURE is never fitted"}
    x = np.array([o["cost_realised"] for o in real], dtype=float)
    pred = np.array([o["cost_predicted"] for o in real], dtype=float)
    q_hat = float(np.quantile(x, quantile))
    div = float(abs(x.sum() - pred.sum()) / pred.sum()) if pred.sum() > 0 else None
    per = defaultdict(list)
    for o in real:
        per[o["instrument_id"]].append(o["cost_realised"])
    fitted = {**base, "evidence_class": "OBSERVED", "fitted": q_hat, "cost_divergence": div,
              "by_instrument": {k: {"n": len(v), "q": float(np.quantile(v, quantile))} for k, v in sorted(per.items())}}
    if fills < min_fills and quotes < min_quotes:
        return {**fitted, "verdict": "INSUFFICIENT",
                "detail": f"{fills} fills and {quotes} quotes; need {min_fills} fills or {min_quotes} quotes"}
    lo, hi = _bootstrap_q(x, quantile, n_boot, ci, seed)
    fitted["interval"] = [lo, hi]
    proposed = round(max(0.0, q_hat - costs.half_spread), 6)
    if lo > current:
        verdict, detail = "RAISE", f"q{quantile:g} realised {q_hat:.5f} > charge {current:.5f} across the {ci:.0%} interval"
    elif hi < current and fills >= min_fills:
        verdict, detail = "LOWER", f"q{quantile:g} realised {q_hat:.5f} < charge {current:.5f} across the {ci:.0%} interval"
    elif hi < current:
        verdict, detail = "NO_CHANGE", f"quotes suggest lower costs, but only {fills} real fills (need {min_fills}) can loosen the model"
    else:
        verdict, detail = "NO_CHANGE", f"charge {current:.5f} is inside the {ci:.0%} interval [{lo:.5f}, {hi:.5f}]"
    out = {**fitted, "verdict": verdict, "detail": detail}
    if verdict in ("RAISE", "LOWER"):
        out["proposed"] = {"slippage_q75": proposed, "side_charge": costs.half_spread + proposed}
        out["loosens"] = verdict == "LOWER"
        out["requires"] = (["§9.3 step 4 cost tornado re-run on the new value"] if verdict == "LOWER" else []) + \
            ["stress battery PASS for the new policy hash", "signed POLICY_ACTIVATE approval"]
    return out


def effect(policy: Mapping[str, Any], series: Sequence[Series], proposal: Mapping[str, Any], *,
           costs: CostModel | None = None, nav0: float = 30_000.0, mu_q_daily: float | None = None) -> dict[str, Any]:
    """What the proposal would have changed on the same bars. Works on copies and proves the policy is untouched."""
    if "proposed" not in proposal:
        return {"compared": False, "reason": proposal.get("verdict")}
    assert proposal["target"] not in FORBIDDEN_TARGETS
    costs = costs or CostModel()
    live_hash = content_hash(policy)
    doc = copy.deepcopy(dict(policy))
    cfg = ReplayConfig(nav0=nav0, mu_q_daily=mu_q_daily, keep_intents_for_last_cycles=len(series[0].c) if series else 1)
    a = replay(series, doc, StrategyRouter(doc), cfg, costs)
    b = replay(series, doc, StrategyRouter(doc), cfg, replace(costs, slippage_q75=proposal["proposed"]["slippage_q75"]))
    if content_hash(policy) != live_hash:
        raise AssertionError("cost retune mutated the live policy")
    key = lambda x: (x["instrument_id"], x["bar_close"])  # noqa: E731
    da = {key(x): x["outcome"] for x in a.intents_tail}
    db = {key(x): x["outcome"] for x in b.intents_tail}
    sa, sb = a.summary(), b.summary()
    pick = ("trades", "net_return", "sharpe_daily_ann", "max_drawdown")
    return {"compared": True, "live_policy_hash": live_hash,
            "decisions_differ": sum(da.get(k) != db.get(k) for k in set(da) | set(db)),
            "current": {k: sa[k] for k in pick}, "proposed": {k: sb[k] for k in pick}, "applies": False}
