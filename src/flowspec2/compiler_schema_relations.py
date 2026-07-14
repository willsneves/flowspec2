"""JSON Schema relation and path proofs used by compiler contract validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Final, cast

from jsonschema import Draft202012Validator, FormatChecker

from .schema_contracts import schema_reference_target

CONTRACT_INVALID: Final[int] = -1
CONTRACT_UNKNOWN: Final[int] = 0
CONTRACT_VALID: Final[int] = 1
_VALUE_TYPE_ATOMS: Final[frozenset[str]] = frozenset(
    {
        "array",
        "boolean",
        "integer",
        "non_integer_number",
        "null",
        "object",
        "string",
    }
)
_NumericBoundary = tuple[Decimal, bool]
_NumericInterval = tuple[_NumericBoundary | None, _NumericBoundary | None]
_SCHEMA_ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$comment",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)


def mutable_contract_json(contract_fragment: Any) -> Any:
    if isinstance(contract_fragment, Mapping):
        return {
            property_name: mutable_contract_json(property_value)
            for property_name, property_value in contract_fragment.items()
        }
    if isinstance(contract_fragment, (list, tuple)):
        return [mutable_contract_json(element) for element in contract_fragment]
    return contract_fragment


def standalone_schema(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> Any:
    """Inline safe local references so contracts from different roots can be composed."""

    if isinstance(schema, Mapping):
        standalone_mapping = {
            keyword: standalone_schema(keyword_value, root_schema, reference_stack)
            for keyword, keyword_value in schema.items()
            if keyword != "$ref"
        }
        if (reference := schema.get("$ref")) is None:
            return standalone_mapping
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            raise ValueError("cyclic local schema reference cannot be materialized")
        referenced_schema = standalone_schema(
            schema_reference_target(root_schema, reference_text),
            root_schema,
            (*reference_stack, reference_text),
        )
        return combined_contract([standalone_mapping or True, referenced_schema])
    if isinstance(schema, (list, tuple)):
        return [standalone_schema(element, root_schema, reference_stack) for element in schema]
    return schema


def schema_accepts_known_instance(
    schema: Any,
    root_schema: Any,
    instance: object,
) -> bool:
    mutable_root_schema = mutable_contract_json(root_schema)
    validator = Draft202012Validator(
        mutable_root_schema,
        format_checker=FormatChecker(),
    )
    return validator.evolve(schema=mutable_contract_json(schema)).is_valid(cast(Any, instance))


def combined_contract(contract_fragments: list[Any], keyword: str = "allOf") -> Any:
    effective_fragments = [
        contract_fragment
        for contract_fragment in contract_fragments
        if contract_fragment is not True
    ]
    if not effective_fragments:
        return True
    if len(effective_fragments) == 1:
        return effective_fragments[0]
    return {keyword: effective_fragments}


def schema_property_contract(
    schema: Any,
    property_name: str,
    root_schema: Any,
    bound_keys: frozenset[str],
    reference_stack: tuple[str, ...] = (),
) -> Any:
    if schema is False:
        return False
    if schema is True or not isinstance(schema, Mapping):
        return True

    property_contracts: list[Any] = []
    properties = schema.get("properties")
    pattern_properties = schema.get("patternProperties")
    has_property_dispatch = (
        isinstance(properties, Mapping)
        or isinstance(pattern_properties, Mapping)
        or "additionalProperties" in schema
    )
    if has_property_dispatch:
        matching_contracts: list[Any] = []
        if isinstance(properties, Mapping) and property_name in properties:
            matching_contracts.append(properties[property_name])
        if isinstance(pattern_properties, Mapping):
            matching_contracts.extend(
                property_contract
                for property_pattern, property_contract in pattern_properties.items()
                if re.search(property_pattern, property_name) is not None
            )
        if not matching_contracts:
            matching_contracts.append(schema.get("additionalProperties", True))
        property_contracts.append(combined_contract(matching_contracts))

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        property_contracts.append(
            True
            if reference_text in reference_stack
            else schema_property_contract(
                schema_reference_target(root_schema, reference_text),
                property_name,
                root_schema,
                bound_keys,
                (*reference_stack, reference_text),
            )
        )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        property_contracts.extend(
            schema_property_contract(
                branch,
                property_name,
                root_schema,
                bound_keys,
                reference_stack,
            )
            for branch in all_of
        )
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, (list, tuple)):
            property_contracts.append(
                combined_contract(
                    [
                        schema_property_contract(
                            branch,
                            property_name,
                            root_schema,
                            bound_keys,
                            reference_stack,
                        )
                        for branch in branches
                    ],
                    "anyOf",
                )
            )
    if "if" in schema:
        property_contracts.append(
            combined_contract(
                [
                    schema_property_contract(
                        schema.get("then", True),
                        property_name,
                        root_schema,
                        bound_keys,
                        reference_stack,
                    ),
                    schema_property_contract(
                        schema.get("else", True),
                        property_name,
                        root_schema,
                        bound_keys,
                        reference_stack,
                    ),
                ],
                "anyOf",
            )
        )
    dependent_schemas = schema.get("dependentSchemas")
    if isinstance(dependent_schemas, Mapping):
        property_contracts.extend(
            schema_property_contract(
                dependent_schema,
                property_name,
                root_schema,
                bound_keys,
                reference_stack,
            )
            for trigger_property, dependent_schema in dependent_schemas.items()
            if trigger_property in bound_keys
        )
    return combined_contract(property_contracts)


def _instance_type_atom(schema_instance: Any) -> str:
    if isinstance(schema_instance, Mapping):
        return "object"
    if isinstance(schema_instance, list):
        return "array"
    if isinstance(schema_instance, bool):
        return "boolean"
    if schema_instance is None:
        return "null"
    if isinstance(schema_instance, int) or (
        isinstance(schema_instance, float) and schema_instance.is_integer()
    ):
        return "integer"
    if isinstance(schema_instance, float):
        return "non_integer_number"
    return "string"


def _atoms_for_schema_type(schema_type: str) -> frozenset[str]:
    if schema_type == "number":
        return frozenset({"integer", "non_integer_number"})
    if schema_type == "integer":
        return frozenset({"integer"})
    return frozenset({schema_type})


def schema_type_atoms(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> frozenset[str]:
    if schema is False:
        return frozenset()
    if schema is True or not isinstance(schema, Mapping):
        return _VALUE_TYPE_ATOMS

    possible_atoms = _VALUE_TYPE_ATOMS
    if (schema_types := schema.get("type")) is not None:
        declared_types = (schema_types,) if isinstance(schema_types, str) else tuple(schema_types)
        possible_atoms &= frozenset().union(
            *(_atoms_for_schema_type(schema_type) for schema_type in declared_types)
        )
    if "const" in schema:
        possible_atoms &= frozenset({_instance_type_atom(schema["const"])})
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        possible_atoms &= frozenset(
            _instance_type_atom(enum_member) for enum_member in enum_members
        )
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            possible_atoms &= schema_type_atoms(
                schema_reference_target(root_schema, reference_text),
                root_schema,
                (*reference_stack, reference_text),
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            possible_atoms &= schema_type_atoms(branch, root_schema, reference_stack)
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, (list, tuple)):
            branch_atoms = frozenset().union(
                *(schema_type_atoms(branch, root_schema, reference_stack) for branch in branches)
            )
            possible_atoms &= branch_atoms
    if "if" in schema:
        conditional_atoms = schema_type_atoms(
            schema.get("then", True), root_schema, reference_stack
        ) | schema_type_atoms(schema.get("else", True), root_schema, reference_stack)
        possible_atoms &= conditional_atoms
    return possible_atoms


def canonical_contract_instance(schema_instance: Any) -> str:
    return json.dumps(
        schema_instance,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def finite_schema_instances(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if schema is False:
        return {}
    if schema is True or not isinstance(schema, Mapping):
        return None

    finite_instances: dict[str, Any] | None = None

    def intersect_instances(candidate_instances: dict[str, Any]) -> None:
        nonlocal finite_instances
        finite_instances = (
            candidate_instances
            if finite_instances is None
            else {
                serialized_instance: schema_instance
                for serialized_instance, schema_instance in finite_instances.items()
                if serialized_instance in candidate_instances
            }
        )

    if "const" in schema:
        constant_instance = schema["const"]
        intersect_instances({canonical_contract_instance(constant_instance): constant_instance})
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        intersect_instances(
            {canonical_contract_instance(enum_member): enum_member for enum_member in enum_members}
        )
    if (schema_types := schema.get("type")) is not None:
        declared_types = {schema_types} if isinstance(schema_types, str) else set(schema_types)
        if declared_types and declared_types <= {"boolean", "null"}:
            finite_type_instances = [
                *([None] if "null" in declared_types else []),
                *([False, True] if "boolean" in declared_types else []),
            ]
            intersect_instances(
                {
                    canonical_contract_instance(type_instance): type_instance
                    for type_instance in finite_type_instances
                }
            )
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if (
            reference_text not in reference_stack
            and (
                referenced_instances := finite_schema_instances(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    (*reference_stack, reference_text),
                )
            )
            is not None
        ):
            intersect_instances(referenced_instances)
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            if (
                branch_instances := finite_schema_instances(branch, root_schema, reference_stack)
            ) is not None:
                intersect_instances(branch_instances)
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        branch_instance_sets = [
            finite_schema_instances(branch, root_schema, reference_stack) for branch in branches
        ]
        if all(branch_instances is not None for branch_instances in branch_instance_sets):
            intersect_instances(
                {
                    serialized_instance: schema_instance
                    for branch_instances in branch_instance_sets
                    if branch_instances is not None
                    for serialized_instance, schema_instance in branch_instances.items()
                }
            )
    if "if" in schema:
        conditional_instance_sets = [
            finite_schema_instances(
                schema.get(conditional_keyword, True),
                root_schema,
                reference_stack,
            )
            for conditional_keyword in ("then", "else")
        ]
        if all(
            conditional_instances is not None for conditional_instances in conditional_instance_sets
        ):
            intersect_instances(
                {
                    serialized_instance: schema_instance
                    for conditional_instances in conditional_instance_sets
                    if conditional_instances is not None
                    for serialized_instance, schema_instance in conditional_instances.items()
                }
            )

    if finite_instances is None:
        return None
    return {
        serialized_instance: schema_instance
        for serialized_instance, schema_instance in finite_instances.items()
        if schema_accepts_known_instance(schema, root_schema, schema_instance)
    }


def _stronger_lower_boundary(
    current_boundary: _NumericBoundary | None,
    candidate_boundary: _NumericBoundary,
) -> _NumericBoundary:
    if current_boundary is None or candidate_boundary[0] > current_boundary[0]:
        return candidate_boundary
    if candidate_boundary[0] == current_boundary[0]:
        return candidate_boundary[0], current_boundary[1] and candidate_boundary[1]
    return current_boundary


def _stronger_upper_boundary(
    current_boundary: _NumericBoundary | None,
    candidate_boundary: _NumericBoundary,
) -> _NumericBoundary:
    if current_boundary is None or candidate_boundary[0] < current_boundary[0]:
        return candidate_boundary
    if candidate_boundary[0] == current_boundary[0]:
        return candidate_boundary[0], current_boundary[1] and candidate_boundary[1]
    return current_boundary


def _schema_numeric_interval(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> _NumericInterval:
    if not isinstance(schema, Mapping):
        return None, None
    lower_boundary: _NumericBoundary | None = None
    upper_boundary: _NumericBoundary | None = None
    if isinstance((minimum := schema.get("minimum")), (int, float)):
        lower_boundary = _stronger_lower_boundary(lower_boundary, (Decimal(str(minimum)), True))
    if isinstance((exclusive_minimum := schema.get("exclusiveMinimum")), (int, float)):
        lower_boundary = _stronger_lower_boundary(
            lower_boundary, (Decimal(str(exclusive_minimum)), False)
        )
    if isinstance((maximum := schema.get("maximum")), (int, float)):
        upper_boundary = _stronger_upper_boundary(upper_boundary, (Decimal(str(maximum)), True))
    if isinstance((exclusive_maximum := schema.get("exclusiveMaximum")), (int, float)):
        upper_boundary = _stronger_upper_boundary(
            upper_boundary, (Decimal(str(exclusive_maximum)), False)
        )

    nested_schemas: list[Any] = []
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            nested_schemas.append(schema_reference_target(root_schema, reference_text))
            reference_stack = (*reference_stack, reference_text)
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        nested_schemas.extend(all_of)
    for nested_schema in nested_schemas:
        nested_lower, nested_upper = _schema_numeric_interval(
            nested_schema, root_schema, reference_stack
        )
        if nested_lower is not None:
            lower_boundary = _stronger_lower_boundary(lower_boundary, nested_lower)
        if nested_upper is not None:
            upper_boundary = _stronger_upper_boundary(upper_boundary, nested_upper)
    return lower_boundary, upper_boundary


def _numeric_intervals_overlap(
    source_interval: _NumericInterval,
    parameter_interval: _NumericInterval,
    *,
    integer_only: bool,
) -> bool:
    lower_boundary: _NumericBoundary | None = None
    upper_boundary: _NumericBoundary | None = None
    for candidate_lower in (source_interval[0], parameter_interval[0]):
        if candidate_lower is not None:
            lower_boundary = _stronger_lower_boundary(lower_boundary, candidate_lower)
    for candidate_upper in (source_interval[1], parameter_interval[1]):
        if candidate_upper is not None:
            upper_boundary = _stronger_upper_boundary(upper_boundary, candidate_upper)
    if lower_boundary is not None and upper_boundary is not None:
        if lower_boundary[0] > upper_boundary[0]:
            return False
        if lower_boundary[0] == upper_boundary[0] and not (lower_boundary[1] and upper_boundary[1]):
            return False
    if not integer_only:
        return True

    minimum_integer = (
        None
        if lower_boundary is None
        else int(lower_boundary[0].to_integral_value(rounding=ROUND_CEILING))
        + (
            1
            if not lower_boundary[1] and lower_boundary[0] == lower_boundary[0].to_integral_value()
            else 0
        )
    )
    maximum_integer = (
        None
        if upper_boundary is None
        else int(upper_boundary[0].to_integral_value(rounding=ROUND_FLOOR))
        - (
            1
            if not upper_boundary[1] and upper_boundary[0] == upper_boundary[0].to_integral_value()
            else 0
        )
    )
    return minimum_integer is None or maximum_integer is None or minimum_integer <= maximum_integer


def _schemas_are_clearly_disjoint(
    source_schema: Any,
    source_root_schema: Any,
    parameter_schema: Any,
    parameter_root_schema: Any,
) -> bool:
    source_instances = finite_schema_instances(source_schema, source_root_schema)
    if source_instances is not None:
        return not any(
            schema_accepts_known_instance(parameter_schema, parameter_root_schema, source_instance)
            for source_instance in source_instances.values()
        )
    parameter_instances = finite_schema_instances(parameter_schema, parameter_root_schema)
    if parameter_instances is not None:
        return not any(
            schema_accepts_known_instance(source_schema, source_root_schema, parameter_instance)
            for parameter_instance in parameter_instances.values()
        )

    shared_type_atoms = schema_type_atoms(source_schema, source_root_schema) & schema_type_atoms(
        parameter_schema, parameter_root_schema
    )
    if not shared_type_atoms:
        return True
    numeric_atoms = frozenset({"integer", "non_integer_number"})
    if shared_type_atoms - numeric_atoms:
        return False
    return not _numeric_intervals_overlap(
        _schema_numeric_interval(source_schema, source_root_schema),
        _schema_numeric_interval(parameter_schema, parameter_root_schema),
        integer_only=shared_type_atoms == {"integer"},
    )


def _schema_assertion_json(schema: Any) -> Any:
    if isinstance(schema, Mapping):
        return {
            keyword: _schema_assertion_json(keyword_value)
            for keyword, keyword_value in schema.items()
            if keyword not in _SCHEMA_ANNOTATION_KEYWORDS
        }
    if isinstance(schema, (list, tuple)):
        return [_schema_assertion_json(element) for element in schema]
    return schema


def schema_without_keyword(schema: Mapping[str, Any], keyword: str) -> Any:
    remaining_schema = {
        schema_keyword: schema_value
        for schema_keyword, schema_value in schema.items()
        if schema_keyword != keyword
    }
    return remaining_schema or True


def _schema_length_bound(
    schema: Any,
    root_schema: Any,
    keyword: str,
    reference_stack: tuple[str, ...] = (),
) -> int | None:
    if not isinstance(schema, Mapping):
        return None
    candidate_bounds: list[int | None] = [
        cast(int, schema[keyword])
        if isinstance(schema.get(keyword), int) and not isinstance(schema[keyword], bool)
        else None
    ]
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            candidate_bounds.append(
                _schema_length_bound(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    keyword,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        candidate_bounds.extend(
            _schema_length_bound(branch, root_schema, keyword, reference_stack) for branch in all_of
        )
    concrete_bounds = [bound for bound in candidate_bounds if bound is not None]
    if not concrete_bounds:
        return None
    return max(concrete_bounds) if keyword == "minLength" else min(concrete_bounds)


def _schema_object_count_bound(
    schema: Any,
    root_schema: Any,
    keyword: str,
    reference_stack: tuple[str, ...] = (),
) -> int | None:
    if not isinstance(schema, Mapping):
        return None
    candidate_bounds: list[int | None] = [
        cast(int, schema[keyword])
        if isinstance(schema.get(keyword), int) and not isinstance(schema[keyword], bool)
        else None
    ]
    if keyword == "minProperties":
        candidate_bounds.append(
            len(
                {
                    property_name
                    for property_name in schema.get("required", ())
                    if isinstance(property_name, str)
                }
            )
        )
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            candidate_bounds.append(
                _schema_object_count_bound(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    keyword,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        candidate_bounds.extend(
            _schema_object_count_bound(branch, root_schema, keyword, reference_stack)
            for branch in all_of
        )
    concrete_bounds = [bound for bound in candidate_bounds if bound is not None]
    if not concrete_bounds:
        return None
    return max(concrete_bounds) if keyword == "minProperties" else min(concrete_bounds)


def _schema_string_assertions(
    schema: Any,
    root_schema: Any,
    keyword: str,
    reference_stack: tuple[str, ...] = (),
) -> frozenset[str]:
    if not isinstance(schema, Mapping):
        return frozenset()
    assertions = {assertion for assertion in (schema.get(keyword),) if isinstance(assertion, str)}
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            assertions.update(
                _schema_string_assertions(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    keyword,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            assertions.update(
                _schema_string_assertions(branch, root_schema, keyword, reference_stack)
            )
    return frozenset(assertions)


def _lower_boundary_contains(
    source_boundary: _NumericBoundary | None,
    target_boundary: _NumericBoundary | None,
) -> bool:
    if target_boundary is None:
        return True
    if source_boundary is None or source_boundary[0] < target_boundary[0]:
        return False
    if source_boundary[0] > target_boundary[0]:
        return True
    return target_boundary[1] or not source_boundary[1]


def _upper_boundary_contains(
    source_boundary: _NumericBoundary | None,
    target_boundary: _NumericBoundary | None,
) -> bool:
    if target_boundary is None:
        return True
    if source_boundary is None or source_boundary[0] > target_boundary[0]:
        return False
    if source_boundary[0] < target_boundary[0]:
        return True
    return target_boundary[1] or not source_boundary[1]


def _multiple_of_is_subset(source_multiple: Any, target_multiple: Any) -> bool:
    if not isinstance(target_multiple, (int, float)) or isinstance(target_multiple, bool):
        return True
    if not isinstance(source_multiple, (int, float)) or isinstance(source_multiple, bool):
        return False
    source_decimal = Decimal(str(source_multiple))
    target_decimal = Decimal(str(target_multiple))
    return target_decimal > 0 and source_decimal > 0 and source_decimal % target_decimal == 0


def schema_is_proven_subset(
    source_schema: Any,
    source_root_schema: Any,
    target_schema: Any,
    target_root_schema: Any,
) -> bool:
    """Conservatively prove JSON-Schema language inclusion for flow value contracts."""

    if target_schema is True or source_schema is False:
        return True
    if target_schema is False:
        return finite_schema_instances(source_schema, source_root_schema) == {}
    if _schema_assertion_json(source_schema) == _schema_assertion_json(target_schema):
        return True

    source_instances = finite_schema_instances(source_schema, source_root_schema)
    if source_instances is not None:
        return all(
            schema_accepts_known_instance(target_schema, target_root_schema, source_instance)
            for source_instance in source_instances.values()
        )
    if not isinstance(source_schema, Mapping) or not isinstance(target_schema, Mapping):
        return False

    if (source_reference := source_schema.get("$ref")) is not None:
        source_without_reference = schema_without_keyword(source_schema, "$ref")
        if source_without_reference is True:
            return schema_is_proven_subset(
                schema_reference_target(source_root_schema, cast(str, source_reference)),
                source_root_schema,
                target_schema,
                target_root_schema,
            )

    for union_keyword in ("anyOf", "oneOf"):
        source_branches = source_schema.get(union_keyword)
        if not isinstance(source_branches, (list, tuple)):
            continue
        source_without_union = schema_without_keyword(source_schema, union_keyword)
        return all(
            schema_is_proven_subset(
                combined_contract([source_without_union, source_branch]),
                source_root_schema,
                target_schema,
                target_root_schema,
            )
            for source_branch in source_branches
        )

    if (target_reference := target_schema.get("$ref")) is not None:
        return schema_is_proven_subset(
            source_schema,
            source_root_schema,
            schema_without_keyword(target_schema, "$ref"),
            target_root_schema,
        ) and schema_is_proven_subset(
            source_schema,
            source_root_schema,
            schema_reference_target(target_root_schema, cast(str, target_reference)),
            target_root_schema,
        )

    if isinstance((target_all_of := target_schema.get("allOf")), (list, tuple)):
        return schema_is_proven_subset(
            source_schema,
            source_root_schema,
            schema_without_keyword(target_schema, "allOf"),
            target_root_schema,
        ) and all(
            schema_is_proven_subset(
                source_schema,
                source_root_schema,
                target_branch,
                target_root_schema,
            )
            for target_branch in target_all_of
        )

    for union_keyword in ("anyOf", "oneOf"):
        target_branches = target_schema.get(union_keyword)
        if not isinstance(target_branches, (list, tuple)):
            continue
        if not schema_is_proven_subset(
            source_schema,
            source_root_schema,
            schema_without_keyword(target_schema, union_keyword),
            target_root_schema,
        ):
            return False
        for branch_index, target_branch in enumerate(target_branches):
            if not schema_is_proven_subset(
                source_schema,
                source_root_schema,
                target_branch,
                target_root_schema,
            ):
                continue
            if union_keyword == "anyOf" or all(
                _schemas_are_clearly_disjoint(
                    source_schema,
                    source_root_schema,
                    other_branch,
                    target_root_schema,
                )
                for other_index, other_branch in enumerate(target_branches)
                if other_index != branch_index
            ):
                return True
        return False

    if any(keyword in target_schema for keyword in ("if", "not")):
        return False
    if "const" in target_schema or "enum" in target_schema:
        return False

    source_atoms = schema_type_atoms(source_schema, source_root_schema)
    target_atoms = schema_type_atoms(target_schema, target_root_schema)
    if not source_atoms <= target_atoms:
        return False

    numeric_atoms = frozenset({"integer", "non_integer_number"})
    if source_atoms & numeric_atoms:
        source_interval = _schema_numeric_interval(source_schema, source_root_schema)
        target_interval = _schema_numeric_interval(target_schema, target_root_schema)
        if not (
            _lower_boundary_contains(source_interval[0], target_interval[0])
            and _upper_boundary_contains(source_interval[1], target_interval[1])
            and _multiple_of_is_subset(
                source_schema.get("multipleOf"), target_schema.get("multipleOf")
            )
        ):
            return False

    if "string" in source_atoms:
        source_minimum_length = _schema_length_bound(source_schema, source_root_schema, "minLength")
        target_minimum_length = _schema_length_bound(target_schema, target_root_schema, "minLength")
        if target_minimum_length is not None and (
            source_minimum_length is None or source_minimum_length < target_minimum_length
        ):
            return False
        source_maximum_length = _schema_length_bound(source_schema, source_root_schema, "maxLength")
        target_maximum_length = _schema_length_bound(target_schema, target_root_schema, "maxLength")
        if target_maximum_length is not None and (
            source_maximum_length is None or source_maximum_length > target_maximum_length
        ):
            return False
        for string_keyword in ("format", "pattern"):
            if not _schema_string_assertions(
                target_schema, target_root_schema, string_keyword
            ) <= _schema_string_assertions(source_schema, source_root_schema, string_keyword):
                return False

    structured_atoms = frozenset({"array", "object"})
    if source_atoms & structured_atoms:
        if "object" in source_atoms:
            source_minimum_properties = _schema_object_count_bound(
                source_schema, source_root_schema, "minProperties"
            )
            target_minimum_properties = _schema_object_count_bound(
                target_schema, target_root_schema, "minProperties"
            )
            if target_minimum_properties is not None and (
                source_minimum_properties is None
                or source_minimum_properties < target_minimum_properties
            ):
                return False
            source_maximum_properties = _schema_object_count_bound(
                source_schema, source_root_schema, "maxProperties"
            )
            target_maximum_properties = _schema_object_count_bound(
                target_schema, target_root_schema, "maxProperties"
            )
            if target_maximum_properties is not None and (
                source_maximum_properties is None
                or source_maximum_properties > target_maximum_properties
            ):
                return False
        structured_assertions = set(target_schema) - {
            *_SCHEMA_ANNOTATION_KEYWORDS,
            "$anchor",
            "$defs",
            "$id",
            "$schema",
            "maxProperties",
            "minProperties",
            "type",
        }
        if structured_assertions:
            return False
    return True


def _projection_all(projections: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    return (
        all(projection[0] for projection in projections),
        all(projection[1] for projection in projections),
    )


def _projection_any(projections: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    return (
        any(projection[0] for projection in projections),
        any(projection[1] for projection in projections),
    )


def _projection_not(projection: tuple[bool, bool]) -> tuple[bool, bool]:
    can_match, must_match = projection
    return not must_match, not can_match


def _projection_one_of(projections: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    possible_projections = [projection for projection in projections if projection[0]]
    guaranteed_matches = sum(projection[1] for projection in possible_projections)
    return (
        bool(possible_projections) and guaranteed_matches < 2,
        guaranteed_matches == 1 and len(possible_projections) == 1,
    )


def _unknown_value_projection(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> tuple[bool, bool]:
    if schema is False:
        return False, False
    if schema is True or not isinstance(schema, Mapping):
        return True, True

    projections: list[tuple[bool, bool]] = []
    value_keywords = set(schema) - {
        "$anchor",
        "$comment",
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "allOf",
        "anyOf",
        "definitions",
        "description",
        "else",
        "examples",
        "if",
        "oneOf",
        "then",
        "title",
    }
    if value_keywords:
        projections.append((True, False))

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            projections.append((True, False))
        else:
            projections.append(
                _unknown_value_projection(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        projections.append(
            _projection_all(
                [
                    _unknown_value_projection(branch, root_schema, reference_stack)
                    for branch in all_of
                ]
            )
        )
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)):
        projections.append(
            _projection_any(
                [
                    _unknown_value_projection(branch, root_schema, reference_stack)
                    for branch in any_of
                ]
            )
        )
    if isinstance((one_of := schema.get("oneOf")), (list, tuple)):
        projections.append(
            _projection_one_of(
                [
                    _unknown_value_projection(branch, root_schema, reference_stack)
                    for branch in one_of
                ]
            )
        )
    if "not" in schema:
        projections.append(
            _projection_not(_unknown_value_projection(schema["not"], root_schema, reference_stack))
        )
    if "if" in schema:
        condition_projection = _unknown_value_projection(schema["if"], root_schema, reference_stack)
        conditional_projection = _projection_any(
            [
                _projection_all(
                    [
                        condition_projection,
                        _unknown_value_projection(
                            schema.get("then", True), root_schema, reference_stack
                        ),
                    ]
                ),
                _projection_all(
                    [
                        _projection_not(condition_projection),
                        _unknown_value_projection(
                            schema.get("else", True), root_schema, reference_stack
                        ),
                    ]
                ),
            ]
        )
        projections.append(conditional_projection)
    return _projection_all(projections) if projections else (True, True)


def key_projection_match(
    schema: Any,
    root_schema: Any,
    bound_keys: frozenset[str],
    reference_stack: tuple[str, ...] = (),
) -> tuple[bool, bool]:
    if schema is False:
        return False, False
    if schema is True or not isinstance(schema, Mapping):
        return True, True

    projections: list[tuple[bool, bool]] = []
    schema_types = schema.get("type")
    if schema_types is not None:
        supports_object = schema_types == "object" or (
            isinstance(schema_types, (list, tuple)) and "object" in schema_types
        )
        projections.append((supports_object, supports_object))

    if "const" in schema:
        constant_object = schema["const"]
        constant_keys_match = (
            isinstance(constant_object, Mapping)
            and frozenset(str(property_name) for property_name in constant_object) == bound_keys
        )
        projections.append((constant_keys_match, False))
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        matching_enum_member = any(
            isinstance(enum_member, Mapping)
            and frozenset(str(property_name) for property_name in enum_member) == bound_keys
            for enum_member in enum_members
        )
        projections.append((matching_enum_member, False))

    required_properties = {
        property_name
        for property_name in schema.get("required", ())
        if isinstance(property_name, str)
    }
    if required_properties:
        has_required_properties = required_properties <= bound_keys
        projections.append((has_required_properties, has_required_properties))
    if isinstance((minimum_properties := schema.get("minProperties")), int):
        meets_minimum = len(bound_keys) >= minimum_properties
        projections.append((meets_minimum, meets_minimum))
    if isinstance((maximum_properties := schema.get("maxProperties")), int):
        meets_maximum = len(bound_keys) <= maximum_properties
        projections.append((meets_maximum, meets_maximum))
    if (property_names := schema.get("propertyNames")) is not None:
        valid_property_names = all(
            schema_accepts_known_instance(property_names, root_schema, bound_key)
            for bound_key in bound_keys
        )
        projections.append((valid_property_names, valid_property_names))

    properties = schema.get("properties")
    pattern_properties = schema.get("patternProperties")
    additional_properties = schema.get("additionalProperties", True)
    if isinstance(properties, Mapping) or isinstance(pattern_properties, Mapping):
        property_projections: list[tuple[bool, bool]] = []
        for bound_key in bound_keys:
            matching_property_schemas: list[Any] = []
            if isinstance(properties, Mapping) and bound_key in properties:
                matching_property_schemas.append(properties[bound_key])
            if isinstance(pattern_properties, Mapping):
                matching_property_schemas.extend(
                    property_schema
                    for property_pattern, property_schema in pattern_properties.items()
                    if re.search(property_pattern, bound_key) is not None
                )
            if not matching_property_schemas:
                if additional_properties is False:
                    property_projections.append((False, False))
                    continue
                matching_property_schemas.append(additional_properties)
            property_projections.append(
                _projection_all(
                    [
                        _unknown_value_projection(property_schema, root_schema, reference_stack)
                        for property_schema in matching_property_schemas
                    ]
                )
            )
        if property_projections:
            projections.append(_projection_all(property_projections))
    elif additional_properties is False and bound_keys:
        projections.append((False, False))

    dependent_required = schema.get("dependentRequired")
    if isinstance(dependent_required, Mapping):
        for trigger_property, dependent_properties in dependent_required.items():
            if trigger_property not in bound_keys:
                continue
            dependencies_present = all(
                dependent_property in bound_keys for dependent_property in dependent_properties
            )
            projections.append((dependencies_present, dependencies_present))
    dependent_schemas = schema.get("dependentSchemas")
    if isinstance(dependent_schemas, Mapping):
        projections.extend(
            key_projection_match(
                dependent_schema,
                root_schema,
                bound_keys,
                reference_stack,
            )
            for trigger_property, dependent_schema in dependent_schemas.items()
            if trigger_property in bound_keys
        )

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            projections.append((True, False))
        else:
            projections.append(
                key_projection_match(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    bound_keys,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        projections.append(
            _projection_all(
                [
                    key_projection_match(branch, root_schema, bound_keys, reference_stack)
                    for branch in all_of
                ]
            )
        )
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)):
        projections.append(
            _projection_any(
                [
                    key_projection_match(branch, root_schema, bound_keys, reference_stack)
                    for branch in any_of
                ]
            )
        )
    if isinstance((one_of := schema.get("oneOf")), (list, tuple)):
        projections.append(
            _projection_one_of(
                [
                    key_projection_match(branch, root_schema, bound_keys, reference_stack)
                    for branch in one_of
                ]
            )
        )
    if "not" in schema:
        projections.append(
            _projection_not(
                key_projection_match(schema["not"], root_schema, bound_keys, reference_stack)
            )
        )
    if "if" in schema:
        condition_projection = key_projection_match(
            schema["if"], root_schema, bound_keys, reference_stack
        )
        projections.append(
            _projection_any(
                [
                    _projection_all(
                        [
                            condition_projection,
                            key_projection_match(
                                schema.get("then", True),
                                root_schema,
                                bound_keys,
                                reference_stack,
                            ),
                        ]
                    ),
                    _projection_all(
                        [
                            _projection_not(condition_projection),
                            key_projection_match(
                                schema.get("else", True),
                                root_schema,
                                bound_keys,
                                reference_stack,
                            ),
                        ]
                    ),
                ]
            )
        )

    return _projection_all(projections) if projections else (True, True)


def schema_property_status(
    schema: Any,
    property_name: str,
    root_schema: Any | None = None,
    reference_stack: tuple[str, ...] = (),
) -> int:
    resolved_root_schema = schema if root_schema is None else root_schema
    if schema is False:
        return CONTRACT_INVALID
    if schema is True or not isinstance(schema, Mapping):
        return CONTRACT_UNKNOWN
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            return CONTRACT_UNKNOWN
        return schema_property_status(
            schema_reference_target(resolved_root_schema, reference_text),
            property_name,
            resolved_root_schema,
            (*reference_stack, reference_text),
        )

    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        statuses = [
            schema_property_status(branch, property_name, resolved_root_schema, reference_stack)
            for branch in all_of
        ]
        if CONTRACT_INVALID in statuses:
            return CONTRACT_INVALID
        if CONTRACT_VALID in statuses:
            return CONTRACT_VALID

    for union_keyword in ("oneOf", "anyOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        statuses = [
            schema_property_status(branch, property_name, resolved_root_schema, reference_stack)
            for branch in branches
        ]
        if CONTRACT_VALID in statuses:
            return CONTRACT_VALID
        if statuses and all(status == CONTRACT_INVALID for status in statuses):
            return CONTRACT_INVALID
        return CONTRACT_UNKNOWN

    schema_types = schema.get("type")
    if isinstance(schema_types, str) and schema_types != "object":
        return CONTRACT_INVALID
    if isinstance(schema_types, (list, tuple)) and "object" not in schema_types:
        return CONTRACT_INVALID

    properties = schema.get("properties")
    if isinstance(properties, Mapping) and property_name in properties:
        return CONTRACT_VALID
    pattern_properties = schema.get("patternProperties")
    if isinstance(pattern_properties, Mapping) and any(
        re.search(pattern, property_name) is not None for pattern in pattern_properties
    ):
        return CONTRACT_VALID
    additional_properties = schema.get("additionalProperties", True)
    if additional_properties is False:
        return CONTRACT_INVALID
    return CONTRACT_VALID if isinstance(additional_properties, Mapping) else CONTRACT_UNKNOWN


def required_schema_properties(
    schema: Any,
    root_schema: Any | None = None,
    reference_stack: tuple[str, ...] = (),
) -> set[str]:
    resolved_root_schema = schema if root_schema is None else root_schema
    if not isinstance(schema, Mapping):
        return set()
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            return set()
        return required_schema_properties(
            schema_reference_target(resolved_root_schema, reference_text),
            resolved_root_schema,
            (*reference_stack, reference_text),
        )
    required_properties = {
        property_name
        for property_name in schema.get("required", ())
        if isinstance(property_name, str)
    }
    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        for branch in all_of:
            required_properties.update(
                required_schema_properties(branch, resolved_root_schema, reference_stack)
            )
    for union_keyword in ("oneOf", "anyOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)) or not branches:
            continue
        branch_requirements = [
            required_schema_properties(branch, resolved_root_schema, reference_stack)
            for branch in branches
        ]
        required_properties.update(set.intersection(*branch_requirements))
    return required_properties


def schema_path_status(
    schema: Any,
    path_segments: tuple[str, ...],
    root_schema: Any | None = None,
    reference_stack: tuple[str, ...] = (),
) -> int:
    resolved_root_schema = schema if root_schema is None else root_schema
    if schema is False:
        return CONTRACT_INVALID
    if not path_segments:
        return CONTRACT_VALID
    if schema is True or not isinstance(schema, Mapping):
        return CONTRACT_UNKNOWN
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            return CONTRACT_UNKNOWN
        return schema_path_status(
            schema_reference_target(resolved_root_schema, reference_text),
            path_segments,
            resolved_root_schema,
            (*reference_stack, reference_text),
        )

    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        statuses = [
            schema_path_status(branch, path_segments, resolved_root_schema, reference_stack)
            for branch in all_of
        ]
        if CONTRACT_INVALID in statuses:
            return CONTRACT_INVALID
        if CONTRACT_VALID in statuses:
            return CONTRACT_VALID

    for union_keyword in ("oneOf", "anyOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        statuses = [
            schema_path_status(branch, path_segments, resolved_root_schema, reference_stack)
            for branch in branches
        ]
        if CONTRACT_VALID in statuses:
            return CONTRACT_VALID
        if statuses and all(status == CONTRACT_INVALID for status in statuses):
            return CONTRACT_INVALID
        return CONTRACT_UNKNOWN

    path_segment, remaining_segments = path_segments[0], path_segments[1:]
    schema_types = schema.get("type")
    supports_array = schema_types == "array" or (
        isinstance(schema_types, (list, tuple)) and "array" in schema_types
    )
    supports_object = (
        schema_types in (None, "object")
        or isinstance(schema.get("properties"), Mapping)
        or (isinstance(schema_types, (list, tuple)) and "object" in schema_types)
    )

    if supports_array and path_segment.isdigit():
        items = schema.get("items", True)
        return schema_path_status(items, remaining_segments, resolved_root_schema, reference_stack)
    if supports_object:
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and path_segment in properties:
            return schema_path_status(
                properties[path_segment],
                remaining_segments,
                resolved_root_schema,
                reference_stack,
            )
        pattern_properties = schema.get("patternProperties")
        if isinstance(pattern_properties, Mapping):
            matching_schemas = [
                nested_schema
                for pattern, nested_schema in pattern_properties.items()
                if re.search(pattern, path_segment) is not None
            ]
            if matching_schemas:
                statuses = [
                    schema_path_status(
                        nested_schema,
                        remaining_segments,
                        resolved_root_schema,
                        reference_stack,
                    )
                    for nested_schema in matching_schemas
                ]
                if CONTRACT_INVALID in statuses:
                    return CONTRACT_INVALID
                if all(status == CONTRACT_VALID for status in statuses):
                    return CONTRACT_VALID
                return CONTRACT_UNKNOWN
        additional_properties = schema.get("additionalProperties", True)
        if additional_properties is False:
            return CONTRACT_INVALID
        if isinstance(additional_properties, Mapping):
            return schema_path_status(
                additional_properties,
                remaining_segments,
                resolved_root_schema,
                reference_stack,
            )
        return CONTRACT_UNKNOWN
    return CONTRACT_INVALID


def _combine_schema_alternatives(
    left_alternatives: list[list[Any]],
    right_alternatives: list[list[Any]],
) -> list[list[Any]]:
    return [
        [*left_alternative, *right_alternative]
        for left_alternative in left_alternatives
        for right_alternative in right_alternatives
    ]


def schema_conjunctive_alternatives(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> list[list[Any]]:
    if schema is False:
        return []
    if schema is True or not isinstance(schema, Mapping):
        return [[]]

    applicator_keywords = {
        "$ref",
        "allOf",
        "anyOf",
        "else",
        "if",
        "oneOf",
        "then",
    }
    direct_schema = {
        property_name: property_contract
        for property_name, property_contract in schema.items()
        if property_name not in applicator_keywords
    }
    alternatives: list[list[Any]] = [[direct_schema]] if direct_schema else [[]]

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            alternatives = _combine_schema_alternatives(
                alternatives,
                schema_conjunctive_alternatives(
                    schema_reference_target(root_schema, reference_text),
                    root_schema,
                    (*reference_stack, reference_text),
                ),
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            alternatives = _combine_schema_alternatives(
                alternatives,
                schema_conjunctive_alternatives(branch, root_schema, reference_stack),
            )
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        union_alternatives = [
            branch_alternative
            for branch in branches
            for branch_alternative in schema_conjunctive_alternatives(
                branch, root_schema, reference_stack
            )
        ]
        alternatives = _combine_schema_alternatives(alternatives, union_alternatives)
    if "if" in schema:
        condition_schema = schema["if"]
        conditional_alternatives = [
            *(
                _combine_schema_alternatives(
                    schema_conjunctive_alternatives(condition_schema, root_schema, reference_stack),
                    schema_conjunctive_alternatives(
                        schema.get("then", True), root_schema, reference_stack
                    ),
                )
            ),
            *(
                _combine_schema_alternatives(
                    [[{"not": condition_schema}]],
                    schema_conjunctive_alternatives(
                        schema.get("else", True), root_schema, reference_stack
                    ),
                )
            ),
        ]
        alternatives = _combine_schema_alternatives(alternatives, conditional_alternatives)
    return alternatives


def _alternative_required_properties(
    schema_fragments: list[Any],
    root_schema: Any,
) -> frozenset[str]:
    required_properties = {
        property_name
        for schema_fragment in schema_fragments
        if isinstance(schema_fragment, Mapping)
        for property_name in schema_fragment.get("required", ())
        if isinstance(property_name, str)
    }
    requirements_changed = True
    while requirements_changed:
        requirements_changed = False
        for schema_fragment in schema_fragments:
            if not isinstance(schema_fragment, Mapping):
                continue
            dependent_required = schema_fragment.get("dependentRequired")
            if isinstance(dependent_required, Mapping):
                for trigger_property, dependent_properties in dependent_required.items():
                    if trigger_property not in required_properties:
                        continue
                    previous_size = len(required_properties)
                    required_properties.update(
                        property_name
                        for property_name in dependent_properties
                        if isinstance(property_name, str)
                    )
                    requirements_changed = (
                        requirements_changed or len(required_properties) > previous_size
                    )
            dependent_schemas = schema_fragment.get("dependentSchemas")
            if not isinstance(dependent_schemas, Mapping):
                continue
            for trigger_property, dependent_schema in dependent_schemas.items():
                if trigger_property not in required_properties:
                    continue
                previous_size = len(required_properties)
                required_properties.update(
                    required_schema_properties(dependent_schema, root_schema)
                )
                requirements_changed = (
                    requirements_changed or len(required_properties) > previous_size
                )
    return frozenset(required_properties)


def _alternative_property_contract(
    schema_fragments: list[Any],
    property_name: str,
    root_schema: Any,
    required_properties: frozenset[str],
) -> Any:
    return combined_contract(
        [
            schema_property_contract(
                schema_fragment,
                property_name,
                root_schema,
                required_properties,
            )
            for schema_fragment in schema_fragments
        ]
    )


def _schema_is_unconditional(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> bool:
    if schema is True:
        return True
    if schema is False or not isinstance(schema, Mapping):
        return False
    assertion_keywords = set(schema) - {
        "$anchor",
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "description",
        "examples",
        "title",
    }
    if not assertion_keywords:
        return True
    if assertion_keywords == {"$ref"}:
        reference_text = cast(str, schema["$ref"])
        return reference_text in reference_stack or _schema_is_unconditional(
            schema_reference_target(root_schema, reference_text),
            root_schema,
            (*reference_stack, reference_text),
        )
    if assertion_keywords == {"allOf"} and isinstance(
        (all_of := schema.get("allOf")), (list, tuple)
    ):
        return all(
            _schema_is_unconditional(branch, root_schema, reference_stack) for branch in all_of
        )
    if assertion_keywords == {"anyOf"} and isinstance(
        (any_of := schema.get("anyOf")), (list, tuple)
    ):
        return any(
            _schema_is_unconditional(branch, root_schema, reference_stack) for branch in any_of
        )
    return False


def _schema_is_guaranteed_for_known_object(
    schema: Any,
    root_schema: Any,
    known_properties: Mapping[str, Any],
    required_properties: frozenset[str],
    reference_stack: tuple[str, ...] = (),
) -> bool:
    if schema is True:
        return True
    if schema is False or not isinstance(schema, Mapping):
        return False

    schema_types = schema.get("type")
    if schema_types is not None and not (
        schema_types == "object"
        or isinstance(schema_types, (list, tuple))
        and "object" in schema_types
    ):
        return False
    if "const" in schema or "enum" in schema:
        return False
    declared_required = {
        property_name
        for property_name in schema.get("required", ())
        if isinstance(property_name, str)
    }
    if not declared_required <= required_properties:
        return False
    if isinstance((minimum_properties := schema.get("minProperties")), int) and (
        len(required_properties) < minimum_properties
    ):
        return False
    if "maxProperties" in schema:
        return False
    if schema.get("additionalProperties") is False:
        return False
    if "propertyNames" in schema:
        return False
    if any(
        schema.get(dependency_keyword)
        for dependency_keyword in ("dependentRequired", "dependentSchemas")
    ):
        return False

    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for property_name, property_contract in properties.items():
            if property_name in known_properties:
                if not schema_accepts_known_instance(
                    property_contract,
                    root_schema,
                    known_properties[property_name],
                ):
                    return False
            elif not _schema_is_unconditional(property_contract, root_schema):
                return False
    pattern_properties = schema.get("patternProperties")
    if isinstance(pattern_properties, Mapping) and any(
        not _schema_is_unconditional(property_contract, root_schema)
        for property_contract in pattern_properties.values()
    ):
        return False

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack and not _schema_is_guaranteed_for_known_object(
            schema_reference_target(root_schema, reference_text),
            root_schema,
            known_properties,
            required_properties,
            (*reference_stack, reference_text),
        ):
            return False
    if isinstance((all_of := schema.get("allOf")), (list, tuple)) and not all(
        _schema_is_guaranteed_for_known_object(
            branch,
            root_schema,
            known_properties,
            required_properties,
            reference_stack,
        )
        for branch in all_of
    ):
        return False
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)) and not any(
        _schema_is_guaranteed_for_known_object(
            branch,
            root_schema,
            known_properties,
            required_properties,
            reference_stack,
        )
        for branch in any_of
    ):
        return False
    if any(composition_keyword in schema for composition_keyword in ("oneOf", "not", "if")):
        return False
    return True


def alternative_is_success_compatible(
    schema_fragments: list[Any],
    root_schema: Any,
) -> bool:
    required_properties = _alternative_required_properties(schema_fragments, root_schema)
    if "status" not in required_properties:
        return True
    status_contract = _alternative_property_contract(
        schema_fragments,
        "status",
        root_schema,
        required_properties,
    )
    if not schema_accepts_known_instance(status_contract, root_schema, "success"):
        return False
    known_properties = {"status": "success"}
    return not any(
        isinstance(schema_fragment, Mapping)
        and (negated_schema := schema_fragment.get("not")) is not None
        and _schema_is_guaranteed_for_known_object(
            negated_schema,
            root_schema,
            known_properties,
            required_properties,
        )
        for schema_fragment in schema_fragments
    )


def _alternative_requires_path(
    schema_fragments: list[Any],
    path_segments: tuple[str, ...],
    root_schema: Any,
) -> bool:
    required_properties = _alternative_required_properties(schema_fragments, root_schema)
    path_segment, remaining_segments = path_segments[0], path_segments[1:]
    if path_segment not in required_properties:
        return False
    if not remaining_segments:
        return True
    nested_contract = _alternative_property_contract(
        schema_fragments,
        path_segment,
        root_schema,
        required_properties,
    )
    return schema_requires_path_in_every_alternative(
        nested_contract,
        remaining_segments,
        root_schema,
    )


def schema_requires_path_in_every_alternative(
    schema: Any,
    path_segments: tuple[str, ...],
    root_schema: Any,
) -> bool:
    alternatives = schema_conjunctive_alternatives(schema, root_schema)
    return bool(alternatives) and all(
        _alternative_requires_path(
            schema_fragments,
            path_segments,
            root_schema,
        )
        for schema_fragments in alternatives
    )


def schema_requires_path_for_every_success(
    schema: Any,
    path_segments: tuple[str, ...],
) -> bool:
    alternatives = schema_conjunctive_alternatives(schema, schema)
    return all(
        not alternative_is_success_compatible(schema_fragments, schema)
        or _alternative_requires_path(schema_fragments, path_segments, schema)
        for schema_fragments in alternatives
    )
