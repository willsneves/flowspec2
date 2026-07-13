"""Versioned reusable subflows (mixins) referenced by ``uses[]`` as ``name@major``.

A subflow splices its own nodes/slots/routers into the compiled graph at its
anchor, returns its slot-to-collector mapping for compiler-validated upward
references, and inherits idempotency/reset/attempt defaults. Each ends with
a ``<name>_done`` no-op exit node whose router returns ``NEXT`` — that single
exit lets internal branches (``anonimo``, confirmed-address) skip the remaining
internal nodes cleanly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias, cast
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from ..nodes import FlowContext, NodeDesc


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(nested_value) for nested_value in value]
    if isinstance(value, list):
        return [_mutable_json(nested_value) for nested_value in value]
    return value


def _immutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _immutable_json(nested_value) for key, nested_value in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_json(nested_value) for nested_value in value)
    return value


_CategoryMatch: TypeAlias = tuple[bool, bool]
_BoundaryMatch: TypeAlias = tuple[_CategoryMatch, ...]
_ALWAYS_MATCHES: _CategoryMatch = (True, True)
_NEVER_MATCHES: _CategoryMatch = (False, False)
_SOMETIMES_MATCHES: _CategoryMatch = (True, False)
_JSON_VALUE_CATEGORIES: tuple[str, ...] = (
    "object",
    "array",
    "boolean",
    "null",
    "number",
    "string",
)
_OBJECT_CATEGORY_INDEX = _JSON_VALUE_CATEGORIES.index("object")
_DRAFT_2020_12_URIS = frozenset(
    {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }
)
_SINGLE_SUBSCHEMA_KEYWORDS = (
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
_ARRAY_SUBSCHEMA_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
_MAPPING_SUBSCHEMA_KEYWORDS = (
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
)


def _nested_schemas(schema: Mapping[str, Any]) -> tuple[Any, ...]:
    nested_schemas: list[Any] = []
    for keyword in _SINGLE_SUBSCHEMA_KEYWORDS:
        if keyword in schema:
            nested_schemas.append(schema[keyword])
    for keyword in _ARRAY_SUBSCHEMA_KEYWORDS:
        keyword_schemas = schema.get(keyword)
        if isinstance(keyword_schemas, (list, tuple)):
            nested_schemas.extend(keyword_schemas)
    for keyword in _MAPPING_SUBSCHEMA_KEYWORDS:
        keyword_schemas = schema.get(keyword)
        if isinstance(keyword_schemas, Mapping):
            nested_schemas.extend(keyword_schemas.values())
    return tuple(nested_schemas)


def _local_anchor_target(root_schema: Mapping[str, Any], anchor: str) -> Any:
    matching_targets: list[Any] = []

    def visit(schema_fragment: Any) -> None:
        if isinstance(schema_fragment, Mapping):
            if schema_fragment.get("$anchor") == anchor:
                matching_targets.append(schema_fragment)
            for nested_schema in _nested_schemas(schema_fragment):
                visit(nested_schema)

    visit(root_schema)
    if len(matching_targets) != 1:
        raise ValueError(
            f"local schema anchor {anchor!r} must resolve exactly once; "
            f"resolved {len(matching_targets)} times"
        )
    return matching_targets[0]


def _decode_json_pointer_segment(encoded_segment: str, reference: str) -> str:
    if re.search(r"~(?:[^01]|$)", encoded_segment) is not None:
        raise ValueError(f"local schema reference has an invalid JSON Pointer: {reference!r}")
    return encoded_segment.replace("~1", "/").replace("~0", "~")


def _local_reference_target(root_schema: Mapping[str, Any], reference: str) -> Any:
    if not reference.startswith("#"):
        raise ValueError(f"subflow schemas only support local references, got {reference!r}")
    fragment = unquote(reference[1:])
    if not fragment:
        return root_schema
    if not fragment.startswith("/"):
        return _local_anchor_target(root_schema, fragment)

    referenced_schema: Any = root_schema
    for encoded_segment in fragment[1:].split("/"):
        reference_segment = _decode_json_pointer_segment(encoded_segment, reference)
        if isinstance(referenced_schema, Mapping) and reference_segment in referenced_schema:
            referenced_schema = referenced_schema[reference_segment]
            continue
        if isinstance(referenced_schema, (list, tuple)) and reference_segment.isdigit():
            reference_index = int(reference_segment)
            if reference_index < len(referenced_schema):
                referenced_schema = referenced_schema[reference_index]
                continue
        raise ValueError(f"local schema reference does not resolve: {reference!r}")
    if not isinstance(referenced_schema, (Mapping, bool)):
        raise ValueError(f"local schema reference does not resolve to a schema: {reference!r}")
    return referenced_schema


def _validate_local_schema_references(
    schema: Any,
    root_schema: Mapping[str, Any],
    *,
    is_root: bool = True,
) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        return
    unsupported_reference_keywords = {
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveAnchor",
        "$recursiveRef",
    } & set(schema)
    if unsupported_reference_keywords:
        unsupported_keyword = sorted(unsupported_reference_keywords)[0]
        raise ValueError(f"subflow schemas do not support {unsupported_keyword}")
    if not is_root and "$id" in schema:
        raise ValueError("subflow schemas do not support nested $id resources")
    if not is_root and "$schema" in schema:
        raise ValueError("subflow schemas do not support nested $schema dialect changes")
    if (reference := schema.get("$ref")) is not None:
        if not isinstance(reference, str):
            raise ValueError("subflow schema $ref must be a string")
        _local_reference_target(root_schema, reference)
    for nested_schema in _nested_schemas(schema):
        _validate_local_schema_references(nested_schema, root_schema, is_root=False)


def _validate_local_reference_cycles(root_schema: Mapping[str, Any]) -> None:
    """Reject recursive local resources that cannot be projected deterministically."""

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(schema: Any) -> None:
        if isinstance(schema, bool) or not isinstance(schema, Mapping):
            return
        schema_identifier = id(schema)
        if schema_identifier in visiting:
            raise ValueError("subflow schemas do not support local schema reference cycles")
        if schema_identifier in visited:
            return
        visiting.add(schema_identifier)
        if (reference := schema.get("$ref")) is not None:
            visit(_local_reference_target(root_schema, cast(str, reference)))
        for nested_schema in _nested_schemas(schema):
            visit(nested_schema)
        visiting.remove(schema_identifier)
        visited.add(schema_identifier)

    visit(root_schema)


def _category_all(category_matches: list[_CategoryMatch]) -> _CategoryMatch:
    return (
        all(category_match[0] for category_match in category_matches),
        all(category_match[1] for category_match in category_matches),
    )


def _category_any(category_matches: list[_CategoryMatch]) -> _CategoryMatch:
    return (
        any(category_match[0] for category_match in category_matches),
        any(category_match[1] for category_match in category_matches),
    )


def _category_not(category_match: _CategoryMatch) -> _CategoryMatch:
    can_match, must_match = category_match
    return not must_match, not can_match


def _category_one_of(category_matches: list[_CategoryMatch]) -> _CategoryMatch:
    possible_matches = [category_match for category_match in category_matches if category_match[0]]
    guaranteed_matches = sum(category_match[1] for category_match in possible_matches)
    return (
        bool(possible_matches) and guaranteed_matches < 2,
        guaranteed_matches == 1 and len(possible_matches) == 1,
    )


def _boundary_all(boundary_matches: list[_BoundaryMatch]) -> _BoundaryMatch:
    return tuple(
        _category_all([boundary_match[category_index] for boundary_match in boundary_matches])
        for category_index in range(len(_JSON_VALUE_CATEGORIES))
    )


def _boundary_any(boundary_matches: list[_BoundaryMatch]) -> _BoundaryMatch:
    return tuple(
        _category_any([boundary_match[category_index] for boundary_match in boundary_matches])
        for category_index in range(len(_JSON_VALUE_CATEGORIES))
    )


def _boundary_not(boundary_match: _BoundaryMatch) -> _BoundaryMatch:
    return tuple(_category_not(category_match) for category_match in boundary_match)


def _boundary_one_of(boundary_matches: list[_BoundaryMatch]) -> _BoundaryMatch:
    return tuple(
        _category_one_of([boundary_match[category_index] for boundary_match in boundary_matches])
        for category_index in range(len(_JSON_VALUE_CATEGORIES))
    )


def _type_boundary_match(schema_type: Any) -> _BoundaryMatch:
    schema_types = {schema_type} if isinstance(schema_type, str) else set(schema_type)
    return tuple(
        (
            _ALWAYS_MATCHES
            if category in schema_types
            else _SOMETIMES_MATCHES
            if category == "number" and "integer" in schema_types
            else _NEVER_MATCHES
        )
        for category in _JSON_VALUE_CATEGORIES
    )


def _instance_category(instance: Any) -> str:
    if isinstance(instance, Mapping):
        return "object"
    if isinstance(instance, list):
        return "array"
    if isinstance(instance, bool):
        return "boolean"
    if instance is None:
        return "null"
    if isinstance(instance, (int, float)):
        return "number"
    return "string"


def _finite_boundary_match(instances: list[Any]) -> _BoundaryMatch:
    return tuple(
        (
            any(_instance_category(instance) == category for instance in instances),
            category == "null"
            and any(instance is None for instance in instances)
            or category == "boolean"
            and any(instance is True for instance in instances)
            and any(instance is False for instance in instances),
        )
        for category in _JSON_VALUE_CATEGORIES
    )


def _category_restriction_boundary(
    category: str,
    category_match: _CategoryMatch,
) -> _BoundaryMatch:
    return tuple(
        category_match if candidate_category == category else _ALWAYS_MATCHES
        for candidate_category in _JSON_VALUE_CATEGORIES
    )


def _conjunctive_schema_fragments(
    schema: Any,
    root_schema: Mapping[str, Any],
) -> list[Any]:
    pending_fragments = [schema]
    visited_fragments: set[int] = set()
    schema_fragments: list[Any] = []
    while pending_fragments:
        schema_fragment = pending_fragments.pop()
        if not isinstance(schema_fragment, Mapping):
            schema_fragments.append(schema_fragment)
            continue
        fragment_identifier = id(schema_fragment)
        if fragment_identifier in visited_fragments:
            continue
        visited_fragments.add(fragment_identifier)
        schema_fragments.append(schema_fragment)
        if (reference := schema_fragment.get("$ref")) is not None:
            pending_fragments.append(_local_reference_target(root_schema, cast(str, reference)))
        if isinstance((all_of := schema_fragment.get("allOf")), (list, tuple)):
            pending_fragments.extend(all_of)
    return schema_fragments


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _finite_object_candidates(schema_fragments: list[Any]) -> dict[str, Any] | None:
    candidate_objects: dict[str, Any] | None = None
    for schema_fragment in schema_fragments:
        if not isinstance(schema_fragment, Mapping):
            continue
        restricted_instances: list[Any] | None = None
        if "const" in schema_fragment:
            restricted_instances = [schema_fragment["const"]]
        if isinstance((enum_members := schema_fragment.get("enum")), (list, tuple)):
            restricted_instances = (
                list(enum_members)
                if restricted_instances is None
                else [
                    instance
                    for instance in restricted_instances
                    if any(instance == enum_member for enum_member in enum_members)
                ]
            )
        if restricted_instances is None:
            continue
        restricted_objects = {
            _canonical_json(instance): instance
            for instance in restricted_instances
            if isinstance(instance, Mapping)
        }
        candidate_objects = (
            restricted_objects
            if candidate_objects is None
            else {
                serialized_object: candidate_object
                for serialized_object, candidate_object in candidate_objects.items()
                if serialized_object in restricted_objects
            }
        )
    return candidate_objects


def _schema_accepts_instance(
    schema: Any,
    root_schema: Mapping[str, Any],
    instance: Any,
) -> bool:
    validator = Draft202012Validator(root_schema, format_checker=FormatChecker())
    return validator.evolve(schema=schema).is_valid(instance)


def _object_constraints_may_match(
    schema: Any,
    root_schema: Mapping[str, Any],
) -> bool:
    schema_fragments = _conjunctive_schema_fragments(schema, root_schema)
    required_properties: set[str] = set()
    expanded_dependency_schemas: set[int] = set()
    while True:
        if any(schema_fragment is False for schema_fragment in schema_fragments):
            return False
        previous_required_properties = frozenset(required_properties)
        for schema_fragment in schema_fragments:
            if not isinstance(schema_fragment, Mapping):
                continue
            required_properties.update(
                property_name
                for property_name in schema_fragment.get("required", ())
                if isinstance(property_name, str)
            )
        dependencies_changed = True
        while dependencies_changed:
            dependencies_changed = False
            for schema_fragment in schema_fragments:
                if not isinstance(schema_fragment, Mapping):
                    continue
                dependent_required = schema_fragment.get("dependentRequired")
                if not isinstance(dependent_required, Mapping):
                    continue
                for trigger_property, dependent_properties in dependent_required.items():
                    if trigger_property not in required_properties:
                        continue
                    previous_size = len(required_properties)
                    required_properties.update(
                        property_name
                        for property_name in dependent_properties
                        if isinstance(property_name, str)
                    )
                    dependencies_changed = (
                        dependencies_changed or len(required_properties) > previous_size
                    )

        added_dependency_schema = False
        for schema_fragment in tuple(schema_fragments):
            if not isinstance(schema_fragment, Mapping):
                continue
            dependent_schemas = schema_fragment.get("dependentSchemas")
            if not isinstance(dependent_schemas, Mapping):
                continue
            for trigger_property, dependent_schema in dependent_schemas.items():
                if trigger_property not in required_properties:
                    continue
                dependency_identifier = id(dependent_schema)
                if dependency_identifier in expanded_dependency_schemas:
                    continue
                expanded_dependency_schemas.add(dependency_identifier)
                schema_fragments.extend(
                    _conjunctive_schema_fragments(dependent_schema, root_schema)
                )
                added_dependency_schema = True
        if not added_dependency_schema and previous_required_properties == frozenset(
            required_properties
        ):
            break

    minimum_properties = 0
    maximum_properties: int | None = None
    closed_property_sets: list[set[str]] = []
    property_name_schemas: list[Any] = []
    for schema_fragment in schema_fragments:
        if not isinstance(schema_fragment, Mapping):
            continue
        if isinstance((fragment_minimum := schema_fragment.get("minProperties")), int):
            minimum_properties = max(minimum_properties, fragment_minimum)
        if isinstance((fragment_maximum := schema_fragment.get("maxProperties")), int):
            maximum_properties = (
                fragment_maximum
                if maximum_properties is None
                else min(maximum_properties, fragment_maximum)
            )
        if (property_names := schema_fragment.get("propertyNames")) is not None:
            property_name_schemas.append(property_names)
        if schema_fragment.get("additionalProperties") is False:
            properties = schema_fragment.get("properties")
            permitted_properties = set(properties) if isinstance(properties, Mapping) else set()
            pattern_properties = schema_fragment.get("patternProperties")
            if isinstance(pattern_properties, Mapping):
                if any(
                    pattern_schema is not False for pattern_schema in pattern_properties.values()
                ):
                    continue
                permitted_properties = {
                    property_name
                    for property_name in permitted_properties
                    if not any(
                        re.search(pattern, property_name) is not None
                        for pattern in pattern_properties
                    )
                }
            closed_property_sets.append(permitted_properties)

    minimum_properties = max(minimum_properties, len(required_properties))
    if maximum_properties is not None and minimum_properties > maximum_properties:
        return False
    if closed_property_sets:
        permitted_properties = set.intersection(*closed_property_sets)
        if not required_properties <= permitted_properties:
            return False
        if minimum_properties > len(permitted_properties):
            return False
    if any(
        not _schema_accepts_instance(property_name_schema, root_schema, required_property)
        for property_name_schema in property_name_schemas
        for required_property in required_properties
    ):
        return False
    if minimum_properties and property_name_schemas:
        property_name_boundary = _boundary_all(
            [
                _object_boundary_match(property_name_schema, root_schema)
                for property_name_schema in property_name_schemas
            ]
        )
        if not property_name_boundary[_JSON_VALUE_CATEGORIES.index("string")][0]:
            return False

    for required_property in required_properties:
        required_property_schemas: list[Any] = []
        for schema_fragment in schema_fragments:
            if not isinstance(schema_fragment, Mapping):
                continue
            properties = schema_fragment.get("properties")
            if isinstance(properties, Mapping) and required_property in properties:
                required_property_schemas.append(properties[required_property])
            pattern_properties = schema_fragment.get("patternProperties")
            if isinstance(pattern_properties, Mapping):
                required_property_schemas.extend(
                    pattern_schema
                    for pattern, pattern_schema in pattern_properties.items()
                    if re.search(pattern, required_property) is not None
                )
        if required_property_schemas:
            property_boundary = _boundary_all(
                [
                    _object_boundary_match(property_schema, root_schema)
                    for property_schema in required_property_schemas
                ]
            )
            if not any(category_match[0] for category_match in property_boundary):
                return False

    finite_candidates = _finite_object_candidates(schema_fragments)
    return finite_candidates is None or any(
        _schema_accepts_instance(schema, root_schema, candidate_object)
        for candidate_object in finite_candidates.values()
    )


def _object_boundary_match(
    schema: Any,
    root_schema: Mapping[str, Any],
    reference_stack: tuple[str, ...] = (),
) -> _BoundaryMatch:
    if schema is False:
        return tuple(_NEVER_MATCHES for _ in _JSON_VALUE_CATEGORIES)
    if schema is True or not isinstance(schema, Mapping):
        return tuple(_ALWAYS_MATCHES for _ in _JSON_VALUE_CATEGORIES)

    boundary_matches: list[_BoundaryMatch] = []
    if not _object_constraints_may_match(schema, root_schema):
        boundary_matches.append(_category_restriction_boundary("object", _NEVER_MATCHES))
    if (schema_type := schema.get("type")) is not None:
        boundary_matches.append(_type_boundary_match(schema_type))

    if "const" in schema:
        boundary_matches.append(_finite_boundary_match([schema["const"]]))
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        boundary_matches.append(_finite_boundary_match(list(enum_members)))

    object_shape_keywords = {
        "additionalProperties",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "properties",
        "propertyNames",
        "unevaluatedProperties",
    }
    if object_shape_keywords & set(schema) or any(
        keyword in schema for keyword in ("maxProperties", "minProperties", "required")
    ):
        boundary_matches.append(_category_restriction_boundary("object", _SOMETIMES_MATCHES))

    if (reference := schema.get("$ref")) is not None:
        if reference in reference_stack:
            boundary_matches.append(tuple(_SOMETIMES_MATCHES for _ in _JSON_VALUE_CATEGORIES))
        else:
            boundary_matches.append(
                _object_boundary_match(
                    _local_reference_target(root_schema, cast(str, reference)),
                    root_schema,
                    (*reference_stack, cast(str, reference)),
                )
            )

    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        boundary_matches.append(
            _boundary_all(
                [_object_boundary_match(branch, root_schema, reference_stack) for branch in all_of]
            )
        )
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)):
        boundary_matches.append(
            _boundary_any(
                [_object_boundary_match(branch, root_schema, reference_stack) for branch in any_of]
            )
        )
    if isinstance((one_of := schema.get("oneOf")), (list, tuple)):
        boundary_matches.append(
            _boundary_one_of(
                [_object_boundary_match(branch, root_schema, reference_stack) for branch in one_of]
            )
        )
    if "not" in schema:
        boundary_matches.append(
            _boundary_not(_object_boundary_match(schema["not"], root_schema, reference_stack))
        )

    if "if" in schema:
        condition_match = _object_boundary_match(schema["if"], root_schema, reference_stack)
        boundary_matches.append(
            _boundary_any(
                [
                    _boundary_all(
                        [
                            condition_match,
                            _object_boundary_match(
                                schema.get("then", True), root_schema, reference_stack
                            ),
                        ]
                    ),
                    _boundary_all(
                        [
                            _boundary_not(condition_match),
                            _object_boundary_match(
                                schema.get("else", True), root_schema, reference_stack
                            ),
                        ]
                    ),
                ]
            )
        )

    return (
        _boundary_all(boundary_matches)
        if boundary_matches
        else tuple(_ALWAYS_MATCHES for _ in _JSON_VALUE_CATEGORIES)
    )


def _validated_schema(
    schema: Mapping[str, Any],
    location: str,
    *,
    require_object_boundary: bool,
) -> Mapping[str, Any]:
    mutable_schema = _mutable_json(schema)
    try:
        json.dumps(mutable_schema, allow_nan=False)
        declared_dialect = mutable_schema.get("$schema")
        if not isinstance(declared_dialect, (str, type(None))):
            raise ValueError("subflow schema $schema must be a string")
        if declared_dialect is not None and declared_dialect not in _DRAFT_2020_12_URIS:
            raise ValueError(
                f"subflow schemas must use JSON Schema Draft 2020-12, got {declared_dialect!r}"
            )
        Draft202012Validator.check_schema(mutable_schema)
        _validate_local_schema_references(mutable_schema, mutable_schema)
        _validate_local_reference_cycles(mutable_schema)
    except (SchemaError, TypeError, ValueError) as error:
        detail = error.message if isinstance(error, SchemaError) else str(error)
        raise ValueError(f"{location} is not a valid JSON Schema: {detail}") from error
    if require_object_boundary:
        boundary_match = _object_boundary_match(mutable_schema, mutable_schema)
        object_match = boundary_match[_OBJECT_CATEGORY_INDEX]
        if not object_match[0]:
            raise ValueError(
                f"{location} cannot validate any object-shaped uses[].with configuration"
            )
        if any(
            category_match[0]
            for category_index, category_match in enumerate(boundary_match)
            if category_index != _OBJECT_CATEGORY_INDEX
        ):
            raise ValueError(
                f"{location} must only validate object-shaped uses[].with configurations"
            )
    return cast(Mapping[str, Any], _immutable_json(mutable_schema))


def _validation_location(base_location: str, path: tuple[Any, ...]) -> str:
    location = base_location
    for segment in path:
        if isinstance(segment, int):
            location += f"[{segment}]"
        elif isinstance(segment, str) and segment.isidentifier():
            location += f".{segment}"
        else:
            location += f"[{segment!r}]"
    return location


@dataclass
class SubflowBuild:
    descriptors: list[NodeDesc]
    entry_id: str
    node_for_slot: dict[str, str] = field(default_factory=dict)


class Subflow(Protocol):
    name: str
    major: int

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild: ...


@dataclass(frozen=True)
class SubflowStateKey:
    """Partition and immutable validation contract for subflow-owned state."""

    partition: Literal["data", "internal"]
    schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.partition not in {"data", "internal"}:
            raise ValueError(f"unsupported subflow state partition: {self.partition!r}")
        object.__setattr__(
            self,
            "schema",
            _validated_schema(
                self.schema,
                "subflow state key schema",
                require_object_boundary=False,
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible state-key contract."""

        return {
            "partition": self.partition,
            "schema": _mutable_json(self.schema),
        }


