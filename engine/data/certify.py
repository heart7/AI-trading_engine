"""Bar certification and dataset quality reports (spec §11.2).

A bar is certified when two sources agree within tolerance (or one source with no anomaly for a
single-venue instrument, flagged), there is no gap, and its timestamp sits on a UTC 4h boundary.
Certified bars carry a content hash. A dataset build without a quality report cannot be certified.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from engine.common.canonical import content_hash
from engine.data.bars import EPOCH, H4, Bar, aligned

DEFAULT_TOLERANCE = 0.002  # 20 bp close-to-close agreement between sources (ASSUMED, owner principal)
DISPERSION_ALERT_MULT = 3.0  # spec §11.1: dispersion flagged > 3x normal


@dataclass(frozen=True)
class CertifiedBar:
    instrument_id: str
    tf: str
    bar: Bar
    sources: tuple[str, ...]
    dispersion: float
    era: str
    single_source: bool
    dispersion_alert: bool
    fixture: bool
    content_hash: str

    @property
    def certified(self) -> bool:
        # FIXTURE data is never certified, whatever its internal consistency (conduct rule 0.2.2).
        return not self.fixture


@dataclass
class QualityReport:
    instrument_id: str
    tf: str
    expected_bars: int
    certified_bars: int
    gaps: list[tuple[str, str]] = field(default_factory=list)
    quarantined: list[tuple[str, str]] = field(default_factory=list)  # (open_time, reason)
    single_source_bars: int = 0
    dispersion_alerts: int = 0
    splices: list[str] = field(default_factory=list)
    fixture: bool = False

    @property
    def coverage(self) -> float:
        return self.certified_bars / self.expected_bars if self.expected_bars else 0.0

    def as_dict(self) -> dict:
        return {"instrument_id": self.instrument_id, "tf": self.tf, "expected_bars": self.expected_bars,
                "certified_bars": self.certified_bars, "coverage": round(self.coverage, 6), "gaps": self.gaps,
                "quarantined": self.quarantined, "single_source_bars": self.single_source_bars,
                "dispersion_alerts": self.dispersion_alerts, "splices": self.splices, "fixture": self.fixture}


@dataclass(frozen=True)
class Dataset:
    bars: tuple[CertifiedBar, ...]
    report: QualityReport | None

    @property
    def certified(self) -> bool:
        return self.report is not None and not self.report.fixture and all(b.certified for b in self.bars)


class UncertifiableDataset(Exception):
    pass


def require_quality_report(ds: Dataset) -> Dataset:
    if ds.report is None:
        raise UncertifiableDataset("dataset build without a quality report cannot be certified")
    return ds


def era_of(ts: datetime, eras: Sequence[tuple[str, datetime]]) -> str:
    """eras: sorted (label, start) pairs. Boundaries are ASSUMED (A-ERA-BOUNDARIES)."""
    label = "pre-era"
    for name, start in eras:
        if ts >= start:
            label = name
    return label


def certify(instrument_id: str, per_source: Mapping[str, Sequence[Bar]], *, start: datetime, end: datetime,
            eras: Sequence[tuple[str, datetime]] = (), single_venue: bool = False,
            tolerance: float = DEFAULT_TOLERANCE, fixture: bool = False) -> Dataset:
    """Certify 4h bars in [start, end). Primary source is the first key of `per_source`."""
    if not aligned(start):
        raise ValueError("start must be on a UTC 4h boundary")
    names = list(per_source)
    index = {n: {b.open_time: b for b in per_source[n]} for n in names}
    report = QualityReport(instrument_id, "4h", 0, 0, fixture=fixture)
    out: list[CertifiedBar] = []
    disp_hist: list[float] = []
    prev_sources: tuple[str, ...] | None = None
    t = start
    gap_start: datetime | None = None
    while t < end:
        report.expected_bars += 1
        have = {n: index[n][t] for n in names if t in index[n]}
        reason = None
        for n, b in list(have.items()):
            if not b.valid() or not aligned(b.open_time):
                report.quarantined.append((t.isoformat(), f"invalid bar from {n}"))
                have.pop(n)
        if not have:
            gap_start = gap_start or t
            t += H4
            continue
        if gap_start is not None:
            report.gaps.append((gap_start.isoformat(), t.isoformat()))
            gap_start = None
        primary = next(n for n in names if n in have)
        closes = [b.c for b in have.values()]
        disp = (max(closes) - min(closes)) / median(closes) if len(closes) > 1 else 0.0
        if len(have) == 1 and not single_venue:
            reason = "only one source for a multi-venue instrument"
        elif len(have) > 1 and disp > tolerance:
            reason = f"sources disagree: dispersion {disp:.4%} > {tolerance:.4%}"
        if reason:
            report.quarantined.append((t.isoformat(), reason))
            t += H4
            continue
        normal = median(disp_hist[-180:]) if len(disp_hist) >= 30 else None
        alert = normal is not None and normal > 0 and disp > DISPERSION_ALERT_MULT * normal
        disp_hist.append(disp)
        srcs = tuple(sorted(have))
        if prev_sources is not None and srcs != prev_sources:
            report.splices.append(f"{t.isoformat()}: {','.join(prev_sources)} -> {','.join(srcs)}")
        prev_sources = srcs
        b = have[primary]
        era = era_of(t, eras)
        h = content_hash({"instrument_id": instrument_id, "tf": "4h", "open_time": t.isoformat(),
                          "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "sources": list(srcs), "era": era})
        out.append(CertifiedBar(instrument_id, "4h", b, srcs, disp, era, len(have) == 1, alert, fixture, h))
        report.certified_bars += 1
        report.single_source_bars += len(have) == 1
        report.dispersion_alerts += alert
        t += H4
    if gap_start is not None:
        report.gaps.append((gap_start.isoformat(), end.isoformat()))
    return Dataset(tuple(out), report)


def freshness(last_close: datetime, now: datetime, ttl: timedelta = timedelta(minutes=5)) -> str:
    """FRESH if the bar that closed at the latest 4h boundary is certified, or that boundary is still within
    the certification TTL; otherwise STALE (spec §11.1: 5 min after bar close)."""
    boundary = now - (now - EPOCH) % H4
    if last_close >= boundary:
        return "FRESH"
    if last_close >= boundary - H4 and now - boundary <= ttl:
        return "FRESH"
    return "STALE"
