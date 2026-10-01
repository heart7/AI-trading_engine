"""P6: mode ladder, shadow runner and journal, shadow metrics, step 6 (N1-N4), learner comparison, live feed parsers."""
import ast
import copy
import re
import urllib.parse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from engine.bff.session import FIXTURE_MU_Q, fixture_universe
from engine.data.store import ChainBroken
from engine.governance import approvals as ap
from engine.modes.ladder import ModeLadder, ModeRefused, StepEvidence, promotion_subject
from engine.policy.loader import load_policy, thaw
from engine.regime.ccmrm import confirm, raw_states
from engine.replay.paper import ReplayConfig, Series, replay
from engine.router.router import StrategyRouter, TierEvidence, evaluate_gates
from engine.shadow import feed, metrics
from engine.shadow import runner as shadow_runner
from engine.shadow.runner import Quote, ShadowJournal, ShadowRunner
from research.harness import step6
from research.harness.verdicts import allowed_mode, record_verdict, regime_authority_allowed
from research.learner.shadow_compare import ProposalRejected, compare
from tests.helpers.authenticator import Authenticator

POL = load_policy()
DOC = thaw(POL.doc)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
H4 = timedelta(hours=4)


# ---------- helpers ----------
def signer():
    a = Authenticator()
    reg = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [{"key_id": "k0", "public_key": a.public_line}]}]})
    return a, reg


def promote_approval(a, subject, rationale="Shadow record reviewed; promoting one rung"):
    stmt = ap.statement("PROMOTE", subject, "principal", rationale, NOW.isoformat())
    return dict(stmt, signature=a.sign(ap.statement_bytes(stmt), ap.NAMESPACE))


def verifier(reg):
    return lambda appr, subject: ap.verify_approval(appr, reg, expected_action="PROMOTE", expected_subject=subject)


def shadow_steps(with_step6_fail=True):
    recs = [record_verdict(s, "A_long", "PASS", f"bt-{s}", "m", 1.0, (0.1, None)) for s in (1, 2, 3, 4, 5, 7, 8)]
    if with_step6_fail:
        recs.append(record_verdict(6, "regime", "FAIL", "s6-x", "N1-N4", 0.09, (None, None)))
    return recs


def good(rung, days=None, **kw):
    need = {"SHADOW": 90, "CANARY": 60, "LIVE_25": 30, "LIVE_50": 30}[rung]
    base = dict(rung=rung, days=need if days is None else days, recon=0.9995, cost_divergence=0.10, h1_d1_incidents=0,
                evidence_class="OBSERVED")
    base.update(kw)
    return StepEvidence(**base)


def journal_rows(days, rung="SHADOW", cls="OBSERVED", start=datetime(2026, 1, 1, tzinfo=timezone.utc), incidents=None,
                 recon_fail_at=None, priced=None):
    rows = []
    for k in range(days * 6):
        close = start + (k + 1) * H4
        inst = [{"instrument_id": "kraken-spot:BTC/USD", "recon_ok": k != recon_fail_at, "outcome": "REJECTED"}]
        if priced and k in priced:
            inst[0].update(cost_predicted=priced[k][0], cost_observed=priced[k][1])
        rows.append({"bar_close": close.isoformat(), "rung": rung, "class": cls, "instruments": inst,
                     "incidents": (incidents or {}).get(k, [])})
    return rows


@pytest.fixture(scope="module")
def fx():
    return fixture_universe(DOC, 1500 + 60)


def cut(series, end):
    return [Series(s.instrument_id, s.open_time[:end], s.o[:end], s.h[:end], s.l[:end], s.c[:end]) for s in series]


def close_of(series):
    return datetime.fromtimestamp(int(series[0].open_time[-1]), tz=timezone.utc) + H4


