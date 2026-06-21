"""Load + JSON-Schema-validate a flowspec/2 document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_PATH = Path(__file__).with_name("flowspec-2.schema.json")
_schema_cache: dict[str, Any] | None = None


def schema() -> dict[str, Any]:
    """The flowspec/2 JSON Schema (Draft 2020-12), cached."""
    global _schema_cache
    cached = _schema_cache
    if cached is None:
        cached = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cached)
        _schema_cache = cached
    return cached


def validate_flow(doc: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` if the document is not valid flowspec/2."""
    jsonschema.validate(doc, schema())


def load_flow(path: str | Path, *, validate: bool = True) -> dict[str, Any]:
    """Read a flow document from disk and (by default) validate it."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if validate:
        validate_flow(doc)
    return doc
