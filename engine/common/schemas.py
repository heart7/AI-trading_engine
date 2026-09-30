"""Access to the JSON-Schema single source of truth in /schemas."""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


class SchemaError(ValueError):
    """Raised when a document does not validate against its schema."""

    def __init__(self, schema: str, errors: list[str]):
        self.schema = schema
        self.errors = errors
        super().__init__(f"{schema}: " + "; ".join(errors[:5]))


@cache
def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate(name: str, doc: Any) -> None:
    errs = sorted(validator(name).iter_errors(doc), key=lambda e: list(e.path))
    if errs:
        raise SchemaError(name, [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs])


def gate_codes() -> list[str]:
    return json.loads((SCHEMA_DIR / "gate_codes.json").read_text())
