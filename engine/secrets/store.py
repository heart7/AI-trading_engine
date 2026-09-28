"""Secret storage behind one interface (spec §13.2, §17, conduct rule 0.2.7).

Keys go from the Settings form straight into the store and are never logged, never written to
disk by the engine and never returned to the UI. The engine only ever holds a `vault_ref`.
`SecretValue` redacts itself in repr/str so an accidental log line cannot leak it.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class SecretValue:
    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and other._v == self._v

    def __hash__(self) -> int:
        return hash(("SecretValue", self._v))


@dataclass(frozen=True)
class SecretRef:
    path: str  # e.g. venues/<venue_instance_id>/<slot>


class SecretStore(Protocol):
    def put(self, ref: SecretRef, fields: dict[str, SecretValue]) -> None: ...
    def get(self, ref: SecretRef) -> dict[str, SecretValue]: ...
    def delete(self, ref: SecretRef) -> None: ...


class InMemorySecretStore:
    """Tests and PAPER development only. Holds values in process memory; nothing touches disk."""

    def __init__(self) -> None:
        self._d: dict[str, dict[str, SecretValue]] = {}

    def put(self, ref: SecretRef, fields: dict[str, SecretValue]) -> None:
        self._d[ref.path] = dict(fields)

    def get(self, ref: SecretRef) -> dict[str, SecretValue]:
        return dict(self._d[ref.path])

    def delete(self, ref: SecretRef) -> None:
        self._d.pop(ref.path, None)


class VaultKV2SecretStore:
    """HashiCorp Vault KV v2 over HTTPS. Address and token are injected at runtime (VAULT_ADDR, VAULT_TOKEN).

    Status: implemented, unverified (no Vault instance in CI yet).
    """

    def __init__(self, addr: str | None = None, token: str | None = None, mount: str = "uchfe"):
        self.addr = (addr or os.environ["VAULT_ADDR"]).rstrip("/")
        self._token = SecretValue(token or os.environ["VAULT_TOKEN"])
        self.mount = mount

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"{self.addr}/v1/{self.mount}/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Vault-Token": self._token.reveal(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - fixed https address from config
            raw = r.read()
        return json.loads(raw) if raw else {}

    def put(self, ref: SecretRef, fields: dict[str, SecretValue]) -> None:
        self._req("POST", f"data/{ref.path}", {"data": {k: v.reveal() for k, v in fields.items()}})

    def get(self, ref: SecretRef) -> dict[str, SecretValue]:
        data = self._req("GET", f"data/{ref.path}")["data"]["data"]
        return {k: SecretValue(v) for k, v in data.items()}

    def delete(self, ref: SecretRef) -> None:
        self._req("DELETE", f"metadata/{ref.path}")
