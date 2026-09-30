"""End-of-day and monthly reports (spec §12.7, §7.9 loss-review cadence).

Reports are built from the BFF projections only, so every number in them is a rendered claim that carries its class,
lineage and FIXTURE marking; the report formats nothing itself and uses each figure's server-side `display`. A
references appendix lists every figure id. The one narrative sentence is REPORTED class and cites the figure ids it
mentions. Monthly adds the stress battery result and the ASSUMED items due for review within 90 days.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from engine.bff import projections as P
from engine.bff.session import PaperSession
from engine.ui.render import walk_figures

REVIEW_HORIZON_DAYS = 90


def _abstentions(s: PaperSession, day_iso: str) -> dict[str, int]:
    c = Counter(x["binding_gate"] or "ENTER" for x in s.intents_visible() if x["bar_close"].startswith(day_iso))
    return dict(sorted(c.items()))


def build(s: PaperSession, kind: str, *, on: date | None = None) -> dict[str, Any]:
    if kind not in ("eod", "monthly"):
        raise ValueError(kind)
    close = s.bar_close(s.last_index())
    on = on or close.date()
    pnl, inc = P.pnl(s), P.incidents(s)
    sections: list[dict[str, Any]] = [
        {"title": "Net P&L", "figures": pnl["net"]},
        {"title": "Reconciliation", "rows": [{"ledger_chain_ok": pnl["ledger"]["chain_ok"], "ledger_head": pnl["ledger"]["head"],
                                              "nav": pnl["net"][0]["verification"]["text"] if pnl["net"][0].get("verification") else None}]},
        {"title": "Abstentions and rejections by binding gate", "rows": [_abstentions(s, (close - timedelta(seconds=1)).date().isoformat())]},
        {"title": "Open incidents", "rows": [{k: r[k] for k in ("code", "severity", "scope", "runbook")} for r in inc["open"]]},
    ]
    if kind == "monthly":
        from engine.evidence.stress import run_battery
        stress = run_battery(s.policy, s.policy_hash, tier="T2")
        gov = P.governance(s)
        due = [a for a in gov["assumed"] if a.get("review_by") and date.fromisoformat(a["review_by"]) <= on + timedelta(days=REVIEW_HORIZON_DAYS)]
        sections += [
            {"title": "Cost stack and hurdle", "figures": [c["value"] for c in pnl["cost_stack"]] + [pnl["cost_hurdle"]]},
            {"title": "Attribution (return decomposition)", "figures": list(pnl["attribution"]["components"].values())},
            {"title": "Attribution (process view)", "figures": list(pnl["attribution"]["process"].values())},
            {"title": "Tier eligibility", "rows": [{"tier": t["tier"], "binding_gate": t["binding"]} for t in P.router(s)["ladder"]]},
            {"title": "Incidents this period", "rows": [{k: r[k] for k in ("code", "severity", "open", "resolution")}
                                                         for r in inc["open"] + inc["resolved"]]},
            {"title": "Validation status", "rows": [{"sleeve": x["sleeve"], "stage": x["current"]} for x in P.validation(s)["sleeves"]]},
            {"title": "Stress battery", "rows": [{"run_id": stress["run_id"], "verdict": stress["verdict"],
                                                  "failed": [x["id"] for x in stress["scenarios"] if x["verdict"] != "PASS"]}]},
            {"title": f"ASSUMED items due for review by {(on + timedelta(days=REVIEW_HORIZON_DAYS)).isoformat()}",
             "figures": due},
        ]
    net = pnl["net"][0]
    narrative = {"class": "REPORTED", "text": f"Net P&L for the record stands at {net['render']['display']}; "
                                              f"{len(inc['open'])} incident(s) open.", "cites": [net["id"]]}
    rep = {"kind": kind, "on": on.isoformat(), "policy_hash": s.policy_hash, "sections": sections, "narrative": narrative}
    figs = list(walk_figures(sections))
    rep["references"] = [{"id": f["id"], "class": f["class"], "fixture": f["fixture"], "lineage": f.get("lineage", [])} for f in figs]
    rep["fixture"] = any(f["fixture"] for f in figs)
    return rep


def _cell(v: Any) -> str:
    return str(v).replace("|", "\\|")


def to_markdown(rep: Mapping[str, Any]) -> str:
    title = "End-of-day report" if rep["kind"] == "eod" else "Monthly report"
    out = [f"# {title} · {rep['on']}", "", f"Policy `{rep['policy_hash'][:12]}`." +
           (" **FIXTURE data: certified false. Not evidence.**" if rep["fixture"] else ""), ""]
    for sec in rep["sections"]:
        out += [f"## {sec['title']}", ""]
        for f in sec.get("figures", []):
            chips = " ".join(f"[{c['text']}]" for c in f["render"]["chips"])
            band = f["render"].get("band")
            shown = f"{f['render']['display']} (band {band['display']})" if band else f["render"]["display"]
            out.append(f"- {f['label']}: **{shown}** ({f['class']}, `{f['id']}`) {chips}".rstrip())
        rows = sec.get("rows", [])
        if rows and rows[0]:
            keys = list(rows[0])
            out += ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
            out += ["| " + " | ".join(_cell(r.get(k)) for k in keys) + " |" for r in rows]
        elif not sec.get("figures"):
            out.append("None.")
        out.append("")
    n = rep["narrative"]
    out += ["## Narrative (REPORTED)", "", f"{n['text']} Cites: {', '.join(f'`{c}`' for c in n['cites'])}.", "",
            "## References", ""]
    out += [f"- `{r['id']}` {r['class']}{' FIXTURE' if r['fixture'] else ''}" for r in rep["references"]]
    return "\n".join(out) + "\n"