# ---------- mode ladder ----------
@pytest.mark.invariant("INV-29")
def test_shadow_promotion_ignores_step6_and_needs_steps_and_signature():
    a, reg = signer()
    lad = ModeLadder(DOC, POL.hash)
    with pytest.raises(ModeRefused) as e:
        lad.promote("SHADOW", on=date(2027, 1, 1), records=shadow_steps()[:-2], evidence=None,
                    approval=promote_approval(a, promotion_subject("PAPER", "SHADOW", POL.hash, None)), verify=verifier(reg))
    assert e.value.reason == "VALIDATION_STEPS_MISSING"
    with pytest.raises(ModeRefused) as e:
        lad.promote("SHADOW", on=date(2027, 1, 1), records=shadow_steps(), evidence=None, approval=None, verify=verifier(reg))
    assert e.value.reason == "APPROVAL_REQUIRED"
    # step 6 FAILED and SHADOW still opens (INV-29)
    lad.promote("SHADOW", on=date(2027, 1, 1), records=shadow_steps(), evidence=None,
                approval=promote_approval(a, promotion_subject("PAPER", "SHADOW", POL.hash, None)), verify=verifier(reg))
    assert lad.rung == "SHADOW" and lad.mode == "SHADOW" and lad.capital_fraction == 0.0


def test_promotion_is_one_rung_at_a_time_and_signature_binds_the_move():
    a, reg = signer()
    lad = ModeLadder(DOC, POL.hash)
    with pytest.raises(ModeRefused, match="ONE_STEP"):
        lad.promote("CANARY", on=date(2027, 1, 1), records=shadow_steps(), evidence=None, approval=None, verify=verifier(reg))
    lad.rung = "SHADOW"
    ev = good("SHADOW")
    other = promote_approval(a, promotion_subject("SHADOW", "CANARY", POL.hash, good("SHADOW", days=91)))
    with pytest.raises(ap.ApprovalRefused) as e:
        lad.promote("CANARY", on=date(2027, 4, 1), records=shadow_steps(), evidence=ev, approval=other, verify=verifier(reg))
    assert e.value.reason == "WRONG_SUBJECT"  # signed for a different record
    lad.promote("CANARY", on=date(2027, 4, 1), records=shadow_steps(), evidence=ev,
                approval=promote_approval(a, promotion_subject("SHADOW", "CANARY", POL.hash, ev)), verify=verifier(reg))
    assert lad.mode == "CANARY" and lad.capital_fraction == 0.05


@pytest.mark.parametrize("ev,gate", [
    (good("SHADOW", days=89), "DWELL"),
    (good("SHADOW", recon=0.9989), "RECON"),
    (good("SHADOW", cost_divergence=0.25), "COST_DIVERGENCE"),
    (good("SHADOW", cost_divergence=None), "COST_DIVERGENCE"),
    (good("SHADOW", h1_d1_incidents=1), "H1_D1"),
    (good("SHADOW", evidence_class="FIXTURE"), "EVIDENCE_CLASS"),
])
def test_each_step_gate_blocks_promotion(ev, gate):
    a, reg = signer()
    lad = ModeLadder(DOC, POL.hash, rung="SHADOW")
    with pytest.raises(ModeRefused) as e:
        lad.promote("CANARY", on=date(2027, 4, 1), records=shadow_steps(), evidence=ev,
                    approval=promote_approval(a, promotion_subject("SHADOW", "CANARY", POL.hash, ev)), verify=verifier(reg))
    assert e.value.reason == "STEP_GATES_FAIL" and gate in e.value.detail


def test_live_ramp_fractions_and_one_step_demotion():
    lad = ModeLadder(DOC, POL.hash, rung="LIVE_50")
    assert lad.mode == "LIVE" and lad.capital_fraction == 0.5
    assert lad.review(good("LIVE_50", days=3), on=date(2027, 9, 1)) is None  # dwell not yet met is not a failure
    ev = lad.review(good("LIVE_50", days=3, h1_d1_incidents=1), on=date(2027, 9, 2))
    assert ev["to"] == "LIVE_25" and lad.rung == "LIVE_25"
    lad.review(good("LIVE_25", cost_divergence=0.31), on=date(2027, 9, 3))
    assert lad.rung == "CANARY"
    assert ModeLadder(DOC, POL.hash).review(good("SHADOW", h1_d1_incidents=3), on=date(2027, 1, 1)) is None  # PAPER floor


