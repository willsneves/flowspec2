"""FlowSpec slot, derive, entry, and terminal value contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .compiler_schema_relations import (
    canonical_contract_instance,
    combined_contract,
    finite_schema_instances,
    mutable_contract_json,
    schema_accepts_known_instance,
    schema_is_proven_subset,
    schema_property_contract,
    schema_type_atoms,
    schema_without_keyword,
    standalone_schema,
)
from .domains import make_slot_model
from .nodes import FlowContext
from .schema_contracts import validate_safe_local_schema_references
from .subflows import SubflowRegistry
from .tools import ToolDefinition


def validate_entry_args_schema(
    doc: dict[str, Any],
    ctx: FlowContext,
    subflows: SubflowRegistry,
) -> None:
    entry_args_schema = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], doc.get("route", {})).get("entry_args_schema"),
    )
    if entry_args_schema is None:
        return
    try:
        Draft202012Validator.check_schema(entry_args_schema)
    except SchemaError as schema_error:
        raise ValueError(
            f"$.route.entry_args_schema is not a valid Draft 2020-12 schema: {schema_error.message}"
        ) from schema_error
    validate_safe_local_schema_references(
        entry_args_schema,
        location="$.route.entry_args_schema",
    )
    if entry_args_schema.get("type") != "object":
        raise ValueError("$.route.entry_args_schema.type must be 'object'")
    if entry_args_schema.get("additionalProperties") is not False:
        raise ValueError("$.route.entry_args_schema.additionalProperties must be false")
    exposed_contracts = exposed_slot_value_contracts(
        doc,
        subflows,
        include_absence=False,
    )
    for property_name, property_schema in cast(
        dict[str, Any], entry_args_schema.get("properties", {})
    ).items():
        target_contract = declared_slot_value_contract(
            doc,
            property_name,
            include_absence=False,
        ) or exposed_contracts.get(property_name)
        if target_contract is None or property_name not in set(ctx.node_for_slot) | set(ctx.slots):
            raise ValueError(
                "$.route.entry_args_schema.properties"
                f"[{property_name!r}] does not resolve to a declared or exposed slot"
            )
        if schema_is_proven_subset(
            property_schema,
            entry_args_schema,
            target_contract.schema,
            target_contract.root_schema,
        ):
            continue
        property_contract_text = json.dumps(
            mutable_contract_json(property_schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        target_contract_text = json.dumps(
            mutable_contract_json(target_contract.schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        raise ValueError(
            "$.route.entry_args_schema.properties"
            f"[{property_name!r}] contract {property_contract_text} is not proven to be a "
            f"subset of {target_contract.origin} contract {target_contract_text}"
        )


@dataclass(frozen=True)
class FlowValueContract:
    schema: Any
    root_schema: Any
    origin: str
    total: bool


def declared_slot_value_contract(
    document: dict[str, Any],
    slot_name: str,
    *,
    include_absence: bool,
) -> FlowValueContract | None:
    slot_definition = document.get("slots", {}).get(slot_name)
    if not isinstance(slot_definition, Mapping):
        return None
    domain_name = cast(str, slot_definition["domain"])
    slot_model = make_slot_model(
        slot_name,
        domain_name,
        cast(dict[str, Any], document["domains"]),
        nullable=bool(slot_definition.get("nullable", False)),
    )
    source_root_schema = slot_model.model_json_schema()
    source_schema: Any = source_root_schema["properties"][slot_name]
    is_total = bool(slot_definition.get("required", False))
    if include_absence and not is_total:
        source_schema = {"anyOf": [source_schema, {"type": "null"}]}
    return FlowValueContract(
        schema=source_schema,
        root_schema=source_root_schema,
        origin=f"slot {slot_name!r} from domain {domain_name!r}",
        total=is_total,
    )


def _schema_excluding_null(schema: Any, root_schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return schema
    for union_keyword in ("anyOf", "oneOf"):
        union_branches = schema.get(union_keyword)
        if not isinstance(union_branches, (list, tuple)):
            continue
        non_null_branches = [
            union_branch
            for union_branch in union_branches
            if schema_type_atoms(union_branch, root_schema) != {"null"}
        ]
        schema_without_union = schema_without_keyword(schema, union_keyword)
        return combined_contract(
            [
                schema_without_union,
                (
                    combined_contract(non_null_branches, union_keyword)
                    if non_null_branches
                    else False
                ),
            ]
        )
    schema_types = schema.get("type")
    if isinstance(schema_types, (list, tuple)) and "null" in schema_types:
        non_null_types = [schema_type for schema_type in schema_types if schema_type != "null"]
        return {
            **schema,
            "type": non_null_types[0] if len(non_null_types) == 1 else non_null_types,
        }
    return schema


def exposed_slot_value_contracts(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    *,
    include_absence: bool,
) -> dict[str, FlowValueContract]:
    contracts: dict[str, FlowValueContract] = {}
    for use_declaration in document.get("uses", []):
        subflow_reference = cast(str, use_declaration["ref"])
        if not subflows.has(subflow_reference):
            continue
        definition = subflows.definition(subflow_reference)
        subflow_configuration = cast(dict[str, Any], use_declaration.get("with") or {})
        is_total = bool(subflow_configuration.get("required")) and (
            subflow_configuration.get("on_exhaust") != "skip"
        )
        for slot_name, slot_schema in definition.exposed_slot_schemas.items():
            standalone_slot_schema = standalone_schema(slot_schema, slot_schema)
            effective_slot_schema = (
                _schema_excluding_null(standalone_slot_schema, standalone_slot_schema)
                if is_total
                else standalone_slot_schema
            )
            allows_null = schema_accepts_known_instance(
                effective_slot_schema,
                effective_slot_schema,
                None,
            )
            contracts[slot_name] = FlowValueContract(
                schema=(
                    {"anyOf": [effective_slot_schema, {"type": "null"}]}
                    if include_absence and not is_total and not allows_null
                    else effective_slot_schema
                ),
                root_schema=effective_slot_schema,
                origin=f"slot {slot_name!r} exposed by subflow {subflow_reference!r}",
                total=is_total,
            )
    return contracts


def _derive_key_component(source_value: Any) -> str:
    return "" if source_value is None else str(source_value)


def _derive_is_total(
    derive_definition: Mapping[str, Any],
    source_contracts: list[FlowValueContract],
) -> bool:
    finite_source_values: list[tuple[Any, ...]] = []
    for source_contract in source_contracts:
        source_instances = finite_schema_instances(
            source_contract.schema,
            source_contract.root_schema,
        )
        if source_instances is None:
            break
        finite_source_values.append(tuple(source_instances.values()))
    else:
        default_value = derive_definition.get("default")
        default_source_index = (
            int(default_match.group(1))
            if isinstance(default_value, str)
            and (default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value))
            else None
        )
        lookup = cast(Mapping[str, Any], derive_definition["lookup"])
        return all(
            "|".join(_derive_key_component(source_value) for source_value in source_values)
            in lookup
            or (
                default_value is not None
                and (
                    default_source_index is None or source_values[default_source_index] is not None
                )
            )
            for source_values in product(*finite_source_values)
        )

    default_value = derive_definition.get("default")
    if default_value is None:
        return False
    if not isinstance(default_value, str) or not (
        default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value)
    ):
        return True
    default_source_contract = source_contracts[int(default_match.group(1))]
    return default_source_contract.total and not schema_accepts_known_instance(
        default_source_contract.schema,
        default_source_contract.root_schema,
        None,
    )


def derived_value_contract(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    slot_name: str,
    *,
    location: str,
    derive_stack: tuple[str, ...] = (),
) -> FlowValueContract | None:
    derive_definition = next(
        (
            cast(dict[str, Any], candidate_definition)
            for candidate_definition in document.get("derive", [])
            if candidate_definition["writes"] == slot_name
        ),
        None,
    )
    if derive_definition is None:
        return None
    if slot_name in derive_stack:
        raise ValueError(f"{location} resolves through a cyclic derive contract: {slot_name!r}")

    exposed_contracts = exposed_slot_value_contracts(
        document,
        subflows,
        include_absence=True,
    )
    source_contracts: list[FlowValueContract] = []
    for source_slot in cast(list[str], derive_definition["from"]):
        source_contract = (
            declared_slot_value_contract(
                document,
                source_slot,
                include_absence=True,
            )
            or derived_value_contract(
                document,
                subflows,
                source_slot,
                location=location,
                derive_stack=(*derive_stack, slot_name),
            )
            or exposed_contracts.get(source_slot)
        )
        if source_contract is None:
            return None
        source_contracts.append(source_contract)

    lookup_values = list(cast(dict[str, Any], derive_definition["lookup"]).values())
    unique_lookup_values = {
        canonical_contract_instance(lookup_value): lookup_value for lookup_value in lookup_values
    }
    produced_alternatives: list[Any] = (
        [{"enum": list(unique_lookup_values.values())}] if unique_lookup_values else []
    )
    default_value = derive_definition.get("default")
    if isinstance(default_value, str) and (
        default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value)
    ):
        default_source_contract = source_contracts[int(default_match.group(1))]
        produced_alternatives.append(
            standalone_schema(
                default_source_contract.schema,
                default_source_contract.root_schema,
            )
        )
    elif (
        default_value is not None
        and (serialized_default := canonical_contract_instance(default_value))
        not in unique_lookup_values
    ):
        produced_alternatives.append({"const": json.loads(serialized_default)})
    produced_schema = (
        combined_contract(produced_alternatives, "anyOf") if (produced_alternatives) else False
    )

    if (
        declared_target_contract := declared_slot_value_contract(
            document,
            slot_name,
            include_absence=False,
        )
    ) is not None and not schema_is_proven_subset(
        produced_schema,
        produced_schema,
        declared_target_contract.schema,
        declared_target_contract.root_schema,
    ):
        raise ValueError(
            f"{location} uses derived value {slot_name!r}, but its produced values are not "
            "proven to satisfy the declared slot domain"
        )

    is_total = _derive_is_total(derive_definition, source_contracts)
    source_schema = (
        produced_schema
        if is_total
        else combined_contract([produced_schema, {"type": "null"}], "anyOf")
    )
    return FlowValueContract(
        schema=source_schema,
        root_schema=source_schema,
        origin=f"derived value {slot_name!r}",
        total=is_total,
    )


def _terminal_source_value_contract(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    slot_name: str,
    *,
    location: str,
) -> FlowValueContract | None:
    return (
        declared_slot_value_contract(document, slot_name, include_absence=True)
        or derived_value_contract(
            document,
            subflows,
            slot_name,
            location=location,
        )
        or exposed_slot_value_contracts(
            document,
            subflows,
            include_absence=True,
        ).get(slot_name)
    )


def validate_terminal_binding_contracts(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    definition: ToolDefinition,
    input_bindings: list[dict[str, Any]],
) -> None:
    bound_keys = frozenset(cast(str, input_binding["param"]) for input_binding in input_bindings)
    for input_index, input_binding in enumerate(input_bindings):
        slot_name = cast(str, input_binding["slot"])
        input_location = f"$.terminal.input[{input_index}]"
        if (
            source_contract := _terminal_source_value_contract(
                document,
                subflows,
                slot_name,
                location=input_location,
            )
        ) is None:
            continue
        parameter_name = cast(str, input_binding["param"])
        parameter_schema = schema_property_contract(
            definition.input_schema,
            parameter_name,
            definition.input_schema,
            bound_keys,
        )
        if schema_is_proven_subset(
            source_contract.schema,
            source_contract.root_schema,
            parameter_schema,
            definition.input_schema,
        ):
            continue
        source_contract_text = json.dumps(
            mutable_contract_json(source_contract.schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        parameter_contract_text = json.dumps(
            mutable_contract_json(parameter_schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        raise ValueError(
            f"{input_location} binds {source_contract.origin} (source contract "
            f"{source_contract_text}) to tool parameter "
            f"{parameter_name!r} in {definition.identifier!r} (parameter contract "
            f"{parameter_contract_text}), but the source contract is not proven to be a "
            "subset of the parameter contract"
        )
