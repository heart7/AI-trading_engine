"""Test helpers for the execution plane: a principal with an emulated hardware key, and a venue walked
through its lifecycle. No real keys: every secret here is a made-up placeholder."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.common.canonical import content_hash
from engine.execution.adapters.kraken_spot import ERRORS
from engine.execution.conformance import run_suite
from engine.execution.venues import AccessRecord, PermissionProbe, VenueRegistry, trade_request_subject
from engine.governance import approvals as ap
from engine.secrets.store import InMemorySecretStore, SecretValue
from tests.helpers.authenticator import Authenticator
from tests.helpers.fixtures import policy

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
READ = PermissionProbe(read=True, trade=False, withdraw=False, trust="ATTESTED")
TRADE = PermissionProbe(read=True, trade=True, withdraw=False, trust="ATTESTED")
FAKE = {"api_key": SecretValue("placeholder-key"), "api_secret": SecretValue("cGxhY2Vob2xkZXI=")}
CAPABILITY = {"instruments": {"BTC-USD": {"lot": 0.0001, "tick": 0.1, "min_notional": 5}}, "order_types":
              ["post-only", "IOC", "stop-loss"], "dead_man_spares_stops": True}


class Principal:
    def __init__(self) -> None:
        self.auth = Authenticator()
        self.registry = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [
            {"key_id": "k0", "public_key": self.auth.public_line}]}]})

    def approve(self, action: str, subject: str, rationale: str = "Reviewed and approved in test") -> ap.VerifiedApproval:
        stmt = ap.statement(action, subject, "principal", rationale, NOW.isoformat())
        rec = dict(stmt, signature=self.auth.sign(ap.statement_bytes(stmt), ap.NAMESPACE))
        return ap.verify_approval(rec, self.registry, expected_action=action, expected_subject=subject)


def access_record(**kw) -> AccessRecord:
    base = dict(venue="kraken", product="spot", legal_entity="principal", residence="GB", kyc_status="verified",
                classification="retail", evidence=({"kind": "venue_confirmation", "ref": "evidence/kraken-uk.pdf"},),
                expires_at=(NOW + timedelta(days=300)).isoformat())
    base.update(kw)
    return AccessRecord(**base)


def registry(now: datetime = NOW) -> VenueRegistry:
    return VenueRegistry(policy(), InMemorySecretStore(), now=lambda: now)


def paper_enabled(reg: VenueRegistry, principal: Principal, instance_id: str = "kraken-main",
                  connector: str = "kraken-spot", environment: str = "live"):
    reg.create_draft(instance_id, connector, "Kraken main", environment)
    reg.add_key(instance_id, "read", FAKE, READ)
    reg.connect_read(instance_id, clock_skew_ms=40, capability=CAPABILITY, latency_ms=80)
    reg.approve_capability(instance_id, principal.approve("CAPABILITY_APPROVE", content_hash(CAPABILITY)))
    conf = dict(run_suite(connector, reg.connectors.get(connector).version, ERRORS, now=NOW), environment="demo")
    reg.enable_paper(instance_id, conf)
    return reg.instances[instance_id]


def trade_ready(reg: VenueRegistry, principal: Principal, instance_id: str = "kraken-main", policy_hash: str = "a" * 64):
    v = paper_enabled(reg, principal, instance_id)
    reg.add_key(instance_id, "trade", FAKE, TRADE)
    rec = access_record()
    reg.attach_access_record(instance_id, rec, principal.approve("ACCESS_RECORD", rec.subject_hash()))
    v.caps = {"exposure_max": 0.40}
    v.ip_allowlist_configured = True
    v.dead_man_armed = True
    reg.geo.observe("GB", reg.incidents)
    subject = trade_request_subject(instance_id, v.connector_type, "spot", "A_long", policy_hash, v.caps)
    return v, principal.approve("VENUE_TRADE_ENABLE", subject)
