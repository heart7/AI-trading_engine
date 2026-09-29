"""P7 offline parts: UK tax matching, canary guard and on-call, profit allocation, go-live checklist, runbooks."""
import csv
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from engine.governance import approvals as ap
from engine.governance import golive
from engine.governance import profit_allocation as pa
from engine.modes.canary import OnCallRota, Shift, check_entry
from engine.policy.loader import load_policy, thaw
from engine.tax.uk import TaxInputError, Transaction, compute, config_hash, export_rows, load_tax_policy, summary, tax_year
from tests.helpers.authenticator import Authenticator

ROOT = Path(__file__).resolve().parents[2]
POL = load_policy()
DOC = thaw(POL.doc)
D = Decimal
FX = {date(2026, 4, 1) + timedelta(days=k): D("0.8") for k in range(800)}


def tx(day, side, qty, usd, fee=0, asset="BTC", account="kraken-spot"):
    return Transaction(datetime.fromisoformat(day).replace(tzinfo=timezone.utc), account, asset, side, D(str(qty)), D(str(usd)), D(str(fee)))


# ---------- UK matching ----------
def test_section_104_pool_average_cost_with_fees():
    ds, pools = compute([tx("2026-05-01T10:00", "BUY", 10, 12500), tx("2026-05-02T10:00", "BUY", 5, 7400, fee=100),
                         tx("2026-05-20T10:00", "SELL", 4, 10050, fee=50)], FX)
    (d,) = ds
    assert [m.rule for m in d.matches] == ["S104"]
    assert d.proceeds_gbp == D("8000.0")  # (10050 - 50) * 0.8
    assert d.cost_gbp == pytest.approx(D(16000) * 4 / 15)  # pool cost 10000 + 6000 incl. fee
    assert pools["BTC"]["qty"] == 11


def test_same_day_then_thirty_day_then_pool():
    ds, _ = compute([tx("2026-05-01T09:00", "BUY", 10, 10000),
                     tx("2026-06-01T09:00", "BUY", 1, 2000), tx("2026-06-01T15:00", "SELL", 4, 6000),
                     tx("2026-06-20T09:00", "BUY", 2, 2500)], FX)
    (d,) = ds
    assert [(m.rule, m.qty) for m in d.matches] == [("SAME_DAY", 1), ("BED_AND_BREAKFAST", 2), ("S104", 1)]
    assert d.matches[1].acquired_on == date(2026, 6, 20) and d.matches[1].cost_gbp == D(2000)


def test_thirty_day_window_is_exactly_thirty_days():
    base = [tx("2026-05-01T09:00", "BUY", 10, 10000), tx("2026-06-01T09:00", "SELL", 1, 1500)]
    inside, _ = compute([*base, tx("2026-07-01T09:00", "BUY", 1, 900)], FX)
    outside, _ = compute([*base, tx("2026-07-02T09:00", "BUY", 1, 900)], FX)
    assert inside[0].matches[0].rule == "BED_AND_BREAKFAST" and outside[0].matches[0].rule == "S104"


def test_same_day_rule_takes_priority_over_an_earlier_disposals_thirty_day_match():
    ds, _ = compute([tx("2026-05-01T09:00", "BUY", 5, 5000), tx("2026-06-01T09:00", "SELL", 1, 1200),
                     tx("2026-06-10T09:00", "SELL", 1, 1300), tx("2026-06-10T12:00", "BUY", 1, 1250)], FX)
    first, second = ds
    assert [m.rule for m in second.matches] == ["SAME_DAY"]
    assert [m.rule for m in first.matches] == ["S104"]  # the 10 June buy was already used by 10 June's sale


def test_accounts_share_one_pool_worldwide():
    ds, pools = compute([tx("2026-05-01T09:00", "BUY", 1, 1000, account="binance-spot"),
                         tx("2026-05-03T09:00", "SELL", 1, 1100, account="kraken-spot")], FX)
    assert ds[0].matches[0].rule == "S104" and pools["BTC"]["qty"] == 0


def test_errors_are_refused_not_guessed():
    with pytest.raises(TaxInputError, match="exceeds holdings"):
        compute([tx("2026-05-01T09:00", "BUY", 1, 1000), tx("2026-05-03T09:00", "SELL", 2, 2000)], FX)
    with pytest.raises(TaxInputError, match="GBP/USD"):
        compute([tx("2020-01-01T09:00", "BUY", 1, 1000)], FX)
    with pytest.raises(TaxInputError):
        compute([tx("2026-05-01T09:00", "HOLD", 1, 1000)], FX)


