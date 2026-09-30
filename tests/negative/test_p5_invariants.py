"""P5 rendering negative tests (spec §16.6, §16.8.3, INV-32). Each class-bound rendering rule has a test here."""
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from engine.bff import activity as act
from engine.bff import projections as P
from engine.bff import reporter
from engine.bff.server import ROUTES
from engine.ui.render import RenderViolation, audit, figure, render, walk_figures
from tests.helpers.bff import Live, fresh_session, session

STATIC = Path(__file__).resolve().parents[2] / "engine" / "ui" / "static"
ALL = ["fund-room", "strategy", "strategy/router", "pnl", "outcomes", "data", "bots", "risk", "incidents", "venues",
       "governance", "validation", "settings/exchanges", "settings"]


def _every_projection(s):
    for name in ALL:
        yield name, P.SCREENS[name](s)
    cyc = P.activity_cycles(s)
    yield "activity/cycles", cyc
    yield "activity/cycle", P.activity_cycle(s, cyc["cycles"][0]["cycle_id"])
    for p in ("BTC", "XRP", "ETH", "SOL"):
        yield f"activity/pair/{p}", P.activity_pair(s, p)
        yield f"probability/{p}", P.probability(s, p)
        yield f"markets/{p}", P.markets(s, p)
    yield "activity/universe", P.activity_universe(s)
    yield "activity/funnel", P.funnel(s)


# ---------------------------------------------------------------- INV-32 FIXTURE hatched
@pytest.mark.invariant("INV-32")
def test_fixture_in_observed_style_fails():
    f = render(figure("nav", "NAV", 30_000.0, "OBSERVED", unit="USD", fixture=True))
    assert "hatched" in f["render"]["style"] and f["certified"] is False
    bad = {**f, "render": {**f["render"], "style": ["solid"], "chips": []}}
    with pytest.raises(RenderViolation, match="FIXTURE_NOT_HATCHED"):
        audit(bad)


@pytest.mark.invariant("INV-32")
def test_every_fixture_figure_in_every_projection_is_hatched_and_labelled():
    s = session()
    n = 0
    for _name, doc in _every_projection(s):
        for f in walk_figures(doc):
            audit(f)  # raises on any §16.6 violation
            if f["fixture"]:
                assert "hatched" in f["render"]["style"] and f["certified"] is False
                assert any(c["kind"] == "FIXTURE" for c in f["render"]["chips"])
                n += 1
    assert n > 100


# ---------------------------------------------------------------- §16.6 rows
def test_estimate_never_renders_as_a_bare_point():
    with pytest.raises(RenderViolation, match="ESTIMATE_WITHOUT_BAND"):
        render(figure("T", "T", 0.6, "ESTIMATED"))
    f = render(figure("T", "T", 0.6, "ESTIMATED", interval=(0.5, 0.7)))
    with pytest.raises(RenderViolation, match="ESTIMATE_WITHOUT_BAND"):
        audit({**f, "render": {**f["render"], "band": None}})


def test_trend_band_cannot_become_a_point_line():
    d = P.activity_pair(session(), "BTC")
    T = d["T"]
    assert T["class"] == "ESTIMATED"
    for t, lo, hi in zip(T["T"], T["lo"], T["hi"], strict=True):
        if t is not None:
            assert lo is not None and hi is not None and lo <= t <= hi


def test_assumed_needs_owner_and_review_date():
    with pytest.raises(RenderViolation, match="ASSUMED_WITHOUT_OWNER"):
        render(figure("a", "fee", "0.25%", "ASSUMED"))
    f = render(figure("a", "fee", "0.25%", "ASSUMED", owner="principal", review_by="2026-12-31"))
    assert any(c["kind"] == "ASSUMED" and "principal" in c["text"] for c in f["render"]["chips"])


def test_reported_is_hatched_muted_and_never_exportable():
    f = render(figure("r", "reporter", "text", "REPORTED", fixture=False))
    assert f["render"]["style"][0] == "hatched-muted" and not f["render"]["exportable_as_evidence"]
    with pytest.raises(RenderViolation, match="REPORTED_EXPORTABLE"):
        audit({**f, "render": {**f["render"], "exportable_as_evidence": True}})


