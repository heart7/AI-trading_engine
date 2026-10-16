"""Hosted engine (decision 0010): the BFF over a live-id certified store, and the daily learning ledger."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.bff import projections as P
from engine.bff import reporter
from engine.bff.server import ROUTES, _default
from engine.bff.session import PaperSession, pair_name
from engine.data.store import AppendOnlyLog
from tests.helpers.fixtures import arrays

ROOT = Path(__file__).resolve().parents[2]
BASES = ["BTC", "XRP", "ETH", "SOL"]


def _store(d: Path, n: int = 2600) -> Path:
    """A certified store shaped like tools/build_dataset.py --live output, with live instrument ids."""
    for j, b in enumerate(BASES):
        a = arrays(f"FIXTURE_{b}", n, 100 + j, 0.03 + 0.01 * j)
        log = AppendOnlyLog(d / f"{b}-USD.bars.jsonl")
        for k in range(n):
            log.append({"instrument_id": f"kraken-spot:{b}/USD",
                        "open_time": datetime.fromtimestamp(int(a["open_time"][k]), tz=timezone.utc).isoformat(),
                        "o": float(a["o"][k]), "h": float(a["h"][k]), "l": float(a["l"][k]), "c": float(a["c"][k]),
                        "v": 1.0, "certified": True})
    return d


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return _store(tmp_path_factory.mktemp("history"))


@pytest.fixture(scope="module")
def live_session(store, tmp_path_factory):
    return PaperSession.build(history=store, learning=tmp_path_factory.mktemp("learning"))


def test_live_session_uses_real_instrument_ids(live_session):
    s = live_session
    assert not s.fixture
    assert [x.instrument_id for x in s.series] == [f"kraken-spot:{b}/USD" for b in BASES]
    assert all(x.certified for x in s.series)
    assert int(s.series[0].open_time[0]) % 86400 == 0
    assert pair_name("kraken-spot:SOL/USD") == "SOL/USD" and pair_name("FIXTURE_SOL") == "SOL/USD"


def test_every_route_answers_on_live_ids(live_session):
    """Main used to hard-code FIXTURE_ ids in activity_universe and the reporter; live ids raised KeyError."""
    s = live_session
    for meth, pat, h in ROUTES:
        if meth == "GET" and "(?P" not in pat:
            json.dumps(h(s, {}, {}), default=_default)
    cards = P.activity_universe(s)["cards"]
    assert [c["pair"] for c in cards] == [f"{b}/USD" for b in ["BTC", "XRP", "ETH", "SOL"]]
    for b in BASES:
        json.dumps(P.activity_pair(s, b, 180), default=_default)
        json.dumps(P.probability(s, b), default=_default)
        ans = reporter.ask(s, f"Why no {b} entry?")
        assert ans  # the reporter retrieves the pair rail by base name


def test_live_figures_are_not_marked_fixture(live_session):
    fig = P.F(live_session, "x", "x", 1.0, "OBSERVED")
    assert fig["fixture"] is False and fig["certified"] is True


def _snapshot(store: Path, out: Path, journal: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "tools" / "learn.py"), "snapshot", "--history", str(store),
                           "--journal", str(journal), "--out", str(out), *extra], capture_output=True, text=True, cwd=out.parent)


def test_learning_snapshot_appends_one_chained_record_per_day(store, tmp_path):
    out, journal = tmp_path / "learning", tmp_path / "journal.jsonl"
    r1 = _snapshot(store, out, journal)
    assert r1.returncode == 0, r1.stderr
    r2 = _snapshot(store, out, journal)
    assert "already in" in r2.stdout
    log = AppendOnlyLog(out / "ledger.jsonl")
    assert log.verify() == 1
    rec = log.records()[0]
    assert rec["cost"]["applies"] is False  # proposals never apply themselves
    assert rec["calibration"]["ece"] is not None and rec["calibration"]["class"] == "OBSERVED"
    snap = out / rec["archive"]
    for name, h in rec["artefact_hashes"].items():
        from engine.common.canonical import content_hash
        assert content_hash(json.loads((snap / name).read_text())) == h
    r3 = _snapshot(store, out, journal, "--force")
    assert r3.returncode == 0 and AppendOnlyLog(out / "ledger.jsonl").verify() == 2


def test_bff_reads_the_ledger_and_calibration(store, tmp_path):
    out = tmp_path / "learning"
    assert _snapshot(store, out, tmp_path / "journal.jsonl").returncode == 0
    s = PaperSession.build(history=store, learning=out)
    hist = P.learning_history(s)
    assert hist["status"].startswith("1 days") and len(hist["days"]) == 1 and hist["applies"] is False
    assert P.bots(s)["calibration"][0]["status"] in ("OK", "WARN", "FAIL")  # measured, no longer NOT_MEASURED
    assert P.bots(s)["learning"]["history"] == hist["days"]


def test_fixture_session_ignores_a_live_calibration(store, tmp_path):
    out = tmp_path / "learning"
    assert _snapshot(store, out, tmp_path / "journal.jsonl").returncode == 0
    s = PaperSession.build(bars=1500, learning=out)
    assert P.bots(s)["calibration"][0]["status"] == "NOT_MEASURED"


def test_deploy_units_are_paper_only_and_local():
    units = {p.name: p.read_text() for p in (ROOT / "deploy" / "systemd").iterdir()}
    ui = units["uchfe-ui.service"]
    assert "--host 127.0.0.1" in ui and "ReadOnlyPaths=/var/lib/uchfe" in ui
    text = "\n".join(units.values()) + (ROOT / "deploy" / "install.sh").read_text()
    for banned in ("API_KEY", "SECRET", "0.0.0.0", "openvpn", "wireguard", "wg-quick", "socks5", "https_proxy"):
        assert banned.lower() not in text.lower(), banned
