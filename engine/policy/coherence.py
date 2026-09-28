"""Coherence checks run at policy load (spec §20.2, INV-34).

A policy that fails any binding check cannot activate, even when signed.

Three §20.2 checks need evidence that cannot exist before the engine has run: the σ* Monte
Carlo run (harness step 8, phase P2b), a stress-battery PASS (§7.8, P3.5) and TRADE_ENABLED
venue instances with access records (P3+). Read literally, §20.2 makes the P0 exit gate
("coherence check passes") unreachable. This module therefore marks those three checks
`binds_from=SHADOW`: they are always evaluated and always reported, and they block activation
for SHADOW, CANARY and LIVE. For a PAPER-mode activation they are reported as DEFERRED, which
is safe because PAPER never sends an order (INV-35 rejects any order to a venue that is not
TRADE_ENABLED). This is an owner decision recorded in docs/decisions/0001-coherence-deferral.md.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from engine.policy.loader import Policy

MODES = ("PAPER", "SHADOW", "CANARY", "LIVE")
TIER_NAMES = ("T1", "T2", "T3", "T4")

# ASSUMED inputs for the informational margin projection (Appendix B.2). Owner: principal.
ASSUMED_B_STOP_DISTANCE = 0.07
ASSUMED_MAINTENANCE_MARGIN = 0.005


@dataclass(frozen=True)
class Check:
    id: str
    rule: str
    passed: bool
    detail: str
    binds_from: str = "PAPER"  # lowest mode at which a failure blocks activation
    informational: bool = False

    def binding_at(self, mode: str) -> bool:
        return not self.informational and MODES.index(mode) >= MODES.index(self.binds_from)

    def status_at(self, mode: str) -> str:
        if self.informational:
            return "INFO"
        if self.passed:
            return "PASS"
        return "FAIL" if self.binding_at(mode) else "DEFERRED"


@dataclass
class EvidenceContext:
    """Records the coherence checks read. Everything absent counts as not on file."""

    mc_runs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    stress_runs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    step6_runs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # connector_type -> list of {"state": ..., "access_records": {product: iso_expiry}}
    venue_instances: Mapping[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    stress_run_id: str | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CoherenceResult:
    policy_hash: str
    checks: tuple[Check, ...]

    def failures(self, mode: str) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.binding_at(mode)]

    def coherent_for(self, mode: str) -> bool:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        return not self.failures(mode)

    def table(self, mode: str) -> list[dict[str, str]]:
        return [{"id": c.id, "status": c.status_at(mode), "rule": c.rule, "detail": c.detail} for c in self.checks]


def _tiers(p: Policy) -> list[tuple[str, Mapping[str, Any]]]:
    return [(t, p.get(f"tiers.{t}")) for t in TIER_NAMES]


def _sleeve_product(sleeve: str) -> str:
    return "spot" if sleeve == "A_long" else "perp"


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_policy(p: Policy, ctx: EvidenceContext | None = None) -> CoherenceResult:
    ctx = ctx or EvidenceContext()
    checks: list[Check] = []

    def add(id_: str, rule: str, ok: bool, detail: str, **kw: Any) -> None:
        checks.append(Check(id_, rule, bool(ok), detail, **kw))

    k_stop, k_trail = p.get("stops.k_stop"), p.get("stops.k_trail")
    add("C01", "k_stop < k_trail", k_stop < k_trail, f"k_stop={k_stop}, k_trail={k_trail}")

    t_entry = p.get("signal.T_entry")
    add("C02", "0 < T_entry <= 1", 0 < t_entry <= 1, f"T_entry={t_entry}")

    crm = p.get("cost.cost_R_max")
    add("C03", "cost_R_max < 1", crm < 1, f"cost_R_max={crm}")

    tiers = _tiers(p)
    bad = [t for t, d in tiers if not d["nav_down"] < d["nav_up"]]
    add("C04", "nav_down < nav_up for every tier", not bad, f"violations={bad or 'none'}")

    ups = [d["nav_up"] for _, d in tiers]
    downs = [d["nav_down"] for _, d in tiers]
    inc = all(a < b for a, b in zip(ups, ups[1:], strict=False)) and all(a < b for a, b in zip(downs, downs[1:], strict=False))
    add("C05", "tiers strictly increasing", inc, f"nav_up={ups}, nav_down={downs}")

    bad = [t for t, d in tiers if "B" in d["r"] and d["r"]["B"] > d["r"].get("A", math.inf)]
    add("C06", "r_B <= r_A", not bad, f"violations={bad or 'none'}")

    # C07 informational: projected posted margin at n_max.B full positions (Appendix B.2).
    phi, term = p.get("perps.phi"), p.get("risk.ladder.dd.terminate")
    lev = min(p.get("perps.max_eff_leverage"),
              1.0 / (max(p.get("perps.liq_buffer.x_stop") * ASSUMED_B_STOP_DISTANCE,
                         p.get("perps.liq_buffer.min_distance")) + ASSUMED_MAINTENANCE_MARGIN))
    details = []
    for t, d in tiers:
        n_b = sum(v for k, v in d["n_max"].items() if k.startswith("B"))
        if n_b and "B" in d["r"]:
            margin_per = d["r"]["B"] / ASSUMED_B_STOP_DISTANCE / lev
            cap = phi * term
            eff = min(n_b, int(cap // margin_per))
            details.append(f"{t}: {n_b} slots x {margin_per:.4f} NAV = {n_b * margin_per:.3f} vs cap {cap:.3f}; effective B slots {eff}")
    add("C07", "projected posted margin at n_max.B reported (ASSUMED d=7%, mm=0.5%)", True,
        "; ".join(details) or "no B sleeves", informational=True)

    ss = p.get("sizing.sigma_star")
    run = ctx.mc_runs.get(ss["mc_run_id"]) if ss["mc_run_id"] else None
    ok = bool(run) and run.get("verdict") == "PASS" and run.get("p_dd20_1y", 1.0) <= p.get("risk.p_dd20_1y_max") \
        and ss["value"] is not None
    add("C08", "sigma_star.mc_run_id set and that run shows P(DD>=20%) <= p_dd20_1y_max", ok,
        "no mc_run_id on policy (harness step 8 not run)" if not ss["mc_run_id"]
        else ("run not on file" if not run else f"run p_dd20_1y={run.get('p_dd20_1y')} verdict={run.get('verdict')}"),
        binds_from="SHADOW")

    bad = [t for t, d in tiers if p.get("sizing.cluster.open_risk_cap") < d["r"].get("A", 0)]
    add("C09", "open_risk_cap >= r_A", not bad, f"open_risk_cap={p.get('sizing.cluster.open_risk_cap')}, violations={bad or 'none'}")

    bad = [t for t, d in tiers if p.get("risk.ladder.daily_stop") < 2 * d["r"].get("A", 0)]
    add("C10", "daily_stop >= 2 x r_A", not bad, f"daily_stop={p.get('risk.ladder.daily_stop')}, violations={bad or 'none'}")

    rb = p.get("sizing.rebalance_band")
    add("C11", "rebalance_band in (0,1)", 0 < rb < 1, f"rebalance_band={rb}")

    auth = p.get("regime.authority")
    s6 = p.doc["regime"].get("step6_run_id")
    s6ok = bool(s6) and ctx.step6_runs.get(s6, {}).get("verdict") == "PASS"
    add("C12", "regime.authority = T1 only if a step 6 run PASS is on file", auth == "T0" or s6ok,
        f"authority={auth}, step6_run_id={s6}")

    missing = []
    for sleeve, venues in p.get("venues.execution_allowed").items():
        product = _sleeve_product(sleeve)
        for v in venues:
            good = False
            for inst in ctx.venue_instances.get(v, []):
                exp = inst.get("access_records", {}).get(product)
                if inst.get("state") == "TRADE_ENABLED" and exp and _parse_ts(exp) > ctx.now:
                    good = True
            if not good:
                missing.append(f"{sleeve}:{v}")
    add("C13", "every venue in execution_allowed is TRADE_ENABLED with an unexpired access record", not missing,
        f"not trade-enabled: {missing or 'none'}", binds_from="SHADOW")

    ccxt = [s for s, vs in p.get("venues.execution_allowed").items() if "ccxt-generic" in vs]
    add("C14", "ccxt-generic never in execution_allowed", not ccxt, f"violations={ccxt or 'none'}")

    sr = ctx.stress_runs.get(ctx.stress_run_id) if ctx.stress_run_id else None
    ok = bool(sr) and sr.get("verdict") == "PASS" and sr.get("policy_hash") == p.hash
    add("C15", "stress battery run (§7.8) with PASS attached for this policy hash", ok,
        "no stress battery run attached" if not sr else f"verdict={sr.get('verdict')}", binds_from="SHADOW")

    dd = p.get("risk.ladder.dd")
    rungs = [dd["halve"], dd["suspend_downgrade"], dd["flatten_decision"], dd["terminate"]]
    add("C16", "drawdown rungs strictly increasing (§7.3 single source)", all(a < b for a, b in zip(rungs, rungs[1:], strict=False)),
        f"rungs={rungs}")

    return CoherenceResult(policy_hash=p.hash, checks=tuple(checks))


def summarise(result: CoherenceResult, mode: str) -> str:
    lines = [f"policy {result.policy_hash[:12]} coherence for {mode}: "
             f"{'COHERENT' if result.coherent_for(mode) else 'INCOHERENT'}"]
    for row in result.table(mode):
        lines.append(f"  {row['id']} {row['status']:<8} {row['rule']} -- {row['detail']}")
    return "\n".join(lines)


def ids(checks: Iterable[Check]) -> list[str]:
    return [c.id for c in checks]
