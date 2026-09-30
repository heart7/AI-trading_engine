"""Shadow learner comparison (spec §10.3b P6, §10.2 L5, §10.3c).

During SHADOW the learner may propose; live parameters stay frozen. A proposal is a registered hypothesis plus a
set of parameter overrides. This module replays the same shadow bars under the live policy and under the
proposal and reports how the decisions would have differed. It has no write path: it works on deep copies,
returns HYPOTHESIS-class output, and proves the live policy hash is unchanged afterwards. Nothing it returns can
be applied; a change still needs harness steps, a stress-battery PASS for the new hash and a signed policy.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from engine.common.canonical import content_hash
from engine.replay.paper import CostModel, ReplayConfig, Series, replay
from engine.router.router import StrategyRouter

# §10.3c: no learner may touch these, whatever the hypothesis says.
FORBIDDEN_PREFIXES = ("tiers", "governance", "venues", "risk.ladder.dd", "regime.authority", "validation")


class ProposalRejected(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _set(doc: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = doc
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            raise ProposalRejected("UNKNOWN_PARAMETER", dotted)
        cur = cur[k]
    if keys[-1] not in cur:
        raise ProposalRejected("UNKNOWN_PARAMETER", dotted)
    cur[keys[-1]] = value


def compare(live_policy: Mapping[str, Any], hypothesis: Mapping[str, Any], overrides: Mapping[str, Any],
            series: Sequence[Series], *, nav0: float = 30_000.0, mu_q_daily: float | None = None,
            costs: CostModel | None = None) -> dict[str, Any]:
    if hypothesis.get("class") != "HYPOTHESIS" or not hypothesis.get("id"):
        raise ProposalRejected("UNREGISTERED", "a proposal must come from a registered hypothesis")
    changes = set(hypothesis.get("changes", []))
    for k in overrides:
        if k.startswith(FORBIDDEN_PREFIXES):
            raise ProposalRejected("FORBIDDEN_PARAMETER", k)
        if k not in changes:
            raise ProposalRejected("NOT_IN_HYPOTHESIS", f"{k} is not among {hypothesis['id']}'s registered changes")
    live_hash = content_hash(live_policy)
    live = copy.deepcopy(dict(live_policy))
    prop = copy.deepcopy(dict(live_policy))
    for k, v in overrides.items():
        _set(prop, k, v)
    cfg = ReplayConfig(nav0=nav0, mu_q_daily=mu_q_daily, keep_intents_for_last_cycles=len(series[0].c) if series else 1)
    costs = costs or CostModel()
    a = replay(series, live, StrategyRouter(live), cfg, costs)
    b = replay(series, prop, StrategyRouter(prop), cfg, costs)
    key = lambda x: (x["instrument_id"], x["bar_close"])  # noqa: E731
    da = {key(x): x["outcome"] for x in a.intents_tail}
    db = {key(x): x["outcome"] for x in b.intents_tail}
    differ = sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))
    sa, sb = a.summary(), b.summary()
    if content_hash(live_policy) != live_hash:
        raise AssertionError("learner comparison mutated the live policy")  # no write path, proved on every call
    return {"class": "HYPOTHESIS", "hypothesis_id": hypothesis["id"], "overrides": dict(overrides),
            "live_policy_hash": live_hash, "decisions_compared": len(set(da) | set(db)), "decisions_differ": len(differ),
            "first_differences": [{"instrument_id": k[0], "bar_close": k[1], "live": da.get(k), "proposal": db.get(k)}
                                  for k in differ[:20]],
            "live": {k: sa[k] for k in ("trades", "net_return", "sharpe_daily_ann", "max_drawdown")},
            "proposal": {k: sb[k] for k in ("trades", "net_return", "sharpe_daily_ann", "max_drawdown")},
            "applies": False, "note": "comparison only; applying needs §9.3 steps, a stress PASS and a signed policy"}
