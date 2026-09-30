#!/usr/bin/env python3
"""Learning loop operator tool (spec §10.2 L1, §10.3, §10.3a). Proposals only; nothing here changes the engine.

  drill    FIXTURE drill for CI: closed paper trades -> episodes -> store -> attribution -> cost retune
  retune   fit the cost model to OBSERVED shadow quotes and real fills; writes a proposal, never applies it
  status   episodes on file by class, process-error rate, training-set size

On your own machine, once the shadow record has entries priced from the order book:

  python3 tools/learn.py retune --journal runs/shadow/journal.jsonl --history data/live

`--episodes` adds real-fill episodes (CANARY onward). `--history` also replays the stored bars under the proposed
cost model and reports what it would have changed. The proposal lands in runs/learning/cost-proposal.json with
applies: false; making it live is a policy change with the harness, a stress PASS and your signature.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.common.canonical import content_hash  # noqa: E402
from engine.data.store import AppendOnlyLog  # noqa: E402
from engine.evidence.attribution import attribute  # noqa: E402
from engine.policy.loader import load_policy, thaw  # noqa: E402
from engine.replay.paper import CostModel, ReplayConfig, replay  # noqa: E402
from engine.router.router import StrategyRouter  # noqa: E402
from research.learner import cost_retune, episodes  # noqa: E402


def _records(path: str | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    return AppendOnlyLog(Path(path)).records()


def cmd_drill(a: argparse.Namespace) -> int:
    from engine.bff.session import FIXTURE_MU_Q, fixture_universe

    pol = load_policy(a.policy)
    doc = thaw(pol.doc)
    before = content_hash(doc)
    series = fixture_universe(doc, a.bars)
    res = replay(series, doc, StrategyRouter(doc), ReplayConfig(mu_q_daily=FIXTURE_MU_Q), CostModel())
    eps = episodes.from_replay(res, series, doc, pol.hash)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    spath = out / "fixture-episodes.jsonl"
    if spath.exists():
        spath.unlink()
    store = episodes.EpisodeStore(spath)
    added, again = store.extend(eps), store.extend(eps)
    chain = store.verify()
    att = attribute([episodes.to_attribution(e) for e in store.records()])
    done = episodes.matured(res)
    ledger_net = sum(t.pnl for t in done)
    prop = cost_retune.fit(cost_retune.observations(store.records()))
    passed = (added == len(done) > 0 and again == 0 and chain == added and att.exact
              and abs(float(att.net) - ledger_net) < 1e-6 and not store.training_set()
              and prop["verdict"] == "NO_EVIDENCE" and prop["evidence_class"] == "FIXTURE" and not prop["applies"]
              and content_hash(doc) == before)
    rec = {"drill": "LEARNING_FIXTURE", "passed": passed, "episodes": added, "readded": again, "chain": chain,
           "attribution_exact": att.exact, "net_usd": float(att.net), "replay_net_usd": ledger_net,
           "process_error_rate": episodes.process_error_rate(store.records()), "training_set": len(store.training_set()),
           "retune": prop, "policy_hash": pol.hash, "class": "FIXTURE"}
    (out / "learning_fixture.json").write_text(json.dumps(rec, indent=2, default=str) + "\n")
    print(f"{'PASS' if passed else 'FAIL'}  LEARNING_FIXTURE  episodes={added} re-added={again} chain={chain} "
          f"attribution exact={att.exact} training set={len(store.training_set())} retune={prop['verdict']} (FIXTURE never fitted)")
    return 0 if passed else 1


def cmd_retune(a: argparse.Namespace) -> int:
    obs = cost_retune.observations(_records(a.episodes), _records(a.journal))
    pol = load_policy(a.policy)
    doc = thaw(pol.doc)
    prop = cost_retune.fit(obs, quantile=float(doc["cost"]["slippage_quantile"]))
    if a.history and "proposed" in prop:
        from engine.shadow import feed
        from engine.shadow.runner import align
        loaded = (feed.load_history(feed.store_path(Path(a.history), b)) for b in a.pair or ["BTC", "XRP", "ETH", "SOL"])
        series = [s for s in loaded if s is not None]
        if series:
            prop["effect"] = cost_retune.effect(doc, align(series), prop, mu_q_daily=a.mu_q)
    prop["policy_hash"] = pol.hash
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cost-proposal.json").write_text(json.dumps(prop, indent=2, default=str) + "\n")
    n = prop["n"]
    print(f"cost retune: {prop['verdict']}  fills={n['fills']} quotes={n['quotes']} (FIXTURE ignored: {n['fixture_ignored']})")
    print(f"  {prop.get('detail', '')}")
    if "proposed" in prop:
        print(f"  proposal: slippage_q75 {prop['current']['slippage_q75']} -> {prop['proposed']['slippage_q75']} "
              f"({'loosens' if prop['loosens'] else 'tightens'}; applies: false)")
    print(f"  written to {out / 'cost-proposal.json'}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    store = episodes.EpisodeStore(Path(a.episodes))
    recs = store.records()
    by = {}
    for e in recs:
        by[e["class"]] = by.get(e["class"], 0) + 1
    rate = episodes.process_error_rate(recs)
    print(f"episodes: {len(recs)} {by}  chain={store.verify()}  training set (OBSERVED, not excluded): {len(store.training_set())}")
    print(f"process-error rate: {'n/a' if rate is None else f'{rate:.1%}'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drill")
    d.add_argument("--out", default="runs/learning")
    d.add_argument("--bars", type=int, default=3000)
    d.set_defaults(fn=cmd_drill)
    r = sub.add_parser("retune")
    r.add_argument("--journal", default="runs/shadow/journal.jsonl")
    r.add_argument("--episodes", default="runs/learning/episodes.jsonl")
    r.add_argument("--history")
    r.add_argument("--pair", action="append")
    r.add_argument("--mu-q", type=float, default=None)
    r.add_argument("--out", default="runs/learning")
    r.set_defaults(fn=cmd_retune)
    st = sub.add_parser("status")
    st.add_argument("--episodes", default="runs/learning/episodes.jsonl")
    st.set_defaults(fn=cmd_status)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
