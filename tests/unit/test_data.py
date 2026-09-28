from datetime import datetime, timedelta, timezone

import pytest

from engine.data.bars import H4, Bar, aligned, daily_from_4h
from engine.data.certify import Dataset, UncertifiableDataset, certify, freshness, require_quality_report
from engine.data.fixtures import fixture_bars, second_source
from engine.data.listings import ConstantUniverseRejected, Listing, ListingTable, require_point_in_time
from engine.data.sources import public_ohlc as src
from engine.data.store import AppendOnlyLog, ChainBroken

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def bars(n=60, seed=1):
    return fixture_bars("FIXTURE_X", T0, n, seed=seed)


def test_alignment_and_daily_aggregation():
    assert aligned(T0) and aligned(T0 + H4) and not aligned(T0 + timedelta(hours=1))
    assert not aligned(datetime(2024, 1, 1))  # naive timestamps are never aligned
    b = bars(12)
    d = daily_from_4h(b)
    assert len(d) == 2
    assert d[0].o == b[0].o and d[0].c == b[5].c and d[0].h == max(x.h for x in b[:6])
    assert len(daily_from_4h(b[:5] + b[6:])) == 1  # an incomplete day is dropped, never filled


def test_two_sources_certify_and_hash():
    p = bars()
    ds = certify("FIXTURE_X", {"a": p, "b": second_source(p, seed=2)}, start=T0, end=T0 + 60 * H4, fixture=True)
    assert ds.report.certified_bars == 60 and ds.report.coverage == 1.0
    assert all(len(cb.sources) == 2 and len(cb.content_hash) == 64 for cb in ds.bars)
    assert not ds.certified  # fixture data never counts as certified


def test_disagreement_single_source_and_gaps_are_reported():
    p = bars()
    q = second_source(p, seed=2)
    q[10] = Bar(q[10].open_time, q[10].o, q[10].h * 1.2, q[10].l, q[10].c * 1.05, q[10].v)  # 5% off
    del q[20]  # single source at bar 20
    ds = certify("X", {"a": p[:30] + p[35:], "b": q}, start=T0, end=T0 + 60 * H4)
    reasons = dict(ds.report.quarantined)
    assert "sources disagree" in reasons[(T0 + 10 * H4).isoformat()]
    assert "only one source" in reasons[(T0 + 20 * H4).isoformat()]
    assert (T0 + 30 * H4) not in {cb.bar.open_time for cb in ds.bars}


def test_single_venue_instrument_flagged_not_rejected():
    p = bars()
    ds = certify("X", {"a": p}, start=T0, end=T0 + 60 * H4, single_venue=True)
    assert ds.report.certified_bars == 60 and ds.report.single_source_bars == 60


def test_invalid_bar_quarantined_and_full_gap_recorded():
    p = bars()
    p[5] = Bar(p[5].open_time, p[5].o, p[5].l * 0.5, p[5].l, p[5].c, p[5].v)  # high < low
    ds = certify("X", {"a": p[:40]}, start=T0, end=T0 + 60 * H4, single_venue=True)
    assert any("invalid" in r for _, r in ds.report.quarantined)
    assert ds.report.gaps == [((T0 + 5 * H4).isoformat(), (T0 + 6 * H4).isoformat()),
                              ((T0 + 40 * H4).isoformat(), (T0 + 60 * H4).isoformat())]


def test_build_without_quality_report_cannot_be_certified():
    with pytest.raises(UncertifiableDataset):
        require_quality_report(Dataset(bars=(), report=None))


def test_freshness_ttl():
    close = T0 + 6 * H4
    assert freshness(close, close + timedelta(minutes=2)) == "FRESH"
    assert freshness(close - H4, close + timedelta(minutes=4)) == "FRESH"  # latest bar still inside TTL
    assert freshness(close - H4, close + timedelta(minutes=6)) == "STALE"


