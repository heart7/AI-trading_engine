"""agent_activity_event generation (spec §16.8.2).

Every claim, every signal_intent (all outcomes), every order transition and every protection read-back in a
cycle gets exactly one pointer event (completeness, §16.8.3). The event is thin: the payload of record stays in
the claim, intent, order or fill it points at.

The paper session has no wall clock inside a cycle, so t_offset_ms comes from a deterministic FIXTURE cycle
clock (A-UI-CYCLE-CLOCK): each step lands inside its §13.4 window with jitter derived from the cycle id. An
event past its step deadline keeps its real offset and is flagged late (CYCLE_TIMEOUT); it is never clamped.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from engine.common.schemas import validate

CODE_VERSION = "0.1.0"
DEADLINES_MS = {"certified": 60_000, "claims": 90_000, "intents": 120_000, "orders": 150_000, "entry_ladder": 900_000}
STEP_ORDER = ("certified", "claims", "intents", "orders", "entry_ladder")
STEP_LABEL = {"certified": "Certify bar", "claims": "Signal + regime claims", "intents": "Router + risk + intent",
              "orders": "Order placed", "entry_ladder": "Entry filled or cancelled"}

# Lanes: the §13.7 agents plus OMS, Protection verifier and Watchdog. §16.8.1 says "10 lanes" but that list has
# 11 members; all 11 are drawn (decision 0005).
LANES = [
    {"id": "data_sentinel", "name": "Data sentinel", "authority": "T3"},
    {"id": "signal_engine", "name": "Signal engine", "authority": "T1"},
    {"id": "regime", "name": "Regime (CCMRM)", "authority": None},  # from policy.regime.authority
    {"id": "strategy_router", "name": "Strategy router", "authority": "T3"},
    {"id": "allocator", "name": "Allocator", "authority": "T2"},
    {"id": "risk_authority", "name": "Risk authority", "authority": "T3"},
    {"id": "oms", "name": "Execution (OMS)", "authority": "T3"},
    {"id": "protection_verifier", "name": "Protection verifier", "authority": "T3"},
    {"id": "verifier", "name": "Verifier", "authority": "T0"},
    {"id": "watchdog", "name": "Watchdog", "authority": "T3"},
    {"id": "reporter", "name": "Reporter (A11)", "authority": "INFORM"},
]


def _jit(*parts: Any, lo: int, hi: int) -> int:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return lo + int.from_bytes(h[:4], "big") % max(1, hi - lo)


def _id(prefix: str, *parts: Any) -> str:
    return prefix + "-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def lanes(policy: Mapping[str, Any], active_tier: str) -> list[dict[str, Any]]:
    """Lane list with the inactive-at-tier flag. The allocator only acts when a tier runs more than one sleeve."""
    out = []
    multi = len(policy["tiers"][active_tier]["sleeves"]) > 1 if active_tier in policy["tiers"] else False
    for L in LANES:
        d = dict(L)
        if d["id"] == "regime":
            a = policy["regime"]["authority"]
            d["authority"] = a
            d["tier_label"] = f"{a} · computed, not applied" if a == "T0" else f"{a} · applied"
        else:
            d["tier_label"] = d["authority"]
        d["active"] = not (d["id"] == "allocator" and not multi)
        if not d["active"]:
            d["tier_label"] = f"inactive at {active_tier}"
        if d["id"] == "oms":
            d["tier_label"] = "paper venue (sim)"
        out.append(d)
    return out


class CycleBuilder:
    """Builds one cycle's events from the records the decision cycle produced."""

    def __init__(self, cycle_close: datetime, policy_hash: str):
        self.cycle = cycle_close
        self.cid = cycle_close.isoformat()
        self.policy_hash = policy_hash
        self.events: list[dict[str, Any]] = []
        self.records: list[tuple[str, str]] = []  # (ref_kind, ref_id) of every record in the cycle

    def emit(self, agent: str, cls: str, subject: str, *, t_ms: int, outcome: str, ref_kind: str, ref_id: str,
             instrument: str | None = None, step: str | None = None, reason: str | None = None) -> dict[str, Any]:
        late = step is not None and t_ms > DEADLINES_MS[step]
        ev = {"event_id": _id("ae", self.cid, agent, ref_kind, ref_id), "cycle_id": self.cid, "agent": agent,
              "emission_class": cls, "subject": subject, "instrument_id": instrument, "t_offset_ms": int(t_ms),
              "deadline_step": step, "late": late, "outcome": outcome,
              "reason_code": ("CYCLE_TIMEOUT" if late and not reason else reason), "ref_kind": ref_kind, "ref_id": ref_id,
              "policy_hash": self.policy_hash, "code_version": CODE_VERSION,
              "created_at": datetime.fromtimestamp(self.cycle.timestamp() + t_ms / 1000, tz=timezone.utc).isoformat()}
        self.records.append((ref_kind, ref_id))
        self.events.append(ev)
        return ev


