"""Load + JSON-Schema-validate a flowspec/2 document."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import jsonschema
from jsonschema.exceptions import best_match

from .json_codec import strict_json_loads

_SCHEMA_PATH = Path(__file__).with_name("flowspec-2.schema.json")
_schema_cache: dict[str, Any] | None = None
_validator_cache: jsonschema.Draft202012Validator | None = None


def _cached_schema() -> dict[str, Any]:
    global _schema_cache
    cached = _schema_cache
    if cached is None:
        cached = strict_json_loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cached)
        _schema_cache = cached
    return cached


def schema() -> dict[str, Any]:
    """Return an owned copy of the cached FlowSpec2 Draft 2020-12 schema."""
    return copy.deepcopy(_cached_schema())


def _cached_validator() -> jsonschema.Draft202012Validator:
    global _validator_cache
    cached = _validator_cache
    if cached is None:
        cached = jsonschema.Draft202012Validator(_cached_schema())
        _validator_cache = cached
    return cached


def validate_flow(doc: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` if the document is not valid flowspec/2."""
    if validation_error := best_match(_cached_validator().iter_errors(doc)):
        raise validation_error


def load_flow(path: str | Path, *, validate: bool = True) -> dict[str, Any]:
    """Read a flow document from disk and (by default) validate it."""
    doc = cast(dict[str, Any], strict_json_loads(Path(path).read_text(encoding="utf-8")))
    if validate:
        validate_flow(doc)
    return doc
