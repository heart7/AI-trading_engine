"""Test configuration and the invariant tracker (spec §18, conduct rule 0.2.9).

Every control ships with its negative test. Tests that prove an invariant are marked
`@pytest.mark.invariant("INV-xx")`. After collection, each registry entry without a proving
test becomes an xfail (red, tracked); the terminal summary prints the full table and, when
UCHFE_INVARIANT_REPORT is set, writes it as JSON for the run record.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REGISTRY = yaml.safe_load((ROOT / "tests" / "negative" / "invariants.yaml").read_text())["invariants"]
_coverage: dict[str, list[str]] = defaultdict(list)
_outcomes: dict[str, list[str]] = defaultdict(list)


def pytest_configure(config):
    config.addinivalue_line("markers", "invariant(id): test proves the named §18 invariant")


def pytest_collection_modifyitems(config, items):
    known = {r["id"] for r in REGISTRY}
    for item in items:
        for m in item.iter_markers("invariant"):
            for inv in m.args:
                if inv not in known:
                    raise pytest.UsageError(f"{item.nodeid}: unknown invariant {inv}")
                _coverage[inv].append(item.nodeid)
    for item in items:
        if item.originalname == "test_invariant_registered":
            inv = item.callspec.params["inv"]["id"]
            if not _coverage.get(inv):
                item.add_marker(pytest.mark.xfail(reason=f"{inv} not yet implemented", strict=True, run=True))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when == "setup" and rep.outcome != "passed"):
        for m in item.iter_markers("invariant"):
            for inv in m.args:
                _outcomes[inv].append(rep.outcome)


def invariant_table() -> list[dict]:
    rows = []
    for r in REGISTRY:
        outs = _outcomes.get(r["id"], [])
        if not _coverage.get(r["id"]):
            status = "NOT_IMPLEMENTED"
        elif outs and all(o == "passed" for o in outs):
            status = "GREEN"
        elif not outs:
            status = "NOT_RUN"
        else:
            status = "RED"
        rows.append({"id": r["id"], "phase": r["phase"], "status": status, "tests": len(_coverage.get(r["id"], []))})
    return rows


def pytest_terminal_summary(terminalreporter):
    rows = invariant_table()
    if not _outcomes:
        return
    tr = terminalreporter
    tr.section("invariants (spec §18)")
    counts = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1
        tr.write_line(f"{row['id']}  {row['phase']:<5} {row['status']:<16} tests={row['tests']}")
    tr.write_line("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    out = os.environ.get("UCHFE_INVARIANT_REPORT")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps({"invariants": rows, "totals": counts}, indent=2))
