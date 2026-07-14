"""Reusable JSON Schema relations for semantic source contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, cast

from jsonschema import Draft202012Validator, FormatChecker

from .semantic_support import mutable_json

_JSON_SCHEMA_TYPES: Final[frozenset[str]] = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)


def slot_model_accepts(model: Any, slot_name: str, slot_value: Any) -> bool:
    try:
        model.model_validate({slot_name: slot_value})
    except Exception:
        return False
    return True


def _json_instance_type(instance: Any) -> str:
    if instance is None:
        return "null"
    if isinstance(instance, bool):
        return "boolean"
    if isinstance(instance, int):
        return "integer"
    if isinstance(instance, float):
        return "number"
    if isinstance(instance, str):
        return "string"
    if isinstance(instance, list):
        return "array"
    return "object"


def _schema_possible_types(schema: Any) -> frozenset[str]:
    if schema is False:
        return frozenset()
    if schema is True or not isinstance(schema, Mapping):
        return _JSON_SCHEMA_TYPES
    if "const" in schema:
        return frozenset({_json_instance_type(schema["const"])})
    if isinstance((enum_values := schema.get("enum")), (list, tuple)):
        return frozenset(_json_instance_type(enum_value) for enum_value in enum_values)
    schema_type = schema.get("type")
    possible_types = (
        frozenset({schema_type})
        if isinstance(schema_type, str)
        else frozenset(schema_type)
        if isinstance(schema_type, (list, tuple))
        else _JSON_SCHEMA_TYPES
    )
    if "number" in possible_types:
        possible_types = possible_types | {"integer"}
    for union_keyword in ("anyOf", "oneOf"):
        if isinstance((branches := schema.get(union_keyword)), (list, tuple)):
            possible_types &= frozenset().union(
                *(_schema_possible_types(branch) for branch in branches)
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            possible_types &= _schema_possible_types(branch)
    return possible_types


def _schema_closed_values(schema: Any) -> tuple[Any, ...] | None:
    if not isinstance(schema, Mapping):
        return () if schema is False else None
    if "const" in schema:
        return (schema["const"],)
    if isinstance((enum_values := schema.get("enum")), (list, tuple)):
        return tuple(enum_values)
    for union_keyword in ("anyOf", "oneOf"):
        if isinstance((branches := schema.get(union_keyword)), (list, tuple)):
            branch_values = tuple(_schema_closed_values(branch) for branch in branches)
            if all(values is not None for values in branch_values):
                return tuple(
                    value for values in branch_values for value in cast(tuple[Any, ...], values)
                )
    return None


def schema_accepts(schema: Mapping[str, Any], instance: Any) -> bool:
    return Draft202012Validator(
        mutable_json(schema),
        format_checker=FormatChecker(),
    ).is_valid(instance)


def schemas_are_obviously_disjoint(
    first_schema: Mapping[str, Any],
    second_schema: Mapping[str, Any],
) -> bool:
    first_values = _schema_closed_values(first_schema)
    if first_values is not None:
        return not any(schema_accepts(second_schema, value) for value in first_values)
    second_values = _schema_closed_values(second_schema)
    if second_values is not None:
        return not any(schema_accepts(first_schema, value) for value in second_values)
    return not bool(_schema_possible_types(first_schema) & _schema_possible_types(second_schema))


def schema_property(schema: Mapping[str, Any], property_path: str) -> Mapping[str, Any] | None:
    candidates: tuple[Any, ...] = (schema,)
    for property_name in property_path.split("."):
        next_candidates: list[Any] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            branches = tuple(
                branch
                for keyword in ("anyOf", "oneOf", "allOf")
                for branch in candidate.get(keyword, ())
                if isinstance(candidate.get(keyword), (list, tuple))
            )
            candidate_schemas = (candidate, *branches)
            for candidate_schema in candidate_schemas:
                if not isinstance(candidate_schema, Mapping):
                    continue
                properties = candidate_schema.get("properties")
                if isinstance(properties, Mapping) and property_name in properties:
                    next_candidates.append(properties[property_name])
        candidates = tuple(next_candidates)
        if not candidates:
            return None
    mutable_candidates = tuple(mutable_json(candidate) for candidate in candidates)
    return cast(
        Mapping[str, Any],
        mutable_candidates[0]
        if len(mutable_candidates) == 1
        else {"allOf": list(mutable_candidates)},
    )
