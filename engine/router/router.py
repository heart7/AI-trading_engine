"""Strategy Router (spec §3). Deterministic, risk plane, no discretion.

    active_tier = min(eligible_tier, user_selected_tier)

The router can compute eligibility and downgrade; it can never upgrade (INV-11). An upgrade needs
every gate PASS plus a verified signed approval (INV-12). A sleeve the active tier does not allow is
reduce-only: open positions keep their stops, new entries fail TIER_NOT_ACTIVE (INV-13).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

TIERS = ("T0", "T1", "T2", "T3", "T4")


def tier_index(t: str) -> int:
    return TIERS.index(t)


@dataclass
class TierEvidence:
    """Everything §3.3 reads. All fields default to 'not on file'."""
    nav_history: Sequence[tuple[date, float, bool]] = ()  # (day, NAV, dual-path reconciled)
    deposits: Sequence[tuple[date, float, float]] = ()  # (day, NAV before, NAV after)
    shadow_days: int = 0
    shadow_recon: float = 0.0
    shadow_h1_d1_incidents: int = 0
    validation_A_steps_pass: frozenset[int] = frozenset()
    prev_tier_live_days: int = 0
    canary_cost_divergence: float | None = None
    derivatives_access_valid: bool = False
    B_short_validated: bool = False
    B_long_validated: bool = False
    collateral_policy_active: bool = False
    open_S1_H1: int = 0
    drawdown: float = 0.0


REQUIRED_A_STEPS = frozenset({1, 2, 3, 4, 5, 7, 8})


def _nav_dwell_ok(ev: TierEvidence, floor: float, days: int, today: date) -> tuple[bool, str]:
    need_from = today - timedelta(days=days - 1)
    for day, before, after in ev.deposits:
        if before < floor <= after and day > need_from:
            need_from = day  # dwell restarts on a deposit that crossed this floor (§12.5)
    window = [x for x in ev.nav_history if need_from <= x[0] <= today]
    run = 0
    for _day, nav, ok in sorted(window):
        run = run + 1 if (nav >= floor and ok) else 0
    counted = min(run, days)
    return run >= days and len(window) >= days, f"NAV >= ${floor:,.0f} for {days} days reconciled (day {counted} of {days})"


def evaluate_gates(tier: str, policy: Mapping, ev: TierEvidence, today: date) -> list[dict]:
    t = policy["tiers"][tier]
    dwell = policy["tiers"]["upgrade_dwell_days"]
    idx = tier_index(tier)
    g = []

    def add(gid: str, ok: bool, detail: str) -> None:
        g.append({"gate": gid, "passed": bool(ok), "detail": detail})

    ok, det = _nav_dwell_ok(ev, t["nav_up"], dwell, today)
    add("G1", ok, det)
    add("G2", ev.shadow_days >= policy["validation"]["shadow_days"] and ev.shadow_recon >= policy["validation"]["recon_min"]
        and ev.shadow_h1_d1_incidents == 0,
        f"T0 shadow {ev.shadow_days}/{policy['validation']['shadow_days']} days, recon {ev.shadow_recon:.4f}, H1/D1 {ev.shadow_h1_d1_incidents}")
    missing = sorted(REQUIRED_A_STEPS - ev.validation_A_steps_pass)
    add("G3", not missing, f"Strategy A validation steps missing: {missing or 'none'}")
    if idx >= 2:
        add("G4", ev.prev_tier_live_days >= 60 and ev.canary_cost_divergence is not None and ev.canary_cost_divergence < 0.25,
            f"previous tier live {ev.prev_tier_live_days}/60 days, cost divergence {ev.canary_cost_divergence}")
    if idx >= 3:
        add("G5", ev.derivatives_access_valid, "derivatives venue access record with professional-client confirmation + legal memo")
        add("G6", ev.B_short_validated, "Strategy B short-book validation")
        add("G8", ev.collateral_policy_active, "collateral policy active")
    if idx >= 4:
        add("G7", ev.B_long_validated, "Strategy B long-book validation")
    add("G9", ev.open_S1_H1 == 0 and ev.drawdown < 0.08, f"open S1/H1 {ev.open_S1_H1}, drawdown {ev.drawdown:.2%}")
    order = ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"]
    return sorted(g, key=lambda r: order.index(r["gate"]))


@dataclass(frozen=True)
class Eligibility:
    eligible_tier: str
    gates: dict[str, list[dict]]
    binding_gate: str | None  # first failing gate of the next tier up
    as_of: date


def eligibility(policy: Mapping, ev: TierEvidence, today: date) -> Eligibility:
    gates = {t: evaluate_gates(t, policy, ev, today) for t in TIERS[1:]}
    eligible = "T0"
    for t in TIERS[1:]:
        if all(x["passed"] for x in gates[t]):
            eligible = t
        else:
            break
    nxt = TIERS[tier_index(eligible) + 1] if eligible != "T4" else None
    binding = None
    if nxt:
        first = next(x for x in gates[nxt] if not x["passed"])
        binding = f"{nxt}:{first['gate']}"
    return Eligibility(eligible, gates, binding, today)


class TierRequestRefused(Exception):
    def __init__(self, binding_gate: str):
        self.binding_gate = binding_gate
        super().__init__(f"refused: {binding_gate}")


@dataclass
class RouterState:
    user_selected: str = "T0"
    eligible: str = "T0"
    notifications: list[str] = field(default_factory=list)
    log: list[tuple[datetime, str]] = field(default_factory=list)
    sleeve_since: dict[str, datetime] = field(default_factory=dict)
    # Tier whose rules PAPER/SHADOW simulate while the fund is at T0 (run configuration, not a live parameter).
    paper_tier: str = "T2"

    @property
    def active(self) -> str:
        return TIERS[min(tier_index(self.eligible), tier_index(self.user_selected))]


class StrategyRouter:
    def __init__(self, policy: Mapping, state: RouterState | None = None):
        self.policy = policy
        self.state = state or RouterState()

    def update_eligibility(self, el: Eligibility, now: datetime) -> None:
        prev = self.state.eligible
        self.state.eligible = el.eligible_tier
        if tier_index(el.eligible_tier) > tier_index(prev) and tier_index(el.eligible_tier) > tier_index(self.state.user_selected):
            # Crossing a floor never upgrades; it only notifies (INV-11).
            self.state.notifications.append(f"{el.eligible_tier} eligible: approve?")
        self.state.log.append((now, f"eligibility {prev}->{el.eligible_tier}"))

    def select_down(self, tier: str, now: datetime) -> str:
        if tier_index(tier) > tier_index(self.state.user_selected):
            raise ValueError("use request_up for an increase")
        self.state.user_selected = tier
        self.state.log.append((now, f"user selected down to {tier}"))
        return self.state.active

    def request_up(self, tier: str, el: Eligibility, approval, now: datetime) -> str:
        """Upgrade: every gate for `tier` must PASS and `approval` must be a verified TIER_UPGRADE approval."""
        from engine.governance.approvals import VerifiedApproval
        failing = [x for t in TIERS[1:tier_index(tier) + 1] for x in el.gates[t] if not x["passed"]]
        if failing:
            self.state.log.append((now, f"request {tier} refused {failing[0]['gate']}"))
            raise TierRequestRefused(f"{tier}:{failing[0]['gate']}")
        if not isinstance(approval, VerifiedApproval) or approval.approval["action"] != "TIER_UPGRADE":
            raise TierRequestRefused(f"{tier}:UNSIGNED")
        before = set(self.policy["tiers"][self.state.active]["sleeves"]) if self.state.active != "T0" else set()
        self.state.user_selected = tier
        for sl in self.policy["tiers"][self.state.active]["sleeves"] if self.state.active != "T0" else ():
            if sl not in before:
                self.state.sleeve_since[sl] = now  # new sleeve: half risk budget for the ramp (§3.5)
        self.state.log.append((now, f"upgraded to {tier} by signed approval"))
        return self.state.active

    def auto_downgrade(self, cause: str, now: datetime) -> str:
        """Downgrade by one tier: NAV below downgrade floor 5 days, DD 12% rung, or a gate expiring (§3.4)."""
        cur = tier_index(self.state.active)
        if cur > 0:
            self.state.user_selected = TIERS[cur - 1]
            self.state.log.append((now, f"TIER_DOWNGRADE {TIERS[cur]}->{TIERS[cur - 1]} cause={cause}"))
        return self.state.active

    def nav_below_downgrade_floor(self, navs_last_days: Sequence[float]) -> bool:
        t = self.state.active
        if t == "T0":
            return False
        n = self.policy["tiers"]["downgrade_dwell_days"]
        floor = self.policy["tiers"][t]["nav_down"]
        return len(navs_last_days) >= n and all(v < floor for v in navs_last_days[-n:])

    def sim_tier(self, mode: str) -> str:
        """Tier whose parameters apply: the active tier when LIVE/CANARY, the paper tier in PAPER/SHADOW at T0."""
        if mode in ("PAPER", "SHADOW") and self.state.active == "T0":
            return self.state.paper_tier
        return self.state.active

    def sleeve_allowed(self, sleeve: str, mode: str = "LIVE") -> bool:
        t = self.sim_tier(mode)
        return t != "T0" and sleeve in self.policy["tiers"][t]["sleeves"]

    def ramp_factor(self, sleeve: str, now: datetime | None) -> float:
        """§3.5: a sleeve new at an upgrade runs at `new_sleeve_ramp.factor` of its risk budget for `days`."""
        since = self.state.sleeve_since.get(sleeve)
        ramp = self.policy["tiers"]["new_sleeve_ramp"]
        if since is None or now is None:
            return 1.0
        return float(ramp["factor"]) if now - since < timedelta(days=ramp["days"]) else 1.0

    def r_tier(self, sleeve: str, mode: str = "LIVE", now: datetime | None = None) -> float:
        t = self.sim_tier(mode)
        if t == "T0":
            return 0.0
        r = self.policy["tiers"][t]["r"]
        base = r["A"] if sleeve == "A_long" else r.get("B", 0.0)
        return base * (self.ramp_factor(sleeve, now) if mode in ("CANARY", "LIVE") else 1.0)

    def n_max(self, sleeve: str, mode: str = "LIVE") -> int:
        t = self.sim_tier(mode)
        if t == "T0":
            return 0
        nm = self.policy["tiers"][t]["n_max"]
        return nm.get("A", 0) if sleeve == "A_long" else nm.get(sleeve, nm.get("B", 0))