def test_derived_carries_lineage_glyph_and_hypothesis_is_dashed():
    assert render(figure("d", "x", 1.0, "DERIVED"))["render"]["lineage_glyph"]
    assert render(figure("h", "x", 1.0, "HYPOTHESIS"))["render"]["style"][0] == "outlined-dashed"
    f = render(figure("o", "x", 1.0, "OBSERVED"))
    with pytest.raises(RenderViolation, match="CLASS_STYLE_MISMATCH"):
        audit({**f, "class": "ESTIMATED", "interval": [0, 2], "render": {**f["render"], "band": {"lo": 0, "hi": 2}}})


def test_stale_greys_and_cannot_be_hidden():
    s = session()
    at = s.now - timedelta(hours=6)
    f = render(figure("px", "Close", 1.0, "OBSERVED", observed_at=at, now=s.now, ttl_s=3600))
    assert f["state"] == "STALE" and "stale" in f["render"]["style"] and not f["render"]["hideable"]
    assert any(c["kind"] == "STALE" and at.isoformat() in c["text"] for c in f["render"]["chips"])
    with pytest.raises(RenderViolation, match="STALE_HIDDEN"):
        render(figure("px", "Close", 1.0, "OBSERVED", observed_at=at, now=s.now, ttl_s=3600), hide=True)


def test_abstain_needs_its_binding_reason():
    with pytest.raises(RenderViolation, match="ABSTAIN_WITHOUT_REASON"):
        render(figure("x", "intent", "REJECTED", "DERIVED", state="ABSTAIN"))


def test_sample_size_warning_is_non_dismissible():
    f = render(figure("p", "win rate", 0.4, "ESTIMATED", interval=(0.3, 0.5), n_eff=12))
    w = f["render"]["warnings"]
    assert w and w[0]["kind"] == "SAMPLE_SIZE" and w[0]["dismissible"] is False
    with pytest.raises(RenderViolation, match="SAMPLE_SIZE_WARNING_MISSING"):
        audit({**f, "render": {**f["render"], "warnings": []}})


def test_n_eff_after_deflation_is_shown_and_not_the_raw_count():
    e = P.strategy(session())["expectancy"]
    assert e["p"]["n_eff"] < e["p"]["n_raw"]


def test_m_regime_above_one_cannot_render():
    with pytest.raises(RenderViolation, match="M_REGIME_ABOVE_ONE"):
        render(figure("m", "m_regime", 1.2, "DERIVED", kind="m_regime"))


def test_probability_without_counts_decayed_throws():
    with pytest.raises(RenderViolation, match="PROBABILITY_WITHOUT_COUNTS"):
        render(figure("p", "P(U)", 0.5, "ESTIMATED", interval=(0.4, 0.6), kind="probability"))
    d = P.probability(session(), "BTC")
    assert all(f["counts_decayed"] for f in d["next"])


def test_live_sharpe_cannot_render_as_evidence():
    with pytest.raises(RenderViolation, match="LIVE_SHARPE_AS_EVIDENCE"):
        render(figure("sr", "Sharpe", 1.4, "DERIVED", kind="evidence_sharpe", extra={"source_kind": "live"}))


def test_pass_needs_run_id_and_positive_step2_bound():
    with pytest.raises(RenderViolation, match="PASS_WITHOUT_RUN_ID"):
        render(figure("v", "Step 1", "PASS", "DERIVED", kind="verdict", extra={"step": 1, "run_id": None, "ci": [None, None]}))
    with pytest.raises(RenderViolation, match="PASS_ON_NONPOSITIVE_CI"):
        render(figure("v", "Step 2", "PASS", "DERIVED", kind="verdict", extra={"step": 2, "run_id": "bt-1", "ci": [-0.31, -0.09]}))


def test_validation_screen_without_run_records_shows_not_run():
    s = replace(session(), harness=None)
    steps = P.validation(s)["sleeves"][0]["steps"]
    assert {r["verdict"]["value"] for r in steps} == {"NOT_RUN"}
    assert not P.validation(s)["promotion"]["enabled"]


def test_validation_screen_rejects_a_fake_step2_pass():
    fake = {"verdicts": [{"step": 2, "verdict": "PASS", "run_id": "bt-s2", "metric": "Sharpe", "value": 0.2, "ci": [-0.31, None]}]}
    steps = P.validation(replace(session(), harness=fake))["sleeves"][0]["steps"]
    assert steps[1]["verdict"]["value"] == "FAIL"


