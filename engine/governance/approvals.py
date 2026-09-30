"""Single-signer approvals and policy activation (spec §0.4, INV-33, INV-34).

An approval is a statement {action, subject_hash, signer_id, rationale, signed_at, namespace}
serialised canonically and signed by an enrolled hardware key. The rationale is inside the
signed bytes, so it cannot be edited after signing. Approvals take effect on signing
(`cooling_off_hours: 0`); the field is honoured if it is ever raised.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from engine.common.canonical import canonical_json
from engine.common.schemas import SchemaError, validate
from engine.governance import sshsig
from engine.policy.coherence import CoherenceResult, EvidenceContext, check_policy
from engine.policy.loader import Policy

NAMESPACE = "uchfe-approval@v1"
MIN_RATIONALE_CHARS = 10
SIGNERS_FILE = Path(__file__).resolve().parents[2] / "policy" / "signers" / "signers.json"


class ApprovalRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class EnrolledKey:
    key_id: str
    public_key: sshsig.PublicKey
    status: str  # ACTIVE | REVOKED


@dataclass(frozen=True)
class Signer:
    signer_id: str
    role: str
    keys: tuple[EnrolledKey, ...]


@dataclass
class SignerRegistry:
    signers: dict[str, Signer]

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> SignerRegistry:
        out: dict[str, Signer] = {}
        for s in doc.get("signers", []):
            keys = tuple(EnrolledKey(k["key_id"], sshsig.parse_public_key_line(k["public_key"]), k.get("status", "ACTIVE"))
                         for k in s.get("keys", []))
            out[s["signer_id"]] = Signer(s["signer_id"], s.get("role", "principal"), keys)
        return cls(out)

    @classmethod
    def load(cls, path: Path = SIGNERS_FILE) -> SignerRegistry:
        return cls.from_doc(json.loads(path.read_text()))

    def enrolled_hardware_keys(self, signer_id: str) -> list[EnrolledKey]:
        s = self.signers.get(signer_id)
        return [k for k in (s.keys if s else ()) if k.status == "ACTIVE" and k.public_key.hardware_backed]


@dataclass
class CounterStore:
    """Last-seen authenticator counter per key; a counter that does not increase suggests a cloned key."""

    last: dict[str, int] = field(default_factory=dict)

    def check_and_record(self, fingerprint: str, counter: int | None) -> None:
        if counter is None:
            return
        prev = self.last.get(fingerprint)
        if prev is not None and counter != 0 and counter <= prev:
            raise ApprovalRefused("AUTHENTICATOR_COUNTER_REPLAY", f"counter {counter} <= last seen {prev}")
        self.last[fingerprint] = counter


def statement(action: str, subject_hash: str, signer_id: str, rationale: str, signed_at: str) -> dict[str, str]:
    return {"action": action, "subject_hash": subject_hash, "signer_id": signer_id,
            "rationale": rationale, "signed_at": signed_at, "namespace": NAMESPACE}


def statement_bytes(stmt: Mapping[str, str]) -> bytes:
    return canonical_json(dict(stmt)).encode("utf-8")


@dataclass(frozen=True)
class VerifiedApproval:
    approval: Mapping[str, str]
    key_id: str
    fingerprint: str
    user_verified: bool
    effective_at: datetime


def verify_approval(approval: Mapping[str, str], registry: SignerRegistry, *, expected_action: str,
                    expected_subject: str, cooling_off_hours: float = 0, counters: CounterStore | None = None,
                    rationale_required: bool = True) -> VerifiedApproval:
    try:
        validate("approval", dict(approval))
    except SchemaError as e:
        # Missing or short rationale lands here as well as malformed records.
        if "rationale" in str(e):
            raise ApprovalRefused("RATIONALE_REQUIRED", str(e)) from e
        raise ApprovalRefused("MALFORMED_APPROVAL", str(e)) from e
    if rationale_required and len(approval["rationale"].strip()) < MIN_RATIONALE_CHARS:
        raise ApprovalRefused("RATIONALE_REQUIRED", "rationale is blank")
    if approval["action"] != expected_action:
        raise ApprovalRefused("WRONG_ACTION", f"{approval['action']} != {expected_action}")
    if approval["subject_hash"] != expected_subject:
        raise ApprovalRefused("WRONG_SUBJECT", "approval was signed for a different object")

    keys = registry.enrolled_hardware_keys(approval["signer_id"])
    if not keys:
        raise ApprovalRefused("SIGNER_NOT_ENROLLED", f"no active hardware key for {approval['signer_id']}")

    msg = statement_bytes({k: approval[k] for k in ("action", "subject_hash", "signer_id", "rationale", "signed_at", "namespace")})
    try:
        vs = sshsig.verify(approval["signature"], msg, NAMESPACE)
    except sshsig.SignatureInvalid as e:
        raise ApprovalRefused("SIGNATURE_INVALID", str(e)) from e
    if not vs.public_key.hardware_backed:
        raise ApprovalRefused("REAUTH_REQUIRED", "signature is not from a hardware (sk-) key")
    match = [k for k in keys if k.public_key.blob == vs.public_key.blob]
    if not match:
        raise ApprovalRefused("SIGNER_KEY_MISMATCH", "signing key is not enrolled for this signer")
    if not vs.user_present:
        raise ApprovalRefused("REAUTH_REQUIRED", "authenticator did not report a user-presence touch")
    (counters or CounterStore()).check_and_record(vs.public_key.fingerprint, vs.counter)

    signed_at = datetime.fromisoformat(approval["signed_at"].replace("Z", "+00:00"))
    return VerifiedApproval(approval, match[0].key_id, vs.public_key.fingerprint, vs.user_verified,
                            signed_at + timedelta(hours=cooling_off_hours))


@dataclass(frozen=True)
class Activation:
    policy_hash: str
    policy_version: str
    mode: str
    coherence: CoherenceResult
    approvals: tuple[VerifiedApproval, ...]
    activated_at: datetime


def activate_policy(policy: Policy, approvals: Iterable[Mapping[str, str]], registry: SignerRegistry, *,
                    mode: str = "PAPER", ctx: EvidenceContext | None = None, counters: CounterStore | None = None,
                    now: datetime | None = None) -> Activation:
    """Activate a policy. Machine checks first: a signature cannot activate an incoherent policy (INV-34)."""
    coh = check_policy(policy, ctx)
    if not coh.coherent_for(mode):
        raise ApprovalRefused("POLICY_INCOHERENT", ", ".join(c.id for c in coh.failures(mode)))
    gov = policy.get("governance.approvals")
    verified: list[VerifiedApproval] = []
    seen: set[str] = set()
    for a in approvals:
        va = verify_approval(a, registry, expected_action="POLICY_ACTIVATE", expected_subject=policy.hash,
                             cooling_off_hours=gov["cooling_off_hours"], counters=counters,
                             rationale_required=gov["rationale_required"])
        if va.approval["signer_id"] not in seen:
            seen.add(va.approval["signer_id"])
            verified.append(va)
    if len(verified) < gov["signers_required"]:
        raise ApprovalRefused("INSUFFICIENT_SIGNERS", f"{len(verified)} of {gov['signers_required']}")
    now = now or datetime.now(timezone.utc)
    pending = [v for v in verified if v.effective_at > now]
    if pending:
        raise ApprovalRefused("COOLING_OFF", f"effective at {max(v.effective_at for v in pending).isoformat()}")
    return Activation(policy.hash, policy.version, mode, coh, tuple(verified), now)
