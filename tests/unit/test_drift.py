"""P9: drift monitors (PSI), calibration expiry, ABSTAIN on FAIL, learner halt, learning surfaced on screens."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from engine.bff import projections as P
from engine.bff.session import PaperSession, fixture_universe
from engine.policy.loader import load_policy, thaw
from engine.regime.ccmrm import RegimeLayer
from engine.replay.paper import Series
from research.learner import drift
from research.registry import registry

POL = load_policy()
DOC = thaw(POL.doc)
RG = DOC["regime"]


@pytest.fixture(scope="module")
def session():
    return PaperSession.build()


def _series(ret_sd_recent=None, n=6 * 400, certified=True, seed=0):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.01, n)
    if ret_sd_recent:
        r[-6 * 30:] = rng.normal(0, ret_sd_recent, 6 * 30)
    c = 100 * np.exp(np.cumsum(r))
    t = np.arange(n, dtype=np.int64) * 4 * 3600 + 1_600_000_000 // 14400 * 14400
    return Series("XBTUSD", t, c, c * 1.005, c * 0.995, c, certified=certified)


# ---------- PSI ----------
def test_psi_zero_on_same_distribution_and_grows_with_shift():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 1, 5000)
    assert drift.psi(a, a) == pytest.approx(0.0, abs=1e-12)
    assert drift.psi(a, rng.normal(0, 1, 5000)) < drift.PSI_WARN
    assert drift.psi(a, rng.normal(0.5, 1, 5000)) > drift.PSI_WARN
    assert drift.psi(a, rng.normal(0, 3, 5000)) > drift.PSI_FAIL
    with pytest.raises(ValueError):
        drift.psi([1.0, 2.0], [1.0])


def test_grade_bands():
    assert [drift.grade(x) for x in (0.0, 0.0999, 0.10, 0.2499, 0.25)] == ["OK", "OK", "WARN", "WARN", "FAIL"]


def test_report_ok_stable_and_fail_on_vol_shift():
    ok = drift.report([_series()])
    assert ok["status"] == "OK" and ok["class"] == "OBSERVED" and {r["feature"] for r in ok["rows"]} == {"log_return_4h", "range_4h"}
    bad = drift.report([_series(ret_sd_recent=0.04)])
    assert bad["status"] == "FAIL"
    assert next(r for r in bad["rows"] if r["feature"] == "log_return_4h")["status"] == "FAIL"


def test_report_not_run_when_history_short_and_fixture_class():
    rep = drift.report([_series(n=6 * 100)])
    assert rep["status"] == "NOT_RUN" and "needs" in rep["rows"][0]["detail"]
    assert drift.report(fixture_universe(DOC, 6 * 400))["class"] == "FIXTURE"
    assert drift.report([])["status"] == "NOT_RUN"


# ---------- calibration expiry ----------
def test_calibration_status_and_expiry():
    kw = {"ece_warn": RG["ece_warn"], "ece_fail": RG["ece_fail"]}
    today = date(2026, 10, 1)
    assert drift.calibration_status(None, today=today, **kw)["status"] == "NOT_MEASURED"
    rec = {"ece": 0.03, "measured_at": "2026-09-20"}
    c = drift.calibration_status(rec, today=today, **kw)
    assert c["status"] == "OK" and c["authoritative"] and c["expires"] == "2026-10-20"
    assert drift.calibration_status(rec, today=date(2026, 10, 21), **kw)["status"] == "EXPIRED"
    assert not drift.calibration_status(rec, today=date(2026, 10, 21), **kw)["authoritative"]
    assert drift.calibration_status(dict(rec, ece=0.06), today=today, **kw)["status"] == "WARN"
    f = drift.calibration_status(dict(rec, ece=0.08), today=today, **kw)
    assert f["status"] == "FAIL" and not f["authoritative"] and "ABSTAIN" in f["text"]


# ---------- FAIL -> ABSTAIN, learner halts ----------
def test_regime_abstains_on_drift_fail():
    s = fixture_universe(DOC, 6 * 800)[0]
    close = datetime.fromtimestamp(int(s.open_time[-1]) + 4 * 3600, tz=timezone.utc)
    ok = RegimeLayer(DOC, POL.hash).claim(s.instrument_id, s.c, len(s.c) - 1, close, now=close)
    bad = RegimeLayer(DOC, POL.hash, drift="FAIL").claim(s.instrument_id, s.c, len(s.c) - 1, close, now=close)
    assert "DRIFT" not in ok["payload"]["binding_reasons"]
    assert "DRIFT" in bad["payload"]["binding_reasons"] and bad["payload"]["m_regime"] == 0.0


def test_learner_halts_on_observed_fail_only():
    bad = drift.report([_series(ret_sd_recent=0.04)])
    assert drift.learner_halt(bad).startswith("drift FAIL on XBTUSD/")
    assert drift.learner_halt(dict(bad, **{"class": "FIXTURE"})) is None  # FIXTURE never halts anything real
    assert drift.learner_halt(drift.report([_series()])) is None
    assert drift.learner_halt(None, [{"model": "CCMRM", "status": "FAIL"}]) == "calibration FAIL on CCMRM"


def test_registry_refuses_while_halted():
    items = registry.load()
    new = dict(items[0], id="H-A-HALT-TEST", budget_debit=1)
    with pytest.raises(registry.LearnerHalted):
        registry.register(items, new, budget_per_year=40, halted="drift FAIL on XBTUSD/log_return_4h")
    assert len(registry.register(items, new, budget_per_year=40, halted=None)) == len(items) + 1


# ---------- screens ----------
def test_data_screen_shows_drift_board(session):
    d = P.data(session)["drift"]
    assert d["class"] == "FIXTURE" and d["status"] in ("OK", "WARN", "FAIL") and not d["halts_learner"]
    assert d["rows"] and all(isinstance(r["psi"], str) for r in d["rows"])  # formatted server-side


def test_bots_screen_shows_learning_without_apply(session):
    b = P.bots(session)
    lr = b["learning"]
    closed, _ = session.trades_visible()
    assert lr["episodes"]["count"] == len(closed) > 0 and lr["episodes"]["class"] == "FIXTURE"
    assert lr["episodes"]["training_set"] == 0 and lr["episodes"]["process_error_rate"] == "0.0%"
    assert lr["apply_control"] is False and b["apply_control"] is False
    assert all(p["applies"] is False for p in lr["proposals"])
    assert lr["proposals"][0]["verdict"] == "NO_EVIDENCE"
    cal = b["calibration"][0]
    assert cal["status"] == "NOT_MEASURED" and not cal["authoritative"] and "30 days" in cal["text"]


def test_learning_respects_playback_cursor(session):
    early = session.mask(session.now - timedelta(days=365))
    assert P.bots(early)["learning"]["episodes"]["count"] < P.bots(session)["learning"]["episodes"]["count"]


def test_ui_renders_learning_and_drift_panels():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "engine" / "ui" / "static" / "app.js").read_text()
    assert "Learner proposals vs live parameters" in js and "Drift monitors (PSI)" in js
    assert not any(w in js for w in ("Apply proposal", "applyProposal"))


def test_uncertified_series_is_fixture(session):
    s = replace(session.series[0], certified=False)
    assert drift.report([s])["class"] == "FIXTURE"
