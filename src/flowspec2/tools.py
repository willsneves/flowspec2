"""Tool registry + injectable backends.

A *tool* is the unit a flow's ``entry.tool`` / ``terminal.tool`` /
``capabilities.await_external.on_resume.enrich`` binds to. Each is an async
callable ``(**kwargs) -> dict``. Backends default to in-memory fakes so the
suite runs offline; swap any of them for a real integration by passing a custom
``ToolRegistry`` to :class:`~flowspec2.runtime.FlowRuntime`.

Terminal idempotency lives here: ``replay`` caches a successful result under a
sha256 of the operation namespace and call inputs, keyed per user. The default
cache is process- and registry-local; cross-process guarantees require a host
adapter backed by durable replay storage.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, TypeAlias, cast
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .json_codec import validate_json_value

Tool = Callable[..., Awaitable[dict[str, Any]]]

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


def _local_anchor_target(root_schema: Any, anchor: str) -> Any:
    matching_targets: list[Any] = []

    def visit(schema_fragment: Any) -> None:
        if isinstance(schema_fragment, Mapping):
            if schema_fragment.get("$anchor") == anchor:
                matching_targets.append(schema_fragment)
            for nested_schema in schema_fragment.values():
                visit(nested_schema)
        elif isinstance(schema_fragment, (list, tuple)):
            for nested_schema in schema_fragment:
                visit(nested_schema)

    visit(root_schema)
    if len(matching_targets) != 1:
        raise ValueError(
            f"local schema anchor {anchor!r} must resolve exactly once; "
            f"resolved {len(matching_targets)} times"
        )
    return matching_targets[0]


def _local_reference_target(root_schema: Any, reference: str) -> Any:
    if not reference.startswith("#"):
        raise ValueError(f"tool schemas only support local references, got {reference!r}")
    fragment = unquote(reference[1:])
    if not fragment:
        return root_schema
    if not fragment.startswith("/"):
        return _local_anchor_target(root_schema, fragment)

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
        raise ValueError(f"local schema reference does not resolve: {reference!r}")
    return referenced_schema


def _validate_local_references(
    schema: Any,
    root_schema: Any,
    *,
    is_root: bool = True,
) -> None:
    if isinstance(schema, Mapping):
        if "$dynamicRef" in schema:
            raise ValueError("tool schemas do not support $dynamicRef")
        if "unevaluatedProperties" in schema:
            raise ValueError("tool schemas do not support unevaluatedProperties")
        if not is_root and "$id" in schema:
            raise ValueError("tool schemas do not support nested $id resources")
        if (reference := schema.get("$ref")) is not None:
            if not isinstance(reference, str):
                raise ValueError("tool schema $ref must be a string")
            _local_reference_target(root_schema, reference)
        for nested_schema in schema.values():
            _validate_local_references(nested_schema, root_schema, is_root=False)
    elif isinstance(schema, (list, tuple)):
        for nested_schema in schema:
            _validate_local_references(nested_schema, root_schema, is_root=False)


def _validate_reference_cycles(root_schema: Any) -> None:
    reference_edges: dict[int, int] = {}
    schema_nodes: dict[int, Any] = {}

    def collect(schema_fragment: Any) -> None:
        if isinstance(schema_fragment, Mapping):
            fragment_identifier = id(schema_fragment)
            schema_nodes[fragment_identifier] = schema_fragment
            if isinstance((reference := schema_fragment.get("$ref")), str):
                reference_target = _local_reference_target(root_schema, reference)
                if isinstance(reference_target, Mapping):
                    reference_edges[fragment_identifier] = id(reference_target)
                    schema_nodes[id(reference_target)] = reference_target
            for nested_fragment in schema_fragment.values():
                collect(nested_fragment)
        elif isinstance(schema_fragment, (list, tuple)):
            for nested_fragment in schema_fragment:
                collect(nested_fragment)

    collect(root_schema)
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(fragment_identifier: int) -> None:
        if fragment_identifier in visited:
            return
        if fragment_identifier in visiting:
            raise ValueError("tool schemas do not support cyclic local references")
        visiting.add(fragment_identifier)
        if (target_identifier := reference_edges.get(fragment_identifier)) is not None:
            visit(target_identifier)
        visiting.remove(fragment_identifier)
        visited.add(fragment_identifier)

    for schema_identifier in schema_nodes:
        visit(schema_identifier)


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


def _conjunctive_schema_fragments(schema: Any, root_schema: Any) -> list[Any]:
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


def _schema_accepts_instance(schema: Any, root_schema: Any, instance: Any) -> bool:
    validator = Draft202012Validator(root_schema, format_checker=FormatChecker())
    return validator.evolve(schema=schema).is_valid(instance)


def _object_constraints_may_match(schema: Any, root_schema: Any) -> bool:
    schema_fragments = _conjunctive_schema_fragments(schema, root_schema)
    if any(schema_fragment is False for schema_fragment in schema_fragments):
        return False

    required_properties: set[str] = set()
    minimum_properties = 0
    maximum_properties: int | None = None
    closed_property_sets: list[set[str]] = []
    property_name_schemas: list[Any] = []
    property_value_schemas: dict[str, list[Any]] = {}
    for schema_fragment in schema_fragments:
        if not isinstance(schema_fragment, Mapping):
            continue
        required_properties.update(
            property_name
            for property_name in schema_fragment.get("required", ())
            if isinstance(property_name, str)
        )
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
        properties = schema_fragment.get("properties")
        if isinstance(properties, Mapping):
            for property_name, property_schema in properties.items():
                property_value_schemas.setdefault(property_name, []).append(property_schema)
        pattern_properties = schema_fragment.get("patternProperties")
        rejects_every_pattern_property = isinstance(pattern_properties, Mapping) and any(
            pattern in {".*", "^.*$"} and pattern_schema is False
            for pattern, pattern_schema in pattern_properties.items()
        )
        if schema_fragment.get("additionalProperties") is False and (
            not pattern_properties or rejects_every_pattern_property
        ):
            properties = schema_fragment.get("properties")
            permitted_properties = set(properties) if isinstance(properties, Mapping) else set()
            if rejects_every_pattern_property:
                permitted_properties.clear()
            closed_property_sets.append(permitted_properties)

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

    for schema_fragment in schema_fragments:
        if not isinstance(schema_fragment, Mapping):
            continue
        dependent_schemas = schema_fragment.get("dependentSchemas")
        if not isinstance(dependent_schemas, Mapping):
            continue
        if any(
            trigger_property in required_properties and dependent_schema is False
            for trigger_property, dependent_schema in dependent_schemas.items()
        ):
            return False

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
    if minimum_properties > 0 and any(
        property_name_schema is False for property_name_schema in property_name_schemas
    ):
        return False
    for required_property in required_properties:
        required_property_schemas = property_value_schemas.get(required_property, [])
        if not required_property_schemas:
            continue
        combined_property_boundary = _boundary_all(
            [
                _object_boundary_match(property_schema, root_schema)
                for property_schema in required_property_schemas
            ]
        )
        if not any(category_match[0] for category_match in combined_property_boundary):
            return False

    finite_candidates = _finite_object_candidates(schema_fragments)
    return finite_candidates is None or any(
        _schema_accepts_instance(schema, root_schema, candidate_object)
        for candidate_object in finite_candidates.values()
    )


def _object_boundary_match(
    schema: Any,
    root_schema: Any,
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


def _validated_schema(schema: Mapping[str, Any], location: str) -> Mapping[str, Any]:
    mutable_schema = _mutable_json(schema)
    try:
        json.dumps(mutable_schema, allow_nan=False)
        declared_dialect = mutable_schema.get("$schema")
        if declared_dialect is not None and declared_dialect not in _DRAFT_2020_12_URIS:
            raise ValueError(
                f"tool schemas must use JSON Schema Draft 2020-12, got {declared_dialect!r}"
            )
        Draft202012Validator.check_schema(mutable_schema)
        _validate_local_references(mutable_schema, mutable_schema)
        _validate_reference_cycles(mutable_schema)
    except (SchemaError, TypeError, ValueError) as error:
        detail = error.message if isinstance(error, SchemaError) else str(error)
        raise ValueError(f"{location} is not a valid JSON Schema: {detail}") from error
    boundary_match = _object_boundary_match(mutable_schema, mutable_schema)
    object_match = boundary_match[_OBJECT_CATEGORY_INDEX]
    if not object_match[0]:
        raise ValueError(f"{location} cannot validate any object-shaped callable boundary")
    if any(
        category_match[0]
        for category_index, category_match in enumerate(boundary_match)
        if category_index != _OBJECT_CATEGORY_INDEX
    ):
        raise ValueError(f"{location} must only validate object-shaped callable boundaries")
    return cast(Mapping[str, Any], _immutable_json(mutable_schema))


def _contract_validation_path(validation_error: Any) -> str:
    return "".join(
        "/" + str(path_segment).replace("~", "~0").replace("/", "~1")
        for path_segment in validation_error.absolute_path
    )


def _validate_contract_instance(
    definition: ToolDefinition,
    instance: object,
    *,
    boundary: str,
    schema: Mapping[str, Any],
) -> None:
    mutable_schema = _mutable_json(schema)
    validator = Draft202012Validator(mutable_schema, format_checker=FormatChecker())
    validation_errors = sorted(
        validator.iter_errors(cast(Any, instance)),
        key=lambda error: (
            tuple(str(path_segment) for path_segment in error.absolute_path),
            tuple(str(path_segment) for path_segment in error.absolute_schema_path),
            str(error.validator),
        ),
    )
    if not validation_errors:
        return
    validation_error = validation_errors[0]
    instance_path = _contract_validation_path(validation_error) or "/"
    raise ValueError(
        f"tool contract {definition.identifier!r} rejected {boundary} at "
        f"{instance_path} ({validation_error.validator})"
    )


@dataclass(frozen=True)
class ToolEffects:
    """Static effect hints for orchestration, review, and policy enforcement."""

    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True

    def __post_init__(self) -> None:
        if self.read_only and self.destructive:
            raise ValueError("a read-only tool cannot be destructive")


@dataclass(frozen=True)
class ToolDefinition:
    """Immutable, versioned contract for a registered tool callable."""

    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    effects: ToolEffects = field(default_factory=ToolEffects)
    legacy_contract: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool definition name must be non-empty")
        if not self.version.strip():
            raise ValueError(f"tool {self.name!r} version must be non-empty")
        if not self.description.strip():
            raise ValueError(f"tool {self.name!r} description must be non-empty")
        object.__setattr__(
            self,
            "input_schema",
            _validated_schema(self.input_schema, f"tool {self.name!r} input_schema"),
        )
        object.__setattr__(
            self,
            "output_schema",
            _validated_schema(self.output_schema, f"tool {self.name!r} output_schema"),
        )

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable catalog entry without exposing mutable state."""

        return {
            "name": self.name,
            "version": self.version,
            "identifier": self.identifier,
            "description": self.description,
            "input_schema": _mutable_json(self.input_schema),
            "output_schema": _mutable_json(self.output_schema),
            "effects": {
                "read_only": self.effects.read_only,
                "destructive": self.effects.destructive,
                "idempotent": self.effects.idempotent,
                "open_world": self.effects.open_world,
            },
            "legacy_contract": self.legacy_contract,
        }

    @classmethod
    def legacy(cls, name: str) -> ToolDefinition:
        open_object_schema = {"type": "object", "additionalProperties": True}
        return cls(
            name=name,
            version="unversioned",
            description="Legacy callable registered without a typed contract.",
            input_schema=open_object_schema,
            output_schema=open_object_schema,
            effects=ToolEffects(open_world=True),
            legacy_contract=True,
        )


