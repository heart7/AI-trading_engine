"""Episode factory (spec §10.2 L1, §10.3a, §11 trade_episode).

A matured episode is one closed position. Each side is priced three ways so the learner can separate the decision
from the execution: the bar close the decision was made on, the model's predicted fill from the arrival price, and
the realised fill. `cost_predicted` and `cost_realised` are adverse fractions of the arrival price (spread plus
slippage; fees are kept apart), which is what the cost-model retune (cost_retune.py) learns from.

- Episodes classed ADL_EVENT, FORCED_FLATTEN, TIER_DOWNGRADE_EXIT or PROTECTION_FAILURE are kept for incident
  analysis and excluded from training (§10.3).
- A side that paid far more than the model predicted is flagged COST_MODEL_OFF, a process error of class execution
  (A-EPISODE-COST-OFF), so the process-error rate the spec measures learning by (§10.4) moves with it.
- Episodes built from the PAPER replay are FIXTURE (paper fills equal the model by construction) and never feed a
  proposal. Real fills from CANARY onward are OBSERVED.
- The store is append-only and hash-chained, and adding the same episode twice is a no-op.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from engine.common.canonical import content_hash
from engine.common.schemas import validate
from engine.data.store import AppendOnlyLog
from engine.evidence.attribution import PROCESS_ERRORS, Episode
from engine.replay.paper import CostModel, ReplayResult, Series

EXCLUDED_CLASSES = ("ADL_EVENT", "FORCED_FLATTEN", "TIER_DOWNGRADE_EXIT", "PROTECTION_FAILURE")
STOP_REASONS = ("INITIAL_STOP", "TRAILING_STOP")
NOT_MATURED = ("END_OF_REPLAY",)  # a position still open when the replay ends is marked, not closed
# ASSUMED (A-EPISODE-COST-OFF): a side is a cost-model process error when it paid more than twice the predicted
# cost, with a 1 bp floor on the prediction so a zero-cost maker side is not flagged on rounding.
COST_OFF_MULT = 2.0
COST_OFF_FLOOR = 1e-4


@dataclass(frozen=True)
class Side:
    decision_px: float
    arrival_px: float
    model_px: float
    fill_px: float
    liquidity: str  # maker | taker

    def costs(self, buy: bool) -> tuple[float, float]:
        sign = 1.0 if buy else -1.0
        return (sign * (self.model_px - self.arrival_px) / self.arrival_px,
                sign * (self.fill_px - self.arrival_px) / self.arrival_px)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def build(*, instrument_id: str, sleeve: str, qty: float, entry: Side, exit: Side, entry_bar_close: str,
          exit_bar_close: str, fees_usd: float, risk_usd: float, hold_bars: int, exit_reason: str, cls: str,
          source: str, policy_hash: str, expected_hold: int, flags: Sequence[str] = ()) -> dict[str, Any]:
    long = sleeve != "B_short"
    ep_pred, ep_real = entry.costs(buy=long)
    xp_pred, xp_real = exit.costs(buy=not long)
    flags = list(dict.fromkeys(flags))
    for pred, real in ((ep_pred, ep_real), (xp_pred, xp_real)):
        if real > COST_OFF_MULT * max(pred, COST_OFF_FLOOR) and "COST_MODEL_OFF" not in flags:
            flags.append("COST_MODEL_OFF")
    direction = 1.0 if long else -1.0
    pnl = direction * qty * (exit.fill_px - entry.fill_px) - fees_usd
    if exit_reason in EXCLUDED_CLASSES:
        classification, perr = exit_reason, None
    else:
        perr = next((PROCESS_ERRORS[f] for f in flags if f in PROCESS_ERRORS), None)
        classification = "PROCESS_ERROR" if perr else "OUTCOME_VARIANCE"
    ep = {
        "id": content_hash({"instrument_id": instrument_id, "sleeve": sleeve, "entry": entry_bar_close,
                            "source": source, "class": cls, "policy_hash": policy_hash}),
        "sleeve": sleeve, "instrument_id": instrument_id, "class": cls, "source": source,
        "entry_intent": {"instrument_id": instrument_id, "bar_close": entry_bar_close},
        "exit_intent": {"instrument_id": instrument_id, "bar_close": exit_bar_close},
        "qty": qty,
        "entry": {**_side(entry), "cost_predicted": ep_pred, "cost_realised": ep_real},
        "exit": {**_side(exit), "cost_predicted": xp_pred, "cost_realised": xp_real},
        "R_realised": pnl / risk_usd if risk_usd > 0 else 0.0,
        "pnl_usd": pnl,
        "costs": {"fees_usd": fees_usd,
                  "predicted_usd": qty * (entry.arrival_px * ep_pred + exit.arrival_px * xp_pred),
                  "realised_usd": qty * (entry.arrival_px * ep_real + exit.arrival_px * xp_real)},
        "hold_bars": int(hold_bars), "expected_hold": int(expected_hold), "exit_reason": exit_reason,
        "classification": classification, "process_error_class": perr, "process_flags": flags,
        "excluded_from_training": classification in EXCLUDED_CLASSES, "policy_hash": policy_hash,
    }
    validate("trade_episode", ep)
    return ep


def _side(s: Side) -> dict[str, Any]:
    return {"decision_px": s.decision_px, "arrival_px": s.arrival_px, "model_px": s.model_px, "fill_px": s.fill_px,
            "liquidity": s.liquidity}


def matured(res: ReplayResult) -> list:
    return [t for t in res.trades if t.exit_reason not in NOT_MATURED]


def from_replay(res: ReplayResult, series: Sequence[Series], policy: Mapping[str, Any], policy_hash: str, *,
                costs: CostModel | None = None, sleeve: str = "A_long") -> list[dict[str, Any]]:
    """FIXTURE episodes from the PAPER replay's closed trades. Paper fills equal the model, so realised == predicted.
    Positions still open at the end of the replay are not episodes."""
    costs = costs or CostModel()
    by_id = {s.instrument_id: s for s in series}
    expected = int(policy["stops"]["time_stop"]["default_dwell_bars"])
    out = []
    for t in matured(res):
        s = by_id[t.instrument_id]
        ie = int(np.searchsorted(s.open_time, t.entry_time))
        ix = int(np.searchsorted(s.open_time, t.exit_time))
        if t.entry_liquidity == "maker":
            entry = Side(float(s.c[ie - 1]) if ie else float(s.o[ie]), float(s.o[ie]), float(t.entry_px), float(t.entry_px), "maker")
        else:
            arr = float(s.o[ie])
            entry = Side(float(s.c[ie - 1]) if ie else arr, arr, arr * (1 + costs.half_spread + costs.slippage_q75),
                         float(t.entry_px), "taker")
        if t.exit_reason in STOP_REASONS:
            arr = float(t.exit_px) / (1 - costs.slippage_q75)
            exit = Side(arr, arr, float(t.exit_px), float(t.exit_px), "taker")
        else:
            arr = float(s.o[ix])
            exit = Side(float(s.c[ix - 1]), arr, arr * (1 - costs.half_spread - costs.slippage_q75), float(t.exit_px), "taker")
        risk = t.pnl / t.R if t.R else 0.0
        out.append(build(instrument_id=t.instrument_id, sleeve=sleeve, qty=float(t.qty), entry=entry, exit=exit,
                         entry_bar_close=_iso(t.entry_time), exit_bar_close=_iso(t.exit_time), fees_usd=float(t.fees),
                         risk_usd=float(risk), hold_bars=t.bars_held, exit_reason=t.exit_reason, cls="FIXTURE",
                         source="PAPER_REPLAY", policy_hash=policy_hash, expected_hold=expected))
    return out


def to_attribution(ep: Mapping[str, Any], *, factor_return: float = 0.0) -> Episode:
    """The L2 view of an episode, so attribution runs on exactly what the learner sees."""
    e, x = ep["entry"], ep["exit"]
    return Episode.of(episode_id=ep["id"], sleeve=ep["sleeve"], qty=ep["qty"], entry_decision=e["decision_px"],
                      entry_model=e["model_px"], entry_fill=e["fill_px"], exit_decision=x["decision_px"],
                      exit_model=x["model_px"], exit_fill=x["fill_px"], fees=ep["costs"]["fees_usd"],
                      factor_return=factor_return, process_flags=tuple(ep["process_flags"]))


def process_error_rate(episodes: Iterable[Mapping[str, Any]]) -> float | None:
    """Share of trainable episodes that were process errors (§10.4: the learning system works if this falls)."""
    eps = [e for e in episodes if not e["excluded_from_training"]]
    return sum(e["classification"] == "PROCESS_ERROR" for e in eps) / len(eps) if eps else None


class EpisodeStore:
    """Append-only, hash-chained episode record. Idempotent by episode id."""

    def __init__(self, path: Path):
        self.log = AppendOnlyLog(Path(path))
        self._ids = {r["id"] for r in self.log.records()} if self.log.path.exists() else set()

    def add(self, ep: Mapping[str, Any]) -> bool:
        validate("trade_episode", dict(ep))
        if ep["id"] in self._ids:
            return False
        self.log.append(dict(ep))
        self._ids.add(ep["id"])
        return True

    def extend(self, eps: Iterable[Mapping[str, Any]]) -> int:
        return sum(self.add(e) for e in eps)

    def records(self) -> list[dict[str, Any]]:
        return self.log.records() if self.log.path.exists() else []

    def verify(self) -> int:
        return self.log.verify() if self.log.path.exists() else 0

    def training_set(self) -> list[dict[str, Any]]:
        """OBSERVED, non-excluded episodes only: FIXTURE never trains anything."""
        return [e for e in self.records() if e["class"] == "OBSERVED" and not e["excluded_from_training"]]