def test_conflicted_state_renders_its_own_style():
    f = render(figure("c", "NAV", 1.0, "OBSERVED", fixture=False, state="CONFLICTED"))
    assert "conflicted" in f["render"]["style"]


# ---------------------------------------------------------------- §16.8.3 Activity acceptance tests
def _cycles(s, n=180):
    i = s.last_index()
    return range(i - n + 1, i + 1)


def test_completeness_one_event_per_record():
    s = session()
    for j in _cycles(s):
        evs = P.cycle_events(s, j)
        close = s.bar_close(j).isoformat()
        intents = [x for x in s.result.intents_tail if x["bar_close"] == close]
        by_intent = [e for e in evs if e["ref_kind"] == "intent"]
        assert len(by_intent) == len(intents)  # every signal_intent, all outcomes
        assert {e["instrument_id"] for e in by_intent} == {x["instrument_id"] for x in intents}
        refs = [(e["ref_kind"], e["ref_id"]) for e in evs]
        assert len(refs) == len(set(refs))  # exactly one event per record
        claims = [e for e in evs if e["subject"] in ("signal_claim", "regime_claim")]
        assert len(claims) == 2 * len(s.series)
        entered = [x for x in intents if x["outcome"] == "ENTERED"]
        placed = [e for e in evs if e["subject"] == "order_placed"]
        assert len(placed) == len(entered)
    cyc = P.activity_cycles(s)["cycles"][0]
    assert P.activity_cycle(s, cyc["cycle_id"])["events"].__len__() == cyc["events"]  # swimlane and log render one list


def test_deadline_miss_renders_at_true_time_with_cycle_timeout():
    s = fresh_session()
    i = s.last_index()
    s.extra_activity.append({"cycle_index": i, "agent": "data_sentinel", "cls": "INFORM", "subject": "bar_accepted",
                             "t_ms": 75_000, "step": "certified", "ref_id": "bc-late"})
    d = P.activity_cycle(s, s.bar_close(i).isoformat())
    late = [e for e in d["events"] if e["ref_id"] == "bc-late"][0]
    assert late["late"] and late["reason_code"] == "CYCLE_TIMEOUT" and late["timeout_marker"]
    assert late["t_offset_ms"] == 75_000 and late["t_s"] == 75.0  # never clamped to the 60 s line
    assert P.activity_cycles(s, 1)["cycles"][0]["late"]
    js = (STATIC / "activity.js").read_text()
    assert "CYCLE_TIMEOUT" in js and "never clamped" in js


def test_each_emission_class_has_its_own_shape():
    d = P.activity_cycle(session(), P.activity_cycles(session())["cycles"][0]["cycle_id"])
    shapes = {e["emission_class"]: e["shape"] for e in d["events"]}
    assert len(set(shapes.values())) == len(shapes)
    js = (STATIC / "activity.js").read_text()
    for cls, shape in [("INFORM", "circle"), ("RECOMMEND", "path"), ("PROPOSE", "rect"), ("EXECUTE", "path")]:
        assert re.search(rf'case "{cls}": s = \'<{shape}', js)
    assert 'fill="var(--panel)" stroke="var(--c-abst)"' in js  # ABSTAIN is hollow, distinguishable without colour


def test_abstain_always_carries_reason_and_cannot_be_filtered_out():
    s = session()
    n = 0
    for j in _cycles(s, 30):
        for e in P.cycle_events(s, j):
            if e["emission_class"] == "ABSTAIN":
                assert e["reason_code"]
                n += 1
    assert n > 0
    js = (STATIC / "activity.js").read_text()
    assert 'if (e.emission_class === "ABSTAIN") return true;' in js
    em = P.emissions_projection(s)
    assert all("ABSTAIN" in a["counts"] for a in em["agents"])