def test_tax_years_and_reserve_unset_until_declared():
    assert tax_year(date(2027, 4, 5)) == "2026-27" and tax_year(date(2027, 4, 6)) == "2027-28"
    ds, _ = compute([tx("2026-05-01T09:00", "BUY", 2, 2000), tx("2026-05-03T09:00", "SELL", 1, 5000),
                     tx("2026-05-04T09:00", "SELL", 1, 500)], FX)
    pol = load_tax_policy()
    y = summary(ds, pol)["2026-27"]
    assert y["gains_gbp"] == D("3200.00") and y["losses_gbp"] == D("400.00") and y["net_gbp"] == D("2800.00")
    assert y["reserve_gbp"] is None and y["reserve_status"].startswith("UNSET")
    pol["years"]["2026-27"] = {"annual_exempt_gbp": 1000, "reserve_rate": 0.24}
    y = summary(ds, pol)["2026-27"]
    assert y["taxable_gbp"] == D("1800.00") and y["reserve_gbp"] == D("432.00")


def test_export_rows_sum_to_the_gains():
    ds, _ = compute([tx("2026-05-01T09:00", "BUY", 10, 10000), tx("2026-06-01T15:00", "SELL", 4, 6000),
                     tx("2026-06-01T09:00", "BUY", 1, 2000), tx("2026-06-20T09:00", "BUY", 2, 2500)], FX)
    rows = export_rows(ds)
    assert sum(D(r["gain_gbp"]) for r in rows if r["gain_gbp"]) == round(sum(d.gain_gbp for d in ds), 2)
    assert {r["rule"] for r in rows} == {"SAME_DAY", "BED_AND_BREAKFAST", "S104"}


def test_tax_report_cli(tmp_path):
    t, f = tmp_path / "t.csv", tmp_path / "fx.csv"
    t.write_text("ts,account,asset,side,qty,value_usd,fee_usd,ref\n2026-05-01T09:00:00+00:00,kraken-spot,BTC,BUY,1,1000,1,a\n"
                 "2026-05-09T09:00:00+00:00,kraken-spot,BTC,SELL,1,1500,1,b\n")
    f.write_text("date,gbp_per_usd\n2026-05-01,0.8\n2026-05-09,0.79\n")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "tax_report.py"), "--transactions", str(t), "--fx", str(f),
                        "--out", str(tmp_path / "o")], capture_output=True, text=True, check=True)
    assert "2026-27: 1 disposals" in r.stdout
    rows = list(csv.DictReader((tmp_path / "o" / "disposals.csv").open()))
    assert rows[0]["rule"] == "S104" and D(rows[0]["gain_gbp"]) == D("383.41")  # 1499*0.79 - 1001*0.8


# ---------- canary guard and on-call ----------
ROTA = OnCallRota([Shift("Gerald", date(2027, 5, 1), date(2027, 5, 31))], [(date(2027, 5, 20), date(2027, 5, 22))])


@pytest.mark.parametrize("rung,deployed,new,day,binding", [
    ("SHADOW", 0, 100, date(2027, 5, 2), "NO_LIVE_ORDERS"),
    ("CANARY", 0, 1_000, date(2027, 6, 2), "NO_ON_CALL"),
    ("CANARY", 0, 1_000, date(2027, 5, 21), "NO_ON_CALL"),  # declared holiday -> STOP
    ("CANARY", 1_000, 600, date(2027, 5, 2), "RUNG_CAPITAL_CAP"),  # 5% of $30k = $1,500
    ("CANARY", 1_000, 500, date(2027, 5, 2), None),
    ("LIVE_25", 7_000, 600, date(2027, 5, 2), "RUNG_CAPITAL_CAP"),
    ("LIVE_100", 20_000, 9_000, date(2027, 5, 2), None),
])
def test_canary_guard(rung, deployed, new, day, binding):
    g = check_entry(rung, DOC, tier_capital_usd=30_000, deployed_usd=deployed, new_notional_usd=new, today=day, rota=ROTA)
    assert g.binding == binding and g.allowed == (binding is None)
    if binding == "NO_ON_CALL":
        assert g.engine_state == "STOP"


def test_rota_gaps():
    assert len(ROTA.uncovered(date(2027, 5, 1), 31)) == 3
    assert golive.load_rota().uncovered(date(2027, 1, 1), 1) == [date(2027, 1, 1)]  # shipped rota is empty


