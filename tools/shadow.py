#!/usr/bin/env python3
"""SHADOW mode operator tool (spec §9.8, §19 P6). Live data, no orders, no keys.

  drill    FIXTURE drill for CI: run shadow cycles over fixture bars, verify the journal chain, recon and incidents
  run      one shadow cycle on live public data (run it from cron a few minutes after each 4h close, 00:05, 04:05 ...)
  status   summarise a journal per rung and show what gate G2 would read from it
  step6    run §9.3 step 6 (regime N1-N4) on a journal's instruments

`run` reads the certified store built by `tools/build_dataset.py --live` and tops it up from Kraken, Binance and
Bybit public endpoints. It needs exchange network access, so run it on your own machine:

  python3 tools/build_dataset.py --live --pair BTC --pair XRP --pair ETH --pair SOL --out data/live
  python3 tools/shadow.py run --history data/live --journal runs/shadow/journal.jsonl
  # crontab: 5 0,4,8,12,16,20 * * * cd <repo> && python3 tools/shadow.py run --history data/live --journal runs/shadow/journal.jsonl

The rung comes from --ladder (default runs/shadow/ladder.json, PAPER when absent). Cycles recorded at PAPER build a
pre-shadow record; days count toward G2 only at the SHADOW rung, which needs §9.3 steps 1-5, 7, 8 PASS and a signed
PROMOTE approval (engine/modes/ladder.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.policy.loader import load_policy, thaw  # noqa: E402
from engine.shadow import metrics  # noqa: E402
from engine.shadow.runner import ShadowJournal, ShadowRunner  # noqa: E402

H4 = timedelta(hours=4)


def _rung(path: Path) -> str:
    try:
        return json.loads(path.read_text())["rung"]
    except (OSError, ValueError, KeyError):
        return "PAPER"


def cmd_drill(a: argparse.Namespace) -> int:
    from engine.bff.session import FIXTURE_MU_Q, fixture_universe
    from engine.replay.paper import Series

    pol = load_policy(a.policy)
    doc = thaw(pol.doc)
    full = fixture_universe(doc, a.history_bars + a.cycles)
    out = Path(a.out)
    jpath = out / "fixture-journal.jsonl"
    if jpath.exists():
        jpath.unlink()
    runner = ShadowRunner(doc, pol.hash, ShadowJournal(jpath), evidence_class="FIXTURE", mu_q_daily=FIXTURE_MU_Q)
    n = len(full[0].c)
    for j in range(a.cycles):
        end = n - a.cycles + j + 1
        cut = [Series(s.instrument_id, s.open_time[:end], s.o[:end], s.h[:end], s.l[:end], s.c[:end]) for s in full]
        close = datetime.fromtimestamp(int(cut[0].open_time[-1]), tz=timezone.utc) + H4
        runner.cycle(cut, bar_close=close, now=close + timedelta(minutes=5), loaded_policy_hash=pol.hash, rung="PAPER")
    chain = runner.journal.verify()
    s = metrics.summarise(runner.journal.records(), "PAPER")
    g2 = metrics.g2_fields(runner.journal.records())
    passed = chain == a.cycles and s["recon"] == 1.0 and not s["h1_d1"] and g2["shadow_days"] == 0
    rec = {"drill": "SHADOW_FIXTURE", "passed": passed, "cycles": chain, "summary": s, "g2_fields": g2,
           "policy_hash": pol.hash, "journal_head": runner.journal.log.head, "class": "FIXTURE"}
    (out / "shadow_fixture.json").write_text(json.dumps(rec, indent=2, default=str) + "\n")
    print(f"{'PASS' if passed else 'FAIL'}  SHADOW_FIXTURE  cycles={chain} recon={s['recon']:.4%} H1/D1={len(s['h1_d1'])} "
          f"G2 days counted={g2['shadow_days']} (FIXTURE never counts)")
    return 0 if passed else 1


def cmd_run(a: argparse.Namespace) -> int:
    from engine.shadow import feed

    pol = load_policy(a.policy)
    doc = thaw(pol.doc)
    now = datetime.now(timezone.utc)
    journal = ShadowJournal(Path(a.journal))
    recs = journal.records()
    if not a.force and not feed.is_due(recs, now):
        print("no cycle due (already recorded for this close, or inside the certification lag)")
        return 0
    frozen = recs[0]["policy_hash"] if recs else pol.hash
    bases = a.pair or ["BTC", "XRP", "ETH", "SOL"]
    series, quotes = [], {}
    for base in bases:
        if not a.offline:
            feed.refresh(Path(a.history), base, now)
            for venue, why in feed.last_skipped.items():
                print(f"{base}: skipped {venue} ({why})", file=sys.stderr)
        s = feed.load_history(feed.store_path(Path(a.history), base))
        if s is None:
            print(f"{base}: no certified history in {a.history}", file=sys.stderr)
            continue
        series.append(s)
        if not a.offline:
            try:
                quotes[s.instrument_id] = feed.kraken_quote(base, now)
            except Exception as e:  # a missing book leaves the entry unpriced; it never blocks the cycle
                print(f"{base}: order book unavailable ({e})", file=sys.stderr)
    if not series:
        return 2
    from engine.admissibility.service import AdmissibilityService, load_blackout
    from engine.bff.session import base_of
    svc = AdmissibilityService(doc, frozen, blackout=load_blackout())
    runner = ShadowRunner(doc, frozen, journal, mu_q_daily=a.mu_q, admission=lambda k, t: svc.gate(base_of(k), t))
    rec = runner.cycle(series, bar_close=feed.last_close(now), now=now, loaded_policy_hash=pol.hash,
                       rung=_rung(Path(a.ladder)), quotes=quotes)
    print(json.dumps({k: rec[k] for k in ("bar_close", "rung", "incidents")}, default=str))
    for row in rec["instruments"]:
        print(f"  {row['instrument_id']}: {row['outcome']} ({row.get('binding_gate') or 'enter'}) recon={row.get('recon_ok')}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    recs = ShadowJournal(Path(a.journal)).records()
    n = ShadowJournal(Path(a.journal)).verify()
    print(f"journal: {n} cycles, hash chain intact")
    for rung in sorted({r["rung"] for r in recs}):
        s = metrics.summarise(recs, rung)
        div = "n/a" if s["cost_divergence"] is None else f"{s['cost_divergence']:.1%}"
        print(f"  {rung}: {s['days']} full days, recon {s['recon']:.4%}, cost divergence {div}, "
              f"H1/D1 {len(s['h1_d1'])}, class {s['class']}")
    print(f"G2 reads: {metrics.g2_fields(recs)}")
    return 0


def cmd_step6(a: argparse.Namespace) -> int:
    import numpy as np

    from engine.regime.ccmrm import confirm, raw_states
    from engine.shadow import feed
    from research.harness.step6 import run_step6

    pol = load_policy(a.policy)
    doc = thaw(pol.doc)
    s = feed.load_history(feed.store_path(Path(a.history), a.pair))
    if s is None:
        print("no certified history", file=sys.stderr)
        return 2
    states = confirm(raw_states(np.asarray(s.c)), doc["regime"]["confirm_bars"])
    rec, tests = run_step6(states, doc, shadow_class="OBSERVED")  # N4 stays NOT RUN until 90 shadow days exist
    print(f"step 6: {rec.verdict} (run_id {rec.run_id})")
    for t in tests:
        print(f"  {t['test']}: {'PASS' if t['passed'] else 'FAIL'} {t.get('metric')} {t.get('value')} {t.get('note', '')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drill")
    d.add_argument("--out", default="runs/shadow")
    d.add_argument("--cycles", type=int, default=12)
    d.add_argument("--history-bars", type=int, default=1500)
    d.set_defaults(fn=cmd_drill)
    r = sub.add_parser("run")
    r.add_argument("--history", required=True)
    r.add_argument("--journal", default="runs/shadow/journal.jsonl")
    r.add_argument("--ladder", default="runs/shadow/ladder.json")
    r.add_argument("--pair", action="append")
    r.add_argument("--mu-q", type=float, default=None, help="harness expectancy; omitted sizes every entry to zero")
    r.add_argument("--offline", action="store_true", help="use the stored history only (no fetch, no order book)")
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_run)
    st = sub.add_parser("status")
    st.add_argument("--journal", default="runs/shadow/journal.jsonl")
    st.set_defaults(fn=cmd_status)
    s6 = sub.add_parser("step6")
    s6.add_argument("--history", required=True)
    s6.add_argument("--pair", default="BTC")
    s6.set_defaults(fn=cmd_step6)
    a = p.parse_args()
    Path("runs/shadow").mkdir(parents=True, exist_ok=True)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
