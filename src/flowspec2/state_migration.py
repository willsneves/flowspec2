"""Deterministic, declarative migration of persisted FlowSpec service state.

The runtime deliberately rejects state produced by a different resolved flow
contract.  This module is the explicit bridge between two such contracts.  A
migration copies whole top-level partition members, supplies literal defaults,
or drops members; it never evaluates callbacks or an expression language.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic_core import PydanticSerializationError

from .clock import UtcClock, utc_timestamp
from .ir import FlowIR
from .json_codec import validate_json_value
from .models import ServiceMetadata, ServiceState

StatePartition = Literal["data", "internal", "payload"]

_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_PLAN_FORMAT: Final[str] = "flowspec2/state-migration-plan@1"
_REPORT_FORMAT: Final[str] = "flowspec2/state-migration-report@1"
_PARTITIONS: Final[tuple[StatePartition, ...]] = ("data", "internal", "payload")
_ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$anchor",
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_JSON_TYPES: Final[frozenset[str]] = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)


class StateMigrationError(ValueError):
    """Raised when a plan or state cannot be migrated without ambiguity."""


def _require_json_value(json_value: Any, *, boundary: str) -> None:
    try:
        validate_json_value(json_value, boundary=boundary)
    except ValueError as value_error:
        raise StateMigrationError(str(value_error)) from value_error


def _canonical_json(json_value: Any) -> str:
    return json.dumps(
        json_value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(json_value: Any) -> str:
    return hashlib.sha256(_canonical_json(json_value).encode("utf-8")).hexdigest()


def _escape_pointer_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _json_pointer(path_segments: tuple[str | int, ...]) -> str:
    return "".join(f"/{_escape_pointer_segment(str(segment))}" for segment in path_segments) or "/"


@dataclass(frozen=True, order=True)
class StatePath:
    """One whole top-level member in a generated state partition."""

    partition: StatePartition
    member: str

    def __post_init__(self) -> None:
        if self.partition not in _PARTITIONS:
            raise StateMigrationError(f"unsupported state partition {self.partition!r}")
        if not isinstance(self.member, str) or not self.member:
            raise StateMigrationError("state path member must be a non-empty string")

    @property
    def pointer(self) -> str:
        """Return the canonical JSON Pointer for this partition member."""

        return f"/{self.partition}/{_escape_pointer_segment(self.member)}"


@dataclass(frozen=True)
class StateCopy:
    """Copy one complete JSON value from a source path to a target path."""

    source: StatePath
    target: StatePath


@dataclass(frozen=True, init=False)
class StateDefault:
    """A literal target value stored internally as immutable canonical JSON."""

    target: StatePath
    _canonical_value_json: str = field(repr=False)

    def __init__(self, target: StatePath, default_value: Any) -> None:
        _require_json_value(default_value, boundary=f"migration default {target.pointer}")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "_canonical_value_json", _canonical_json(default_value))

    @property
    def default_value(self) -> Any:
        """Return a fresh mutable copy of the declared JSON literal."""

        return json.loads(self._canonical_value_json)


@dataclass(frozen=True)
class FlowStateContract:
    """Exact source or target provenance pinned by a migration plan."""

    ir_format: str
    flow: str
    version: str
    source_digest: str
    flow_ir_digest: str
    dependency_digest: str
    profile_digest: str
    state_schema_digest: str

    def __post_init__(self) -> None:
        for field_name in ("ir_format", "flow", "version"):
            field_value = cast(str, getattr(self, field_name))
            if not isinstance(field_value, str) or not field_value.strip():
                raise StateMigrationError(f"{field_name} must be a non-empty string")
        for field_name in (
            "source_digest",
            "flow_ir_digest",
            "dependency_digest",
            "profile_digest",
            "state_schema_digest",
        ):
            field_value = cast(str, getattr(self, field_name))
            if not isinstance(field_value, str) or _DIGEST_PATTERN.fullmatch(field_value) is None:
                raise StateMigrationError(f"{field_name} must be a lowercase SHA-256 digest")

    @classmethod
    def from_ir(cls, flow_ir: FlowIR) -> FlowStateContract:
        """Project the exact persisted-state contract from canonical IR."""

        return cls(
            ir_format=flow_ir.ir_format,
            flow=flow_ir.flow,
            version=flow_ir.version,
            source_digest=flow_ir.source_digest,
            flow_ir_digest=flow_ir.digest,
            dependency_digest=flow_ir.dependency_digest,
            profile_digest=flow_ir.profile_digest,
            state_schema_digest=_digest(flow_ir.state_schema()),
        )

    def to_dict(self) -> dict[str, str]:
        """Return the stable JSON representation used by plans and reports."""

        return {
            "ir_format": self.ir_format,
            "flow": self.flow,
            "version": self.version,
            "source_digest": self.source_digest,
            "flow_ir_digest": self.flow_ir_digest,
            "dependency_digest": self.dependency_digest,
            "profile_digest": self.profile_digest,
            "state_schema_digest": self.state_schema_digest,
        }


@dataclass(frozen=True)
class StateMigrationPlan:
    """Closed declarative transformation between two exact flow contracts."""

    source: FlowStateContract
    target: FlowStateContract
    copies: tuple[StateCopy, ...] = ()
    defaults: tuple[StateDefault, ...] = ()
    drops: tuple[StatePath, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "copies", tuple(self.copies))
        object.__setattr__(self, "defaults", tuple(self.defaults))
        object.__setattr__(self, "drops", tuple(self.drops))
        if not all(isinstance(copy_definition, StateCopy) for copy_definition in self.copies):
            raise StateMigrationError("copies must contain only StateCopy declarations")
        if not all(
            isinstance(default_definition, StateDefault) for default_definition in self.defaults
        ):
            raise StateMigrationError("defaults must contain only StateDefault declarations")
        if not all(isinstance(drop_path, StatePath) for drop_path in self.drops):
            raise StateMigrationError("drops must contain only StatePath declarations")

        source_paths = [copy_definition.source for copy_definition in self.copies]
        source_paths.extend(self.drops)
        duplicate_source_paths = _duplicates(source_paths)
        if duplicate_source_paths:
            raise StateMigrationError(
                "source paths must have one disposition: "
                + ", ".join(path.pointer for path in duplicate_source_paths)
            )

        target_paths = [copy_definition.target for copy_definition in self.copies]
        target_paths.extend(default_definition.target for default_definition in self.defaults)
        duplicate_target_paths = _duplicates(target_paths)
        if duplicate_target_paths:
            raise StateMigrationError(
                "target paths must have one producer: "
                + ", ".join(path.pointer for path in duplicate_target_paths)
            )

    def _payload(self) -> dict[str, Any]:
        sorted_copies = sorted(
            self.copies,
            key=lambda copy_definition: (
                copy_definition.source.pointer,
                copy_definition.target.pointer,
            ),
        )
        sorted_defaults = sorted(
            self.defaults,
            key=lambda default_definition: default_definition.target.pointer,
        )
        return {
            "format": _PLAN_FORMAT,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "copies": [
                {
                    "source": copy_definition.source.pointer,
                    "target": copy_definition.target.pointer,
                }
                for copy_definition in sorted_copies
            ],
            "defaults": [
                {
                    "target": default_definition.target.pointer,
                    "value": default_definition.default_value,
                }
                for default_definition in sorted_defaults
            ],
            "drops": sorted(drop_path.pointer for drop_path in self.drops),
        }

    @property
    def digest(self) -> str:
        """Return the stable digest of the complete declarative plan."""

        return _digest(self._payload())

    def canonical_json(self) -> str:
        """Serialize the plan independently of declaration ordering."""

        return _canonical_json({**self._payload(), "digest": self.digest})


def _duplicates(paths: list[StatePath]) -> tuple[StatePath, ...]:
    counts: dict[StatePath, int] = {}
    for path in paths:
        counts[path] = counts.get(path, 0) + 1
    return tuple(sorted(path for path, count in counts.items() if count > 1))


@dataclass(frozen=True, init=False)
class StateMigrationLossReport:
    """Canonical evidence for every copied, defaulted, and discarded path."""

    source: FlowStateContract
    target: FlowStateContract
    plan_digest: str
    source_state_digest: str
    target_state_digest: str
    copied: tuple[StateCopy, ...]
    defaulted: tuple[str, ...]
    dropped: tuple[str, ...]
    digest: str

    def __init__(
        self,
        *,
        plan: StateMigrationPlan,
        source_state: ServiceState,
        target_state: ServiceState,
    ) -> None:
        copied = tuple(
            sorted(
                plan.copies,
                key=lambda copy_definition: (
                    copy_definition.source.pointer,
                    copy_definition.target.pointer,
                ),
            )
        )
        defaulted = tuple(
            sorted(default_definition.target.pointer for default_definition in plan.defaults)
        )
        dropped_paths = [drop_path.pointer for drop_path in plan.drops]
        if source_state.agent_response is not None:
            dropped_paths.append("/agent_response")

        object.__setattr__(self, "source", plan.source)
        object.__setattr__(self, "target", plan.target)
        object.__setattr__(self, "plan_digest", plan.digest)
        object.__setattr__(self, "source_state_digest", _state_digest(source_state))
        object.__setattr__(self, "target_state_digest", _state_digest(target_state))
        object.__setattr__(self, "copied", copied)
        object.__setattr__(self, "defaulted", defaulted)
        object.__setattr__(self, "dropped", tuple(sorted(dropped_paths)))
        object.__setattr__(self, "digest", _digest(self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "format": _REPORT_FORMAT,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "plan_digest": self.plan_digest,
            "source_state_digest": self.source_state_digest,
            "target_state_digest": self.target_state_digest,
            "copied": [
                {"source": copy_definition.source.pointer, "target": copy_definition.target.pointer}
                for copy_definition in self.copied
            ],
            "defaulted": list(self.defaulted),
            "dropped": list(self.dropped),
        }

    def canonical_json(self) -> str:
        """Serialize the report with its self-verifying digest."""

        return _canonical_json({**self._payload(), "digest": self.digest})

    def verify(
        self,
        *,
        plan: StateMigrationPlan,
        source_state: ServiceState,
        target_state: ServiceState,
    ) -> None:
        """Raise if this report does not bind the supplied plan and states."""

        expected_report = StateMigrationLossReport(
            plan=plan,
            source_state=source_state,
            target_state=target_state,
        )
        if self != expected_report:
            raise StateMigrationError(
                "migration report does not match the supplied plan and states"
            )


@dataclass(frozen=True)
class StateMigrationResult:
    """A fresh runtime state and the evidence binding it to the migration."""

    state: ServiceState
    report: StateMigrationLossReport


def _state_projection(state: ServiceState) -> dict[str, Any]:
    try:
        projection = state.model_dump(mode="json")
    except PydanticSerializationError as serialization_error:
        raise StateMigrationError(
            f"service state is not canonically JSON serializable: {serialization_error}"
        ) from serialization_error
    _require_json_value(projection, boundary="service state")
    return projection


def _state_digest(state: ServiceState) -> str:
    return _digest(_state_projection(state))


def _partition_projection(state: ServiceState) -> dict[str, dict[str, Any]]:
    return {
        partition: copy.deepcopy(cast(dict[str, Any], getattr(state, partition)))
        for partition in _PARTITIONS
    }


def _validate_ir_contract(
    declared_contract: FlowStateContract,
    flow_ir: FlowIR,
    *,
    role: str,
) -> None:
    actual_contract = FlowStateContract.from_ir(flow_ir)
    if declared_contract == actual_contract:
        return
    for field_name in (
        "ir_format",
        "flow",
        "version",
        "source_digest",
        "flow_ir_digest",
        "dependency_digest",
        "profile_digest",
        "state_schema_digest",
    ):
        if getattr(declared_contract, field_name) != getattr(actual_contract, field_name):
            raise StateMigrationError(
                f"plan {role} {field_name} does not match the supplied {role} FlowIR"
            )
    raise StateMigrationError(f"plan {role} contract does not match the supplied {role} FlowIR")


def _validate_source_envelope(state: ServiceState, contract: FlowStateContract) -> None:
    if state.status != "progress":
        raise StateMigrationError("only active states with status 'progress' can be migrated")
    if not state.user_id.strip():
        raise StateMigrationError("source state user_id must be non-empty")
    if state.service_name != contract.flow:
        raise StateMigrationError("source state service_name does not match the source contract")
    if state.metadata.await_resume is not None:
        raise StateMigrationError(
            "a state with an accepted external-resume signal cannot be migrated safely"
        )
    expected_metadata = {
        "flow_version": contract.version,
        "flow_ir_digest": contract.flow_ir_digest,
        "dependency_digest": contract.dependency_digest,
        "profile_digest": contract.profile_digest,
    }
    for metadata_field, expected_value in expected_metadata.items():
        if getattr(state.metadata, metadata_field) != expected_value:
            raise StateMigrationError(
                f"source state metadata {metadata_field} does not match the source contract"
            )


def _schema_validator(schema: dict[str, Any], *, role: str) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as schema_error:
        raise StateMigrationError(
            f"{role} generated state schema is invalid: {schema_error.message}"
        ) from schema_error
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_partitions(
    partitions: dict[str, dict[str, Any]],
    validator: Draft202012Validator,
    *,
    role: str,
) -> None:
    _require_json_value(partitions, boundary=f"{role} state partitions")
    validation_errors = sorted(
        validator.iter_errors(partitions),
        key=lambda error: (
            tuple(str(segment) for segment in error.absolute_path),
            tuple(str(segment) for segment in error.absolute_schema_path),
            str(error.validator),
        ),
    )
    if not validation_errors:
        return
    validation_error = validation_errors[0]
    raise StateMigrationError(
        f"{role} state at {_json_pointer(tuple(validation_error.absolute_path))}: "
        f"{validation_error.message}"
    )


def _member_schema(root_schema: dict[str, Any], path: StatePath, *, role: str) -> Any:
    properties = root_schema.get("properties")
    partition_schema = properties.get(path.partition) if isinstance(properties, Mapping) else None
    partition_properties = (
        partition_schema.get("properties") if isinstance(partition_schema, Mapping) else None
    )
    if isinstance(partition_properties, Mapping) and path.member in partition_properties:
        return partition_properties[path.member]
    if isinstance(partition_schema, Mapping):
        additional_properties = partition_schema.get("additionalProperties", True)
        if additional_properties is not False:
            return additional_properties
    raise StateMigrationError(
        f"{role} path {path.pointer} is forbidden by the generated state schema"
    )


def _schema_without_annotations(schema: Any) -> Any:
    if isinstance(schema, Mapping):
        normalized_mapping = {
            key: _schema_without_annotations(nested_schema)
            for key, nested_schema in schema.items()
            if key not in _ANNOTATION_KEYWORDS
        }
        if isinstance(normalized_mapping.get("type"), list):
            normalized_mapping["type"] = sorted(normalized_mapping["type"])
        return normalized_mapping
    if isinstance(schema, list):
        return [_schema_without_annotations(nested_schema) for nested_schema in schema]
    return schema


def _local_reference_target(root_schema: Any, reference: str) -> Any:
    if not reference.startswith("#"):
        raise StateMigrationError(f"non-local state schema reference {reference!r}")
    fragment = unquote(reference[1:])
    if not fragment:
        return root_schema
    if not fragment.startswith("/"):
        raise StateMigrationError(f"unsupported state schema anchor reference {reference!r}")
    target = root_schema
    for encoded_segment in fragment[1:].split("/"):
        segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping) and segment in target:
            target = target[segment]
            continue
        if isinstance(target, list) and segment.isdigit() and int(segment) < len(target):
            target = target[int(segment)]
            continue
        raise StateMigrationError(f"unresolved state schema reference {reference!r}")
    return target


def _schema_with_resolved_reference(schema: Any, root_schema: Any) -> Any:
    if not isinstance(schema, Mapping) or "$ref" not in schema:
        return schema
    reference = schema["$ref"]
    if not isinstance(reference, str):
        raise StateMigrationError("state schema $ref must be a string")
    referenced_schema = _local_reference_target(root_schema, reference)
    siblings = {key: sibling_schema for key, sibling_schema in schema.items() if key != "$ref"}
    if not siblings:
        return referenced_schema
    return {"allOf": [referenced_schema, siblings]}


def _finite_schema_values(schema: Any, root_schema: Any) -> tuple[Any, ...] | None:
    schema = _schema_with_resolved_reference(schema, root_schema)
    if schema is False:
        return ()
    if not isinstance(schema, Mapping):
        return None
    if "const" in schema:
        return (copy.deepcopy(schema["const"]),)
    if isinstance(schema.get("enum"), list):
        return tuple(copy.deepcopy(schema["enum"]))
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, list):
            continue
        finite_branches = [_finite_schema_values(branch, root_schema) for branch in branches]
        if any(branch_values is None for branch_values in finite_branches):
            return None
        unique_values: dict[str, Any] = {}
        for branch_values in finite_branches:
            for branch_value in cast(tuple[Any, ...], branch_values):
                unique_values[_canonical_json(branch_value)] = branch_value
        return tuple(unique_values[key] for key in sorted(unique_values))
    return None


def _schema_types(schema: Any) -> frozenset[str]:
    if schema is False:
        return frozenset()
    if schema is True or not isinstance(schema, Mapping) or "type" not in schema:
        return _JSON_TYPES
    declared_type = schema["type"]
    if isinstance(declared_type, str):
        return frozenset({declared_type})
    if isinstance(declared_type, list):
        return frozenset(cast(list[str], declared_type))
    return frozenset()


def _types_are_subset(source_types: frozenset[str], target_types: frozenset[str]) -> bool:
    for source_type in source_types:
        if source_type in target_types:
            continue
        if source_type == "integer" and "number" in target_types:
            continue
        return False
    return True


def _schema_accepts(
    schema: Any,
    root_schema: Any,
    instance: Any,
) -> bool:
    try:
        validator = Draft202012Validator(
            cast(dict[str, Any], root_schema),
            format_checker=FormatChecker(),
        )
        return validator.evolve(schema=schema).is_valid(instance)
    except Exception as validation_error:  # noqa: BLE001
        raise StateMigrationError(
            f"state schema fragment could not be evaluated: {validation_error}"
        ) from validation_error


def _minimum_is_stronger(source: Any, target: Any) -> bool:
    if target is None:
        return True
    return isinstance(source, (int, float)) and source >= target


def _maximum_is_stronger(source: Any, target: Any) -> bool:
    if target is None:
        return True
    return isinstance(source, (int, float)) and source <= target


def _lower_bound(schema: Mapping[str, Any]) -> tuple[int | float, bool] | None:
    candidates = [
        (cast(int | float, schema[keyword]), keyword == "exclusiveMinimum")
        for keyword in ("minimum", "exclusiveMinimum")
        if isinstance(schema.get(keyword), (int, float)) and not isinstance(schema[keyword], bool)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda bound: (bound[0], bound[1]))


def _upper_bound(schema: Mapping[str, Any]) -> tuple[int | float, bool] | None:
    candidates = [
        (cast(int | float, schema[keyword]), keyword == "exclusiveMaximum")
        for keyword in ("maximum", "exclusiveMaximum")
        if isinstance(schema.get(keyword), (int, float)) and not isinstance(schema[keyword], bool)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda bound: (bound[0], not bound[1]))


def _lower_bound_is_stronger(
    source_schema: Mapping[str, Any],
    target_schema: Mapping[str, Any],
) -> bool:
    target_bound = _lower_bound(target_schema)
    if target_bound is None:
        return True
    source_bound = _lower_bound(source_schema)
    if source_bound is None:
        return False
    source_value, source_exclusive = source_bound
    target_value, target_exclusive = target_bound
    return source_value > target_value or (
        source_value == target_value and (source_exclusive or not target_exclusive)
    )


def _upper_bound_is_stronger(
    source_schema: Mapping[str, Any],
    target_schema: Mapping[str, Any],
) -> bool:
    target_bound = _upper_bound(target_schema)
    if target_bound is None:
        return True
    source_bound = _upper_bound(source_schema)
    if source_bound is None:
        return False
    source_value, source_exclusive = source_bound
    target_value, target_exclusive = target_bound
    return source_value < target_value or (
        source_value == target_value and (source_exclusive or not target_exclusive)
    )


def _schema_is_proven_subset(
    source_schema: Any,
    source_root_schema: Any,
    target_schema: Any,
    target_root_schema: Any,
) -> bool:
    if target_schema is True or source_schema is False:
        return True
    if target_schema is False:
        return source_schema is False
    if _schema_without_annotations(source_schema) == _schema_without_annotations(target_schema):
        return True
    source_schema = _schema_with_resolved_reference(source_schema, source_root_schema)
    target_schema = _schema_with_resolved_reference(target_schema, target_root_schema)

    finite_source_values = _finite_schema_values(source_schema, source_root_schema)
    if finite_source_values is not None:
        return all(
            _schema_accepts(target_schema, target_root_schema, source_value)
            for source_value in finite_source_values
        )
    if not isinstance(source_schema, Mapping) or not isinstance(target_schema, Mapping):
        return False

    for union_keyword in ("anyOf", "oneOf"):
        source_branches = source_schema.get(union_keyword)
        if isinstance(source_branches, list):
            source_siblings = {
                key: sibling_schema
                for key, sibling_schema in source_schema.items()
                if key != union_keyword
            }
            return all(
                _schema_is_proven_subset(
                    {"allOf": [source_siblings, branch]} if source_siblings else branch,
                    source_root_schema,
                    target_schema,
                    target_root_schema,
                )
                for branch in source_branches
            )
    source_all_of = source_schema.get("allOf")
    if isinstance(source_all_of, list) and any(
        _schema_is_proven_subset(
            branch,
            source_root_schema,
            target_schema,
            target_root_schema,
        )
        for branch in source_all_of
    ):
        return True

    target_all_of = target_schema.get("allOf")
    if isinstance(target_all_of, list):
        target_siblings = {
            key: sibling_schema for key, sibling_schema in target_schema.items() if key != "allOf"
        }
        return (
            not target_siblings
            or _schema_is_proven_subset(
                source_schema,
                source_root_schema,
                target_siblings,
                target_root_schema,
            )
        ) and all(
            _schema_is_proven_subset(
                source_schema,
                source_root_schema,
                branch,
                target_root_schema,
            )
            for branch in target_all_of
        )
    target_branches = target_schema.get("anyOf")
    if isinstance(target_branches, list):
        target_siblings = {
            key: sibling_schema for key, sibling_schema in target_schema.items() if key != "anyOf"
        }
        return (
            not target_siblings
            or _schema_is_proven_subset(
                source_schema,
                source_root_schema,
                target_siblings,
                target_root_schema,
            )
        ) and any(
            _schema_is_proven_subset(
                source_schema,
                source_root_schema,
                branch,
                target_root_schema,
            )
            for branch in target_branches
        )
    if "oneOf" in target_schema:
        return False

    if "const" in target_schema or "enum" in target_schema or "not" in target_schema:
        return False
    source_types = _schema_types(source_schema)
    target_types = _schema_types(target_schema)
    if not _types_are_subset(source_types, target_types):
        return False

    handled_target_keywords = {"type", *_ANNOTATION_KEYWORDS}
    if "string" in source_types:
        handled_target_keywords.update({"format", "maxLength", "minLength", "pattern"})
        if not _minimum_is_stronger(source_schema.get("minLength"), target_schema.get("minLength")):
            return False
        if not _maximum_is_stronger(source_schema.get("maxLength"), target_schema.get("maxLength")):
            return False
        for keyword in ("format", "pattern"):
            if keyword in target_schema and source_schema.get(keyword) != target_schema[keyword]:
                return False
    if {"integer", "number"} & source_types:
        handled_target_keywords.update(
            {"exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum", "multipleOf"}
        )
        if not _lower_bound_is_stronger(source_schema, target_schema):
            return False
        if not _upper_bound_is_stronger(source_schema, target_schema):
            return False
        if "multipleOf" in target_schema and source_schema.get("multipleOf") != target_schema.get(
            "multipleOf"
        ):
            return False
    if "object" in source_types:
        handled_target_keywords.update(
            {"additionalProperties", "maxProperties", "minProperties", "properties", "required"}
        )
        if not _minimum_is_stronger(
            source_schema.get("minProperties"), target_schema.get("minProperties")
        ):
            return False
        if not _maximum_is_stronger(
            source_schema.get("maxProperties"), target_schema.get("maxProperties")
        ):
            return False
        source_required = set(cast(list[str], source_schema.get("required", [])))
        target_required = set(cast(list[str], target_schema.get("required", [])))
        if not target_required <= source_required:
            return False
        source_properties = cast(dict[str, Any], source_schema.get("properties", {}))
        target_properties = cast(dict[str, Any], target_schema.get("properties", {}))
        source_additional = source_schema.get("additionalProperties", True)
        target_additional = target_schema.get("additionalProperties", True)
        for property_name, target_property_schema in target_properties.items():
            source_property_schema = source_properties.get(property_name, source_additional)
            if source_property_schema is False:
                continue
            if not _schema_is_proven_subset(
                source_property_schema,
                source_root_schema,
                target_property_schema,
                target_root_schema,
            ):
                return False
        if target_additional is False:
            if source_additional is not False:
                return False
            if any(
                property_name not in target_properties and property_schema is not False
                for property_name, property_schema in source_properties.items()
            ):
                return False
        elif target_additional is not True:
            if any(
                property_schema is not False
                and not _schema_is_proven_subset(
                    property_schema,
                    source_root_schema,
                    target_additional,
                    target_root_schema,
                )
                for property_name, property_schema in source_properties.items()
                if property_name not in target_properties
            ):
                return False
            if source_additional is True or not _schema_is_proven_subset(
                source_additional,
                source_root_schema,
                target_additional,
                target_root_schema,
            ):
                return False

    return not (set(target_schema) - handled_target_keywords)


def _validate_plan_paths(
    plan: StateMigrationPlan,
    source_schema: dict[str, Any],
    target_schema: dict[str, Any],
) -> None:
    for copy_definition in sorted(
        plan.copies,
        key=lambda definition: (definition.source.pointer, definition.target.pointer),
    ):
        source_member_schema = _member_schema(
            source_schema,
            copy_definition.source,
            role="source",
        )
        target_member_schema = _member_schema(
            target_schema,
            copy_definition.target,
            role="target",
        )
        if not _schema_is_proven_subset(
            source_member_schema,
            source_schema,
            target_member_schema,
            target_schema,
        ):
            raise StateMigrationError(
                f"copy {copy_definition.source.pointer} -> {copy_definition.target.pointer} "
                "is not proven schema-compatible"
            )
    for default_definition in sorted(
        plan.defaults,
        key=lambda definition: definition.target.pointer,
    ):
        target_member_schema = _member_schema(
            target_schema,
            default_definition.target,
            role="target",
        )
        if not _schema_accepts(
            target_member_schema,
            target_schema,
            default_definition.default_value,
        ):
            raise StateMigrationError(
                f"default for {default_definition.target.pointer} violates its target schema"
            )
    for drop_path in sorted(plan.drops):
        _member_schema(source_schema, drop_path, role="source")


def _validate_source_coverage(state: ServiceState, plan: StateMigrationPlan) -> None:
    declared_source_paths = {
        *(copy_definition.source for copy_definition in plan.copies),
        *plan.drops,
    }
    actual_source_paths = {
        StatePath(partition, member)
        for partition in _PARTITIONS
        for member in cast(dict[str, Any], getattr(state, partition))
    }
    missing_source_paths = sorted(declared_source_paths - actual_source_paths)
    if missing_source_paths:
        raise StateMigrationError(
            "declared source paths are missing from the active state: "
            + ", ".join(path.pointer for path in missing_source_paths)
        )
    unaccounted_source_paths = sorted(actual_source_paths - declared_source_paths)
    if unaccounted_source_paths:
        raise StateMigrationError(
            "active state paths require an explicit copy or drop: "
            + ", ".join(path.pointer for path in unaccounted_source_paths)
        )


def _build_target_partitions(
    state: ServiceState,
    plan: StateMigrationPlan,
) -> dict[str, dict[str, Any]]:
    target_partitions: dict[str, dict[str, Any]] = {partition: {} for partition in _PARTITIONS}
    for copy_definition in sorted(
        plan.copies,
        key=lambda definition: (definition.source.pointer, definition.target.pointer),
    ):
        source_partition = cast(dict[str, Any], getattr(state, copy_definition.source.partition))
        target_partitions[copy_definition.target.partition][copy_definition.target.member] = (
            copy.deepcopy(source_partition[copy_definition.source.member])
        )
    for default_definition in sorted(
        plan.defaults,
        key=lambda definition: definition.target.pointer,
    ):
        target_partitions[default_definition.target.partition][default_definition.target.member] = (
            default_definition.default_value
        )
    return target_partitions


def migrate_service_state(
    state: ServiceState,
    *,
    source_ir: FlowIR,
    target_ir: FlowIR,
    plan: StateMigrationPlan,
    clock: UtcClock,
) -> StateMigrationResult:
    """Atomically migrate one active state between two exact IR contracts.

    The source is never mutated.  Every present partition member must have one
    explicit disposition, every referenced path must exist in its generated
    schema and in the active source state, and every copy must be statically
    proven compatible.  The derived ``agent_response`` is cleared; an accepted
    external-resume signal is rejected rather than silently reinterpreted.
    """

    _validate_ir_contract(plan.source, source_ir, role="source")
    _validate_ir_contract(plan.target, target_ir, role="target")
    _validate_source_envelope(state, plan.source)
    source_schema = source_ir.state_schema()
    target_schema = target_ir.state_schema()
    source_validator = _schema_validator(source_schema, role="source")
    target_validator = _schema_validator(target_schema, role="target")
    _validate_partitions(
        _partition_projection(state),
        source_validator,
        role="source",
    )
    _validate_plan_paths(plan, source_schema, target_schema)
    _validate_source_coverage(state, plan)

    target_partitions = _build_target_partitions(state, plan)
    _validate_partitions(target_partitions, target_validator, role="target")
    migrated_state = ServiceState(
        user_id=state.user_id,
        service_name=plan.target.flow,
        status="progress",
        data=target_partitions["data"],
        internal=target_partitions["internal"],
        payload=target_partitions["payload"],
        metadata=ServiceMetadata(
            created_at=state.metadata.created_at,
            updated_at=utc_timestamp(clock),
            flow_version=plan.target.version,
            flow_ir_digest=plan.target.flow_ir_digest,
            dependency_digest=plan.target.dependency_digest,
            profile_digest=plan.target.profile_digest,
            saved=state.metadata.saved,
        ),
        agent_response=None,
    )
    report = StateMigrationLossReport(
        plan=plan,
        source_state=state,
        target_state=migrated_state,
    )
    return StateMigrationResult(state=migrated_state, report=report)