# ---------- runner ----------
def test_runner_records_every_cycle_with_verifier_agreement(tmp_path, fx):
    r = ShadowRunner(DOC, POL.hash, ShadowJournal(tmp_path / "j.jsonl"), evidence_class="FIXTURE", mu_q_daily=FIXTURE_MU_Q)
    n = len(fx[0].c)
    for end in range(n - 6, n + 1):
        c = cut(fx, end)
        rec = r.cycle(c, bar_close=close_of(c), now=close_of(c) + timedelta(minutes=5), loaded_policy_hash=POL.hash, rung="PAPER")
        assert {x["instrument_id"] for x in rec["instruments"]} == {s.instrument_id for s in fx}
        assert all(x["recon_ok"] for x in rec["instruments"]) and not rec["incidents"]
        assert all(x["outcome"] in ("ENTERED", "REJECTED", "ABSTAINED", "HOLDING") for x in rec["instruments"])
    assert r.journal.verify() == 7


def test_policy_change_during_shadow_opens_h1_and_abstains(tmp_path, fx):
    r = ShadowRunner(DOC, POL.hash, ShadowJournal(tmp_path / "j.jsonl"), evidence_class="FIXTURE")
    rec = r.cycle(fx, bar_close=close_of(fx), now=close_of(fx), loaded_policy_hash="f" * 64, rung="SHADOW")
    assert rec["abstained"] == "PARAMS_NOT_FROZEN" and rec["incidents"][0]["severity"] == "H1" and rec["instruments"] == []


def test_missing_bar_is_d1_and_that_instrument_abstains(tmp_path, fx):
    r = ShadowRunner(DOC, POL.hash, ShadowJournal(tmp_path / "j.jsonl"), evidence_class="FIXTURE")
    late = [*fx[:3], cut([fx[3]], len(fx[3].c) - 1)[0]]
    rec = r.cycle(late, bar_close=close_of(fx), now=close_of(fx), loaded_policy_hash=POL.hash, rung="PAPER")
    miss = [x for x in rec["instruments"] if x["binding_gate"] == "DATA_STALE"]
    assert [x["instrument_id"] for x in miss] == [fx[3].instrument_id]
    assert {i["code"]: i["severity"] for i in rec["incidents"]} == {"BAR_MISSING": "D1"}


def test_uncertified_live_bar_never_feeds_a_shadow_cycle(tmp_path, fx):
    r = ShadowRunner(DOC, POL.hash, ShadowJournal(tmp_path / "j.jsonl"))  # OBSERVED runner, fixture series uncertified
    rec = r.cycle(fx, bar_close=close_of(fx), now=close_of(fx), loaded_policy_hash=POL.hash, rung="SHADOW")
    assert len(rec["incidents"]) == 4 and all(i["code"] == "BAR_MISSING" for i in rec["incidents"])


def test_verifier_disagreement_opens_recon_break(tmp_path, fx, monkeypatch):
    real = shadow_runner.independent_signal
    monkeypatch.setattr(shadow_runner, "independent_signal", lambda *a: (real(*a) or 0.0) + 1e-6)
    r = ShadowRunner(DOC, POL.hash, ShadowJournal(tmp_path / "j.jsonl"), evidence_class="FIXTURE")
    rec = r.cycle(fx, bar_close=close_of(fx), now=close_of(fx), loaded_policy_hash=POL.hash, rung="PAPER")
    assert {i["code"] for i in rec["incidents"]} == {"RECON_BREAK"} and not any(x["recon_ok"] for x in rec["instruments"])
    s = metrics.summarise(r.journal.records(), "PAPER")
    assert s["recon"] == 0.0 and len(s["h1_d1"]) == 4


