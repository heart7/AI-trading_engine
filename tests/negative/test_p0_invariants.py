"""Negative tests for the invariants P0 owns: INV-01, INV-33, INV-34."""
import pytest
import yaml

from engine.common.schemas import SchemaError, validate
from engine.governance import approvals as ap
from engine.policy.loader import POLICY_DIR, load_policy, parse_policy
from tests.helpers.authenticator import Authenticator
from tests.unit.test_approvals import NOW, approval, registry

H = "a" * 64


def intent(**over):
    doc = {"intent_id": "i1", "instrument_id": "kraken-spot:BTC/USD", "bar_close": "2026-09-28T16:00:00Z",
           "sleeve": "A_long", "outcome": "REJECTED",
           "gate_ladder": [{"gate": "NO_BREAKOUT", "passed": False, "value": 0.0, "limit": 0.0}],
           "binding_gate": "NO_BREAKOUT", "claim_refs": ["c1"], "policy_hash": H, "code_version": "0.1.0",
           "created_at": "2026-09-28T16:01:30Z"}
    doc.update(over)
    return doc


@pytest.mark.invariant("INV-01")
def test_reporter_text_in_signal_intent_is_schema_rejected():
    validate("signal_intent", intent())
    with pytest.raises(SchemaError):
        validate("signal_intent", intent(reporter_note="BTC looks strong, the reporter suggests entering"))
    with pytest.raises(SchemaError):
        validate("signal_intent", intent(binding_gate="LLM_SAYS_BUY"))


@pytest.mark.invariant("INV-01")
def test_reporter_text_in_claim_payload_is_schema_rejected():
    claim = {"claim_id": "c1", "kind": "admissibility_claim", "class": "DERIVED", "snapshot_hash": H,
             "policy_hash": H, "code_version": "0.1.0", "created_at": "2026-09-28T16:01:00Z", "certified": True,
             "payload": {"pair": "BTC/USD", "bar_close": "2026-09-28T16:00:00Z", "admissible": True, "binding_reason": None}}
    validate("admissibility_claim", claim)
    claim["payload"]["narrative"] = "model thinks this is admissible"
    with pytest.raises(SchemaError):
        validate("admissibility_claim", claim)
    claim["payload"].pop("narrative")
    claim["class"] = "REPORTED"
    with pytest.raises(SchemaError):
        validate("admissibility_claim", claim)


@pytest.mark.invariant("INV-33")
def test_approval_without_hardware_reauth_refused():
    pol = load_policy()
    soft = Authenticator("ed25519")
    reg = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [
        {"key_id": "soft", "public_key": soft.public_line}]}]})
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(pol, [approval(soft, pol.hash)], reg, now=NOW)
    assert e.value.reason == "SIGNER_NOT_ENROLLED"  # software keys never count as enrolled approval keys


@pytest.mark.invariant("INV-33")
def test_approval_without_touch_refused():
    pol, a = load_policy(), Authenticator()
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(pol, [approval(a, pol.hash, touch=False)], registry(a), now=NOW)
    assert e.value.reason == "REAUTH_REQUIRED"


@pytest.mark.invariant("INV-33")
@pytest.mark.parametrize("rationale", ["", "   ", "ok"])
def test_approval_without_rationale_refused(rationale):
    pol, a = load_policy(), Authenticator()
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(pol, [approval(a, pol.hash, rationale=rationale)], registry(a), now=NOW)
    assert e.value.reason == "RATIONALE_REQUIRED"


@pytest.mark.invariant("INV-33")
def test_valid_approval_takes_effect_on_signing():
    pol, a = load_policy(), Authenticator()
    act = ap.activate_policy(pol, [approval(a, pol.hash)], registry(a), now=NOW)
    assert act.activated_at == NOW and act.approvals[0].effective_at == NOW


@pytest.mark.invariant("INV-34")
def test_signed_incoherent_policy_cannot_activate():
    raw = (POLICY_DIR / "policy-10.4.0.yaml").read_text().replace("k_stop: 2.0", "k_stop: 3.5")
    bad = parse_policy(raw)
    a = Authenticator()
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(bad, [approval(a, bad.hash)], registry(a), now=NOW)
    assert e.value.reason == "POLICY_INCOHERENT" and "C01" in e.value.detail


@pytest.mark.invariant("INV-34")
def test_paper_coherent_policy_cannot_activate_for_shadow_without_evidence():
    pol, a = load_policy(), Authenticator()
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(pol, [approval(a, pol.hash)], registry(a), mode="SHADOW", now=NOW)
    assert e.value.reason == "POLICY_INCOHERENT"
    assert set(e.value.detail.split(", ")) == {"C08", "C13", "C15"}


@pytest.mark.invariant("INV-34")
def test_schema_invalid_policy_cannot_load():
    raw = (POLICY_DIR / "policy-10.4.0.yaml").read_text().replace("margin_mode: ISOLATED", "margin_mode: CROSS")
    with pytest.raises(SchemaError):
        parse_policy(raw)
    assert yaml.safe_load(raw)["perps"]["margin_mode"] == "CROSS"
