"""OpenSSH SSHSIG signature verification (PROTOCOL.sshsig), in pure Python.

The principal signs approvals with a FIDO2 hardware key through the standard OpenSSH tool:

    ssh-keygen -Y sign -f ~/.ssh/id_ed25519_sk -n uchfe-approval@v1 statement.json

Each signature from an `sk-` key carries authenticator flags. The user-presence flag (0x01)
proves a physical touch for that signature, which is how spec §0.4 item 1 ("fresh
hardware-key touch") is enforced for approvals made outside the browser (CLI, out-of-band
kill, §8.8). The web UI (P5) will use WebAuthn directly; it produces the same flags.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

MAGIC = b"SSHSIG"
ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
ARMOR_END = "-----END SSH SIGNATURE-----"

ED25519 = "ssh-ed25519"
SK_ED25519 = "sk-ssh-ed25519@openssh.com"
SK_ECDSA = "sk-ecdsa-sha2-nistp256@openssh.com"
HARDWARE_KEY_TYPES = frozenset({SK_ED25519, SK_ECDSA})

FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04


class SignatureInvalid(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise SignatureInvalid("truncated SSH structure")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def string(self) -> bytes:
        return self.take(self.u32())

    def done(self) -> bool:
        return self.pos == len(self.data)


def ssh_string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


@dataclass(frozen=True)
class PublicKey:
    key_type: str
    blob: bytes
    key: bytes  # raw ed25519 key or uncompressed EC point
    application: bytes | None

    @property
    def hardware_backed(self) -> bool:
        return self.key_type in HARDWARE_KEY_TYPES

    @property
    def fingerprint(self) -> str:
        return "SHA256:" + base64.b64encode(hashlib.sha256(self.blob).digest()).decode().rstrip("=")


def parse_public_key_blob(blob: bytes) -> PublicKey:
    r = _Reader(blob)
    kt = r.string().decode()
    if kt == ED25519:
        key, app = r.string(), None
    elif kt == SK_ED25519:
        key, app = r.string(), r.string()
    elif kt == SK_ECDSA:
        curve = r.string()
        if curve != b"nistp256":
            raise SignatureInvalid("unsupported curve")
        key, app = r.string(), r.string()
    else:
        raise SignatureInvalid(f"unsupported key type {kt}")
    if not r.done():
        raise SignatureInvalid("trailing bytes in public key")
    return PublicKey(kt, blob, key, app)


def parse_public_key_line(line: str) -> PublicKey:
    """Parse an OpenSSH public key line: '<type> <base64> [comment]'."""
    parts = line.strip().split()
    if len(parts) < 2:
        raise SignatureInvalid("malformed public key line")
    pk = parse_public_key_blob(base64.b64decode(parts[1]))
    if pk.key_type != parts[0]:
        raise SignatureInvalid("key type mismatch")
    return pk


@dataclass(frozen=True)
class VerifiedSignature:
    public_key: PublicKey
    namespace: str
    flags: int | None
    counter: int | None

    @property
    def user_present(self) -> bool:
        return self.flags is not None and bool(self.flags & FLAG_USER_PRESENT)

    @property
    def user_verified(self) -> bool:
        return self.flags is not None and bool(self.flags & FLAG_USER_VERIFIED)


def dearmor(text: str) -> bytes:
    t = text.strip()
    if not (t.startswith(ARMOR_BEGIN) and t.endswith(ARMOR_END)):
        raise SignatureInvalid("not an armored SSH signature")
    body = "".join(t[len(ARMOR_BEGIN):-len(ARMOR_END)].split())
    return base64.b64decode(body)


def armor(blob: bytes) -> str:
    b64 = base64.b64encode(blob).decode()
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    return "\n".join([ARMOR_BEGIN, *lines, ARMOR_END]) + "\n"


def signed_data(namespace: str, hash_alg: str, message: bytes) -> bytes:
    h = hashlib.sha512(message).digest() if hash_alg == "sha512" else hashlib.sha256(message).digest()
    return MAGIC + ssh_string(namespace.encode()) + ssh_string(b"") + ssh_string(hash_alg.encode()) + ssh_string(h)


def sk_message(application: bytes, flags: int, counter: int, data: bytes) -> bytes:
    return hashlib.sha256(application).digest() + bytes([flags]) + struct.pack(">I", counter) + hashlib.sha256(data).digest()


def verify(armored: str, message: bytes, namespace: str) -> VerifiedSignature:
    """Verify an armored SSHSIG over `message`. Raises SignatureInvalid on any defect."""
    r = _Reader(dearmor(armored))
    if r.take(6) != MAGIC:
        raise SignatureInvalid("bad magic")
    if r.u32() != 1:
        raise SignatureInvalid("unsupported sshsig version")
    pk = parse_public_key_blob(r.string())
    ns = r.string().decode()
    r.string()  # reserved
    hash_alg = r.string().decode()
    sig_blob = r.string()
    if not r.done():
        raise SignatureInvalid("trailing bytes in signature")
    if ns != namespace:
        raise SignatureInvalid(f"namespace {ns!r} != expected {namespace!r}")
    if hash_alg not in ("sha256", "sha512"):
        raise SignatureInvalid("unsupported hash algorithm")
    data = signed_data(ns, hash_alg, message)

    s = _Reader(sig_blob)
    sig_type = s.string().decode()
    if sig_type != pk.key_type:
        raise SignatureInvalid("signature type does not match key type")
    raw = s.string()
    flags = counter = None
    if pk.hardware_backed:
        flags, counter = s.u8(), s.u32()
    if not s.done():
        raise SignatureInvalid("trailing bytes in signature blob")

    try:
        if pk.key_type == ED25519:
            Ed25519PublicKey.from_public_bytes(pk.key).verify(raw, data)
        elif pk.key_type == SK_ED25519:
            assert pk.application is not None
            Ed25519PublicKey.from_public_bytes(pk.key).verify(raw, sk_message(pk.application, flags, counter, data))
        else:
            assert pk.application is not None
            rr = _Reader(raw)
            r_int = int.from_bytes(rr.string(), "big")
            s_int = int.from_bytes(rr.string(), "big")
            pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pk.key)
            pub.verify(encode_dss_signature(r_int, s_int), sk_message(pk.application, flags, counter, data),
                       ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise SignatureInvalid("signature does not verify") from e
    return VerifiedSignature(pk, ns, flags, counter)