def test_would_be_entries_are_priced_by_model_and_by_the_book(tmp_path):
    fx = fixture_universe(DOC, 6570)
    full = replay(fx, DOC, StrategyRouter(DOC), ReplayConfig(mu_q_daily=FIXTURE_MU_Q, keep_intents_for_last_cycles=6570 - 1500))
    hit = next(x for x in full.intents_tail if x["outcome"] == "ENTERED")
    end = int(np.searchsorted(fx[0].open_time, int(datetime.fromisoformat(hit["bar_close"]).timestamp()) - 14400)) + 1
    c = cut(fx, end)
    q = Quote(99.9, 100.1, 1.0e6, NOW)
    r = ShadowRunner(DOC, POL.hash, ShadowJournal(tmp_path / "j.jsonl"), evidence_class="FIXTURE", mu_q_daily=FIXTURE_MU_Q)
    rec = r.cycle(c, bar_close=close_of(c), now=close_of(c), loaded_policy_hash=POL.hash, rung="PAPER",
                  quotes={hit["instrument_id"]: q})
    row = next(x for x in rec["instruments"] if x["instrument_id"] == hit["instrument_id"])
    assert row["outcome"] == "ENTERED" and row["notional"] == pytest.approx(hit["notional"])
    assert row["cost_predicted"] == pytest.approx(0.001)
    assert row["cost_observed"] == pytest.approx(0.001 + 0.005 * min(1, hit["notional"] / 1e6))
    assert metrics.summarise(r.journal.records(), "PAPER")["priced_entries"] == 1


def test_runner_has_no_order_path():
    src = Path(shadow_runner.__file__).read_text() + Path(feed.__file__).read_text()
    mods = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    assert not any(m and (m.startswith("engine.execution") or m.startswith("engine.secrets")) for m in mods)
    assert not re.search(r"(?<![a-z_])place\(", src) and "api_key" not in src.lower()


def test_journal_tamper_breaks_the_chain(tmp_path):
    j = ShadowJournal(tmp_path / "j.jsonl")
    for row in journal_rows(1):
        j.append(row)
    p = tmp_path / "j.jsonl"
    p.write_text(p.read_text().replace('"recon_ok":true', '"recon_ok":false', 1))
    with pytest.raises(ChainBroken):
        j.verify()


# ---------- metrics and G2 ----------
def test_ninety_observed_days_satisfy_g2_and_fixture_never_does():
    rows = journal_rows(90)
    ev = metrics.tier_evidence(rows)
    assert (ev.shadow_days, ev.shadow_recon, ev.shadow_h1_d1_incidents) == (90, 1.0, 0)
    g2 = lambda e: next(g for g in evaluate_gates("T1", DOC, e, date(2027, 1, 1)) if g["gate"] == "G2")["passed"]  # noqa: E731
    assert g2(ev)
    assert not g2(metrics.tier_evidence(journal_rows(90, cls="FIXTURE")))
    assert not g2(metrics.tier_evidence(journal_rows(89)))
    assert not g2(metrics.tier_evidence(journal_rows(90, incidents={7: [{"code": "RECON_BREAK", "severity": "D1"}]})))
    assert not g2(metrics.tier_evidence(journal_rows(90, rung="PAPER")))  # pre-shadow cycles never count


def test_partial_days_do_not_count_and_divergence_is_aggregate():
    rows = journal_rows(3, priced={2: (0.001, 0.0012), 9: (0.001, 0.0011)})
    del rows[8]
    s = metrics.summarise(rows, "SHADOW")
    assert s["days"] == 2 and s["priced_entries"] == 2 and s["cost_divergence"] == pytest.approx(0.15)
    ev = metrics.step_evidence(rows, "SHADOW")
    assert ev.cost_divergence == pytest.approx(0.15) and ev.evidence_class == "OBSERVED"


def test_recon_below_target_fails_the_step():
    rows = journal_rows(90, recon_fail_at=5)  # 1 bad check in 540
    s = metrics.step_evidence(rows, "SHADOW")
    assert s.recon == pytest.approx(539 / 540) and s.recon < 0.999


# ---------- step 6 ----------
@pytest.fixture(scope="module")
def states():
    return confirm(raw_states(fixture_universe(DOC, 6570)[0].c), DOC["regime"]["confirm_bars"])


def test_step6_on_fixture_is_not_run_and_does_not_touch_shadow(states):
    rec, tests = step6.run_step6(states, DOC)
    assert rec.verdict == "NOT_RUN" and rec.run_id is None
    assert [t["passed"] for t in tests[:3]] == [True, True, True] and tests[3]["runnable"] is False
    recs = shadow_steps(with_step6_fail=False) + [rec]
    assert allowed_mode(recs) == "SHADOW" and regime_authority_allowed(recs) == "T0"