# ---------- profit allocation ----------
def test_reinvest_change_needs_a_signed_approval_over_the_exact_change():
    a = Authenticator()
    reg = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [{"key_id": "k0", "public_key": a.public_line}]}]})
    verify = lambda appr, subj: ap.verify_approval(appr, reg, expected_action="PROFIT_ALLOCATION", expected_subject=subj)  # noqa: E731
    p = pa.propose(100, 70, POL.hash)
    with pytest.raises(pa.AllocationRefused, match="APPROVAL_REQUIRED"):
        pa.apply(p, None, verify)

    def sign(subject, action="PROFIT_ALLOCATION"):
        st = ap.statement(action, subject, "principal", "Take 30% of profit off-venue from now on", "2027-05-01T00:00:00+00:00")
        return dict(st, signature=a.sign(ap.statement_bytes(st), ap.NAMESPACE))
    with pytest.raises(ap.ApprovalRefused):
        pa.apply(p, sign(pa.propose(100, 50, POL.hash).subject_hash), verify)
    with pytest.raises(ap.ApprovalRefused):
        pa.apply(p, sign(p.subject_hash, action="PROMOTE"), verify)
    assert pa.apply(p, sign(p.subject_hash), verify) == 70
    for bad in (-1, 101):
        with pytest.raises(pa.AllocationRefused, match="OUT_OF_RANGE"):
            pa.propose(100, bad, POL.hash)


def test_sweep_is_a_human_transfer_after_the_tax_reserve():
    ti = pa.sweep_intent(10_000, 2_000, 70, venue="kraken-spot", period="2027-05")
    assert ti.amount == 2_400 and ti.executed_by == "human" and ti.to == "off-exchange-reserve"
    assert pa.sweep_intent(1_000, 2_000, 0, venue="kraken-spot", period="2027-05") is None
    assert pa.sweep_intent(10_000, 0, 100, venue="kraken-spot", period="2027-05") is None


# ---------- go-live checklist ----------
def test_golive_checklist_reports_every_item_and_nothing_is_ready_today():
    items = golive.checklist(POL, today=date(2026, 9, 29))
    assert [i["item"] for i in items] == [f"L{k}" for k in range(1, 10)]
    assert not golive.ready(items)
    by = {i["item"]: i for i in items}
    assert not by["L2"]["met"] and not by["L6"]["met"] and by["L6"]["owner"] == "accountant"
    assert "tax config" in by["L6"]["detail"]


def test_accountant_signoff_binds_the_exact_tax_config(tmp_path):
    import shutil
    root = tmp_path
    (root / "policy" / "tax").mkdir(parents=True)
    shutil.copy(ROOT / "policy" / "tax" / "tax-uk-v1.yaml", root / "policy" / "tax" / "tax-uk-v1.yaml")
    h = config_hash(load_tax_policy())
    (root / "policy" / "tax" / "signoff.json").write_text(json.dumps({"config_hash": "0" * 64, "accountant": "A", "signed_on": "2027-01-01"}))
    item = lambda: next(i for i in golive.checklist(POL, today=date(2027, 1, 2), root=root) if i["item"] == "L6")  # noqa: E731
    assert not item()["met"]
    (root / "policy" / "tax" / "signoff.json").write_text(json.dumps({"config_hash": h, "accountant": "A", "signed_on": "2027-01-01"}))
    assert item()["met"]


def test_golive_cli_runs():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "golive.py")], capture_output=True, text=True, check=True, cwd=ROOT)
    assert "NOT READY" in r.stdout


# ---------- runbooks ----------
SPEC_CLASSES = ["NO_BREAKOUT", "COST_R_EXCEEDED", "COST_INPUT_STALE", "STOP_WIDEN_ATTEMPT", "V_GT_ONE", "ADD_ON_LOSER",
                "PROTECTION_UNVERIFIED", "MARGIN_INVARIANT", "ADL_EVENT", "TIER_DOWNGRADE", "RECON_BREAK",
                "COST_MODEL_DIVERGENCE", "VENUE_PERMISSION_VIOLATION", "CLOCK_SKEW", "ORDER_STATE_MISMATCH"]


def test_every_incident_the_engine_can_open_has_a_runbook():
    codes = set(SPEC_CLASSES)
    for f in (ROOT / "engine").rglob("*.py"):
        src = f.read_text()
        codes |= set(re.findall(r'open_incident\("([A-Z0-9_]+)"', src))
        codes |= set(re.findall(r'"code": "([A-Z0-9_]+)", "severity"', src))
    missing = sorted(c for c in codes if not (ROOT / "ops" / "runbooks" / f"{c}.md").exists())
    assert not missing, missing
    for c in codes:
        text = (ROOT / "ops" / "runbooks" / f"{c}.md").read_text()
        assert all(k in text for k in ("What fired", "What the system already did", "What you own", "Resolve when"))


def test_incident_rows_link_their_runbook():
    from engine.bff import projections as P
    from tests.helpers.bff import fresh_session
    s = fresh_session()
    s.incidents.open_incident("RECON_BREAK", "D1", "engine", "test")
    row = P.incidents(s)["open"][0]
    assert row["runbook"] == "ops/runbooks/RECON_BREAK.md" and (ROOT / row["runbook"]).exists()