def test_regime_at_t0_is_computed_not_applied_and_its_edge_is_dashed():
    s = session()
    assert s.policy["regime"]["authority"] == "T0"
    d = P.activity_cycle(s, P.activity_cycles(s)["cycles"][0]["cycle_id"])
    edge = next(e for e in d["edges"] if e["from"] == "regime" and e["to"] == "strategy_router")
    assert edge["dashed"] and not edge["authorised"] and edge["label"] == "computed, not applied"
    assert "computed, not applied" in next(L for L in d["lanes"] if L["id"] == "regime")["tier_label"]
    m = next(f for f in P.pair_rail(s, "FIXTURE_BTC")["figures"] if f["id"].startswith("m-"))
    assert "computed, not applied" in m["render"]["display"]


def test_inactive_agent_greyed_and_silent():
    s = session()
    lanes = act.lanes(s.policy, P.active_tier(s))
    alloc = next(L for L in lanes if L["id"] == "allocator")
    assert not alloc["active"] and alloc["tier_label"] == "inactive at T2"
    row = next(a for a in P.emissions_projection(s)["agents"] if a["agent"] == "allocator")
    assert row["total"] == 0


def test_trailing_stop_line_monotone_in_the_positions_favour():
    s = session()
    for p in ("BTC", "XRP", "ETH", "SOL"):
        for seg in P.activity_pair(s, p, 2000)["stops"]:
            v = [x for _, x in seg["points"]]
            assert all(b >= a for a, b in zip(v, v[1:], strict=False))
    with pytest.raises(RenderViolation, match="STOP_MOVED_AGAINST_POSITION"):
        render(figure("s", "stop", [100.0, 101.0, 99.0], "DERIVED", kind="stop_series", extra={"side": 1}))


def test_funnel_non_increasing_and_losses_sum():
    f = P.funnel(session())
    c = [r["count"] for r in f["steps"]]
    assert all(a >= b for a, b in zip(c, c[1:], strict=False))
    assert sum(r["lost"] for r in f["steps"]) == c[0] - c[-1]


def test_playback_cannot_fetch_post_cursor_data():
    s = session()
    cursor = s.bar_close(s.last_index() - 40) + timedelta(seconds=30)
    m = s.mask(cursor)
    d = P.activity_pair(m, "BTC")
    cut = cursor.isoformat()
    assert max(d["bars"]["time"]) <= cut
    assert all(mk["time"] <= cut for mk in d["markers"])
    assert all(pt[0] <= cut for seg in d["stops"] for pt in seg["points"])
    assert all(x["bar_close"] <= cut for x in m.intents_visible())
    cyc = P.activity_cycles(m, 1)["cycles"][0]
    assert all(e["created_at"] <= cut for e in P.activity_cycle(m, cyc["cycle_id"])["events"])
    with pytest.raises(KeyError):
        P.activity_cycle(m, s.bar_close(s.last_index()).isoformat())  # a future cycle does not exist in playback
    live = Live(s)
    try:
        code, doc = live.req("GET", f"/v1/activity/pairs/BTC?cursor={cut.replace('+', '%2B')}")
        assert code == 200 and max(doc["bars"]["time"]) <= cut
        code, doc = live.req("GET", f"/v1/outcomes/playback?pair=ETH&at={cut.replace('+', '%2B')}")
        assert code == 200 and max(doc["pair"]["bars"]["time"]) <= cut
    finally:
        live.close()


def test_cycle_replay_cursor_masks_events_server_side():
    s = session()
    cid = P.activity_cycles(s)["cycles"][0]["cycle_id"]
    d = P.activity_cycle(s, cid, cursor_ms=70_000)
    assert d["events"] and all(e["t_offset_ms"] <= 70_000 for e in d["events"])


def test_killing_the_stream_greys_p1_and_p2a_with_last_good_time():
    s = fresh_session()
    s.kill_stream()
    cid = P.activity_cycles(s)["cycles"][0]["cycle_id"]
    for st in (P.activity_cycle(s, cid)["stream"], P.pair_rail(s, "FIXTURE_BTC")["stream"]):
        assert st["state"] == "STALE" and "stale" in st["render"]["style"] and st["last_good"]
    assert P.topbar(s)["stale"] == 1
    live = Live(s)
    try:
        code, doc = live.req("GET", "/v1/activity/stream")
        assert code == 503 and doc["last_good"]
    finally:
        live.close()
    js = (STATIC / "activity.js").read_text()
    assert 'classList.toggle("stale"' in js


