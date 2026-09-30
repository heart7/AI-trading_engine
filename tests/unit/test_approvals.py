from datetime import datetime, timedelta, timezone

import pytest

from engine.governance import approvals as ap
from engine.governance import sshsig
from engine.policy.loader import load_policy
from tests.helpers.authenticator import Authenticator

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def registry(*auths, signer="principal"):
    return ap.SignerRegistry.from_doc({"signers": [{"signer_id": signer, "keys": [
        {"key_id": f"k{i}", "public_key": a.public_line} for i, a in enumerate(auths)]}]})


def approval(auth, subject, *, action="POLICY_ACTIVATE", rationale="Paper-mode activation of v10.4.0",
             signer="principal", **sign_kw):
    stmt = ap.statement(action, subject, signer, rationale, NOW.isoformat())
    return dict(stmt, signature=auth.sign(ap.statement_bytes(stmt), ap.NAMESPACE, **sign_kw))


@pytest.mark.parametrize("kind", ["ed25519-sk", "ecdsa-sk"])
def test_hardware_signed_approval_activates_paper_policy(kind):
    pol, a = load_policy(), Authenticator(kind)
    act = ap.activate_policy(pol, [approval(a, pol.hash)], registry(a), now=NOW)
    assert act.policy_hash == pol.hash and act.mode == "PAPER"
    assert act.approvals[0].effective_at == NOW  # cooling_off_hours: 0


def test_backup_key_also_valid():
    pol, primary, backup = load_policy(), Authenticator(), Authenticator("ecdsa-sk")
    ap.activate_policy(pol, [approval(backup, pol.hash)], registry(primary, backup), now=NOW)


def test_signature_over_rationale_cannot_be_edited():
    pol, a = load_policy(), Authenticator()
    rec = approval(a, pol.hash)
    rec["rationale"] = "a different reason entirely"
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.verify_approval(rec, registry(a), expected_action="POLICY_ACTIVATE", expected_subject=pol.hash)
    assert e.value.reason == "SIGNATURE_INVALID"


def test_approval_for_other_policy_refused():
    pol, a = load_policy(), Authenticator()
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(pol, [approval(a, "1" * 64)], registry(a), now=NOW)
    assert e.value.reason == "WRONG_SUBJECT"


def test_unenrolled_key_refused():
    pol, a, other = load_policy(), Authenticator(), Authenticator()
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.activate_policy(pol, [approval(other, pol.hash)], registry(a), now=NOW)
    assert e.value.reason == "SIGNER_KEY_MISMATCH"


def test_counter_replay_refused():
    pol, a = load_policy(), Authenticator()
    counters = ap.CounterStore()
    ap.verify_approval(approval(a, pol.hash, counter=10), registry(a), expected_action="POLICY_ACTIVATE",
                       expected_subject=pol.hash, counters=counters)
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.verify_approval(approval(a, pol.hash, counter=9), registry(a), expected_action="POLICY_ACTIVATE",
                           expected_subject=pol.hash, counters=counters)
    assert e.value.reason == "AUTHENTICATOR_COUNTER_REPLAY"


def test_cooling_off_honoured_when_raised():
    pol, a = load_policy(), Authenticator()
    va = ap.verify_approval(approval(a, pol.hash), registry(a), expected_action="POLICY_ACTIVATE",
                            expected_subject=pol.hash, cooling_off_hours=24)
    assert va.effective_at == NOW + timedelta(hours=24)


def test_no_signers_enrolled_in_repo_yet():
    reg = ap.SignerRegistry.load()
    assert reg.enrolled_hardware_keys("principal") == []


def test_software_key_cannot_be_enrolled_as_hardware():
    pk = sshsig.parse_public_key_line(Authenticator("ed25519").public_line)
    assert not pk.hardware_backed


def test_cross_check_envelope_with_independent_sshsig_implementation():
    ext = pytest.importorskip("sshsig")
    a = Authenticator("ed25519")
    ext.check_signature(b"payload", a.sign(b"payload", "ns"), "ns")
