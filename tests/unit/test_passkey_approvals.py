import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.governance import approvals as ap
from engine.governance import webauthn
from engine.policy.loader import load_policy
from tests.helpers.authenticator import Authenticator
from tests.helpers.passkey import Passkey

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
PAGE = Path(__file__).resolve().parents[2] / "docs" / "passkey" / "index.html"


def registry(*keys):
    docs = []
    for i, k in enumerate(keys):
        docs.append(k.enrolment(f"pk{i}") if isinstance(k, Passkey) else {"key_id": f"hw{i}", "public_key": k.public_line})
    return ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": docs}]})


def approval(pk, subject, rationale="Paper-mode activation of v10.4.0", **kw):
    stmt = ap.statement("POLICY_ACTIVATE", subject, "principal", rationale, NOW.isoformat())
    return dict(stmt, signature=pk.sign(ap.statement_bytes(stmt), **kw))


def refused(rec, reg, subject):
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.verify_approval(rec, reg, expected_action="POLICY_ACTIVATE", expected_subject=subject)
    return e.value.reason


@pytest.mark.parametrize("alg", [webauthn.ES256, webauthn.RS256])
def test_passkey_approval_activates_paper_policy(alg):
    pol, pk = load_policy(), Passkey(alg)
    act = ap.activate_policy(pol, [approval(pk, pol.hash)], registry(pk), now=NOW)
    assert act.mode == "PAPER" and act.approvals[0].key_id == "pk0" and act.approvals[0].user_verified


def test_passkey_and_hardware_key_can_be_enrolled_together():
    pol, pk, hw = load_policy(), Passkey(), Authenticator()
    reg = registry(pk, hw)
    ap.activate_policy(pol, [approval(pk, pol.hash)], reg, now=NOW)
    stmt = ap.statement("POLICY_ACTIVATE", pol.hash, "principal", "Paper-mode activation of v10.4.0", NOW.isoformat())
    ap.activate_policy(pol, [dict(stmt, signature=hw.sign(ap.statement_bytes(stmt), ap.NAMESPACE))], reg, now=NOW)


def test_edited_rationale_breaks_the_passkey_signature():
    pol, pk = load_policy(), Passkey()
    rec = approval(pk, pol.hash)
    rec["rationale"] = "a different reason entirely"
    assert refused(rec, registry(pk), pol.hash) == "SIGNATURE_INVALID"


def test_biometric_or_pin_is_required_every_time():
    pol, pk = load_policy(), Passkey()
    assert refused(approval(pk, pol.hash, verified=False), registry(pk), pol.hash) == "REAUTH_REQUIRED"
    assert refused(approval(pk, pol.hash, present=False), registry(pk), pol.hash) == "REAUTH_REQUIRED"


def test_other_site_or_rp_or_registration_refused():
    pol, pk = load_policy(), Passkey()
    assert refused(approval(pk, pol.hash, origin="https://evil.example"), registry(pk), pol.hash) == "SIGNATURE_INVALID"
    assert refused(approval(pk, pol.hash, rp_id="evil.example"), registry(pk), pol.hash) == "SIGNATURE_INVALID"
    assert refused(approval(pk, pol.hash, kind="webauthn.create"), registry(pk), pol.hash) == "SIGNATURE_INVALID"


def test_unenrolled_passkey_refused():
    pol, pk, other = load_policy(), Passkey(), Passkey()
    assert refused(approval(other, pol.hash), registry(pk), pol.hash) == "SIGNER_KEY_MISMATCH"


def test_revoked_passkey_refused():
    pol, pk = load_policy(), Passkey()
    doc = pk.enrolment()
    doc["status"] = "REVOKED"
    reg = ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [doc]}]})
    assert refused(approval(pk, pol.hash), reg, pol.hash) == "SIGNER_NOT_ENROLLED"


def test_counter_replay_refused_but_synced_zero_counter_allowed():
    pol, pk = load_policy(), Passkey()
    reg, store = registry(pk), ap.CounterStore()
    for c in (0, 0):  # synced passkeys (iCloud, Google) always report 0
        ap.verify_approval(approval(pk, pol.hash, counter=c), reg, expected_action="POLICY_ACTIVATE",
                           expected_subject=pol.hash, counters=store)
    ap.verify_approval(approval(pk, pol.hash, counter=5), reg, expected_action="POLICY_ACTIVATE",
                       expected_subject=pol.hash, counters=store)
    with pytest.raises(ap.ApprovalRefused) as e:
        ap.verify_approval(approval(pk, pol.hash, counter=5), reg, expected_action="POLICY_ACTIVATE",
                           expected_subject=pol.hash, counters=store)
    assert e.value.reason == "AUTHENTICATOR_COUNTER_REPLAY"


def test_malformed_passkey_enrolment_refused():
    doc = Passkey().enrolment()
    doc["public_key_spki"] = "bm90IGEga2V5"
    with pytest.raises(ValueError):
        ap.SignerRegistry.from_doc({"signers": [{"signer_id": "principal", "keys": [doc]}]})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_canonical_form_matches_python():
    """The page hashes the statement itself, so its canonical JSON must be byte-identical to the engine's."""
    js = re.search(r"const canonical = (.*?);\n", PAGE.read_text(), re.S).group(1)
    stmt = ap.statement("POLICY_ACTIVATE", "a" * 64, "principal", 'Reason with "quotes", é, ü, \\ and a\nnewline',
                        NOW.isoformat())
    out = subprocess.run(["node", "-e", f"const canonical = {js}; process.stdout.write(canonical(JSON.parse(process.argv[1])))",
                          json.dumps(stmt)], capture_output=True, text=True, check=True).stdout
    assert out.encode() == ap.statement_bytes(stmt)
