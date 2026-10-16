"""Reporter A11 (spec §16.5): INFORM only, answers from a retrieved bundle, cites claim ids.

- Refuses EXECUTE-type requests ("enter BTC", "raise limit", "mark step 2 PASS", "switch to T4"). It may explain how
  a tier is requested through Governance.
- Groundedness filter (in the BFF, after any drafter): a number in the draft that is not in the retrieved bundle
  is stripped and a RECON_BREAK is logged against the reporter.
- Outputs are REPORTED class and cannot be exported as evidence. The reporter is not in the decision path.

The drafter is pluggable. The default is a deterministic template drafter over the bundle; a language-model drafter
can be passed in, and its output goes through the same filter.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from engine.bff import projections as P
from engine.bff.session import PaperSession, pair_name
from engine.common.canonical import content_hash
from engine.ui.render import walk_figures

EXECUTE_PATTERNS = [
    r"\b(enter|buy|sell|short|long|open|close|exit|flatten|liquidate)\b.*\b(btc|eth|xrp|sol|position|trade|order)\b",
    r"\b(place|send|submit|cancel)\b.*\border",
    r"\b(raise|increase|lower|reduce|change|set|lift|remove)\b.*\b(limit|cap|risk|stop|leverage|size)\b",
    r"\bmark\b.*\b(pass|passed|fail|step)\b",
    r"\b(switch|move|upgrade|downgrade|promote|go)\b.*\b(t[0-4]|tier|live|canary|shadow)\b",
    r"^\s*(please\s+)?(approve|sign|activate|enable|disable|re-?arm|unlock|start|stop|kill|halt|resume)\b",
    r"\b(can|could|will|would) you\b.*\b(approve|sign|activate|enable|re-?arm|unlock|start|stop|kill)\b",
]
INTERROGATIVE = re.compile(r"^\s*(why|what|how|when|where|which|who|is|are|was|were|does|do|did|has|have|explain|show|tell)\b")
POLITE_COMMAND = re.compile(r"\b(can|could|will|would) you\b|\bplease\b|\bgo ahead\b")
NUM = re.compile(r"(?<![\w.])[-+−]?\$?\d[\d,]*(?:\.\d+)?%?(?![\w])")
PAIRS = ("BTC", "XRP", "ETH", "SOL")


def _norm(tok: str) -> str:
    t = tok.replace("−", "-").replace("$", "").replace(",", "").replace("%", "").lstrip("+")
    try:
        v = float(t)
    except ValueError:
        return t
    return f"{v:g}"


def is_execute(question: str) -> bool:
    q = question.lower().strip()
    if INTERROGATIVE.match(q) and not POLITE_COMMAND.search(q):
        return False  # a question about the engine, e.g. "how do I switch to T4?", is answered
    return any(re.search(p, q) for p in EXECUTE_PATTERNS)


def retrieve(s: PaperSession, question: str) -> dict[str, Any]:
    """Pick the projections the question is about. The bundle is the only source the answer may use."""
    q = question.lower()
    pair = next((p for p in PAIRS if p.lower() in q), None)
    parts: dict[str, Any] = {"policy": {"version": s.policy_version, "hash": s.policy_hash}}
    parts["topbar"] = P.topbar(s)
    if pair or any(w in q for w in ("why", "trade", "entry", "gate", "signal")):
        fr = P.fund_room(s)
        parts["why_not_trading"] = [w for w in fr["why_not_trading"] if pair is None or w["pair"].startswith(pair)]
    if pair:
        parts["rail"] = P.pair_rail(s, f"FIXTURE_{pair}")
        if any(w in q for w in ("regime", "probab", "state")):
            parts["probability"] = P.probability(s, pair)
    if any(w in q for w in ("nav", "p&l", "pnl", "profit", "loss", "money", "return")):
        fr = P.fund_room(s)
        parts["nav"] = fr["nav"]
        parts["pnl"] = fr["pnl"]
    if any(w in q for w in ("risk", "drawdown", "limit", "ladder", "rung")):
        parts["loss_ladder"] = P.loss_ladder(s)
    if any(w in q for w in ("validat", "step", "harness", "evidence")):
        parts["validation"] = P.validation(s)["sleeves"][0]["steps"]
    if any(w in q for w in ("incident", "broken", "wrong", "alert")):
        parts["incidents"] = P.incidents(s)["open"]
    return parts


def bundle_numbers(bundle: Any) -> set[str]:
    out: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, Mapping):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, bool):
            return
        elif isinstance(x, (int, float)):
            out.add(f"{float(x):g}")
        elif isinstance(x, str):
            for m in NUM.findall(x):
                out.add(_norm(m))
    walk(bundle)
    return out


def ground(draft: str, bundle: Any) -> tuple[str, list[str]]:
    """Strip every number that is not in the bundle. Returns (filtered text, stripped tokens)."""
    allowed = bundle_numbers(bundle)
    stripped: list[str] = []

    def sub(m: re.Match) -> str:
        if _norm(m.group(0)) in allowed:
            return m.group(0)
        stripped.append(m.group(0))
        return "[number removed: not in the retrieved evidence]"
    return NUM.sub(sub, draft), stripped


def template_drafter(question: str, bundle: Mapping[str, Any]) -> tuple[str, list[str]]:
    lines, cites = [], []
    tb = bundle["topbar"]
    lines.append(f"Mode {tb['mode']}, engine {tb['engine_state']}. {tb['strategy_chip']}. {tb['nav']['text']}.")
    for w in bundle.get("why_not_trading", []):
        if w["in_position"]:
            lines.append(f"{w['pair']}: in position.")
        else:
            g = w["binding_gate"]
            row = next((r for r in w["ladder"] if r["gate"] == g), None)
            detail = f" (value {row['value']} vs limit {row['limit']})" if row and row["value"] is not None else ""
            lines.append(f"{w['pair']}: no entry at the {w['bar_close']} close; binding gate {g}{detail}." if g else
                         f"{w['pair']}: entry signal at the {w['bar_close']} close.")
        cites.append(f"si:{w['pair']}:{w['bar_close']}")
    if "rail" in bundle:
        for f in bundle["rail"]["figures"]:
            if f["id"].startswith(("T-", "costR-", "m-", "regime-")):
                band = f" ({f['render']['band']['display']})" if f["render"].get("band") else ""
                lines.append(f"{f['label']}: {f['render']['display']}{band}.")
                cites.append(f["id"])
    if "probability" in bundle:
        p = bundle["probability"]
        lines.append(f"Regime {p['state']} ({p['authority_badge']}); claim {p['claim_id']}.")
        cites.append(p["claim_id"])
    if "nav" in bundle:
        lines.append(f"NAV {bundle['nav']['render']['display']}.")
        cites.append(bundle["nav"]["id"])
        for r in bundle["pnl"]:
            lines.append(f"Net P&L {r['period']}: {r['value']['render']['display']}.")
            cites.append(r["value"]["id"])
    for r in bundle.get("loss_ladder", []):
        lines.append(f"{r['label']}: {r['value']['render']['display']} of {r['limit']['render']['display']} ({r['consequence']}).")
        cites.append(r["value"]["id"])
    for r in bundle.get("validation", []):
        lines.append(f"Step {r['step']}: {r['verdict']['value']}" + (f" (run {r['run_id']})." if r["run_id"] else "."))
        cites.append(r["verdict"]["id"])
    if "incidents" in bundle:
        lines.append(f"Open incidents: {len(bundle['incidents'])}.")
    if "tier" in question.lower() or re.search(r"\bt[0-4]\b", question.lower()):
        lines.append("Tiers change only through Governance → Tier requests: an upgrade needs every gate to pass and a signed "
                     "passkey approval. The reporter cannot request or change a tier.")
    return " ".join(lines), cites


def ask(s: PaperSession, question: str, *, drafter: Callable[[str, Mapping[str, Any]], tuple[str, list[str]]] | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    now = now or s.clock
    qid = "rp-" + content_hash([question, now.isoformat()])[:16]
    if is_execute(question):
        text = ("I can only inform. I cannot place, change or approve anything. Orders come from the engine's own gates, "
                "limits and tiers change only through a Governance proposal signed with your passkey, and verdicts come "
                "only from harness run records. To request a tier, open Governance → Tier requests.")
        out = {"id": qid, "question": question, "answer": text, "citations": [], "refused": True, "refusal": "EXECUTE_REQUEST",
               "class": "REPORTED", "exportable_as_evidence": False, "stripped": []}
        s.reporter_log.append({"at": now.isoformat(), "question": question, "refused": True, "id": qid})
        return out
    bundle = retrieve(s, question)
    draft, cites = (drafter or template_drafter)(question, bundle)
    text, stripped = ground(draft, bundle)
    if stripped:
        s.incidents.open_incident("RECON_BREAK", "S3", "reporter", f"{qid}: ungrounded numbers stripped {stripped[:5]}")
    known = {f["id"] for f in walk_figures(bundle)} | {c for c in cites if c.startswith("si:")}
    if "probability" in bundle:
        known.add(bundle["probability"]["claim_id"])
    cites = [c for c in cites if c in known]
    s.reporter_log.append({"at": now.isoformat(), "question": question, "refused": False, "id": qid, "stripped": len(stripped)})
    return {"id": qid, "question": question, "answer": text, "citations": cites, "refused": False, "class": "REPORTED",
            "exportable_as_evidence": False, "stripped": stripped, "pairs": [pair_name(f"FIXTURE_{p}") for p in PAIRS if p.lower() in question.lower()]}


def reporter_events(log: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(x) for x in log]