def build_cycle(*, cycle_close: datetime, bar_index: int, instruments: list[str], intents: list[dict],
                signals: Mapping[str, Mapping[str, Any]], regime_authority: str, policy_hash: str,
                entries_filled: Mapping[str, Mapping[str, Any]], exits: Mapping[str, str], stop_changes: Iterable[str],
                stop_fills: Mapping[str, str], extra: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    b = CycleBuilder(cycle_close, policy_hash)
    cid = b.cid
    by_inst = {x["instrument_id"]: x for x in intents}
    b.emit("watchdog", "INFORM", "heartbeat_mesh", t_ms=_jit(cid, "wd", lo=4_000, hi=12_000), outcome="verifier + OMS heartbeats ok",
           ref_kind="claim", ref_id=_id("hb", cid))
    for k, inst in enumerate(instruments):
        t = _jit(cid, inst, "cert", lo=30_000 + k * 2_000, hi=48_000 + k * 2_000)
        b.emit("data_sentinel", "INFORM", "bar_accepted", instrument=inst, t_ms=t, step="certified",
               outcome="FIXTURE bar accepted · certified: false (PAPER)", ref_kind="claim", ref_id=_id("bc", cid, inst))
    for k, inst in enumerate(instruments):
        sig = signals[inst]
        t = _jit(cid, inst, "sig", lo=58_000 + k * 1_500, hi=76_000 + k * 1_500)
        T = sig.get("T")
        it = by_inst.get(inst)
        if T is None:
            b.emit("signal_engine", "ABSTAIN", "signal_claim", instrument=inst, t_ms=t, step="claims",
                   outcome="signal abstained", reason="INSUFFICIENT_HISTORY", ref_kind="claim", ref_id=_id("sc", cid, inst))
        else:
            rec = it is not None and it["outcome"] == "ENTERED"
            b.emit("signal_engine", "RECOMMEND" if rec else "INFORM", "signal_claim", instrument=inst, t_ms=t, step="claims",
                   outcome=f"signal_claim · T {T:+.2f} · B {sig['B']:+.2f}", ref_kind="claim", ref_id=_id("sc", cid, inst))
        tr = _jit(cid, inst, "reg", lo=62_000 + k * 1_500, hi=84_000 + k * 1_500)
        b.emit("regime", "INFORM", "regime_claim", instrument=inst, t_ms=tr, step="claims",
               outcome="regime_claim · m computed, not applied" if regime_authority == "T0" else "regime_claim · m applied",
               ref_kind="claim", ref_id=_id("rc", cid, inst))
    b.emit("strategy_router", "INFORM", "tier_eligibility_claim", t_ms=_jit(cid, "rt", lo=91_000, hi=99_000),
           step="intents", outcome="active tier unchanged · downgrade-only", ref_kind="claim", ref_id=_id("te", cid))
    for k, inst in enumerate(instruments):
        it = by_inst.get(inst)
        if it is None:
            continue  # instrument in position: exits are evaluated, no entry intent (replay contract)
        t = _jit(cid, inst, "int", lo=100_000 + k * 2_000, hi=112_000 + k * 2_000)
        iid = _id("si", cid, inst)
        if it["outcome"] == "ENTERED":
            b.emit("risk_authority", "PROPOSE", "signal_intent", instrument=inst, t_ms=t, step="intents",
                   outcome="signal_intent ENTERED · sizing_claim within limits", ref_kind="intent", ref_id=iid)
        else:
            b.emit("risk_authority", "ABSTAIN", "signal_intent", instrument=inst, t_ms=t, step="intents",
                   outcome=f"signal_intent {it['outcome']}", reason=it["binding_gate"], ref_kind="intent", ref_id=iid)
    for k, (inst, fill) in enumerate(sorted(entries_filled.items())):
        oid = _id("or", cid, inst, "entry")
        b.emit("oms", "EXECUTE", "order_placed", instrument=inst, t_ms=_jit(cid, inst, "pl", lo=121_000 + k * 3_000, hi=140_000 + k * 3_000),
               step="orders", outcome="post-only buy placed at touch (paper)", ref_kind="order", ref_id=oid + ":PLACED")
        if fill.get("filled"):
            tf = _jit(cid, inst, "fl", lo=200_000, hi=780_000)
            b.emit("oms", "EXECUTE", "order_filled", instrument=inst, t_ms=tf, step="entry_ladder",
                   outcome=f"filled · {fill['liquidity']}", ref_kind="order", ref_id=oid + ":FILLED")
            b.emit("protection_verifier", "EXECUTE", "protection_readback", instrument=inst, t_ms=tf + _jit(cid, inst, "pv", lo=4_000, hi=11_000),
                   outcome="venue stop placed · read-back ✓", ref_kind="order", ref_id=_id("or", cid, inst, "stop") + ":READBACK")
        else:
            b.emit("oms", "EXECUTE", "order_cancelled", instrument=inst, t_ms=_jit(cid, inst, "cx", lo=880_000, hi=899_000),
                   step="entry_ladder", outcome="IOC would breach cost budget · cancelled, no trade", reason="COST_R_EXCEEDED",
                   ref_kind="order", ref_id=oid + ":CANCELLED")
    for inst, reason in sorted(exits.items()):
        b.emit("oms", "EXECUTE", "exit_placed", instrument=inst, t_ms=_jit(cid, inst, "ex", lo=121_000, hi=146_000), step="orders",
               outcome=f"reduce-only exit placed · {reason}", ref_kind="order", ref_id=_id("or", cid, inst, "exit") + ":PLACED")
    for inst, reason in sorted(stop_fills.items()):
        b.emit("oms", "INFORM", "venue_stop_filled", instrument=inst, t_ms=_jit(cid, inst, "sf", lo=8_000, hi=20_000),
               outcome=f"venue-resident stop filled during the bar · {reason}", ref_kind="fill", ref_id=_id("fl", cid, inst))
    for inst in sorted(stop_changes):
        b.emit("protection_verifier", "INFORM", "protection_readback", instrument=inst, t_ms=_jit(cid, inst, "ra", lo=112_000, hi=118_000),
               outcome="trailing stop ratcheted · read-back ✓", ref_kind="order", ref_id=_id("or", cid, inst, "ratchet") + ":READBACK")
    b.emit("verifier", "INFORM", "reconciliation", t_ms=_jit(cid, "vf", lo=150_000, hi=178_000),
           outcome="independent recompute of signal + sizing · match ✓", ref_kind="claim", ref_id=_id("vr", cid))
    for e in extra:
        b.emit(**e)
    evs = sorted(b.events, key=lambda e: (e["t_offset_ms"], e["agent"], e["event_id"]))
    for e in evs:
        validate("agent_activity_event", e)
    return evs


def step_timings(events: Iterable[Mapping[str, Any]]) -> dict[str, int | None]:
    """Duration of each step in one cycle = offset of its last event."""
    out: dict[str, int | None] = dict.fromkeys(STEP_ORDER)
    for e in events:
        s = e.get("deadline_step")
        if s:
            out[s] = max(out[s] or 0, e["t_offset_ms"])
    return out


def emissions(cycles: Iterable[list[Mapping[str, Any]]]) -> dict[str, Counter]:
    out: dict[str, Counter] = {L["id"]: Counter() for L in LANES}
    for evs in cycles:
        for e in evs:
            out[e["agent"]][e["emission_class"]] += 1
    return out


def flow_edges(events: list[Mapping[str, Any]], regime_authority: str) -> list[dict[str, Any]]:
    """Message-flow edges with counts. An edge whose consumer may not act on it is dashed (authorised: false)."""
    cnt = Counter(e["agent"] for e in events)
    oms_entry = sum(1 for e in events if e["agent"] == "oms" and e["subject"] in ("order_placed", "exit_placed"))
    edges = [("data_sentinel", "signal_engine", cnt["data_sentinel"], True, None),
             ("data_sentinel", "regime", cnt["data_sentinel"], True, None),
             ("signal_engine", "strategy_router", cnt["signal_engine"], True, None),
             ("regime", "strategy_router", cnt["regime"], regime_authority != "T0",
              "computed, not applied" if regime_authority == "T0" else None),
             ("strategy_router", "risk_authority", cnt["strategy_router"], True, None),
             ("risk_authority", "oms", oms_entry, True, None),
             ("oms", "protection_verifier", cnt["protection_verifier"], True, None),
             ("verifier", "risk_authority", cnt["verifier"], True, None),
             ("watchdog", "risk_authority", cnt["watchdog"], True, None)]
    return [{"from": a, "to": b, "count": n, "authorised": ok, "dashed": not ok, "label": lbl} for a, b, n, ok, lbl in edges]
