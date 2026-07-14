"""External suspension, resume-token, and recovery compiler contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Optional, cast

from .compiler_schema_relations import (
    CONTRACT_VALID,
    combined_contract,
    mutable_contract_json,
    required_schema_properties,
    schema_accepts_known_instance,
    schema_is_proven_subset,
    schema_path_status,
    schema_property_contract,
    schema_requires_path_in_every_alternative,
    schema_type_atoms,
)
from .compiler_tool_contracts import (
    registered_tool_definition,
    validate_read_only_tool_effects,
    validate_tool_inputs,
    validate_tool_result_path,
)
from .compiler_value_contracts import FlowValueContract, declared_slot_value_contract
from .schema_contracts import schema_reference_target, validate_resume_token_schema
from .subflows import SubflowRegistry
from .tools import ToolRegistry

_RESUME_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\$token(?:\.[A-Za-z][A-Za-z0-9_]*)+$"
)


def _uses_identification_v2(doc: dict[str, Any]) -> bool:
    return any(use.get("ref") == "identification@2" for use in doc.get("uses", []))


def bind_await_external(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    capability = cast(
        Optional[dict[str, Any]],
        (doc.get("capabilities") or {}).get("await_external"),
    )
    path_steps = [step for step in doc["path"] if step.get("await_external") is True]
    if len(path_steps) > 1:
        raise ValueError("flowspec/2 supports one capabilities.await_external binding")
    if path_steps and capability is None:
        raise ValueError("path await_external requires capabilities.await_external")
    if capability is None:
        return None

    if path_steps:
        path_step = path_steps[0]
        unsupported_fields = {
            "ask_when",
            "skip_when",
            "on_reject",
            "correctable",
        } & path_step.keys()
        if unsupported_fields:
            unsupported = ", ".join(sorted(unsupported_fields))
            raise ValueError(f"path await_external does not support these fields: {unsupported}")
        path_node_id = path_step.get("step")
        capability_node_id = capability.get("step")
        if path_node_id and capability_node_id and path_node_id != capability_node_id:
            raise ValueError(
                "path await_external step conflicts with capabilities.await_external.step"
            )
        bound_node_id = path_node_id or capability_node_id or "await_external"
        path_step["step"] = bound_node_id
        capability["step"] = bound_node_id
        return capability

    if not capability.get("step") and _uses_identification_v2(doc):
        # Backward compatibility for documents authored before the explicit
        # subflow binding existed.
        capability["step"] = "authenticate_govbr"
    if not capability.get("step"):
        raise ValueError(
            "capabilities.await_external must declare step or have a path await_external anchor"
        )
    return capability


def _validate_binding_map(
    bindings: dict[str, Any],
    *,
    namespace: str,
    location: str,
) -> None:
    if not isinstance(bindings, dict):
        raise ValueError(f"{location} must be an object")
    prefix = f"${namespace}."
    for target, binding in bindings.items():
        if not isinstance(target, str) or not target:
            raise ValueError(f"{location} contains an empty destination")
        if binding is not None and not isinstance(binding, (str, int, float, bool)):
            raise ValueError(f"{location}.{target} must be a JSON scalar")
        if isinstance(binding, str) and binding.startswith("$"):
            reference_path = binding.removeprefix(prefix)
            if (
                not binding.startswith(prefix)
                or not reference_path
                or any(not segment for segment in reference_path.split("."))
            ):
                raise ValueError(f"{location}.{target} must use {prefix}path")


def _resume_reference_segments(reference: str, *, location: str) -> tuple[str, ...]:
    if _RESUME_REFERENCE_PATTERN.fullmatch(reference) is None:
        raise ValueError(
            f"{location} must be an exact $token property path with identifier segments"
        )
    return tuple(reference.removeprefix("$token.").split("."))


def _schema_array_index_contract(
    schema: Any,
    array_index: int,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> Any:
    if schema is False:
        return False
    if schema is True or not isinstance(schema, Mapping):
        return True

    contract_fragments: list[Any] = []
    prefix_items = schema.get("prefixItems")
    if isinstance(prefix_items, (list, tuple)) and array_index < len(prefix_items):
        contract_fragments.append(prefix_items[array_index])
    elif "items" in schema:
        contract_fragments.append(schema["items"])

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        contract_fragments.append(
            True
            if reference_text in reference_stack
            else _schema_array_index_contract(
                schema_reference_target(root_schema, reference_text),
                array_index,
                root_schema,
                (*reference_stack, reference_text),
            )
        )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        contract_fragments.extend(
            _schema_array_index_contract(branch, array_index, root_schema, reference_stack)
            for branch in all_of
        )
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, (list, tuple)):
            contract_fragments.append(
                combined_contract(
                    [
                        _schema_array_index_contract(
                            branch,
                            array_index,
                            root_schema,
                            reference_stack,
                        )
                        for branch in branches
                    ],
                    "anyOf",
                )
            )
    return combined_contract(contract_fragments)


def _schema_path_value_contract(
    schema: Any,
    path_segments: tuple[str, ...],
    root_schema: Any,
) -> Any:
    current_contract = schema
    for path_segment in path_segments:
        if path_segment.isdigit():
            current_contract = _schema_array_index_contract(
                current_contract,
                int(path_segment),
                root_schema,
            )
        else:
            current_contract = schema_property_contract(
                current_contract,
                path_segment,
                root_schema,
                frozenset(required_schema_properties(current_contract, root_schema)),
            )
    return current_contract


def _resume_token_path_contract(
    token_schema: dict[str, Any],
    reference: str,
    *,
    location: str,
    require_presence: bool,
) -> FlowValueContract:
    path_segments = _resume_reference_segments(reference, location=location)
    path_status = schema_path_status(token_schema, path_segments)
    if path_status != CONTRACT_VALID:
        raise ValueError(f"{location} does not resolve exactly in resume.schema: {reference!r}")
    if require_presence and not schema_requires_path_in_every_alternative(
        token_schema,
        path_segments,
        token_schema,
    ):
        raise ValueError(f"{location} must be required by every resume.schema alternative")
    path_contract = _schema_path_value_contract(token_schema, path_segments, token_schema)
    return FlowValueContract(
        schema=path_contract,
        root_schema=token_schema,
        origin=f"resume token path {reference!r}",
        total=require_presence,
    )


@dataclass(frozen=True)
class _AwaitStateTargetContract:
    value_contract: FlowValueContract
    partition: str


def _await_state_target_contract(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    state_key: str,
) -> _AwaitStateTargetContract | None:
    if declared_contract := declared_slot_value_contract(
        document,
        state_key,
        include_absence=False,
    ):
        slot_definition = cast(dict[str, Any], document["slots"][state_key])
        return _AwaitStateTargetContract(
            value_contract=declared_contract,
            partition=cast(str, slot_definition.get("persist", "data")),
        )

    for use_definition in document.get("uses", []):
        subflow_reference = cast(str, use_definition["ref"])
        if not subflows.has(subflow_reference):
            continue
        subflow_definition = subflows.definition(subflow_reference)
        state_contract = subflow_definition.owned_state_keys.get(state_key)
        if state_contract is None:
            continue
        state_schema = mutable_contract_json(state_contract.schema)
        return _AwaitStateTargetContract(
            value_contract=FlowValueContract(
                schema=state_schema,
                root_schema=state_schema,
                origin=f"state key {state_key!r} owned by subflow {subflow_reference!r}",
                total=True,
            ),
            partition=state_contract.partition,
        )
    return None


def _validate_source_contract_for_await_state(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    state_key: str,
    source_contract: FlowValueContract,
    *,
    location: str,
) -> None:
    target_contract = _await_state_target_contract(document, subflows, state_key)
    if target_contract is None:
        return
    if target_contract.partition == "internal" and state_key not in document.get("slots", {}):
        raise ValueError(f"{location} cannot write subflow-owned internal state key {state_key!r}")
    if schema_is_proven_subset(
        source_contract.schema,
        source_contract.root_schema,
        target_contract.value_contract.schema,
        target_contract.value_contract.root_schema,
    ):
        return
    raise ValueError(
        f"{location} {source_contract.origin} is not proven to satisfy "
        f"{target_contract.value_contract.origin}"
    )


def _validate_literal_for_await_state(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    state_key: str,
    literal_value: Any,
    *,
    location: str,
) -> None:
    target_contract = _await_state_target_contract(document, subflows, state_key)
    if target_contract is None:
        return
    if target_contract.partition == "internal" and state_key not in document.get("slots", {}):
        raise ValueError(f"{location} cannot write subflow-owned internal state key {state_key!r}")
    if not schema_accepts_known_instance(
        target_contract.value_contract.schema,
        target_contract.value_contract.root_schema,
        literal_value,
    ):
        raise ValueError(
            f"{location} literal does not satisfy {target_contract.value_contract.origin}"
        )


def _validate_typed_resume_contract(
    document: dict[str, Any],
    capability: dict[str, Any],
    tools: ToolRegistry,
    subflows: SubflowRegistry,
) -> None:
    resume_contract = cast(dict[str, Any] | None, capability.get("resume"))
    if resume_contract is None:
        return
    token_schema = cast(dict[str, Any], resume_contract["schema"])
    validate_resume_token_schema(token_schema)
    correlation_reference = cast(str, resume_contract["correlation"])
    correlation_contract = _resume_token_path_contract(
        token_schema,
        correlation_reference,
        location="$.capabilities.await_external.resume.correlation",
        require_presence=True,
    )
    scalar_correlation_types = frozenset({"boolean", "integer", "non_integer_number", "string"})
    correlation_types = schema_type_atoms(
        correlation_contract.schema,
        correlation_contract.root_schema,
    )
    if not correlation_types or not correlation_types <= scalar_correlation_types:
        raise ValueError(
            "$.capabilities.await_external.resume.correlation must resolve to a "
            "non-null JSON scalar"
        )

    on_resume = cast(dict[str, Any], capability.get("on_resume") or {})
    for state_key, binding in cast(dict[str, Any], on_resume.get("set") or {}).items():
        binding_location = f"$.capabilities.await_external.on_resume.set[{state_key!r}]"
        if isinstance(binding, str) and binding.startswith("$"):
            source_contract = _resume_token_path_contract(
                token_schema,
                binding,
                location=binding_location,
                require_presence=False,
            )
            _validate_source_contract_for_await_state(
                document,
                subflows,
                state_key,
                source_contract,
                location=binding_location,
            )
        else:
            _validate_literal_for_await_state(
                document,
                subflows,
                state_key,
                binding,
                location=binding_location,
            )

    enrichment = on_resume.get("enrich")
    for transition_name, transition in {
        "timeout": capability.get("timeout"),
        **cast(dict[str, Any], capability.get("recovery") or {}),
    }.items():
        if not isinstance(transition, dict):
            continue
        for state_key, literal_value in cast(dict[str, Any], transition.get("set") or {}).items():
            _validate_literal_for_await_state(
                document,
                subflows,
                state_key,
                literal_value,
                location=(f"$.capabilities.await_external.{transition_name}.set[{state_key!r}]"),
            )
    if not isinstance(enrichment, dict):
        return
    tool_definition = tools.definition(cast(str, enrichment["tool"]))
    enrichment_input = cast(dict[str, Any], enrichment.get("input") or {})
    required_parameters = required_schema_properties(tool_definition.input_schema)
    bound_parameters = frozenset(enrichment_input)
    for parameter_name, binding in enrichment_input.items():
        binding_location = (
            f"$.capabilities.await_external.on_resume.enrich.input[{parameter_name!r}]"
        )
        parameter_contract = schema_property_contract(
            tool_definition.input_schema,
            parameter_name,
            tool_definition.input_schema,
            bound_parameters,
        )
        if isinstance(binding, str) and binding.startswith("$"):
            source_contract = _resume_token_path_contract(
                token_schema,
                binding,
                location=binding_location,
                require_presence=parameter_name in required_parameters,
            )
            if not schema_is_proven_subset(
                source_contract.schema,
                source_contract.root_schema,
                parameter_contract,
                tool_definition.input_schema,
            ):
                raise ValueError(
                    f"{binding_location} {source_contract.origin} is not proven to satisfy "
                    f"tool parameter {parameter_name!r} in {tool_definition.identifier!r}"
                )
        elif not schema_accepts_known_instance(
            parameter_contract,
            tool_definition.input_schema,
            binding,
        ):
            raise ValueError(
                f"{binding_location} literal does not satisfy tool parameter "
                f"{parameter_name!r} in {tool_definition.identifier!r}"
            )

    for state_key, binding in cast(dict[str, Any], enrichment.get("set") or {}).items():
        binding_location = f"$.capabilities.await_external.on_resume.enrich.set[{state_key!r}]"
        if isinstance(binding, str) and binding.startswith("$result."):
            result_path = tuple(binding.removeprefix("$result.").split("."))
            source_schema = _schema_path_value_contract(
                tool_definition.output_schema,
                result_path,
                tool_definition.output_schema,
            )
            _validate_source_contract_for_await_state(
                document,
                subflows,
                state_key,
                FlowValueContract(
                    schema=source_schema,
                    root_schema=tool_definition.output_schema,
                    origin=f"tool result path {binding!r}",
                    total=False,
                ),
                location=binding_location,
            )
        else:
            _validate_literal_for_await_state(
                document,
                subflows,
                state_key,
                binding,
                location=binding_location,
            )


def validate_await_external_definition(
    document: dict[str, Any],
    capability: Optional[dict[str, Any]],
    tools: ToolRegistry,
    subflows: SubflowRegistry,
    path_step: Optional[dict[str, Any]],
) -> None:
    if capability is None:
        return
    if not isinstance(capability.get("step"), str) or not capability["step"]:
        raise ValueError("capabilities.await_external.step must be a non-empty string")
    if not isinstance(capability.get("resume_on"), str) or not capability["resume_on"]:
        raise ValueError("capabilities.await_external.resume_on must be a non-empty string")
    effective_interactive = (path_step or {}).get("interactive") or capability.get("interactive")
    if effective_interactive:
        if effective_interactive.get("kind") != "cta_url":
            raise ValueError("await_external interactive.kind must be 'cta_url'")
        if effective_interactive.get("field") != capability["resume_on"]:
            raise ValueError("await_external interactive.field must match resume_on")
        if effective_interactive.get("out_of_band", True) is not True:
            raise ValueError("await_external interactive.out_of_band cannot be false")
        next_step = effective_interactive.get("next_step")
        if next_step is not None and next_step != capability["step"]:
            raise ValueError("await_external interactive.next_step must match step")
    on_resume = capability.get("on_resume") or {}
    token_bindings = on_resume.get("set") or {}
    _validate_binding_map(
        token_bindings,
        namespace="token",
        location="capabilities.await_external.on_resume.set",
    )

    enrichment = on_resume.get("enrich")
    if enrichment is None:
        _validate_typed_resume_contract(document, capability, tools, subflows)
        return
    enrichment_definition: dict[str, Any] | None = None
    if isinstance(enrichment, str):
        if path_step is not None:
            raise ValueError("path await_external requires object-form on_resume.enrich")
        tool_name = enrichment
    else:
        if not isinstance(enrichment, dict):
            raise ValueError("await_external enrichment must be a tool name or object")
        enrichment_definition = enrichment
        tool_name_value = enrichment.get("tool")
        if not isinstance(tool_name_value, str) or not tool_name_value:
            raise ValueError("await_external enrichment.tool must be a non-empty string")
        tool_name = tool_name_value
        _validate_binding_map(
            enrichment.get("input") or {},
            namespace="token",
            location="capabilities.await_external.on_resume.enrich.input",
        )
        enrichment_bindings = enrichment.get("set") or {}
        _validate_binding_map(
            enrichment_bindings,
            namespace="result",
            location="capabilities.await_external.on_resume.enrich.set",
        )
        duplicate_writes = set(token_bindings) & set(enrichment_bindings)
        if duplicate_writes:
            duplicates = ", ".join(sorted(duplicate_writes))
            raise ValueError(f"await_external mappings write the same keys twice: {duplicates}")
    tool_definition = registered_tool_definition(
        tools,
        tool_name,
        location="$.capabilities.await_external.on_resume.enrich.tool",
    )
    validate_read_only_tool_effects(
        tool_definition,
        location="$.capabilities.await_external.on_resume.enrich.tool",
    )
    if enrichment_definition is None:
        _validate_typed_resume_contract(document, capability, tools, subflows)
        return
    enrichment_input = cast(dict[str, Any], enrichment_definition.get("input") or {})
    validate_tool_inputs(
        tool_definition,
        [
            (
                parameter_name,
                f"$.capabilities.await_external.on_resume.enrich.input[{parameter_name!r}]",
            )
            for parameter_name in enrichment_input
        ],
        call_location="$.capabilities.await_external.on_resume.enrich.input",
    )
    for state_key, result_binding in cast(
        dict[str, Any], enrichment_definition.get("set") or {}
    ).items():
        if isinstance(result_binding, str) and result_binding.startswith("$result."):
            validate_tool_result_path(
                tool_definition,
                result_binding,
                location=(f"$.capabilities.await_external.on_resume.enrich.set[{state_key!r}]"),
                allow_array_indices=True,
            )
    _validate_typed_resume_contract(document, capability, tools, subflows)


def validate_await_external_targets(
    capability: Optional[dict[str, Any]],
    node_ids: set[str],
    await_external_node_ids: set[str],
) -> None:
    if capability is None:
        return
    bound_node_id = capability["step"]
    if bound_node_id not in node_ids:
        raise ValueError(
            f"capabilities.await_external.step does not resolve to a node: {bound_node_id!r}"
        )
    if bound_node_id not in await_external_node_ids:
        raise ValueError(
            "capabilities.await_external.step resolves to a node that does not implement "
            f"await_external: {bound_node_id!r}"
        )
    transitions = {
        "timeout": capability.get("timeout"),
        **(capability.get("recovery") or {}),
    }
    for event, transition in transitions.items():
        if transition is None:
            continue
        target = transition["goto"]
        if event == "resend" and target != bound_node_id:
            raise ValueError(
                "capabilities.await_external resend target must match its bound step: "
                f"{bound_node_id!r}"
            )
        if target != "END" and target not in node_ids:
            raise ValueError(
                f"capabilities.await_external {event} target does not resolve to a node: {target!r}"
            )
