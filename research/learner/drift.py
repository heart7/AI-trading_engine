"""Calibration and drift monitors (spec §10.2 L3, §10.3a, §16 Data "drift monitors (PSI, calibration)").

- Drift is the population stability index of each input feature over the last 30 days against the 180 days before
  them, on ten quantile bins of the reference window (A-DRIFT-PSI). PSI < 0.10 is OK, < 0.25 WARN, otherwise FAIL.
- A calibration measurement expires 30 days after it was taken (A-CALIBRATION-EXPIRY). An expired or missing one is
  non-authoritative (§16 Bots & Models), and ECE at or above `regime.ece_fail` is FAIL.
- On FAIL the model's outputs render ABSTAIN (the regime layer adds binding reason DRIFT or N1) and the learner's
  budget halts: no new hypothesis can be registered until the monitor clears (§10.3a).
FIXTURE series give a FIXTURE report: it shows the monitor working and never clears or halts anything real.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from engine.replay.paper import Series

BPD = 6
REF_DAYS = 180  # ASSUMED (A-DRIFT-PSI)
CUR_DAYS = 30
BINS = 10
PSI_WARN = 0.10
PSI_FAIL = 0.25
CALIBRATION_TTL_DAYS = 30  # ASSUMED (A-CALIBRATION-EXPIRY)
_EPS = 1e-4
_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}


def psi(ref: Sequence[float], cur: Sequence[float], bins: int = BINS) -> float:
    r = np.asarray(ref, float)
    c = np.asarray(cur, float)
    r, c = r[np.isfinite(r)], c[np.isfinite(c)]
    if len(r) < bins or len(c) < bins:
        raise ValueError("too few observations for PSI")
    edges = np.unique(np.quantile(r, np.linspace(0, 1, bins + 1)[1:-1]))
    pr = np.bincount(np.searchsorted(edges, r, side="right"), minlength=len(edges) + 1) / len(r)
    pc = np.bincount(np.searchsorted(edges, c, side="right"), minlength=len(edges) + 1) / len(c)
    pr, pc = np.maximum(pr, _EPS), np.maximum(pc, _EPS)
    return float(np.sum((pc - pr) * np.log(pc / pr)))


def grade(value: float) -> str:
    return "OK" if value < PSI_WARN else ("WARN" if value < PSI_FAIL else "FAIL")


def features(s: Series) -> dict[str, np.ndarray]:
    """Model inputs whose distribution the models assume stable. Signal outputs such as T are not monitored: a trend
    signal moves between regimes by design, so its PSI would always read FAIL."""
    c = np.asarray(s.c, float)
    return {"log_return_4h": np.diff(np.log(c), prepend=np.nan),
            "range_4h": (np.asarray(s.h, float) - np.asarray(s.l, float)) / c}


def report(series: Iterable[Series], *, ref_days: int = REF_DAYS, cur_days: int = CUR_DAYS) -> dict[str, Any]:
    rows, fixture = [], False
    need = (ref_days + cur_days) * BPD
    for s in series:
        fixture = fixture or not s.certified
        if len(s.c) < need:
            rows.append({"instrument_id": s.instrument_id, "feature": "all", "psi": None, "status": "NOT_RUN",
                         "detail": f"{len(s.c)} bars; needs {need}"})
            continue
        for name, x in features(s).items():
            ref, cur = x[-need:-cur_days * BPD], x[-cur_days * BPD:]
            try:
                v = psi(ref, cur)
            except ValueError as e:
                rows.append({"instrument_id": s.instrument_id, "feature": name, "psi": None, "status": "NOT_RUN", "detail": str(e)})
                continue
            rows.append({"instrument_id": s.instrument_id, "feature": name, "psi": v, "status": grade(v), "detail": ""})
    graded = [r["status"] for r in rows if r["status"] in _RANK]
    status = max(graded, key=_RANK.__getitem__) if graded else "NOT_RUN"
    return {"status": status, "class": "FIXTURE" if fixture else ("OBSERVED" if rows else "NONE"), "rows": rows,
            "window": {"reference_days": ref_days, "current_days": cur_days},
            "thresholds": {"warn": PSI_WARN, "fail": PSI_FAIL}}


def calibration_status(record: Mapping[str, Any] | None, *, today: date, ece_warn: float, ece_fail: float) -> dict[str, Any]:
    if not record or record.get("ece") is None:
        return {"status": "NOT_MEASURED", "authoritative": False, "expires": None, "text": "not yet measured"}
    measured = datetime.fromisoformat(str(record["measured_at"])).date()
    expires = measured + timedelta(days=CALIBRATION_TTL_DAYS)
    ece = float(record["ece"])
    if ece >= ece_fail:
        st = "FAIL"
    elif today > expires:
        st = "EXPIRED"
    else:
        st = "WARN" if ece > ece_warn else "OK"
    text = {"FAIL": f"ECE {ece:.3f} ≥ {ece_fail}: outputs render ABSTAIN", "EXPIRED": f"expired {expires.isoformat()}",
            "WARN": f"ECE {ece:.3f} above {ece_warn}", "OK": f"ECE {ece:.3f}"}[st]
    return {"status": st, "authoritative": st in ("OK", "WARN"), "expires": expires.isoformat(), "ece": ece, "text": text}


def learner_halt(drift: Mapping[str, Any] | None, calibrations: Iterable[Mapping[str, Any]] = ()) -> str | None:
    """Why the learner's budget is halted, or None. FIXTURE drift never halts (nor clears) anything."""
    if drift and drift.get("class") == "OBSERVED" and drift.get("status") == "FAIL":
        worst = [f"{r['instrument_id']}/{r['feature']}" for r in drift["rows"] if r["status"] == "FAIL"]
        return f"drift FAIL on {', '.join(worst[:4])}"
    bad = [c.get("model", "model") for c in calibrations if c.get("status") == "FAIL"]
    return f"calibration FAIL on {', '.join(bad)}" if bad else None