def test_step6_pass_needs_observed_shadow_uplift(states):
    rng = np.random.default_rng(1)
    base = rng.normal(0.0002, 0.02, 120)
    m = np.where(base < -0.01, 0.2, 1.0)  # throttling on the bad days only
    rec, tests = step6.run_step6(states, DOC, shadow_base=base, shadow_throttled=step6.throttled_returns(base, m),
                                 shadow_class="OBSERVED")
    assert tests[3]["passed"] and rec.verdict == "PASS" and rec.run_id.startswith("s6-")
    assert regime_authority_allowed([rec]) == "T1"
    rec2, t2 = step6.run_step6(states, DOC, shadow_base=base, shadow_throttled=base * 0.9 - 0.001, shadow_class="OBSERVED")
    assert rec2.verdict == "FAIL" and not t2[3]["passed"]


def test_n1_fails_on_miscalibrated_probabilities(states):
    P, Y, C = step6.walk_forward_probs(states, DOC["regime"]["H_days"])
    flat = np.full_like(P, 0.25)
    flat[:, 0] = 0.85
    flat[:, 1:] = 0.05
    assert step6.n1_calibration(P, Y, 0.05)["passed"] and not step6.n1_calibration(flat, Y, 0.05)["passed"]


def test_n2_label_shuffle_rejects_uninformative_and_leaky_models(states):
    P, Y, C = step6.walk_forward_probs(states, DOC["regime"]["H_days"])
    assert not step6.n2_label_shuffle(C, Y, C, n_perm=50)["passed"]  # climatology has no skill over itself
    assert step6.n2_label_shuffle(P, Y, C, n_perm=50)["passed"]


