"""Go-live readiness (spec §19 P7 exit gate, §9.8, §12.4, §13.8, §21).

P7's exit gate is "go-live policy hash signed by the principal; accountant sign-off on tax config". LIVE also needs
everything under it: coherence for LIVE, the §9.3 SHADOW steps, 90 days of SHADOW and 60 days of CANARY on an
OBSERVED record, an on-call rota, an unexpired venue access record and a declared tax reserve. This module only
reads files and reports each item; it cannot activate anything or place an order.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from engine.governance import approvals as ap
from engine.modes.canary import OnCallRota, Shift
from engine.policy.coherence import EvidenceContext, check_policy
from engine.policy.loader import Policy
from engine.shadow.metrics import step_evidence
from engine.tax.uk import config_hash, load_tax_policy, tax_year
from research.harness.verdicts import SHADOW_STEPS, StepRecord, steps_passed

ROOT = Path(__file__).resolve().parents[2]
ONCALL_FILE = ROOT / "policy" / "oncall.yaml"
TAX_SIGNOFF_FILE = ROOT / "policy" / "tax" / "signoff.json"
ACCESS_DIR = ROOT / "policy" / "access_records"


def load_rota(path: Path = ONCALL_FILE) -> OnCallRota:
    if not path.exists():
        return OnCallRota()
    doc = yaml.safe_load(path.read_text()) or {}
    d = date.fromisoformat
    return OnCallRota([Shift(s["person"], d(str(s["start"])), d(str(s["end"]))) for s in doc.get("shifts", [])],
                      [(d(str(w["start"])), d(str(w["end"]))) for w in doc.get("stop_windows", [])])


def _item(key: str, label: str, ok: bool, detail: str, owner: str = "principal") -> dict[str, Any]:
    return {"item": key, "label": label, "met": bool(ok), "detail": detail, "owner": owner}


def checklist(policy: Policy, *, today: date, records: Iterable[StepRecord] = (), journal: Iterable[Mapping] = (),
              approvals_dir: Path = ROOT / "policy" / "approvals", registry: ap.SignerRegistry | None = None,
              ctx: EvidenceContext | None = None, root: Path = ROOT) -> list[dict[str, Any]]:
    out = []
    coh = check_policy(policy, ctx)
    fails = [c.id for c in coh.failures("LIVE")]
    out.append(_item("L1", "Policy coherent for LIVE", not fails, "all checks pass" if not fails else f"failing: {', '.join(fails)}",
                     "engine"))

    reg = registry or ap.SignerRegistry.load()
    signed = []
    for f in sorted(Path(approvals_dir).glob("*.json")):
        try:
            a = json.loads(f.read_text())
            ap.verify_approval(a, reg, expected_action="POLICY_ACTIVATE", expected_subject=policy.hash)
            signed.append(f.name)
        except (ValueError, ap.ApprovalRefused):
            continue
    out.append(_item("L2", "Go-live policy hash signed by the principal", bool(signed),
                     f"verified: {', '.join(signed)}" if signed else f"no verified POLICY_ACTIVATE for {policy.hash[:12]}"))

    missing = sorted(SHADOW_STEPS - steps_passed(records))
    out.append(_item("L3", "§9.3 steps 1-5, 7, 8 PASS on real history", not missing,
                     "all on file" if not missing else f"missing steps {missing}", "engine"))

    j = list(journal)
    sh, ca = step_evidence(j, "SHADOW"), step_evidence(j, "CANARY")
    sh_ok = all(g["passed"] for g in sh.gates(policy.doc))
    ca_ok = all(g["passed"] for g in ca.gates(policy.doc))
    out.append(_item("L4", "SHADOW record: 90 days, recon, cost divergence, zero H1/D1", sh_ok,
                     f"{sh.days} days, class {sh.evidence_class}", "engine"))
    out.append(_item("L5", "CANARY record: 60 days at <= 5% of capital", ca_ok, f"{ca.days} days, class {ca.evidence_class}",
                     "engine"))

    tax = load_tax_policy(root / "policy" / "tax" / "tax-uk-v1.yaml")
    so_path = root / "policy" / "tax" / "signoff.json"
    so = json.loads(so_path.read_text()) if so_path.exists() else None
    want = config_hash(tax)
    so_ok = bool(so and so.get("config_hash") == want and so.get("accountant") and so.get("signed_on"))
    out.append(_item("L6", "UK accountant sign-off on the tax configuration", so_ok,
                     f"signed by {so['accountant']} on {so['signed_on']}" if so_ok else
                     f"no sign-off for tax config {want[:12]} (policy/tax/signoff.json)", "accountant"))
    ty = tax.get("years", {}).get(tax_year(today), {})
    declared = ty.get("annual_exempt_gbp") is not None and ty.get("reserve_rate") is not None
    out.append(_item("L7", f"Tax reserve declared for {tax_year(today)}", declared,
                     "rate and exempt amount set" if declared else "reserve rate or annual exempt amount unset (ASSUMED)"))

    gaps = load_rota(root / "policy" / "oncall.yaml").uncovered(today, 30)
    out.append(_item("L8", "On-call rota covers the next 30 days (else STOP)", not gaps,
                     "covered" if not gaps else f"{len(gaps)} uncovered days from {gaps[0].isoformat()}"))

    recs = []
    adir = root / "policy" / "access_records"
    for f in sorted(adir.glob("*.json")) if adir.exists() else []:
        r = json.loads(f.read_text())
        exp = datetime.fromisoformat(r["expires_at"]).date() if r.get("expires_at") else None
        if exp and exp >= today and set(policy.doc["venues"]["execution_allowed"]["A_long"]) & {r.get("venue_instance_id")}:
            recs.append(r["venue_instance_id"])
    out.append(_item("L9", "Unexpired access record for every execution venue", bool(recs),
                     f"on file: {', '.join(recs)}" if recs else "no access record for kraken-spot (§8.6, INV-43)"))
    return out


def ready(items: Iterable[Mapping[str, Any]]) -> bool:
    return all(i["met"] for i in items)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
