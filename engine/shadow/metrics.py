"""Shadow record -> step evidence and §3.3 G2 inputs (spec §9.3 step 9, §9.8, §3.3).

- A day counts when all six 4h cycles of that UTC day are in the journal for the rung being measured.
- Recon is the share of instrument-cycles where the engine's T agrees with the verifier's (target >= 99.9%).
- Cost divergence is |sum observed - sum predicted| / sum predicted over the rung's priced would-be entries
  (target < 25%); with no priced entry it is unknown, and an unknown divergence does not pass.
- H1/D1 counts every incident of class H or D at severity 1 in the rung's cycles (target zero).
- A journal holding any FIXTURE cycle is FIXTURE class and never feeds a gate (INV-32 carried to evidence).
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from engine.modes.ladder import StepEvidence
from engine.router.router import TierEvidence

CYCLES_PER_DAY = 6


def _rung_cycles(journal: Iterable[Mapping[str, Any]], rung: str, since: datetime | None) -> list[Mapping[str, Any]]:
    out = []
    for r in journal:
        if r["rung"] != rung:
            continue
        if since is not None and datetime.fromisoformat(r["bar_close"]) <= since:
            continue
        out.append(r)
    return out


def summarise(journal: Iterable[Mapping[str, Any]], rung: str, *, since: datetime | None = None) -> dict[str, Any]:
    cyc = _rung_cycles(journal, rung, since)
    per_day: dict[str, set[str]] = defaultdict(set)
    checks = ok = 0
    pred = obs = 0.0
    priced = unpriced = 0
    h1d1: list[dict[str, Any]] = []
    classes = set()
    for r in cyc:
        classes.add(r["class"])
        close = datetime.fromisoformat(r["bar_close"])
        # the cycle at 00:00 closes the previous day's last bar
        day = (close.timestamp() - 1) // 86400
        per_day[str(int(day))].add(r["bar_close"])
        for row in r.get("instruments", []):
            if row.get("recon_ok") is not None:
                checks += 1
                ok += bool(row["recon_ok"])
            if "cost_predicted" in row:
                if row.get("cost_observed") is None:
                    unpriced += 1
                else:
                    priced += 1
                    pred += float(row["cost_predicted"])
                    obs += float(row["cost_observed"])
        h1d1 += [dict(i, bar_close=r["bar_close"]) for i in r.get("incidents", []) if i.get("severity") in ("H1", "D1")]
    days = sum(1 for v in per_day.values() if len(v) >= CYCLES_PER_DAY)
    div = abs(obs - pred) / pred if priced and pred > 0 else None
    cls = "FIXTURE" if ("FIXTURE" in classes or not classes) else "OBSERVED"
    return {"rung": rung, "cycles": len(cyc), "days": days, "recon_checks": checks,
            "recon": ok / checks if checks else 0.0, "priced_entries": priced, "unpriced_entries": unpriced,
            "cost_divergence": None if div is None or math.isnan(div) else div, "h1_d1": h1d1, "class": cls}


def step_evidence(journal: Iterable[Mapping[str, Any]], rung: str, *, since: datetime | None = None) -> StepEvidence:
    s = summarise(journal, rung, since=since)
    return StepEvidence(rung, s["days"], s["recon"], s["cost_divergence"], len(s["h1_d1"]), s["class"])


def g2_fields(journal: Iterable[Mapping[str, Any]], *, since: datetime | None = None) -> dict[str, Any]:
    """TierEvidence fields for G2. FIXTURE journals contribute nothing: the gate reads 'not on file'."""
    s = summarise(journal, "SHADOW", since=since)
    if s["class"] != "OBSERVED":
        return {"shadow_days": 0, "shadow_recon": 0.0, "shadow_h1_d1_incidents": 0}
    return {"shadow_days": s["days"], "shadow_recon": s["recon"], "shadow_h1_d1_incidents": len(s["h1_d1"])}


def tier_evidence(journal: Iterable[Mapping[str, Any]], base: TierEvidence | None = None, **kw: Any) -> TierEvidence:
    from dataclasses import replace
    return replace(base or TierEvidence(), **g2_fields(journal, **kw))