@dataclass(frozen=True)
class SubflowDefinition:
    """Immutable authoring and integration contract for a versioned subflow."""

    ref: str
    description: str
    configuration_schema: Mapping[str, Any]
    exposed_slots: frozenset[str] | None = None
    exposed_slot_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset()
    required_tools: Mapping[str, str] = field(default_factory=dict)
    state_keys: Mapping[str, SubflowStateKey] | None = None
    node_identifiers: frozenset[str] = frozenset()
    legacy_manifest: bool = field(default=False, init=False)
    _state_ownership_complete: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*@[0-9]+", self.ref) is None:
            raise ValueError(f"subflow definition ref must be name@major: {self.ref!r}")
        if not self.description.strip():
            raise ValueError(f"subflow {self.ref!r} description must be non-empty")
        object.__setattr__(
            self,
            "configuration_schema",
            _validated_schema(
                self.configuration_schema,
                f"subflow {self.ref!r} configuration_schema",
                require_object_boundary=True,
            ),
        )
        if self.exposed_slots is not None:
            if any(not slot_name.strip() for slot_name in self.exposed_slots):
                raise ValueError(f"subflow {self.ref!r} exposed slots must be non-empty")
            object.__setattr__(self, "exposed_slots", frozenset(self.exposed_slots))
        validated_slot_schemas = {
            slot_name: _validated_schema(
                slot_schema,
                f"subflow {self.ref!r} exposed_slot_schemas[{slot_name!r}]",
                require_object_boundary=False,
            )
            for slot_name, slot_schema in self.exposed_slot_schemas.items()
        }
        if self.exposed_slots is None and validated_slot_schemas:
            raise ValueError(
                f"subflow {self.ref!r} cannot declare slot schemas with open exposures"
            )
        if self.exposed_slots is not None and set(validated_slot_schemas) != self.exposed_slots:
            raise ValueError(
                f"subflow {self.ref!r} exposed slot schemas must exactly match exposed_slots"
            )
        object.__setattr__(
            self,
            "exposed_slot_schemas",
            MappingProxyType(validated_slot_schemas),
        )
        if any(not capability.strip() for capability in self.capabilities):
            raise ValueError(f"subflow {self.ref!r} capabilities must be non-empty")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        validated_required_tools: dict[str, str] = {}
        for tool_name, tool_version in self.required_tools.items():
            if re.fullmatch(r"[a-z][a-z0-9_]*", tool_name) is None:
                raise ValueError(
                    f"subflow {self.ref!r} required tool name is invalid: {tool_name!r}"
                )
            if not tool_version.strip():
                raise ValueError(
                    f"subflow {self.ref!r} required tool {tool_name!r} version must be non-empty"
                )
            validated_required_tools[tool_name] = tool_version
        object.__setattr__(
            self,
            "required_tools",
            MappingProxyType(validated_required_tools),
        )

        declared_state_keys = self.state_keys is not None
        validated_state_keys = dict(self.state_keys or {})
        for state_key, state_contract in validated_state_keys.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:-]*", state_key) is None:
                raise ValueError(f"subflow {self.ref!r} state key is invalid: {state_key!r}")
            if not isinstance(state_contract, SubflowStateKey):
                raise TypeError(
                    f"subflow {self.ref!r} state key {state_key!r} must use SubflowStateKey"
                )
        if not declared_state_keys and self.exposed_slots is not None:
            validated_state_keys.update(
                {
                    exposed_slot: SubflowStateKey(
                        partition="data",
                        schema=validated_slot_schemas[exposed_slot],
                    )
                    for exposed_slot in self.exposed_slots
                }
            )
        if declared_state_keys and self.exposed_slots is not None:
            for exposed_slot in self.exposed_slots:
                if (exposed_state_contract := validated_state_keys.get(exposed_slot)) is None:
                    raise ValueError(
                        f"subflow {self.ref!r} state_keys must include exposed slot "
                        f"{exposed_slot!r}"
                    )
                if exposed_state_contract.partition != "data":
                    raise ValueError(
                        f"subflow {self.ref!r} exposed slot {exposed_slot!r} must use data state"
                    )
                if _canonical_json(_mutable_json(exposed_state_contract.schema)) != _canonical_json(
                    _mutable_json(validated_slot_schemas[exposed_slot])
                ):
                    raise ValueError(
                        f"subflow {self.ref!r} exposed slot {exposed_slot!r} schemas disagree"
                    )
        object.__setattr__(self, "state_keys", MappingProxyType(validated_state_keys))
        object.__setattr__(
            self,
            "_state_ownership_complete",
            declared_state_keys and self.exposed_slots is not None,
        )
        if any(not node_identifier.strip() for node_identifier in self.node_identifiers):
            raise ValueError(f"subflow {self.ref!r} node identifiers must be non-empty")
        object.__setattr__(self, "node_identifiers", frozenset(self.node_identifiers))

    @property
    def state_ownership_complete(self) -> bool:
        """Return whether the manifest closes the subflow's persistent state surface."""

        return self._state_ownership_complete

    @property
    def owned_state_keys(self) -> Mapping[str, SubflowStateKey]:
        """Return every state key known to the manifest, including legacy exposures."""

        return cast(Mapping[str, SubflowStateKey], self.state_keys)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable catalog entry without exposing mutable state."""

        return {
            "ref": self.ref,
            "description": self.description,
            "configuration_schema": _mutable_json(self.configuration_schema),
            "exposed_slots": (
                sorted(self.exposed_slots) if self.exposed_slots is not None else None
            ),
            "exposed_slot_schemas": {
                slot_name: _mutable_json(slot_schema)
                for slot_name, slot_schema in sorted(self.exposed_slot_schemas.items())
            },
            "capabilities": sorted(self.capabilities),
            "required_tools": dict(sorted(self.required_tools.items())),
            "state_keys": {
                state_key: state_contract.as_dict()
                for state_key, state_contract in sorted(self.owned_state_keys.items())
            },
            "state_ownership_complete": self.state_ownership_complete,
            "node_identifiers": sorted(self.node_identifiers),
            "legacy_manifest": self.legacy_manifest,
        }

    @classmethod
    def legacy(cls, ref: str) -> SubflowDefinition:
        legacy_definition = cls(
            ref=ref,
            description="Legacy subflow registered without a typed manifest.",
            configuration_schema={"type": "object", "additionalProperties": True},
            exposed_slots=None,
        )
        object.__setattr__(legacy_definition, "legacy_manifest", True)
        return legacy_definition


class SubflowRegistry:
    def __init__(self) -> None:
        self._subflows: dict[str, Subflow] = {}
        self._definitions: dict[str, SubflowDefinition] = {}

    def register(
        self,
        subflow: Subflow,
        *,
        definition: SubflowDefinition | None = None,
    ) -> None:
        ref = f"{subflow.name}@{subflow.major}"
        if definition is not None and definition.ref != ref:
            raise ValueError(
                f"subflow definition ref {definition.ref!r} does not match registry ref {ref!r}"
            )
        self._subflows[ref] = subflow
        if definition is not None:
            self._definitions[ref] = definition
        elif ref not in self._definitions:
            self._definitions[ref] = SubflowDefinition.legacy(ref)

    def has(self, ref: str) -> bool:
        return ref in self._subflows

    def get(self, ref: str) -> Subflow:
        if ref not in self._subflows:
            raise KeyError(f"subflow not registered: {ref!r}")
        return self._subflows[ref]

    def definition(self, ref: str) -> SubflowDefinition:
        if ref not in self._definitions:
            raise KeyError(f"subflow definition not registered: {ref!r}")
        return self._definitions[ref]

    @property
    def definitions(self) -> Mapping[str, SubflowDefinition]:
        return MappingProxyType(dict(self._definitions))

    def validate_configuration(
        self,
        ref: str,
        configuration: Mapping[str, Any],
        *,
        location: str,
    ) -> None:
        definition = self.definition(ref)
        schema = _mutable_json(definition.configuration_schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validation_errors = sorted(
            validator.iter_errors(dict(configuration)),
            key=lambda error: (
                tuple(str(segment) for segment in error.absolute_path),
                tuple(str(segment) for segment in error.absolute_schema_path),
                str(error.validator),
            ),
        )
        if not validation_errors:
            return
        validation_error = validation_errors[0]
        error_location = _validation_location(
            location,
            tuple(validation_error.absolute_path),
        )
        raise ValueError(f"{error_location}: {validation_error.message}")


_EXHAUSTION_MODES = ["reask", "skip", "default", "handoff", "END"]


def _address_definition() -> SubflowDefinition:
    address_schema = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["logradouro"],
                "properties": {
                    "logradouro": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "minLength": 1},
                    "bairro": {"type": "string", "minLength": 1},
                    "municipio": {"type": "string", "minLength": 1},
                },
            },
            {"type": "null"},
        ]
    }
    return SubflowDefinition(
        ref="address@1",
        description="Collect, geocode, and optionally confirm a citizen address.",
        configuration_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "required": {"type": "boolean"},
                "needs_confirmation": {"type": "boolean"},
                "max_attempts": {"type": "integer", "minimum": 1},
                "on_exhaust": {"enum": _EXHAUSTION_MODES},
            },
        },
        exposed_slots=frozenset({"address"}),
        exposed_slot_schemas={"address": address_schema},
        capabilities=frozenset({"geocoding"}),
        required_tools={"geocode": "1"},
        state_keys={
            "address": SubflowStateKey("data", address_schema),
            "address_confirmed": SubflowStateKey("data", {"type": "boolean"}),
            "address_needs_confirmation": SubflowStateKey("data", {"type": "boolean"}),
            "address_attempts": SubflowStateKey("data", {"type": "integer", "minimum": 1}),
            "address_completed": SubflowStateKey("data", {"type": "boolean"}),
            "address_defaulted": SubflowStateKey("data", {"type": "boolean"}),
            "address_skipped": SubflowStateKey("data", {"type": "boolean"}),
        },
        node_identifiers=frozenset({"collect_address", "confirm_address", "address_done"}),
    )


def _identification_definition() -> SubflowDefinition:
    cpf_schema = {"type": "string", "pattern": "^[0-9]{11}$"}
    email_schema = {"type": "string", "format": "email"}
    name_schema = {"type": "string", "minLength": 2}
    boolean_state = {"type": "boolean"}
    attempts_state = {"type": "integer", "minimum": 1}
    return SubflowDefinition(
        ref="identification@2",
        description="Collect citizen identity through CPF, gov.br, or anonymous continuation.",
        configuration_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "required": {"type": "boolean"},
                "methods": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"enum": ["cpf", "govbr", "anonimo"]},
                },
                "max_attempts": {"type": "integer", "minimum": 1},
                "on_exhaust": {"enum": _EXHAUSTION_MODES},
            },
        },
        exposed_slots=frozenset({"cpf", "email", "name"}),
        exposed_slot_schemas={
            "cpf": cpf_schema,
            "email": email_schema,
            "name": name_schema,
        },
        capabilities=frozenset({"external_authentication", "identity_lookup"}),
        required_tools={"cpf_lookup": "1", "get_user_info": "1"},
        state_keys={
            "cpf": SubflowStateKey("data", cpf_schema),
            "email": SubflowStateKey("data", email_schema),
            "name": SubflowStateKey("data", name_schema),
            "phone": SubflowStateKey("data", {"type": "string"}),
            "identification_method": SubflowStateKey("data", {"enum": ["cpf", "govbr", "anonimo"]}),
            "identificacao_pulada": SubflowStateKey("data", boolean_state),
            "cadastro_verificado": SubflowStateKey("data", boolean_state),
            "email_processed": SubflowStateKey("data", boolean_state),
            "name_processed": SubflowStateKey("data", boolean_state),
            "govbr_auth_sent": SubflowStateKey("data", boolean_state),
            "govbr_authenticated": SubflowStateKey("data", boolean_state),
            "_attempts_method": SubflowStateKey("data", attempts_state),
            "_attempts_cpf": SubflowStateKey("data", attempts_state),
            "_attempts_email": SubflowStateKey("data", attempts_state),
            "_attempts_name": SubflowStateKey("data", attempts_state),
            "_cpf_lookup_derived:email": SubflowStateKey("internal", boolean_state),
            "_cpf_lookup_derived:name": SubflowStateKey("internal", boolean_state),
            "_await_external_sent:authenticate_govbr": SubflowStateKey("internal", boolean_state),
            "_await_external_completed:authenticate_govbr": SubflowStateKey(
                "internal", boolean_state
            ),
        },
        node_identifiers=frozenset(
            {
                "select_identification_method",
                "authenticate_govbr",
                "collect_cpf",
                "collect_email",
                "collect_name",
                "identification_done",
            }
        ),
    )


def default_subflows() -> SubflowRegistry:
    from .address import AddressSubflow
    from .identification import IdentificationSubflow

    reg = SubflowRegistry()
    reg.register(AddressSubflow(), definition=_address_definition())
    reg.register(IdentificationSubflow(), definition=_identification_definition())
    return reg
