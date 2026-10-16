"""Passkey (WebAuthn) approval signatures, verified in pure Python (spec §0.4 as revised 2 Oct 2026).

A passkey lives on the principal's phone or laptop and is unlocked with Face ID, Touch ID, Windows Hello or the
device PIN. The approval page (docs/passkey/index.html) asks the browser for an assertion whose challenge is the
SHA-256 of the canonical approval statement, so the signature covers the action, the subject hash and the written
rationale. The assertion record travels as the approval's `signature` string (compact JSON, `"type": "passkey"`).

Checks, in order: credential is enrolled, client data is a `webauthn.get` for exactly this challenge from the
enrolled origin, the authenticator data names the enrolled RP ID, user presence AND user verification flags are
set (a biometric or device PIN for every approval), then the ES256 or RS256 signature over
authenticatorData || SHA-256(clientDataJSON).
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

ES256, RS256 = -7, -257
FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04


class AssertionInvalid(ValueError):
    pass


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def challenge_for(message: bytes) -> str:
    """The WebAuthn challenge for an approval: base64url(SHA-256(canonical statement bytes))."""
    return b64url(hashlib.sha256(message).digest())


@dataclass(frozen=True)
class PasskeyCredential:
    credential_id: str  # base64url
    public_key_spki: bytes  # DER SubjectPublicKeyInfo, from AuthenticatorAttestationResponse.getPublicKey()
    alg: int
    rp_id: str
    origin: str

    @classmethod
    def from_doc(cls, d: Mapping[str, Any]) -> PasskeyCredential:
        alg = int(d.get("alg", ES256))
        if alg not in (ES256, RS256):
            raise AssertionInvalid(f"unsupported passkey algorithm {alg}")
        spki = base64.b64decode(d["public_key_spki"])
        serialization.load_der_public_key(spki)  # refuse a malformed key at enrolment, not at signing
        return cls(d["credential_id"], spki, alg, d["rp_id"], d["origin"])

    @property
    def fingerprint(self) -> str:
        return "SHA256:" + base64.b64encode(hashlib.sha256(self.public_key_spki).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class VerifiedAssertion:
    credential: PasskeyCredential
    flags: int
    counter: int

    @property
    def user_present(self) -> bool:
        return bool(self.flags & FLAG_USER_PRESENT)

    @property
    def user_verified(self) -> bool:
        return bool(self.flags & FLAG_USER_VERIFIED)


def is_passkey_signature(signature: str) -> bool:
    return signature.lstrip().startswith("{")


def parse_signature(signature: str) -> dict[str, str]:
    try:
        d = json.loads(signature)
    except ValueError as e:
        raise AssertionInvalid("passkey signature is not JSON") from e
    if d.get("type") != "passkey":
        raise AssertionInvalid("not a passkey signature")
    for k in ("credential_id", "authenticator_data", "client_data_json", "signature"):
        if not isinstance(d.get(k), str):
            raise AssertionInvalid(f"passkey signature missing {k}")
    return d


def verify(signature: str, message: bytes, credentials: Mapping[str, PasskeyCredential]) -> VerifiedAssertion:
    """Verify a passkey assertion over `message` against enrolled credentials keyed by credential_id."""
    d = parse_signature(signature)
    cred = credentials.get(d["credential_id"])
    if cred is None:
        raise AssertionInvalid("credential is not enrolled")
    auth_data, cdj, sig = unb64url(d["authenticator_data"]), unb64url(d["client_data_json"]), unb64url(d["signature"])
    try:
        client = json.loads(cdj)
    except ValueError as e:
        raise AssertionInvalid("client data is not JSON") from e
    if client.get("type") != "webauthn.get":
        raise AssertionInvalid("client data is not an assertion")
    if client.get("challenge") != challenge_for(message):
        raise AssertionInvalid("assertion was made for a different statement")
    if client.get("origin") != cred.origin:
        raise AssertionInvalid(f"origin {client.get('origin')!r} is not the enrolled origin")
    if len(auth_data) < 37:
        raise AssertionInvalid("authenticator data too short")
    if auth_data[:32] != hashlib.sha256(cred.rp_id.encode()).digest():
        raise AssertionInvalid("authenticator data names a different relying party")
    flags, counter = auth_data[32], struct.unpack(">I", auth_data[33:37])[0]
    signed = auth_data + hashlib.sha256(cdj).digest()
    pub = serialization.load_der_public_key(cred.public_key_spki)
    try:
        if cred.alg == ES256 and isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        elif cred.alg == RS256 and isinstance(pub, rsa.RSAPublicKey):
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise AssertionInvalid("key type does not match its algorithm")
    except InvalidSignature as e:
        raise AssertionInvalid("signature does not verify") from e
    return VerifiedAssertion(cred, flags, counter)
