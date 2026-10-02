"""Software emulation of a platform passkey producing WebAuthn assertions. Tests only."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from engine.governance import webauthn
from engine.governance.webauthn import b64url

ORIGIN, RP_ID = "https://heart7.github.io", "heart7.github.io"


class Passkey:
    def __init__(self, alg: int = webauthn.ES256, origin: str = ORIGIN, rp_id: str = RP_ID):
        self.alg, self.origin, self.rp_id = alg, origin, rp_id
        self.priv = ec.generate_private_key(ec.SECP256R1()) if alg == webauthn.ES256 else rsa.generate_private_key(65537, 2048)
        self.credential_id = b64url(os.urandom(16))
        self.counter = 0

    def enrolment(self, key_id: str = "phone") -> dict:
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"key_id": key_id, "type": "passkey", "credential_id": self.credential_id,
                "public_key_spki": base64.b64encode(spki).decode(), "alg": self.alg, "rp_id": self.rp_id, "origin": self.origin}

    def sign(self, message: bytes, *, present: bool = True, verified: bool = True, origin: str | None = None,
             rp_id: str | None = None, counter: int | None = None, kind: str = "webauthn.get") -> str:
        self.counter = self.counter + 1 if counter is None else counter
        flags = (webauthn.FLAG_USER_PRESENT if present else 0) | (webauthn.FLAG_USER_VERIFIED if verified else 0)
        auth = hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter)
        cdj = json.dumps({"type": kind, "challenge": webauthn.challenge_for(message), "origin": origin or self.origin,
                          "crossOrigin": False}).encode()
        data = auth + hashlib.sha256(cdj).digest()
        if self.alg == webauthn.ES256:
            sig = self.priv.sign(data, ec.ECDSA(hashes.SHA256()))
        else:
            sig = self.priv.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return json.dumps({"type": "passkey", "credential_id": self.credential_id, "authenticator_data": b64url(auth),
                           "client_data_json": b64url(cdj), "signature": b64url(sig)})