def test_n3_weight_needs_eligibility(states):
    flags = [(i // 600) % 2 == 0 for i in range(len(states))]
    out = step6.n3_contexts(states, {"M1": flags}, DOC["regime"]["H_days"], block=20, pool_weights={"M1": 0.3}, n_boot=100)
    assert out["contexts"]["M1"]["eligible"] is False and not out["passed"]
    assert not step6.n3_contexts(states, {}, 730, block=20, pool_weights={"M2": 0.1})["passed"]
    assert step6.n3_contexts(states, {}, 730, block=20)["passed"]


# ---------- learner comparison ----------
HYP = {"id": "H-TEST-TENTRY", "class": "HYPOTHESIS", "changes": ["signal.T_entry"]}


def test_learner_compares_without_touching_live(fx):
    live = copy.deepcopy(DOC)
    out = compare(live, HYP, {"signal.T_entry": 0.35}, fx, mu_q_daily=FIXTURE_MU_Q)
    assert live == DOC and out["class"] == "HYPOTHESIS" and out["applies"] is False
    assert out["decisions_compared"] > 0 and out["live_policy_hash"]


@pytest.mark.parametrize("hyp,over,reason", [
    (HYP, {"tiers.T2.r.A": 0.01}, "FORBIDDEN_PARAMETER"),
    (HYP, {"regime.authority": "T1"}, "FORBIDDEN_PARAMETER"),
    (HYP, {"cost.cost_R_max": 0.3}, "NOT_IN_HYPOTHESIS"),
    ({"id": "x", "class": "OBSERVED", "changes": ["signal.T_entry"]}, {"signal.T_entry": 0.4}, "UNREGISTERED"),
    ({**HYP, "changes": ["signal.nope"]}, {"signal.nope": 1}, "UNKNOWN_PARAMETER"),
])
def test_learner_refusals(fx, hyp, over, reason):
    with pytest.raises(ProposalRejected) as e:
        compare(DOC, hyp, over, fx[:1])
    assert e.value.reason == reason


# ---------- live feed (recorded payload shapes; no network) ----------
def _fake_exchange(start, n, now):
    rows = [(start + k * H4, 100 + k, 101 + k, 99 + k, 100.5 + k) for k in range(n)]

    def fetch(url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if "kraken" in u.netloc and u.path.endswith("OHLC"):
            if q["pair"] == "USDTUSD":
                return {"error": [], "result": {"USDTZUSD": [[int(t.timestamp()), "1", "1", "1", "1", "1", "1", 1] for t, *_ in rows], "last": 0}}
            since = int(q["since"])
            return {"error": [], "result": {"XXBTZUSD": [[int(t.timestamp()), str(o), str(h), str(lo), str(c), "0", "5", 3]
                                                          for t, o, h, lo, c in rows if t.timestamp() >= since], "last": 0}}
        if "binance" in u.netloc:
            s = int(q["startTime"]) / 1000
            return [[int(t.timestamp() * 1000), str(o), str(h), str(lo), str(c * 1.0003), "7"] for t, o, h, lo, c in rows if t.timestamp() >= s][:1000]
        if "bybit" in u.netloc:
            s = int(q["start"]) / 1000
            got = [[str(int(t.timestamp() * 1000)), str(o), str(h), str(lo), str(c * 0.9998), "7", "0"] for t, o, h, lo, c in rows if t.timestamp() >= s]
            return {"retCode": 0, "result": {"list": list(reversed(got[:1000]))}}
        if "bitstamp" in u.netloc:
            s = int(q["start"])
            got = [{"timestamp": str(int(t.timestamp())), "open": str(o), "high": str(h), "low": str(lo),
                    "close": str(c * 1.0001), "volume": "7"} for t, o, h, lo, c in rows if t.timestamp() >= s]
            return {"data": {"pair": "BTC/USD", "ohlc": got[:1000]}}
        raise AssertionError(url)
    return fetch


def test_refresh_certifies_from_three_public_sources_and_appends_only_new_bars(tmp_path):
    now = datetime(2026, 9, 28, 12, 7, tzinfo=timezone.utc)
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    fetch = _fake_exchange(start, 60, now)
    assert feed.refresh(tmp_path, "BTC", now, fetch=fetch) == 30
    assert feed.refresh(tmp_path, "BTC", now, fetch=fetch) == 0
    s = feed.load_history(feed.store_path(tmp_path, "BTC"))
    assert s.certified and len(s.c) == 30 and int(s.open_time[-1]) == int((datetime(2026, 9, 28, 12, tzinfo=timezone.utc) - H4).timestamp())
    assert s.instrument_id == "kraken-spot:BTC/USD"


def test_refresh_skips_geo_blocked_venues_and_still_certifies_from_two(tmp_path):
    import urllib.error
    now = datetime(2026, 9, 28, 12, 7, tzinfo=timezone.utc)
    inner = _fake_exchange(datetime(2026, 9, 20, tzinfo=timezone.utc), 60, now)

    def fetch(url):
        if "binance" in url or "bybit" in url:
            raise urllib.error.HTTPError(url, 451, "restricted location", None, None)
        return inner(url)
    assert feed.refresh(tmp_path, "BTC", now, fetch=fetch) == 30
    assert set(feed.last_skipped) == {"binance-spot", "bybit-v5-spot"}
    from engine.data.store import AppendOnlyLog
    recs = list(AppendOnlyLog(feed.store_path(tmp_path, "BTC")).records())
    assert all(r["sources"] == ["bitstamp", "kraken-spot"] for r in recs)


def test_refresh_with_kraken_alone_certifies_nothing(tmp_path):
    import urllib.error
    now = datetime(2026, 9, 28, 12, 7, tzinfo=timezone.utc)
    inner = _fake_exchange(datetime(2026, 9, 20, tzinfo=timezone.utc), 60, now)

    def fetch(url):
        if "kraken" not in url:
            raise urllib.error.URLError("proxy denied")
        return inner(url)
    feed.refresh(tmp_path, "BTC", now, fetch=fetch)
    assert len(feed.last_skipped) == 3 and feed.load_history(feed.store_path(tmp_path, "BTC")) is None


def test_kraken_only_certifies_when_allowed_and_flags_single_source(tmp_path):
    import urllib.error
    now = datetime(2026, 9, 28, 12, 7, tzinfo=timezone.utc)
    inner = _fake_exchange(datetime(2026, 9, 20, tzinfo=timezone.utc), 60, now)

    def fetch(url):
        if "kraken" not in url:
            raise urllib.error.URLError("proxy denied")
        return inner(url)
    assert feed.refresh(tmp_path, "BTC", now, fetch=fetch, allow_single_source=True) == 30
    path = feed.store_path(tmp_path, "BTC")
    assert feed.load_history(path).certified
    assert feed.single_source_at(path, datetime(2026, 9, 28, 8, tzinfo=timezone.utc))
    # once a second venue answers, new bars carry two sources and lose the flag
    later = now + 4 * H4
    feed.refresh(tmp_path, "BTC", later, fetch=_fake_exchange(datetime(2026, 9, 20, tzinfo=timezone.utc), 70, later),
                 allow_single_source=True)
    assert not feed.single_source_at(path, datetime(2026, 9, 29, tzinfo=timezone.utc))


def test_new_journal_reads_as_empty(tmp_path):
    from engine.shadow.runner import ShadowJournal
    assert ShadowJournal(tmp_path / "none.jsonl").records() == []


def test_load_history_keeps_the_newest_contiguous_run(tmp_path):
    from engine.data.store import AppendOnlyLog
    log = AppendOnlyLog(tmp_path / "X-USD.bars.jsonl")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for k in [0, 1, 2, 4, 5, 6]:
        t = t0 + k * H4
        log.append({"instrument_id": "x", "open_time": t.isoformat(), "o": 1, "h": 1, "l": 1, "c": 1, "certified": True})
    s = feed.load_history(tmp_path / "X-USD.bars.jsonl")
    assert len(s.c) == 3 and int(s.open_time[0]) == int((t0 + 4 * H4).timestamp())


def test_depth_parser_and_due_check():
    book = {"error": [], "result": {"XXBTZUSD": {"asks": [["100.1", "2", 1], ["100.4", "3", 1], ["101", "50", 1]],
                                                 "bids": [["99.9", "1", 1], ["99", "9", 1]]}}}
    q = feed.parse_kraken_depth(book, NOW)
    assert (q.bid, q.ask) == (99.9, 100.1) and q.depth_50bp_usd == pytest.approx(100.1 * 2 + 100.4 * 3)
    assert q.half_spread == pytest.approx(0.001)
    t = datetime(2026, 9, 28, 12, 2, tzinfo=timezone.utc)
    assert not feed.is_due([], t)  # inside the certification lag
    assert feed.is_due([], t + timedelta(minutes=5))
    assert not feed.is_due([{"bar_close": "2026-09-28T12:00:00+00:00"}], t + timedelta(minutes=5))


def test_router_tier_evidence_defaults_untouched():
    assert replace(TierEvidence(), **metrics.g2_fields([])) == TierEvidence()


# ---------- decision 0004 default: the unsigned venue-cap proposal ----------
def test_venue_cap_proposal_passes_the_battery_for_its_own_hash_and_is_unsigned():
    import json

    from engine.evidence.stress import run_battery
    from engine.governance.proposals import propose

    root = Path(__file__).resolve().parents[2]
    prop = load_policy(root / "policy" / "proposals" / "policy-10.4.1-venue-cap.yaml")
    run = run_battery(prop.doc, prop.hash, tier="T2")
    assert run["verdict"] == "PASS"
    stored = json.loads((root / "policy" / "proposals" / "policy-10.4.1-venue-cap.stress.json").read_text())
    assert stored["policy_hash"] == prop.hash and stored["verdict"] == "PASS"
    assert propose(POL, prop, stored).proposed.hash == prop.hash
    a, b = thaw(POL.doc), thaw(prop.doc)
    assert b["risk"]["venue_exposure_max"] == 0.20 and b["venues"]["per_venue_exposure_max"] == 0.20
    a["risk"]["venue_exposure_max"] = b["risk"]["venue_exposure_max"]
    a["venues"]["per_venue_exposure_max"] = b["venues"]["per_venue_exposure_max"]
    a["policy_version"] = b["policy_version"]
    assert a == b  # nothing else changed
    assert not list((root / "policy" / "approvals").glob("10.4.1*"))  # takes effect only when the principal signs
