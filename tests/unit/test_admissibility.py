"""P10: admissibility service C1 (§5.1): universe, listings, delisting notice, blackout, venue, TTF, cost gate."""
from datetime import datetime, timedelta, timezone

import pytest

from engine.admissibility.service import REASONS, AdmissibilityService, Window, load_blackout
from engine.bff import projections as P
from engine.bff.session import FIXTURE_MU_Q, PaperSession, base_of, fixture_universe
from engine.common.schemas import validate
from engine.data.listings import Listing
from engine.policy.loader import load_policy, thaw
from engine.replay.paper import ReplayConfig, replay
from engine.router.router import StrategyRouter

POL = load_policy()
DOC = thaw(POL.doc)
T0 = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
UTC = timezone.utc


def lst(base, listed=datetime(2015, 1, 1, tzinfo=UTC), notice=None, delisted=None):
    return Listing(f"kraken-spot:{base}/USD", "kraken-spot", f"{base}/USD", "spot", base, "USD", listed, delisted, notice)


def svc(**kw):
    return AdmissibilityService(DOC, POL.hash, **kw)


def reason(claim):
    return claim["payload"]["binding_reason"]


def test_claim_validates_and_lists_unchecked_inputs():
    c = svc().evaluate("BTC", T0, T=0.7, now=T0)
    validate("admissibility_claim", c)
    assert c["payload"]["admissible"] and reason(c) is None
    assert "unchecked:NOT_LISTED" in c["inputs"] and "unchecked:VENUE_STALE" in c["inputs"] and "BLACKOUT" in c["inputs"]


def test_universe_is_policy_only():
    assert reason(svc().evaluate("DOGE", T0, T=0.9)) == "NOT_IN_UNIVERSE"
    assert svc().universe() == {"BTC", "XRP", "ETH", "SOL"}


def test_listing_checks_are_point_in_time():
    s = svc(listings={"SOL": lst("SOL", listed=datetime(2027, 1, 1, tzinfo=UTC))})
    assert reason(s.evaluate("SOL", T0, T=0.9)) == "NOT_LISTED"
    assert reason(svc(listings={}).evaluate("SOL", T0, T=0.9)) == "NOT_LISTED"
    s = svc(listings={"XRP": lst("XRP", notice=T0 - timedelta(days=1))})
    assert reason(s.evaluate("XRP", T0, T=0.9)) == "DELISTING_NOTICE"
    assert reason(s.evaluate("XRP", T0 - timedelta(days=2), T=0.9)) is None  # before the notice
    s = svc(listings={"XRP": lst("XRP", notice=T0 - timedelta(days=30), delisted=T0 - timedelta(days=1))})
    assert reason(s.evaluate("XRP", T0, T=0.9)) == "DELISTED"  # delisted outranks the notice


def test_blackout_windows():
    w = Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1), ("SOL",), "Kraken maintenance", "status page")
    s = svc(blackout=[w])
    assert reason(s.evaluate("SOL", T0, T=0.9)) == "BLACKOUT"
    assert reason(s.evaluate("BTC", T0, T=0.9)) is None
    assert reason(s.evaluate("SOL", T0 + timedelta(hours=1), T=0.9)) is None  # end is exclusive
    assert reason(svc(blackout=[Window(w.start, w.end, ("*",), "fork", "x")]).evaluate("BTC", T0, T=0.9)) == "BLACKOUT"


def test_venue_ttf_cost_and_history():
    assert reason(svc().evaluate("BTC", T0, T=0.9, venue_fresh=False)) == "VENUE_STALE"
    big = svc().evaluate("BTC", T0, T=0.9, positions_usd={}, add_usd=1e9, volume_usd_per_min={"BTC": 1e5})
    assert reason(big) == "TTF_EXCEEDED"
    small = svc().evaluate("BTC", T0, T=0.9, positions_usd={}, add_usd=1e3, volume_usd_per_min={"BTC": 1e5})
    assert reason(small) is None and "TTF_EXCEEDED" in small["inputs"]
    assert reason(svc().evaluate("BTC", T0, T=0.9, cost_gate="COST_R_EXCEEDED")) == "COST_GATE"
    assert reason(svc().evaluate("BTC", T0, T=0.9, cost_gate=None)) is None
    assert reason(svc().evaluate("BTC", T0, T=None)) == "INSUFFICIENT_HISTORY"


def test_first_failure_binds_in_spec_order():
    w = Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1), ("*",), "fork", "x")
    c = svc(blackout=[w], listings={"XRP": lst("XRP", notice=T0 - timedelta(days=1))}).evaluate(
        "XRP", T0, T=None, venue_fresh=False, cost_gate="COST_R_EXCEEDED")
    assert reason(c) == "DELISTING_NOTICE"
    assert REASONS.index("DELISTING_NOTICE") < REASONS.index("BLACKOUT") < REASONS.index("INSUFFICIENT_HISTORY")


def test_blackout_file_loads_and_rejects_bad_windows(tmp_path):
    assert load_blackout() == []  # shipped empty
    assert load_blackout(tmp_path / "none.yaml") == []
    f = tmp_path / "b.yaml"
    f.write_text('windows:\n  - {start: "2026-11-02T08:00:00", end: "2026-11-02T12:00:00", pairs: [sol], reason: m, source: s}\n')
    w = load_blackout(f)[0]
    assert w.pairs == ("SOL",) and w.start.tzinfo is not None
    f.write_text('windows:\n  - {start: "2026-11-02T12:00:00", end: "2026-11-02T08:00:00", pairs: [SOL], reason: m, source: s}\n')
    with pytest.raises(ValueError):
        load_blackout(f)


def test_replay_hook_blocks_entries_and_default_is_unchanged():
    series = fixture_universe(DOC, 3000)
    base = replay(series, DOC, StrategyRouter(DOC), ReplayConfig(mu_q_daily=FIXTURE_MU_Q))
    open_ = svc()
    same = replay(series, DOC, StrategyRouter(DOC),
                  ReplayConfig(mu_q_daily=FIXTURE_MU_Q, admission=lambda k, t: open_.gate(base_of(k), t)))
    assert same.result_hash == base.result_hash  # empty calendar, full universe: identical decisions
    allw = Window(datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC), ("*",), "test", "test")
    shut = svc(blackout=[allw])
    blocked = replay(series, DOC, StrategyRouter(DOC),
                     ReplayConfig(mu_q_daily=FIXTURE_MU_Q, admission=lambda k, t: shut.gate(base_of(k), t)))
    assert blocked.trades == [] and blocked.binding_counts.get("NOT_ADMISSIBLE", 0) > 0


def test_data_screen_admissibility_board():
    s = PaperSession.build()
    b = P.data(s)["admissibility"]
    assert [r["pair"] for r in b["rows"]] == ["BTC/USD", "XRP/USD", "ETH/USD", "SOL/USD"]
    assert all("NOT_LISTED" in r["unchecked"] for r in b["rows"]) and b["blackout"] == []