def test_no_mutating_routes():
    assert {m for m, _, _ in ROUTES} == {"GET", "POST"}
    assert [p for m, p, _ in ROUTES if m == "POST"] == [r"/v1/reporter/ask"]
    live = Live(fresh_session())
    try:
        for m, path in [("POST", "/v1/risk"), ("PUT", "/v1/venues"), ("DELETE", "/v1/governance"), ("PATCH", "/v1/strategy"),
                        ("POST", "/v1/orders")]:
            assert live.req(m, path, {})[0] == 405
    finally:
        live.close()
    js = "".join(p.read_text() for p in STATIC.glob("*.js"))
    methods = re.findall(r'method:\s*"(\w+)"', js)
    assert set(methods) == {"GET", "POST"}
    assert js.count('method: "POST"') == 1 and '"/v1/reporter/ask", {method: "POST"' in js
    assert "WebSocket" not in js and "XMLHttpRequest" not in js


def test_ui_formats_no_figures():
    """The client prints the BFF's display strings; it never formats or computes a material figure (§15.3)."""
    js = "".join(p.read_text() for p in STATIC.glob("*.js"))
    for banned in ("toFixed(", "toLocaleString(", "toPrecision(", "Intl.NumberFormat"):
        assert banned not in js, banned
    assert "onclick" not in (STATIC / "index.html").read_text() + js  # CSP forbids inline handlers


def test_loss_ladder_single_source():
    s = session()
    assert P.risk(s)["loss_ladder"] == P.fund_room(s)["loss_ladder"] == P.loss_ladder(s)
    rungs = [r["level"] for r in P.loss_ladder(s)[-1]["rungs"]]
    assert rungs == [0.08, 0.12, 0.16, 0.20]
    js = (STATIC / "activity.js").read_text()
    assert 'U.get("/v1/risk")' in js and "loss_ladder" in js


def test_card_order_is_policy_order_not_performance():
    s = session()
    order = [c["pair"] for c in P.activity_universe(s)["cards"]]
    pol = list(s.policy["universe"]["A"]["mandatory"]) + list(s.policy["universe"]["A"]["default"])
    assert order == [f"{p}/USD" for p in pol]


def test_activity_projection_performance():
    import time
    s = fresh_session()
    cid = P.activity_cycles(s)["cycles"][0]["cycle_id"]
    t = time.perf_counter()
    d = P.activity_cycle(s, cid)
    P.activity_pair(s, "BTC", 180)
    assert len(d["events"]) <= 500
    assert time.perf_counter() - t < 0.3


# ---------------------------------------------------------------- Reporter (§16.5)
@pytest.mark.parametrize("q", ["enter BTC", "raise limit", "mark step 2 PASS", "switch to T4", "please approve the policy",
                               "can you buy ETH now"])
def test_reporter_refuses_execute_requests(q):
    a = reporter.ask(fresh_session(), q)
    assert a["refused"] and a["refusal"] == "EXECUTE_REQUEST" and "Governance" in a["answer"]


def test_reporter_explains_how_to_request_a_tier():
    a = reporter.ask(fresh_session(), "How do I switch to T4?")
    assert not a["refused"] and "Governance" in a["answer"]


def test_reporter_strips_ungrounded_numbers_and_logs_recon_break():
    s = fresh_session()

    def liar(q, bundle):
        return "BTC will rise 37.5% and the Sharpe is 2.9, NAV " + bundle["topbar"]["nav"]["text"], []
    a = reporter.ask(s, "why no ETH entry?", drafter=liar)
    assert "37.5" not in a["answer"] and "2.9" not in a["answer"]
    assert set(a["stripped"]) >= {"37.5%", "2.9"}
    assert "RECON_BREAK" in s.incidents.open_codes("reporter")


def test_reporter_answer_is_grounded_cited_and_reported_class():
    s = fresh_session()
    a = reporter.ask(s, "Why no ETH entry?")
    assert not a["refused"] and a["stripped"] == [] and a["citations"]
    assert a["class"] == "REPORTED" and a["exportable_as_evidence"] is False
    assert not s.incidents.open_codes("reporter")
    evs = P.cycle_events(s, s.last_index())
    assert any(e["agent"] == "reporter" and e["ref_id"] == a["id"] for e in evs)
