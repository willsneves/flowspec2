"""Closed local JSON Schema reference and resume-token contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

_DRAFT_2020_12_DIALECTS: Final[frozenset[str]] = frozenset(
    {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }
)
_SINGLE_NESTED_SCHEMA_KEYWORDS: Final[tuple[str, ...]] = (
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_ARRAY_NESTED_SCHEMA_KEYWORDS: Final[tuple[str, ...]] = (
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
)
_MAPPING_NESTED_SCHEMA_KEYWORDS: Final[tuple[str, ...]] = (
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
)


def _schema_anchor_target(root_schema: Any, anchor: str) -> Any:
    matching_targets: list[Any] = []
    pending_fragments = [root_schema]
    while pending_fragments:
        schema_fragment = pending_fragments.pop()
        if isinstance(schema_fragment, Mapping):
            if schema_fragment.get("$anchor") == anchor:
                matching_targets.append(schema_fragment)
            pending_fragments.extend(schema_fragment.values())
        elif isinstance(schema_fragment, (list, tuple)):
            pending_fragments.extend(schema_fragment)
    if len(matching_targets) != 1:
        raise ValueError(
            f"local tool schema anchor {anchor!r} must resolve exactly once; "
            f"resolved {len(matching_targets)} times"
        )
    return matching_targets[0]


def schema_reference_target(root_schema: Any, reference: str) -> Any:
    """Resolve one decoded local JSON Pointer or anchor against its root schema."""

    fragment = unquote(reference.removeprefix("#"))
    if not fragment:
        return root_schema
    if not fragment.startswith("/"):
        return _schema_anchor_target(root_schema, fragment)

    referenced_schema = root_schema
    for encoded_segment in fragment[1:].split("/"):
        reference_segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(referenced_schema, Mapping) and reference_segment in referenced_schema:
            referenced_schema = referenced_schema[reference_segment]
            continue
        if isinstance(referenced_schema, (list, tuple)) and reference_segment.isdigit():
            reference_index = int(reference_segment)
            if reference_index < len(referenced_schema):
                referenced_schema = referenced_schema[reference_index]
                continue
        raise ValueError(f"local tool schema reference does not resolve: {reference!r}")
    return referenced_schema


def validate_safe_local_schema_references(schema: Any, *, location: str) -> None:
    """Reject reference features whose meaning can escape the embedded schema."""

    reference_edges: dict[int, int] = {}
    schema_nodes: dict[int, Any] = {}
    pending_fragments: list[tuple[Any, bool]] = [(schema, True)]
    while pending_fragments:
        schema_fragment, is_root = pending_fragments.pop()
        if isinstance(schema_fragment, Mapping):
            fragment_identifier = id(schema_fragment)
            schema_nodes[fragment_identifier] = schema_fragment
            if any(
                dynamic_keyword in schema_fragment
                for dynamic_keyword in (
                    "$dynamicAnchor",
                    "$dynamicRef",
                    "$recursiveAnchor",
                    "$recursiveRef",
                )
            ):
                raise ValueError(f"{location} does not support dynamic schema references")
            if not is_root and "$id" in schema_fragment:
                raise ValueError(f"{location} does not support nested $id resources")
            if (reference := schema_fragment.get("$ref")) is not None:
                if not isinstance(reference, str):
                    raise ValueError(f"{location} $ref values must be strings")
                if not reference.startswith("#"):
                    raise ValueError(
                        f"{location} only supports local $ref values, got {reference!r}"
                    )
                try:
                    reference_target = schema_reference_target(schema, reference)
                except ValueError as reference_error:
                    raise ValueError(
                        f"{location} contains an unresolved $ref: {reference!r}"
                    ) from reference_error
                if isinstance(reference_target, Mapping):
                    target_identifier = id(reference_target)
                    reference_edges[fragment_identifier] = target_identifier
                    schema_nodes[target_identifier] = reference_target
            pending_fragments.extend(
                (nested_schema, False) for nested_schema in schema_fragment.values()
            )
        elif isinstance(schema_fragment, (list, tuple)):
            pending_fragments.extend((nested_schema, False) for nested_schema in schema_fragment)

    visited_identifiers: set[int] = set()
    for schema_identifier in schema_nodes:
        path_identifiers: set[int] = set()
        current_identifier: int | None = schema_identifier
        while current_identifier is not None and current_identifier not in visited_identifiers:
            if current_identifier in path_identifiers:
                raise ValueError(f"{location} does not support cyclic local references")
            path_identifiers.add(current_identifier)
            current_identifier = reference_edges.get(current_identifier)
        visited_identifiers.update(path_identifiers)


def _nested_schema_fragments(schema_fragment: Mapping[str, Any]) -> tuple[Any, ...]:
    nested_fragments: list[Any] = []
    nested_fragments.extend(
        schema_fragment[keyword]
        for keyword in _SINGLE_NESTED_SCHEMA_KEYWORDS
        if keyword in schema_fragment
    )
    for keyword in _ARRAY_NESTED_SCHEMA_KEYWORDS:
        keyword_value = schema_fragment.get(keyword)
        if isinstance(keyword_value, (list, tuple)):
            nested_fragments.extend(keyword_value)
    for keyword in _MAPPING_NESTED_SCHEMA_KEYWORDS:
        keyword_value = schema_fragment.get(keyword)
        if isinstance(keyword_value, Mapping):
            nested_fragments.extend(keyword_value.values())
    return tuple(nested_fragments)


def validate_resume_token_schema(schema: dict[str, Any]) -> None:
    """Validate the closed Draft 2020-12 contract accepted by external resume."""

    location = "$.capabilities.await_external.resume.schema"
    try:
        json.dumps(schema, ensure_ascii=False, allow_nan=False)
        declared_dialect = schema.get("$schema")
        if not isinstance(declared_dialect, (str, type(None))):
            raise ValueError("$schema must be a string")
        if declared_dialect is not None and declared_dialect not in _DRAFT_2020_12_DIALECTS:
            raise ValueError(f"must use JSON Schema Draft 2020-12, got {declared_dialect!r}")
        Draft202012Validator.check_schema(schema)
        validate_safe_local_schema_references(schema, location=location)
    except (SchemaError, TypeError, ValueError) as schema_error:
        detail = (
            schema_error.message if isinstance(schema_error, SchemaError) else str(schema_error)
        )
        raise ValueError(f"{location} is not a valid closed local JSON Schema: {detail}") from (
            schema_error
        )
    if schema.get("type") != "object":
        raise ValueError(f"{location}.type must be 'object'")
    if schema.get("additionalProperties") is not False:
        raise ValueError(f"{location}.additionalProperties must be false")

    pending_fragments: list[Any] = [schema]
    while pending_fragments:
        schema_fragment = pending_fragments.pop()
        if not isinstance(schema_fragment, Mapping):
            continue
        declares_object_shape = schema_fragment.get("type") == "object" or isinstance(
            schema_fragment.get("properties"), Mapping
        )
        if declares_object_shape and schema_fragment.get("additionalProperties") is not False:
            raise ValueError(
                f"{location} must close every declared object with additionalProperties:false"
            )
        pending_fragments.extend(_nested_schema_fragments(schema_fragment))
