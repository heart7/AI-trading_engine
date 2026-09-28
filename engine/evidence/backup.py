"""Encrypted backups and the restore drill (spec §13.8). Status: the snapshot format and drill are implemented;
continuous WAL archiving, the synchronous replica and Object Lock storage are infrastructure (not yet built).

A snapshot is the ledger and the OMS journal, serialised canonically, content-hashed, and sealed with AES-256-GCM
under a key held in the secret store. Restore checks the tag, the content hash and the ledger hash chain before
anything is used.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from engine.common.canonical import canonical_json, content_hash
from engine.evidence.ledger import Entry, Ledger
from engine.secrets.store import SecretRef, SecretStore, SecretValue

KEY_REF = SecretRef("backup/snapshot-key")


class RestoreFailed(Exception):
    pass


def ensure_key(store: SecretStore) -> None:
    try:
        store.get(KEY_REF)
    except KeyError:
        store.put(KEY_REF, {"key": SecretValue(base64.b64encode(AESGCM.generate_key(256)).decode())})


def _key(store: SecretStore) -> bytes:
    return base64.b64decode(store.get(KEY_REF)["key"].reveal())


def snapshot(ledger: Ledger, journal: dict[str, Any], store: SecretStore, *, ts: str) -> dict[str, str]:
    body = {"ts": ts, "ledger": [dict(asdict(e), amount=str(e.amount)) for e in ledger.entries], "journal": journal}
    plain = canonical_json(body).encode()
    nonce = os.urandom(12)
    ct = AESGCM(_key(store)).encrypt(nonce, plain, b"uchfe-snapshot-v1")
    return {"ts": ts, "content_hash": content_hash(body), "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ct).decode(), "ledger_head": ledger.head}


def restore(snap: dict[str, str], store: SecretStore) -> tuple[Ledger, dict[str, Any]]:
    try:
        plain = AESGCM(_key(store)).decrypt(base64.b64decode(snap["nonce"]), base64.b64decode(snap["ciphertext"]),
                                            b"uchfe-snapshot-v1")
    except Exception as e:  # tag mismatch, wrong key
        raise RestoreFailed("snapshot failed authentication") from e
    body = json.loads(plain)
    if content_hash(body) != snap["content_hash"]:
        raise RestoreFailed("content hash mismatch")
    led = Ledger()
    for d in body["ledger"]:
        led.entries.append(Entry(**dict(d, amount=Decimal(d["amount"]))))
        led._refs.add(d["ref"])
    if not led.verify() or led.head != snap["ledger_head"]:
        raise RestoreFailed("ledger hash chain broken after restore")
    return led, body["journal"]


def restore_drill(ledger: Ledger, journal: dict[str, Any], store: SecretStore, *, ts: str) -> dict[str, Any]:
    """Quarterly drill: snapshot, restore into a fresh process state, compare. Returns the drill record."""
    snap = snapshot(ledger, journal, store, ts=ts)
    led2, j2 = restore(snap, store)
    ok = led2.head == ledger.head and len(led2.entries) == len(ledger.entries) and canonical_json(j2) == canonical_json(journal)
    return {"drill": "RESTORE", "ts": ts, "passed": ok, "entries": len(led2.entries), "ledger_head": led2.head,
            "snapshot_hash": snap["content_hash"]}
