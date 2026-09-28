"""Venue instances, key slots, permission probes and access records (spec §8.5.2-§8.6).

INV-02/36  A key with withdrawal permission is refused, and a re-probe that finds one suspends the venue (S1).
INV-35     Only a TRADE_ENABLED venue may receive an order; TRADE_ENABLED needs a signed approval.
INV-37     A read slot never holds a trade-capable key.
INV-38     `ccxt-generic` can never be TRADE_ENABLED.
INV-43     Trading needs an access record whose residence equals `declared_residence` and that holds the
           venue's own confirmation; an egress country other than the declared residence blocks all trading.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from engine.common.canonical import content_hash
from engine.common.incidents import IncidentLog
from engine.execution.registry import ConnectorRegistry
from engine.governance.approvals import VerifiedApproval
from engine.secrets.store import SecretRef, SecretStore, SecretValue

STATES = ("DRAFT", "CONNECTED_READ", "PAPER_ENABLED", "TRADE_ENABLED", "SUSPENDED", "REMOVED")
SPOT_SLOTS = ("read", "trade")
PERP_SLOTS = ("deriv_read", "deriv_trade")
READ_SLOTS = frozenset({"read", "deriv_read"})
# Entries on a venue stop while any of these is open for it (spec §8.3, §8.7).
VENUE_BLOCKING = frozenset({"PROTECTION_UNVERIFIED", "CLOCK_SKEW", "ORDER_STATE_MISMATCH", "COST_MODEL_DIVERGENCE",
                            "VENUE_PERMISSION_VIOLATION", "WS_RESYNC_PENDING", "CAPABILITY_CHANGED"})


class VenueRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PermissionProbe:
    read: bool
    trade: bool
    withdraw: bool
    deriv_trade: bool = False
    ip_restricted: bool = False
    trust: str = "VERIFIED"  # VERIFIED (from a venue endpoint) | ATTESTED (principal's statement + negative probes)
    evidence: str = ""

    @property
    def trade_capable(self) -> bool:
        return self.trade or self.deriv_trade


@dataclass(frozen=True)
class KeyMeta:
    slot: str
    ref: SecretRef
    created_at: datetime
    probe: PermissionProbe
    trust: str


@dataclass(frozen=True)
class AccessRecord:
    """Spec §8.6: the entity may lawfully use this product on this venue. Evidence items name what is on file."""

    venue: str
    product: str
    legal_entity: str
    residence: str  # ISO country of the account holder's residence, as stated to the venue
    kyc_status: str
    classification: str  # retail | elective_professional
    evidence: tuple[Mapping[str, str], ...]  # {"kind": "venue_confirmation" | "legal_memo", "ref": ...}
    expires_at: str

    def subject_hash(self) -> str:
        return content_hash(asdict(self))


@dataclass
class VenueInstance:
    instance_id: str
    connector_type: str
    label: str
    environment: str
    sub_account: str | None
    state: str = "DRAFT"
    keys: dict[str, KeyMeta] = field(default_factory=dict)
    access_records: dict[str, tuple[AccessRecord, VerifiedApproval]] = field(default_factory=dict)
    caps: dict[str, float] = field(default_factory=dict)
    ip_allowlist_configured: bool = False
    dead_man_armed: bool = False
    capability: dict[str, Any] | None = None
    capability_approved_hash: str | None = None
    conformance: Mapping[str, Any] | None = None
    trade_products: set[str] = field(default_factory=set)
    history: list[dict[str, Any]] = field(default_factory=list)  # append-only


@dataclass
class GeoGuard:
    """INV-43. The engine never routes through a VPN or proxy to appear elsewhere; if the observed egress country
    differs from the declared residence, every venue is blocked from trading until a human resolves it."""

    declared_residence: str
    observed: str | None = None

    def observe(self, country: str, incidents: IncidentLog) -> bool:
        self.observed = country.upper()
        if self.observed != self.declared_residence:
            incidents.open_incident("GEO_EGRESS_MISMATCH", "S1", "engine",
                                    f"egress {self.observed} != declared residence {self.declared_residence}")
            return False
        incidents.resolve("GEO_EGRESS_MISMATCH", "engine", f"egress {self.observed} matches")
        return True

    @property
    def trading_allowed(self) -> bool:
        return self.observed == self.declared_residence


def _utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def trade_request_subject(instance_id: str, connector_type: str, product: str, sleeve: str, policy_hash: str,
                          caps: Mapping[str, float]) -> str:
    """Hash the principal signs to move a venue to TRADE_ENABLED (action VENUE_TRADE_ENABLE)."""
    return content_hash({"instance_id": instance_id, "connector_type": connector_type, "product": product,
                         "sleeve": sleeve, "policy_hash": policy_hash, "caps": dict(caps)})


class VenueRegistry:
    def __init__(self, policy: Mapping[str, Any], secrets: SecretStore, *, connectors: ConnectorRegistry | None = None,
                 incidents: IncidentLog | None = None, now: Callable[[], datetime] | None = None):
        self.policy = policy
        self.venues_policy = policy["venues"]
        self.secrets = secrets
        self.connectors = connectors or ConnectorRegistry.shipped()
        self.incidents = incidents or IncidentLog()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.geo = GeoGuard(self.venues_policy["declared_residence"])
        self.instances: dict[str, VenueInstance] = {}

    # -- helpers ---------------------------------------------------------------------------------
    def _get(self, instance_id: str) -> VenueInstance:
        v = self.instances.get(instance_id)
        if v is None or v.state == "REMOVED":
            raise VenueRefused("UNKNOWN_VENUE", instance_id)
        return v

    def _log(self, v: VenueInstance, event: str, **detail: Any) -> None:
        v.history.append({"at": self.now().isoformat(), "event": event, "state": v.state, **detail})

    def _move(self, v: VenueInstance, to: str, **detail: Any) -> None:
        frm, v.state = v.state, to
        self._log(v, "TRANSITION", frm=frm, to=to, **detail)

    def _slots(self, v: VenueInstance) -> tuple[str, ...]:
        prods = self.connectors.get(v.connector_type).products
        return (SPOT_SLOTS if "spot" in prods else ()) + (PERP_SLOTS if "perp" in prods else ())

    def _trade_slot(self, product: str) -> str:
        return "trade" if product == "spot" else "deriv_trade"

    # -- lifecycle -------------------------------------------------------------------------------
    def create_draft(self, instance_id: str, connector_type: str, label: str, environment: str,
                     sub_account: str | None = None) -> VenueInstance:
        c = self.connectors.get(connector_type)
        if environment not in c.environments:
            raise VenueRefused("ENVIRONMENT_UNSUPPORTED", f"{connector_type} supports {c.environments}")
        if instance_id in self.instances:
            raise VenueRefused("DUPLICATE_INSTANCE", instance_id)
        v = VenueInstance(instance_id, connector_type, label, environment, sub_account)
        self.instances[instance_id] = v
        self._log(v, "CREATED", connector_type=connector_type, environment=environment)
        return v

    def add_key(self, instance_id: str, slot: str, fields: Mapping[str, SecretValue], probe: PermissionProbe,
                created_at: datetime | None = None) -> KeyMeta:
        """Keys go from the form to the secret store. Only metadata stays on the instance (never the key)."""
        v = self._get(instance_id)
        c = self.connectors.get(v.connector_type)
        if slot not in self._slots(v):
            raise VenueRefused("SLOT_UNSUPPORTED", f"{slot} not a slot of {v.connector_type}")
        if probe.withdraw:
            self.incidents.open_incident("WITHDRAWAL_KEY_REFUSED", "S2", f"venue:{instance_id}",
                                         f"{slot} key has withdrawal permission; refused, not stored")
            raise VenueRefused("WITHDRAWAL_PERMISSION", "the engine never holds withdrawal-scope credentials")
        if slot in READ_SLOTS and probe.trade_capable:
            raise VenueRefused("READ_SLOT_TRADE_CAPABLE", "a read slot needs a read-only key")
        if slot not in READ_SLOTS and not c.trade_capable:
            raise VenueRefused("CONNECTOR_NOT_TRADE_CAPABLE", f"{v.connector_type} is data and PAPER only")
        trust = "ATTESTED" if "ATTESTED" in (probe.trust, c.trust_cap) else "VERIFIED"
        ref = SecretRef(f"venues/{instance_id}/{slot}")
        self.secrets.put(ref, dict(fields))
        meta = KeyMeta(slot, ref, created_at or self.now(), probe, trust)
        v.keys[slot] = meta
        self._log(v, "KEY_STORED", slot=slot, trust=trust)
        return meta

    def connect_read(self, instance_id: str, *, clock_skew_ms: float, capability: Mapping[str, Any],
                     latency_ms: float) -> VenueInstance:
        v = self._get(instance_id)
        if v.state not in ("DRAFT", "SUSPENDED", "CONNECTED_READ"):
            raise VenueRefused("BAD_TRANSITION", f"{v.state} -> CONNECTED_READ")
        read_slot = next((s for s in self._slots(v) if s in READ_SLOTS and s in v.keys), None)
        if read_slot is None:
            raise VenueRefused("NO_READ_KEY", "store a read key first")
        if abs(clock_skew_ms) >= self.venues_policy["clock_skew_max_ms"]:
            raise VenueRefused("CLOCK_SKEW", f"{clock_skew_ms} ms")
        new_hash = content_hash(dict(capability))
        if v.capability is not None and content_hash(v.capability) != new_hash:
            v.capability_approved_hash = None  # any change is diffed and needs approval again
        v.capability = dict(capability)
        self._log(v, "HEALTH", status="OBSERVED", latency_ms=latency_ms, clock_skew_ms=clock_skew_ms)
        if v.state != "CONNECTED_READ":
            self._move(v, "CONNECTED_READ")
        return v

    def refresh_capability(self, instance_id: str, capability: Mapping[str, Any]) -> bool:
        """Daily capability pull (spec §8.5.4). Any change is kept for review and blocks entries until approved."""
        v = self._get(instance_id)
        changed = v.capability is not None and content_hash(v.capability) != content_hash(dict(capability))
        if changed:
            self._log(v, "CAPABILITY_DIFF", before=content_hash(v.capability), after=content_hash(dict(capability)))
            v.capability, v.capability_approved_hash = dict(capability), None
            self.incidents.open_incident("CAPABILITY_CHANGED", "S3", f"venue:{instance_id}", "review the capability diff")
        return changed

    def approve_capability(self, instance_id: str, approval: VerifiedApproval) -> None:
        v = self._get(instance_id)
        if v.capability is None:
            raise VenueRefused("NO_CAPABILITY_SNAPSHOT")
        h = content_hash(v.capability)
        self._require(approval, "CAPABILITY_APPROVE", h)
        v.capability_approved_hash = h
        self.incidents.resolve("CAPABILITY_CHANGED", f"venue:{instance_id}", "capability approved")
        self._log(v, "CAPABILITY_APPROVED", hash=h)

    def enable_paper(self, instance_id: str, conformance: Mapping[str, Any]) -> VenueInstance:
        v = self._get(instance_id)
        c = self.connectors.get(v.connector_type)
        if v.state != "CONNECTED_READ":
            raise VenueRefused("BAD_TRANSITION", f"{v.state} -> PAPER_ENABLED")
        if conformance.get("verdict") != "PASS":
            raise VenueRefused("CONFORMANCE_NOT_PASSED")
        if conformance.get("connector_type") != v.connector_type or conformance.get("connector_version") != c.version:
            raise VenueRefused("CONFORMANCE_STALE", "suite must pass for this connector version")
        if conformance.get("environment") not in ("testnet", "demo"):
            raise VenueRefused("CONFORMANCE_ENVIRONMENT", "the suite must pass on the venue's testnet or demo")
        if not v.capability or v.capability_approved_hash != content_hash(v.capability):
            raise VenueRefused("CAPABILITY_NOT_APPROVED")
        v.conformance = dict(conformance)
        self._move(v, "PAPER_ENABLED", conformance_run=conformance.get("run_id"))
        return v

    def access_record_valid(self, v: VenueInstance, product: str) -> tuple[bool, str]:
        entry = v.access_records.get(product)
        if not entry:
            return False, "no access record"
        rec, _ = entry
        if _utc(rec.expires_at) <= self.now():
            return False, "access record expired"
        if rec.residence.upper() != self.venues_policy["declared_residence"]:
            return False, f"residence {rec.residence} != declared {self.venues_policy['declared_residence']}"
        if not any(e.get("kind") == "venue_confirmation" for e in rec.evidence):
            return False, "no written confirmation from the venue on file"
        return True, "ok"

    def attach_access_record(self, instance_id: str, rec: AccessRecord, approval: VerifiedApproval) -> None:
        v = self._get(instance_id)
        self._require(approval, "ACCESS_RECORD", rec.subject_hash())
        max_days = self.venues_policy["access_record_max_days"]
        if _utc(rec.expires_at) > self.now() + timedelta(days=max_days):
            raise VenueRefused("ACCESS_RECORD_TOO_LONG", f"expiry must be within {max_days} days")
        if rec.residence.upper() != self.venues_policy["declared_residence"]:
            raise VenueRefused("RESIDENCE_MISMATCH", "an access record must state the declared residence (INV-43)")
        v.access_records[rec.product] = (rec, approval)
        self._log(v, "ACCESS_RECORD", product=rec.product, expires_at=rec.expires_at)

    def key_age_days(self, meta: KeyMeta) -> float:
        return (self.now() - meta.created_at).total_seconds() / 86400

    def enable_trade(self, instance_id: str, *, product: str, sleeve: str, policy_hash: str,
                     approval: VerifiedApproval) -> VenueInstance:
        v = self._get(instance_id)
        c = self.connectors.get(v.connector_type)
        if not c.trade_capable:
            raise VenueRefused("CONNECTOR_NEVER_TRADES", f"{v.connector_type} can never be TRADE_ENABLED")
        if v.state not in ("PAPER_ENABLED", "TRADE_ENABLED"):
            raise VenueRefused("BAD_TRANSITION", f"{v.state} -> TRADE_ENABLED")
        if product not in c.products:
            raise VenueRefused("PRODUCT_UNSUPPORTED", product)
        if v.connector_type not in self.venues_policy["execution_allowed"].get(sleeve, []):
            raise VenueRefused("NOT_IN_EXECUTION_ALLOWED", f"{v.connector_type} not allowed for {sleeve} by policy")
        if v.environment != "live":
            raise VenueRefused("ENVIRONMENT_NOT_LIVE", "testnet and demo instances stay PAPER_ENABLED")
        meta = v.keys.get(self._trade_slot(product))
        if meta is None or not (meta.probe.trade if product == "spot" else meta.probe.deriv_trade):
            raise VenueRefused("NO_TRADE_KEY")
        if self.key_age_days(meta) >= self.venues_policy["key_rotation_days"]["block_trade"]:
            raise VenueRefused("KEY_TOO_OLD", "rotate the trade key")
        ok, why = self.access_record_valid(v, product)
        if not ok:
            raise VenueRefused("ACCESS_RECORD_INVALID", why)
        if not v.caps:
            raise VenueRefused("CAPS_NOT_SET")
        if not v.ip_allowlist_configured:
            raise VenueRefused("IP_ALLOWLIST_MISSING")
        # "Where supported": the venue has a timer AND it spares resting stops (spec §8.7; otherwise it is not used).
        if c.dead_man_switch and (v.capability or {}).get("dead_man_spares_stops") and not v.dead_man_armed:
            raise VenueRefused("DEAD_MAN_NOT_ARMED")
        if not self.geo.trading_allowed:
            raise VenueRefused("GEO_EGRESS_MISMATCH", "egress country not confirmed as the declared residence")
        self._require(approval, "VENUE_TRADE_ENABLE",
                      trade_request_subject(instance_id, v.connector_type, product, sleeve, policy_hash, v.caps))
        v.trade_products.add(product)
        if v.state != "TRADE_ENABLED":
            self._move(v, "TRADE_ENABLED", product=product, sleeve=sleeve, policy_hash=policy_hash)
        return v

    def suspend(self, instance_id: str, reason: str, *, code: str = "VENUE_SUSPENDED", severity: str = "S2") -> None:
        v = self._get(instance_id)
        self.incidents.open_incident(code, severity, f"venue:{instance_id}", reason)
        if v.state in ("CONNECTED_READ", "PAPER_ENABLED", "TRADE_ENABLED"):
            v.trade_products.clear()
            self._move(v, "SUSPENDED", reason=reason)

    def reprobe(self, instance_id: str, slot: str, probe: PermissionProbe) -> None:
        """Daily re-probe (spec §8.5.3). A key that gained withdrawal or trade rights on a read slot suspends the venue."""
        v = self._get(instance_id)
        if probe.withdraw:
            self.suspend(instance_id, f"{slot} key now has withdrawal permission", code="VENUE_PERMISSION_VIOLATION",
                         severity="S1")
        elif slot in READ_SLOTS and probe.trade_capable:
            self.suspend(instance_id, f"{slot} key is now trade-capable", code="VENUE_PERMISSION_VIOLATION",
                         severity="S1")
        self._log(v, "REPROBE", slot=slot, withdraw=probe.withdraw, trade=probe.trade_capable)

    def daily_checks(self) -> None:
        for v in list(self.instances.values()):
            if v.state == "TRADE_ENABLED":
                for product in list(v.trade_products):
                    ok, why = self.access_record_valid(v, product)
                    if not ok:
                        self.suspend(v.instance_id, f"{product}: {why}", code="ACCESS_RECORD_EXPIRED")
            for meta in v.keys.values():
                if self.key_age_days(meta) >= self.venues_policy["key_rotation_days"]["remind"]:
                    self._log(v, "KEY_ROTATION_REMINDER", slot=meta.slot)

    def remove(self, instance_id: str, approval: VerifiedApproval, *, open_positions: int, open_orders: int,
               balance_usd: float, dust_usd: float = 5.0) -> None:
        v = self._get(instance_id)
        if open_positions or open_orders or balance_usd >= dust_usd:
            raise VenueRefused("NOT_EMPTY", "close positions, cancel orders and move balances first")
        self._require(approval, "VENUE_REMOVE", content_hash({"instance_id": instance_id}))
        for meta in v.keys.values():
            self.secrets.delete(meta.ref)
        self._move(v, "REMOVED")

    # -- queries used by the OMS and router ------------------------------------------------------
    def can_trade(self, instance_id: str, product: str) -> tuple[bool, str]:
        v = self.instances.get(instance_id)
        if v is None or v.state != "TRADE_ENABLED" or product not in v.trade_products:
            return False, "VENUE_NOT_TRADE_ENABLED"
        if not self.geo.trading_allowed:
            return False, "GEO_EGRESS_MISMATCH"
        ok, _ = self.access_record_valid(v, product)
        if not ok:
            return False, "VENUE_NOT_TRADE_ENABLED"
        return True, "OK"

    def entries_blocked(self, instance_id: str) -> set[str]:
        return self.incidents.open_codes(f"venue:{instance_id}") & VENUE_BLOCKING

    def execution_venues(self, sleeve: str, product: str = "spot") -> list[str]:
        allowed = set(self.venues_policy["execution_allowed"].get(sleeve, []))
        return [v.instance_id for v in self.instances.values()
                if v.connector_type in allowed and self.can_trade(v.instance_id, product)[0]]

    def data_venues(self) -> list[str]:
        return [v.instance_id for v in self.instances.values()
                if v.state in ("CONNECTED_READ", "PAPER_ENABLED", "TRADE_ENABLED")]

    @staticmethod
    def _require(approval: VerifiedApproval, action: str, subject: str) -> None:
        if not isinstance(approval, VerifiedApproval):
            raise VenueRefused("APPROVAL_REQUIRED", "pass a VerifiedApproval from verify_approval()")
        if approval.approval["action"] != action or approval.approval["subject_hash"] != subject:
            raise VenueRefused("APPROVAL_MISMATCH", f"need {action} signed for this exact request")
