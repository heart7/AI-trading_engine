"""Loss ladder (spec §7.3, single source of truth) and kill switches (§7.7).

All thresholds come from policy.risk.ladder. Latched states (SUSPEND, FLATTEN, TERMINATE) clear only
with a signed re-arm (spec §0.4); automatic rungs clear on their own conditions.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

REARM_REQUIRED = {"ROLLING_5D", "DD_12", "DD_16", "DD_20", "KILL_SUSPEND", "KILL_FLATTEN", "KILL_STOP"}


@dataclass(frozen=True)
class LadderInputs:
    now: datetime
    loss_today: float  # net loss since 00:00 UTC as a fraction of NAV (positive = loss)
    loss_5d: float
    loss_20d: float
    drawdown: float  # from unit-value HWM, fraction
    daily_review_logged: bool = False


@dataclass
class LadderState:
    daily_stop_until: datetime | None = None
    halve_r_until: datetime | None = None
    dd_halve_active: bool = False
    latched: dict[str, datetime] = field(default_factory=dict)  # rung -> fired_at
    flatten_deadline: datetime | None = None
    flatten_decided: str | None = None  # "FLATTEN" | "HOLD" once the principal decides
    tier_downgrades: int = 0
    events: list[tuple[datetime, str]] = field(default_factory=list)


@dataclass(frozen=True)
class LadderDecision:
    entries_allowed: bool
    reduce_only: bool
    flatten: bool
    terminate: bool
    r_multiplier: float
    size_multiplier: float
    downgrade_tier: bool
    binding: str | None
    next_rung: str | None


class LossLadder:
    def __init__(self, ladder: Mapping, *, deadman_hours: float | None = None):
        self.cfg = ladder
        self.deadman = timedelta(hours=deadman_hours if deadman_hours is not None else ladder["flatten_deadman_hours"])
        self.state = LadderState()

    def _fire(self, rung: str, now: datetime) -> None:
        if rung not in self.state.latched:
            self.state.latched[rung] = now
            self.state.events.append((now, rung))

    def evaluate(self, x: LadderInputs) -> LadderDecision:
        c, s = self.cfg, self.state
        dd = c["dd"]
        next_midnight = (x.now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if x.loss_today >= c["daily_stop"] and s.daily_stop_until is None:
            s.daily_stop_until = next_midnight
            s.events.append((x.now, "DAILY"))
        if s.daily_stop_until and x.now >= s.daily_stop_until and x.daily_review_logged:
            s.daily_stop_until = None  # automatic at 00:00 after review logged
        if x.loss_5d >= c["rolling_5d_suspend"]:
            self._fire("ROLLING_5D", x.now)
        if x.loss_20d >= c["rolling_20d_halve_r"] and s.halve_r_until is None:
            s.halve_r_until = x.now + timedelta(days=20)
            s.events.append((x.now, "ROLLING_20D"))
        if s.halve_r_until and x.now >= s.halve_r_until:
            s.halve_r_until = None
        if x.drawdown >= dd["halve"]:
            s.dd_halve_active = True
        elif x.drawdown < 0.05:  # re-arm automatically when DD < 5% (spec §7.3)
            s.dd_halve_active = False
        downgrade = False
        if x.drawdown >= dd["suspend_downgrade"] and "DD_12" not in s.latched:
            self._fire("DD_12", x.now)
            s.tier_downgrades += 1
            downgrade = True
        if x.drawdown >= dd["flatten_decision"] and "DD_16" not in s.latched:
            self._fire("DD_16", x.now)
            s.flatten_deadline = x.now + self.deadman
        if x.drawdown >= dd["terminate"]:
            self._fire("DD_20", x.now)

        terminate = "DD_20" in s.latched
        flatten = terminate or "KILL_FLATTEN" in s.latched or s.flatten_decided == "FLATTEN" or (
            "DD_16" in s.latched and s.flatten_decided is None and s.flatten_deadline is not None and x.now >= s.flatten_deadline)
        suspend = bool({"ROLLING_5D", "DD_12", "DD_16", "KILL_SUSPEND"} & s.latched.keys())
        stop = s.daily_stop_until is not None or "KILL_STOP" in s.latched
        binding = next((r for r, cond in [("DD_20", terminate), ("FLATTEN", flatten), ("DD_16", "DD_16" in s.latched),
                                          ("DD_12", "DD_12" in s.latched), ("ROLLING_5D", "ROLLING_5D" in s.latched),
                                          ("KILL_SUSPEND", "KILL_SUSPEND" in s.latched), ("KILL_STOP", "KILL_STOP" in s.latched),
                                          ("DAILY", s.daily_stop_until is not None)] if cond), None)
        rungs = [("DD_8", dd["halve"]), ("DD_12", dd["suspend_downgrade"]), ("DD_16", dd["flatten_decision"]), ("DD_20", dd["terminate"])]
        nxt = next((name for name, lvl in rungs if x.drawdown < lvl), None)
        return LadderDecision(
            entries_allowed=not (stop or suspend or flatten or terminate), reduce_only=suspend or flatten or terminate,
            flatten=flatten, terminate=terminate, r_multiplier=0.5 if s.halve_r_until else 1.0,
            size_multiplier=0.5 if s.dd_halve_active else 1.0, downgrade_tier=downgrade, binding=binding, next_rung=nxt)

    # --- kill switches (§7.7): activation is instant; re-arm needs a signed approval ---
    def kill(self, switch: str, now: datetime) -> None:
        if switch not in ("STOP", "SUSPEND", "FLATTEN"):
            raise ValueError(switch)
        self._fire(f"KILL_{switch}", now)

    def decide_flatten(self, decision: str) -> None:
        if decision not in ("FLATTEN", "HOLD"):
            raise ValueError(decision)
        self.state.flatten_decided = decision

    def rearm(self, rung: str, approval) -> None:
        """`approval` must be a VerifiedApproval for action REARM (see engine.governance.approvals)."""
        from engine.governance.approvals import VerifiedApproval
        if rung not in REARM_REQUIRED:
            raise ValueError(f"{rung} re-arms automatically")
        if rung == "DD_20":
            raise PermissionError("DD 20% terminates the programme: restart needs a full review and a new policy")
        if not isinstance(approval, VerifiedApproval) or approval.approval["action"] != "REARM":
            raise PermissionError("re-arm requires a verified, signed REARM approval with rationale")
        self.state.latched.pop(rung, None)
        if rung == "DD_16":
            self.state.flatten_deadline, self.state.flatten_decided = None, None