@pytest.mark.invariant("INV-21")
def test_backtest_with_constant_universe_rejected():
    with pytest.raises(ConstantUniverseRejected):
        require_point_in_time(["BTC", "ETH", "XRP", "SOL"])
    lt = ListingTable([
        Listing("kraken-spot:XBT/USD", "kraken-spot", "XBT/USD", "spot", "BTC", "USD", datetime(2013, 10, 1, tzinfo=timezone.utc)),
        Listing("kraken-spot:SOL/USD", "kraken-spot", "SOL/USD", "spot", "SOL", "USD", datetime(2021, 6, 1, tzinfo=timezone.utc)),
        Listing("kraken-spot:LUNA/USD", "kraken-spot", "LUNA/USD", "spot", "LUNA", "USD", datetime(2021, 1, 1, tzinfo=timezone.utc),
                delisted_at=datetime(2022, 6, 1, tzinfo=timezone.utc)),
    ])
    assert require_point_in_time(lt) is lt
    ids = lambda t: [x.base for x in lt.universe_at(t)]  # noqa: E731
    assert ids(datetime(2020, 1, 1, tzinfo=timezone.utc)) == ["BTC"]
    assert ids(datetime(2022, 1, 1, tzinfo=timezone.utc)) == ["LUNA", "SOL", "BTC"]
    assert ids(datetime(2023, 1, 1, tzinfo=timezone.utc)) == ["SOL", "BTC"]
    assert lt.history_days_at("kraken-spot:SOL/USD", datetime(2021, 11, 28, tzinfo=timezone.utc)) == 180


def test_append_only_log_detects_edit(tmp_path):
    log = AppendOnlyLog(tmp_path / "x.jsonl")
    for i in range(5):
        log.append({"i": i})
    assert log.verify() == 5 and AppendOnlyLog(tmp_path / "x.jsonl").head == log.head
    lines = (tmp_path / "x.jsonl").read_text().splitlines()
    lines[2] = lines[2].replace('"i":2', '"i":7')
    (tmp_path / "x.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        log.verify()


NOW = T0 + 3 * H4 + timedelta(hours=1)


def test_parse_kraken_drops_open_bar():
    ts = [int((T0 + i * H4).timestamp()) for i in range(4)]
    payload = {"error": [], "result": {"XXBTZUSD": [[t, "100.0", "110.0", "95.0", "105.0", "103.1", "12.5", 40] for t in ts],
                                       "last": ts[-1]}}
    out = src.parse_kraken(payload, NOW)
    assert len(out) == 3 and out[0].c == 105.0 and out[0].v == 12.5


def test_parse_binance_and_bybit_ordering():
    ms = [int((T0 + i * H4).timestamp() * 1000) for i in range(3)]
    bn = [[m, "1", "2", "0.5", "1.5", "10", m + 14399999, "15", 5, "5", "7", "0"] for m in ms]
    assert [b.open_time for b in src.parse_binance(bn, NOW)] == [T0, T0 + H4, T0 + 2 * H4]
    by = {"retCode": 0, "result": {"list": [[str(m), "1", "2", "0.5", "1.5", "10", "15"] for m in reversed(ms)]}}
    assert [b.open_time for b in src.parse_bybit(by, NOW)] == [T0, T0 + H4, T0 + 2 * H4]
    with pytest.raises(RuntimeError):
        src.parse_bybit({"retCode": 10001, "retMsg": "bad"}, NOW)


def test_fetch_binance_pages_with_injected_fetcher():
    def fake(url):
        start = int(url.split("startTime=")[1].split("&")[0])
        return [[start + i * 14400000, "1", "2", "0.5", "1.5", "1", 0, "0", 0, "0", "0", "0"] for i in range(2)]
    out = src.fetch_binance("BTCUSDT", T0, T0 + 5 * H4, fetch=fake)
    assert [b.open_time for b in out] == [T0 + i * H4 for i in range(5)]


def test_quote_conversion_never_assumes_par():
    b = bars(3)
    out = src.convert_quote(b, {b[0].open_time: 0.998, b[2].open_time: 1.001})
    assert len(out) == 2 and out[0].c == pytest.approx(b[0].c * 0.998)
