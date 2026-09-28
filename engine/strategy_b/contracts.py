"""Contract specification versioning (spec §6.8, INV-24, INV-25).

Specs are point-in-time records read from the venue capability snapshot. The funding interval always comes from
here: there is no default interval anywhere in the engine. A change to the maintenance-margin schedule, funding
cap or settlement asset invalidates the snapshot, recomputes every affected liquidation distance and blocks
entries on the instrument until the principal re-approves the new spec.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field

from engine.common.canonical import content_hash
from engine.common.incidents import IncidentLog
from engine.governance.approvals import VerifiedApproval

MATERIAL = ("mm_schedule", "funding_cap", "settlement_asset")


@dataclass(frozen=True)
class ContractSpec:
    instrument_id: str
    venue: str
    valid_from: str
    tick: float
    lot: float
    mm_schedule: tuple[tuple[float, float], ...]  # (notional up to USD, maintenance margin rate), ascending
    funding_interval_h: float
    funding_cap: float
    settlement_asset: str
    delisting_notice: str | None = None

    @property
    def hash(self) -> str:
        return content_hash(asdict(self))

    def mmr(self, notional_usd: float) -> float:
        for upto, rate in self.mm_schedule:
            if notional_usd <= upto:
                return rate
        return self.mm_schedule[-1][1]

    def funding_events_per_day(self) -> float:
        return 24.0 / self.funding_interval_h


@dataclass
class ContractRegistry:
    incidents: IncidentLog
    current: dict[str, ContractSpec] = field(default_factory=dict)
    approved: dict[str, str] = field(default_factory=dict)  # instrument -> approved spec hash
    history: list[ContractSpec] = field(default_factory=list)

    def update(self, spec: ContractSpec, *, recompute: Callable[[ContractSpec], Mapping[str, float]] | None = None) -> dict:
        old = self.current.get(spec.instrument_id)
        self.history.append(spec)
        self.current[spec.instrument_id] = spec
        changed = [k for k in MATERIAL if old is not None and getattr(old, k) != getattr(spec, k)]
        out: dict = {"material_change": changed, "liquidation_distances": {}}
        if old is None:
            self.approved.pop(spec.instrument_id, None)  # a new instrument needs its first approval
        elif changed:
            self.approved.pop(spec.instrument_id, None)
            self.incidents.open_incident("CONTRACT_SPEC_CHANGED", "S2", f"instrument:{spec.instrument_id}",
                                         f"{', '.join(changed)} changed; entries blocked until re-approved")
            if recompute is not None:
                out["liquidation_distances"] = dict(recompute(spec))
        elif old.hash != spec.hash and self.approved.get(spec.instrument_id) == old.hash:
            self.approved[spec.instrument_id] = spec.hash  # tick/lot-only change: no re-approval needed
        return out

    def approve(self, instrument_id: str, approval: VerifiedApproval) -> None:
        spec = self.current[instrument_id]
        if approval.approval["action"] != "CAPABILITY_APPROVE" or approval.approval["subject_hash"] != spec.hash:
            raise PermissionError("approval must be CAPABILITY_APPROVE for this exact spec")
        self.approved[instrument_id] = spec.hash
        self.incidents.resolve("CONTRACT_SPEC_CHANGED", f"instrument:{instrument_id}", "spec re-approved")

    def entries_allowed(self, instrument_id: str) -> tuple[bool, str]:
        spec = self.current.get(instrument_id)
        if spec is None:
            return False, "NO_CONTRACT_SPEC"
        if self.approved.get(instrument_id) != spec.hash:
            return False, "CONTRACT_SPEC_UNAPPROVED"
        return True, "OK"
