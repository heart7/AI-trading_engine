"""Load, validate and hash the declared parameter policy object (spec §20)."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from engine.common.canonical import content_hash
from engine.common.schemas import validate

POLICY_DIR = Path(__file__).resolve().parents[2] / "policy"


@dataclass(frozen=True)
class Policy:
    """An immutable, schema-valid policy object and its content hash."""

    doc: Mapping[str, Any]
    hash: str
    source: str

    @property
    def version(self) -> str:
        return self.doc["policy_version"]

    def get(self, dotted: str) -> Any:
        node: Any = self.doc
        for part in dotted.split("."):
            node = node[part]
        return node


def _freeze(obj: Any) -> Any:
    # Deep copy into plain containers; callers must treat Policy.doc as read-only.
    if isinstance(obj, dict):
        return {k: _freeze(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_freeze(v) for v in obj]
    return obj


class _Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 float syntax, so `1.0e8` is a number, not a string."""


_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)(?:[eE][-+]?[0-9]+)?$"),
    list("-+0123456789."),
)


def parse_policy(text: str, source: str = "<string>") -> Policy:
    doc = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    validate("policy_object", doc)
    return Policy(doc=_freeze(doc), hash=content_hash(doc), source=source)


def load_policy(path: str | Path | None = None) -> Policy:
    p = Path(path) if path else POLICY_DIR / "policy-10.4.0.yaml"
    return parse_policy(p.read_text(), source=str(p))


def thaw(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: thaw(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [thaw(v) for v in obj]
    return obj


def policy_from_doc(doc: Mapping[str, Any], source: str = "<proposal>") -> Policy:
    """A proposed policy built in code (e.g. a Settings change). Validated and hashed exactly like a YAML file."""
    plain = thaw(doc)
    validate("policy_object", plain)
    return Policy(doc=_freeze(plain), hash=content_hash(plain), source=source)
