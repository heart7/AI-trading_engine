"""Software emulation of a FIDO2 authenticator producing OpenSSH `sk-` signatures.

Tests only. It builds byte-identical structures to `ssh-keygen -Y sign` with an sk key, so the
verifier is exercised end to end without a physical device.
"""
from __future__ import annotations

import base64
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from engine.governance import sshsig
from engine.governance.sshsig import ssh_string


def _mpint(n: int) -> bytes:
    b = n.to_bytes((n.bit_length() + 8) // 8, "big") if n else b""
    return ssh_string(b)


class Authenticator:
    def __init__(self, kind: str = "ed25519-sk", application: bytes = b"ssh:"):
        self.kind, self.application, self.counter = kind, application, 0
        if kind == "ed25519-sk":
            self.priv = Ed25519PrivateKey.generate()
            raw = self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.key_type = sshsig.SK_ED25519
            self.blob = ssh_string(self.key_type.encode()) + ssh_string(raw) + ssh_string(application)
        elif kind == "ecdsa-sk":
            self.priv = ec.generate_private_key(ec.SECP256R1())
            pt = self.priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            self.key_type = sshsig.SK_ECDSA
            self.blob = ssh_string(self.key_type.encode()) + ssh_string(b"nistp256") + ssh_string(pt) + ssh_string(application)
        elif kind == "ed25519":  # plain software key: must be refused for approvals
            self.priv = Ed25519PrivateKey.generate()
            raw = self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.key_type = sshsig.ED25519
            self.blob = ssh_string(self.key_type.encode()) + ssh_string(raw)
        else:
            raise ValueError(kind)

    @property
    def public_line(self) -> str:
        return f"{self.key_type} {base64.b64encode(self.blob).decode()} test"

    def sign(self, message: bytes, namespace: str, *, touch: bool = True, verified: bool = True,
             counter: int | None = None, hash_alg: str = "sha512") -> str:
        data = sshsig.signed_data(namespace, hash_alg, message)
        if self.kind == "ed25519":
            sig_blob = ssh_string(self.key_type.encode()) + ssh_string(self.priv.sign(data))
        else:
            self.counter = self.counter + 1 if counter is None else counter
            flags = (sshsig.FLAG_USER_PRESENT if touch else 0) | (sshsig.FLAG_USER_VERIFIED if verified else 0)
            msg = sshsig.sk_message(self.application, flags, self.counter, data)
            if self.kind == "ed25519-sk":
                raw = self.priv.sign(msg)
            else:
                r, s = decode_dss_signature(self.priv.sign(msg, ec.ECDSA(hashes.SHA256())))
                raw = _mpint(r) + _mpint(s)
            sig_blob = ssh_string(self.key_type.encode()) + ssh_string(raw) + bytes([flags]) + struct.pack(">I", self.counter)
        blob = (sshsig.MAGIC + struct.pack(">I", 1) + ssh_string(self.blob) + ssh_string(namespace.encode())
                + ssh_string(b"") + ssh_string(hash_alg.encode()) + ssh_string(sig_blob))
        return sshsig.armor(blob)