@dataclass
class _ReplayFlight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    participants: int = 0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._definitions: dict[str, ToolDefinition] = {}
        self._binding_revisions: dict[str, int] = {}
        self._next_binding_revision = 0
        self._replay: dict[str, dict[str, Any]] = {}
        self._replay_flights: dict[str, _ReplayFlight] = {}

    def register(
        self,
        name: str,
        tool: Tool,
        *,
        definition: ToolDefinition | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("tool registry name must be non-empty")
        if definition is not None and definition.name != name:
            raise ValueError(
                f"tool definition name {definition.name!r} does not match registry name {name!r}"
            )
        self._next_binding_revision += 1
        self._binding_revisions[name] = self._next_binding_revision
        self._tools[name] = tool
        if definition is not None:
            self._definitions[name] = definition
        elif name not in self._definitions:
            self._definitions[name] = ToolDefinition.legacy(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def definition(self, name: str) -> ToolDefinition:
        if name not in self._definitions:
            raise KeyError(f"tool definition not registered: {name!r}")
        return self._definitions[name]

    def binding_revision(self, name: str) -> int:
        """Return the process-local revision of one callable/contract binding."""

        if name not in self._binding_revisions:
            raise KeyError(f"tool not registered: {name!r}")
        return self._binding_revisions[name]

    @property
    def definitions(self) -> Mapping[str, ToolDefinition]:
        return MappingProxyType(dict(self._definitions))

    async def call(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"tool not registered: {name!r}")
        definition = self.definition(name)
        validate_json_value(
            kwargs,
            boundary=f"tool contract {definition.identifier!r} rejected input",
        )
        _validate_contract_instance(
            definition,
            kwargs,
            boundary="input",
            schema=definition.input_schema,
        )
        tool_result = await self._tools[name](**kwargs)
        validate_json_value(
            tool_result,
            boundary=f"tool contract {definition.identifier!r} rejected output",
        )
        _validate_contract_instance(
            definition,
            tool_result,
            boundary="output",
            schema=definition.output_schema,
        )
        return tool_result

    # ── idempotency replay cache ─────────────────────────────────────────────

    @staticmethod
    def operation_namespace(
        flow_name: str,
        flow_revision: str,
        terminal_step: str,
        tool_identifier: str,
    ) -> str:
        """Return a stable namespace for one versioned terminal operation."""

        namespace_components = {
            "flow": flow_name,
            "revision": flow_revision,
            "terminal": terminal_step,
            "tool": tool_identifier,
        }
        if empty_components := [
            component_name
            for component_name, component_content in namespace_components.items()
            if not component_content
        ]:
            rendered_components = ", ".join(empty_components)
            raise ValueError(
                f"operation namespace components must be non-empty: {rendered_components}"
            )
        return _canonical_json(namespace_components)

    @staticmethod
    def idempotency_key(
        user_id: str,
        operation_namespace: str,
        inputs: dict[str, Any],
    ) -> str:
        """Hash strict JSON inputs within a stable, versioned operation namespace."""

        if not operation_namespace:
            raise ValueError("operation namespace must be non-empty")
        validate_json_value(inputs, boundary="idempotency inputs")
        blob = _canonical_json(
            {
                "inputs": inputs,
                "operation_namespace": operation_namespace,
                "user_id": user_id,
            }
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def replay_get(self, key: str) -> dict[str, Any] | None:
        replayed_result = self._replay.get(key)
        return copy.deepcopy(replayed_result) if replayed_result is not None else None

    def replay_put(self, key: str, result: dict[str, Any]) -> None:
        self._replay[key] = copy.deepcopy(result)

    async def call_idempotent(
        self,
        name: str,
        idempotency_key: str,
        /,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Coalesce one process-local call per key and replay successful results."""

        if not idempotency_key:
            raise ValueError("idempotency key must be non-empty")
        replay_flight = self._replay_flights.get(idempotency_key)
        if replay_flight is None:
            replay_flight = _ReplayFlight()
            self._replay_flights[idempotency_key] = replay_flight
        replay_flight.participants += 1
        try:
            async with replay_flight.lock:
                if (replayed_result := self.replay_get(idempotency_key)) is not None:
                    return replayed_result
                tool_result = await self.call(name, **kwargs)
                if tool_result.get("status", "success") == "success":
                    self.replay_put(idempotency_key, tool_result)
                return tool_result
        finally:
            replay_flight.participants -= 1
            if (
                replay_flight.participants == 0
                and self._replay_flights.get(idempotency_key) is replay_flight
            ):
                self._replay_flights.pop(idempotency_key, None)


# ── default fake backends ────────────────────────────────────────────────────


async def _fake_hub_search(**kwargs: Any) -> dict[str, Any]:
    """Best-effort knowledge load (entry.tool). Never blocks the flow."""
    return {
        "status": "ok",
        "name": "Streetlight repair",
        "summary": "Repair public lighting infrastructure.",
        "estimated_resolution": "Three business days",
    }


async def _fake_geocode(address: str = "", **_: Any) -> dict[str, Any]:
    """Geocode/validate an address string (address subflow backend)."""
    text = (address or "").strip()
    if not text:
        return {"status": "not_found", "error": "empty address"}
    folded = text.lower()
    kind = "square" if "square" in folded else "street"
    return {
        "status": "ok",
        "needs_confirmation": True,
        "address": {
            "street": text,
            "kind": kind,
            "district": "Downtown",
            "city": "Example City",
        },
    }


async def _fake_brazilian_tax_id_lookup(brazilian_tax_id: str = "", **_: Any) -> dict[str, Any]:
    """Look up a citizen's registry by Brazilian tax ID (identification backend)."""
    return {"status": "ok", "name": "", "email": "", "phones": []}


async def _fake_govbr_enrich(brazilian_tax_id: str = "", **_: Any) -> dict[str, Any]:
    """Enrich gov.br data with internal registry (await_external.on_resume.enrich)."""
    return {"status": "ok", "phones": ["5521999999999"]}


async def _fake_open_service_request(**inputs: Any) -> dict[str, Any]:
    """Open a ticketing-system request (terminal.tool). Return its protocol ID."""
    digest = hashlib.sha256(_canonical_json(inputs).encode()).hexdigest()
    return {
        "status": "success",
        "protocol_id": f"REQ-{digest[:10].upper()}",
        "message": "Service request opened successfully.",
    }


def _status_schema(*statuses: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(statuses)}


def _default_tool_definitions() -> dict[str, ToolDefinition]:
    read_only_external = ToolEffects(read_only=True, idempotent=True, open_world=True)
    return {
        "hub_search": ToolDefinition(
            name="hub_search",
            version="1",
            description="Load public service knowledge for flow initialization.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"const": "ok"},
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "estimated_resolution": {"type": "string"},
                },
                "required": ["status"],
            },
            effects=read_only_external,
        ),
        "geocode": ToolDefinition(
            name="geocode",
            version="1",
            description="Resolve and classify a citizen-provided address.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"address": {"type": "string"}},
                "required": ["address"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": _status_schema("ok", "not_found", "error"),
                    "needs_confirmation": {"type": "boolean"},
                    "error": {"type": "string"},
                    "address": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "street": {"type": "string", "minLength": 1},
                            "kind": {"type": "string", "minLength": 1},
                            "district": {"type": "string"},
                            "city": {"type": "string"},
                        },
                        "required": ["street"],
                    },
                },
                "required": ["status"],
                "allOf": [
                    {
                        "if": {
                            "properties": {"status": {"const": "ok"}},
                            "required": ["status"],
                        },
                        "then": {"required": ["address", "needs_confirmation"]},
                        "else": {"required": ["error"]},
                    }
                ],
            },
            effects=read_only_external,
        ),
        "brazilian_tax_id_lookup": ToolDefinition(
            name="brazilian_tax_id_lookup",
            version="1",
            description="Look up optional citizen contact data by Brazilian tax ID.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"brazilian_tax_id": {"type": "string", "pattern": "^[0-9]{11}$"}},
                "required": ["brazilian_tax_id"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": _status_schema("ok", "error"),
                    "name": {"type": "string"},
                    "email": {"anyOf": [{"const": ""}, {"type": "string", "format": "email"}]},
                    "phones": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^\\+?[1-9][0-9]{7,14}$"},
                    },
                    "error": {"type": "string", "minLength": 1},
                },
                "required": ["status"],
                "allOf": [
                    {
                        "if": {
                            "properties": {"status": {"const": "error"}},
                            "required": ["status"],
                        },
                        "then": {"required": ["error"]},
                    }
                ],
            },
            effects=read_only_external,
        ),
        "get_user_info": ToolDefinition(
            name="get_user_info",
            version="1",
            description="Enrich a gov.br identity with internal citizen contact data.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"brazilian_tax_id": {"type": "string", "pattern": "^[0-9]{11}$"}},
                "required": ["brazilian_tax_id"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": _status_schema("ok", "error"),
                    "phones": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^\\+?[1-9][0-9]{7,14}$"},
                    },
                    "email": {"anyOf": [{"const": ""}, {"type": "string", "format": "email"}]},
                    "name": {"type": "string"},
                    "error": {"type": "string", "minLength": 1},
                },
                "required": ["status"],
                "allOf": [
                    {
                        "if": {
                            "properties": {"status": {"const": "error"}},
                            "required": ["status"],
                        },
                        "then": {"required": ["error"]},
                    }
                ],
            },
            effects=read_only_external,
        ),
        "open_service_request": ToolDefinition(
            name="open_service_request",
            version="2",
            description="Create a service request in the ticketing system and return its protocol.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "streetlightIssue": {"type": "string", "minLength": 1},
                    "address": {"type": "object", "minProperties": 1},
                    "referencePoint": {"type": ["string", "null"]},
                    "nearSportsCourt": {"type": ["boolean", "null"]},
                    "requester": {"type": ["string", "null"]},
                    "potholeType": {"type": "string", "minLength": 1},
                    "size": {"type": "string", "minLength": 1},
                    "problem": {"type": "string", "minLength": 1},
                },
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": _status_schema("success", "retryable", "fatal"),
                    "protocol_id": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                    "error": {"type": "string", "minLength": 1},
                },
                "required": ["status"],
                "allOf": [
                    {
                        "if": {
                            "properties": {"status": {"const": "success"}},
                            "required": ["status"],
                        },
                        "then": {"required": ["protocol_id"]},
                    }
                ],
            },
            effects=ToolEffects(
                read_only=False,
                destructive=False,
                idempotent=True,
                open_world=True,
            ),
        ),
    }


def default_tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    definitions = _default_tool_definitions()
    reg.register("hub_search", _fake_hub_search, definition=definitions["hub_search"])
    reg.register("geocode", _fake_geocode, definition=definitions["geocode"])
    reg.register(
        "brazilian_tax_id_lookup",
        _fake_brazilian_tax_id_lookup,
        definition=definitions["brazilian_tax_id_lookup"],
    )
    reg.register("get_user_info", _fake_govbr_enrich, definition=definitions["get_user_info"])
    reg.register(
        "open_service_request",
        _fake_open_service_request,
        definition=definitions["open_service_request"],
    )
    return reg