# ---------- reports (§12.7) ----------
@pytest.mark.parametrize("kind", ["eod", "monthly"])
def test_reports_are_built_from_claims_with_a_references_appendix(kind):
    from engine.reports.reports import build, to_markdown
    from engine.ui.render import walk_figures
    from tests.helpers.bff import session
    rep = build(session(), kind)
    ids = [f["id"] for f in walk_figures(rep["sections"])]
    assert ids and [r["id"] for r in rep["references"]] == ids
    assert rep["narrative"]["class"] == "REPORTED" and set(rep["narrative"]["cites"]) <= set(ids)
    assert rep["fixture"] and all(not f["render"]["exportable_as_evidence"] for f in walk_figures(rep["sections"]))
    md = to_markdown(rep)
    assert "FIXTURE data: certified false" in md and "## References" in md
    for f in walk_figures(rep["sections"]):
        assert f["render"]["display"] in md  # every number shown is the server's own rendering
    if kind == "monthly":
        titles = [s["title"] for s in rep["sections"]]
        for need in ("Cost stack and hurdle", "Attribution (return decomposition)", "Attribution (process view)",
                     "Tier eligibility", "Validation status", "Stress battery"):
            assert need in titles


def test_report_module_formats_no_number_itself():
    src = (ROOT / "engine" / "reports" / "reports.py").read_text()
    assert not re.search(r"\{[^}]*:[^}]*[0-9]*[.,][0-9]*[fe%]\}", src) and "round(" not in src


# ---------- new-sleeve ramp (§3.5) ----------
def test_upgrade_starts_new_sleeves_at_half_risk_for_thirty_days():
    from engine.router.router import TIERS, Eligibility, RouterState, StrategyRouter
    a = Authenticator()
    reg = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [{"key_id": "k", "public_key": a.public_line}]}]})
    now = datetime(2028, 1, 3, tzinfo=timezone.utc)
    stmt = ap.statement("TIER_UPGRADE", "c" * 64, "principal", "Add the short book at Core+Short", now.isoformat())
    va = ap.verify_approval(dict(stmt, signature=a.sign(ap.statement_bytes(stmt), ap.NAMESPACE)), reg,
                            expected_action="TIER_UPGRADE", expected_subject="c" * 64)
    router = StrategyRouter(DOC, RouterState(user_selected="T2", eligible="T3"))
    el = Eligibility("T3", {t: [{"gate": "G1", "passed": True, "detail": ""}] for t in TIERS[1:]}, None, now.date())
    assert router.request_up("T3", el, va, now) == "T3"
    rB, rA = DOC["tiers"]["T3"]["r"]["B"], DOC["tiers"]["T3"]["r"]["A"]
    assert router.r_tier("B_short", "LIVE", now + timedelta(days=10)) == pytest.approx(rB * 0.5)
    assert router.r_tier("A_long", "LIVE", now + timedelta(days=10)) == rA  # A was already live at T2
    assert router.r_tier("B_short", "LIVE", now + timedelta(days=30)) == rB
    assert router.r_tier("B_short", "PAPER", now + timedelta(days=10)) == rB  # paper replays are not ramped


# ---------- surfacing on the Validation and Governance screens ----------
def test_validation_screen_reads_the_shadow_journal(tmp_path):
    from engine.bff import projections as P
    from engine.shadow.runner import ShadowJournal
    assert P.shadow_record(tmp_path / "none.jsonl")["status"] == "no shadow record yet"
    j = ShadowJournal(tmp_path / "j.jsonl")
    start = datetime(2027, 1, 1, tzinfo=timezone.utc)
    for k in range(12):
        j.append({"bar_close": (start + (k + 1) * timedelta(hours=4)).isoformat(), "rung": "SHADOW", "class": "OBSERVED",
                  "instruments": [{"instrument_id": "kraken-spot:BTC/USD", "recon_ok": True}], "incidents": []})
    r = P.shadow_record(tmp_path / "j.jsonl")
    assert r["status"] == "12 cycles, hash chain intact" and r["g2_days"] == 2
    assert r["rungs"][0] == {"rung": "SHADOW", "days": 2, "cycles": 12, "recon": "100.00%", "cost_divergence": "no priced entries",
                             "h1_d1": 0, "class": "OBSERVED"}


def test_governance_screen_carries_the_golive_checklist():
    from engine.bff import projections as P
    from tests.helpers.bff import session
    g = P.governance(session())["golive"]
    assert [x["item"] for x in g] == [f"L{k}" for k in range(1, 10)] and not any(x["met"] for x in g[1:2])
    js = (ROOT / "engine" / "ui" / "static" / "app.js").read_text()
    assert "Go-live checklist" in js and "Shadow record (P6)" in js
