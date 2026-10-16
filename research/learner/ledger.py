"""Learning ledger: one hash-chained record per day of what the learner saw and proposed (spec §10.2-§10.3a).

The learner never changes the engine. What it produces each day (a cost-model proposal, drift monitors, the regime
calibration, the episode store's state) is a proposal or a measurement. The ledger keeps that trail so its trajectory
can be read back later: when the proposed slippage started to diverge from the live one, when drift first warned,
when the calibration last passed. Every artefact behind a record is archived under snapshots/<date>/ and its content
hash is written into the record, so a record can be checked against the files it summarises.

Layout of a learning directory (deploy/README.md):

  ledger.jsonl                 append-only, hash-chained (engine.data.store.AppendOnlyLog)
  cost-proposal.json           latest proposal (applies: false), read by the BFF
  drift.json                   latest drift report
  calibration.json             latest regime calibration (ECE), read by the BFF; expires after 30 days
  episodes.jsonl               trade episodes (CANARY onward; empty in PAPER and SHADOW)
  snapshots/<YYYY-MM-DD>/      copies of the four JSON files above as they were that day
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.common.canonical import content_hash
from engine.data.store import AppendOnlyLog

ARTEFACTS = ("cost-proposal.json", "drift.json", "calibration.json")


def ledger(learning_dir: Path) -> AppendOnlyLog:
    return AppendOnlyLog(Path(learning_dir) / "ledger.jsonl")


def recorded_dates(learning_dir: Path) -> set[str]:
    p = Path(learning_dir) / "ledger.jsonl"
    return {r["date"] for r in AppendOnlyLog(p).records()} if p.exists() else set()


def calibration(series: Sequence[Any], policy: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Regime calibration on certified bars: N1 (walk-forward ECE) per pair; the record carries the worst pair."""
    import numpy as np

    from engine.regime.ccmrm import confirm, raw_states
    from research.harness.step6 import n1_calibration, walk_forward_probs
    rg = policy["regime"]
    per = {}
    for s in series:
        states = confirm(raw_states(np.asarray(s.c)), rg["confirm_bars"])
        P, Y, _C = walk_forward_probs(states, rg["H_days"])
        per[s.instrument_id] = None if len(Y) == 0 else float(n1_calibration(P, Y, rg["ece_warn"])["value"])
    vals = [v for v in per.values() if v is not None]
    certified = bool(series) and all(getattr(s, "certified", False) for s in series)
    return {"model": "CCMRM", "ece": max(vals) if vals else None, "per_instrument": per, "measured_at": now.isoformat(),
            "class": "OBSERVED" if certified else "FIXTURE", "method": "§9.3 step 6 N1, walk-forward one-bar-ahead"}


def summarise(*, day: str, at: datetime, policy_hash: str, code_commit: str | None, journal: Sequence[Mapping],
              journal_head: str | None, proposal: Mapping, drift_report: Mapping, halt: str | None,
              calib: Mapping, calib_status: Mapping, episodes: Sequence[Mapping], process_error_rate: float | None,
              archive: str, hashes: Mapping[str, str]) -> dict[str, Any]:
    """The ledger payload for one day. Plain numbers only, so a later reader needs nothing but this file."""
    fails = [f"{r['instrument_id']}/{r['feature']}" for r in drift_report.get("rows", []) if r.get("status") == "FAIL"]
    warns = [f"{r['instrument_id']}/{r['feature']}" for r in drift_report.get("rows", []) if r.get("status") == "WARN"]
    priced = sum(1 for r in journal for row in r.get("instruments", []) if row.get("cost_observed") is not None)
    return {
        "date": day, "at": at.isoformat(), "policy_hash": policy_hash, "code_commit": code_commit,
        "journal": {"cycles": len(journal), "head": journal_head, "rows_priced": priced,
                    "last_bar_close": journal[-1]["bar_close"] if journal else None},
        "cost": {"verdict": proposal.get("verdict"), "n": proposal.get("n"),
                 "live_slippage_q75": proposal.get("current", {}).get("slippage_q75"),
                 "proposed_slippage_q75": proposal.get("proposed", {}).get("slippage_q75"),
                 "loosens": proposal.get("loosens"), "applies": bool(proposal.get("applies", False))},
        "drift": {"status": drift_report.get("status"), "class": drift_report.get("class"), "fail": fails, "warn": warns},
        "calibration": {"ece": calib.get("ece"), "status": calib_status.get("status"), "expires": calib_status.get("expires"),
                        "class": calib.get("class")},
        "episodes": {"count": len(episodes), "training_set": sum(1 for e in episodes if e.get("class") == "OBSERVED"
                                                                 and not e.get("excluded_from_training")),
                     "process_error_rate": process_error_rate},
        "learner_budget": "halted: " + halt if halt else "open",
        "archive": archive, "artefact_hashes": dict(hashes),
    }


def archive(learning_dir: Path, day: str) -> tuple[str, dict[str, str]]:
    """Copy today's artefacts into snapshots/<day>/ and return (relative path, {file: content hash})."""
    root = Path(learning_dir)
    dest = root / "snapshots" / day
    dest.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in ARTEFACTS:
        src = root / name
        if src.exists():
            shutil.copy2(src, dest / name)
            hashes[name] = content_hash(json.loads(src.read_text()))
    return str(dest.relative_to(root)), hashes


def history(learning_dir: Path, limit: int | None = None) -> list[dict[str, Any]]:
    p = Path(learning_dir) / "ledger.jsonl"
    if not p.exists():
        return []
    recs = AppendOnlyLog(p).records()
    return recs[-limit:] if limit else recs
