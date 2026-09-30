"""BFF routes, the UI shell and an optional headless-browser check of the Activity screen."""
import glob
import json
import re
from pathlib import Path

import pytest

from engine.bff import projections as P
from engine.common.schemas import validate
from tests.helpers.bff import Live, fresh_session, session

STATIC = Path(__file__).resolve().parents[2] / "engine" / "ui" / "static"
GETS = ["/v1/topbar", "/v1/fund-room", "/v1/activity/cycles?limit=6", "/v1/activity/pairs/BTC?bars=180", "/v1/activity/universe",
        "/v1/activity/funnel", "/v1/activity/timing?days=30", "/v1/activity/emissions?days=30", "/v1/markets/ETH?tf=1d",
        "/v1/strategy", "/v1/strategy/router", "/v1/probability/SOL", "/v1/pnl", "/v1/outcomes", "/v1/data", "/v1/bots",
        "/v1/risk", "/v1/incidents", "/v1/venues", "/v1/governance", "/v1/validation", "/v1/settings", "/v1/settings/exchanges"]


@pytest.fixture(scope="module")
def live():
    lv = Live(fresh_session())
    yield lv
    lv.close()


def test_every_projection_route_answers_json(live):
    for path in GETS:
        code, doc = live.req("GET", path)
        assert code == 200 and isinstance(doc, dict), path
    code, cyc = live.req("GET", "/v1/activity/cycles?limit=1")
    cid = cyc["cycles"][0]["cycle_id"].replace("+", "%2B")
    code, doc = live.req("GET", f"/v1/activity/cycles/{cid}")
    assert code == 200 and doc["events"]
    assert live.req("GET", "/v1/markets/DOGE")[0] == 404


def test_sse_stream_carries_the_current_cycle(live):
    code, body = live.req("GET", "/v1/activity/stream")
    assert code == 200 and body.count("event: activity") > 5 and body.rstrip().endswith("data: {}")


def test_reporter_route(live):
    code, a = live.req("POST", "/v1/reporter/ask", {"question": "what is running?"})
    assert code == 200 and a["class"] == "REPORTED"
    assert live.req("POST", "/v1/reporter/ask", {"nope": 1})[0] == 400


def test_static_shell_and_nav_has_fifteen_screens(live):
    code, html = live.req("GET", "/")
    assert code == 200 and "app.js" in html
    assert live.req("GET", "/../pyproject.toml")[0] == 404
    js = (STATIC / "app.js").read_text()
    nav = re.search(r"var NAV = \[(.*?)\];", js, re.S).group(1)
    assert len(re.findall(r'\["[a-z-]+", "', nav)) == 15


def test_events_validate_against_schema():
    s = session()
    for e in P.cycle_events(s, s.last_index()):
        validate("agent_activity_event", json.loads(json.dumps(e)))


def test_markets_daily_aggregation_is_server_side():
    d = P.markets(session(), "BTC", 360, "1d")
    assert len(d["bars"]["c"]) == 60 and d["timeframe"] == "1d"


def _chromium() -> str | None:
    c = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    return c[0] if c else None


@pytest.mark.skipif(_chromium() is None, reason="no local Chromium")
def test_browser_activity_screen_renders_fixture_hatched():
    pw = pytest.importorskip("playwright.sync_api")
    lv = Live(fresh_session())
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path=_chromium())
            pg = b.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(lv.base + "/#/activity")
            pg.wait_for_selector("#swim svg .mark", state="attached")
            pg.wait_for_selector("#evTable tbody tr", state="attached")
            marks = pg.eval_on_selector_all("#swim svg .mark", "els => els.length")
            rows = pg.eval_on_selector_all("#evTable tbody tr", "els => els.length")
            assert marks == rows > 0  # P1a contains every mark P1 draws
            unhatched = pg.eval_on_selector_all(
                "[data-fid]", "els => els.filter(e => !e.dataset.style.split(' ').includes('hatched')).map(e => e.dataset.fid)")
            assert unhatched == []  # every figure on this screen is FIXTURE
            shapes = pg.eval_on_selector_all("#swim svg .mark", "els => [...new Set(els.map(e => e.dataset.class + ':' + e.dataset.shape))]")
            assert len({s.split(':')[1] for s in shapes}) == len(shapes)
            dashed = pg.get_attribute('line[data-edge="regime>strategy_router"]', "stroke-dasharray")
            assert dashed
            assert errors == []
            b.close()
    finally:
        lv.close()
