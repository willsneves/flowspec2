"""Tool input, output, effect, and terminal protocol compiler contracts."""

from __future__ import annotations

from typing import Any, Final, cast

from .compiler_schema_relations import (
    CONTRACT_INVALID,
    alternative_is_success_compatible,
    finite_schema_instances,
    key_projection_match,
    required_schema_properties,
    schema_accepts_known_instance,
    schema_conjunctive_alternatives,
    schema_path_status,
    schema_property_contract,
    schema_property_status,
    schema_requires_path_for_every_success,
)
from .compiler_value_contracts import validate_terminal_binding_contracts
from .subflows import SubflowRegistry
from .tools import ToolDefinition, ToolRegistry

_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"success", "retryable", "fatal"})


def _validate_terminal_output_protocol(definition: ToolDefinition) -> None:
    output_schema = definition.output_schema
    if schema_property_status(output_schema, "status") != CONTRACT_INVALID:
        status_contract = schema_property_contract(
            output_schema,
            "status",
            output_schema,
            frozenset(required_schema_properties(output_schema)),
        )
        status_instances = finite_schema_instances(status_contract, output_schema)
        if status_instances is None or any(
            status_instance not in _TERMINAL_STATUSES
            for status_instance in status_instances.values()
        ):
            allowed_statuses = ", ".join(sorted(_TERMINAL_STATUSES))
            raise ValueError(
                f"$.terminal.tool output contract {definition.identifier!r} must restrict "
                f"status to the terminal protocol: {allowed_statuses}"
            )

    output_alternatives = schema_conjunctive_alternatives(output_schema, output_schema)
    if not any(
        alternative_is_success_compatible(output_alternative, output_schema)
        for output_alternative in output_alternatives
    ):
        raise ValueError(
            f"$.terminal.tool output contract {definition.identifier!r} has no "
            "success-compatible branch"
        )


def _tool_supports_terminal_status(definition: ToolDefinition, status: str) -> bool:
    if schema_property_status(definition.output_schema, "status") == CONTRACT_INVALID:
        return False
    status_contract = schema_property_contract(
        definition.output_schema,
        "status",
        definition.output_schema,
        frozenset(required_schema_properties(definition.output_schema)),
    )
    return schema_accepts_known_instance(
        status_contract,
        definition.output_schema,
        status,
    )


def validate_read_only_tool_effects(
    definition: ToolDefinition,
    *,
    location: str,
) -> None:
    if definition.effects.read_only and not definition.effects.destructive:
        return
    raise ValueError(
        f"{location} tool contract {definition.identifier!r} must be read_only and non-destructive"
    )


def registered_tool_definition(
    tools: ToolRegistry,
    tool_name: str,
    *,
    location: str,
) -> ToolDefinition:
    if not tools.has(tool_name):
        raise ValueError(f"{location} tool is not registered: {tool_name!r}")
    return tools.definition(tool_name)


def validate_tool_inputs(
    definition: ToolDefinition,
    parameters: list[tuple[str, str]],
    *,
    call_location: str,
    require_all: bool = True,
) -> None:
    seen_parameters: dict[str, str] = {}
    for parameter_name, parameter_location in parameters:
        if parameter_name in seen_parameters:
            raise ValueError(
                f"{parameter_location} duplicates tool parameter {parameter_name!r}; "
                f"first bound at {seen_parameters[parameter_name]}"
            )
        seen_parameters[parameter_name] = parameter_location
        if schema_property_status(definition.input_schema, parameter_name) == CONTRACT_INVALID:
            raise ValueError(
                f"{parameter_location} does not exist in tool contract "
                f"{definition.identifier!r}: {parameter_name!r}"
            )

    if not require_all:
        return
    missing_parameters = required_schema_properties(definition.input_schema) - set(seen_parameters)
    if missing_parameters:
        missing = ", ".join(repr(parameter) for parameter in sorted(missing_parameters))
        raise ValueError(
            f"{call_location} does not bind required parameters for tool contract "
            f"{definition.identifier!r}: {missing}"
        )
    bound_parameters = frozenset(seen_parameters)
    if not key_projection_match(
        definition.input_schema,
        definition.input_schema,
        bound_parameters,
    )[0]:
        rendered_parameters = ", ".join(repr(parameter) for parameter in sorted(bound_parameters))
        raise ValueError(
            f"{call_location} bound-key projection [{rendered_parameters}] does not satisfy "
            f"the complete input contract {definition.identifier!r}"
        )


def validate_tool_result_path(
    definition: ToolDefinition,
    result_path: str,
    *,
    location: str,
    allow_array_indices: bool,
) -> None:
    path_segments = tuple(result_path.split("."))
    required_namespace = "$result" if allow_array_indices else "result"
    if not path_segments or path_segments[0] != required_namespace:
        raise ValueError(f"{location} must use the {required_namespace}.* namespace")
    path_segments = path_segments[1:]
    if not path_segments or any(not segment for segment in path_segments):
        raise ValueError(f"{location} must contain a non-empty tool result path")
    if not allow_array_indices and any(segment.isdigit() for segment in path_segments):
        raise ValueError(f"{location} cannot traverse arrays in terminal tool results")
    if schema_path_status(definition.output_schema, path_segments) == CONTRACT_INVALID:
        raise ValueError(
            f"{location} does not resolve in tool contract {definition.identifier!r}: "
            f"{result_path!r}"
        )
    if not allow_array_indices and not schema_requires_path_for_every_success(
        definition.output_schema,
        path_segments,
    ):
        raise ValueError(
            f"{location} must be required in every success-compatible output branch "
            f"of tool contract {definition.identifier!r}: {result_path!r}"
        )


def validate_flow_tool_contracts(
    doc: dict[str, Any],
    tools: ToolRegistry,
    subflows: SubflowRegistry,
) -> None:
    entry_definition = doc.get("entry")
    if entry_definition:
        entry_tool_name = cast(str, entry_definition["tool"])
        tool_definition = registered_tool_definition(
            tools,
            entry_tool_name,
            location="$.entry.tool",
        )
        validate_read_only_tool_effects(tool_definition, location="$.entry.tool")
        validate_tool_inputs(
            tool_definition,
            [],
            call_location="$.entry",
        )

    terminal_definition = doc.get("terminal")
    if not terminal_definition:
        return
    terminal_tool_name = cast(str, terminal_definition["tool"])
    tool_definition = registered_tool_definition(
        tools,
        terminal_tool_name,
        location="$.terminal.tool",
    )
    _validate_terminal_output_protocol(tool_definition)
    terminal_declares_idempotency = bool(terminal_definition.get("idempotent", False))
    if terminal_declares_idempotency and not tool_definition.effects.idempotent:
        raise ValueError(
            f"$.terminal.idempotent is true, but tool contract "
            f"{tool_definition.identifier!r} does not declare idempotent effects"
        )
    retryable_outcome = cast(
        dict[str, Any],
        cast(dict[str, Any], terminal_definition.get("outcomes", {})).get("retryable", {}),
    )
    if (
        retryable_outcome.get("preserve_state", True) is True
        and _tool_supports_terminal_status(tool_definition, "retryable")
        and not (terminal_declares_idempotency or tool_definition.effects.idempotent)
    ):
        raise ValueError(
            "$.terminal.outcomes.retryable.preserve_state requires an idempotent terminal "
            f"or idempotent tool contract {tool_definition.identifier!r}"
        )
    terminal_input_bindings = cast(list[dict[str, Any]], terminal_definition.get("input", []))
    terminal_parameters = [
        (
            cast(str, input_binding["param"]),
            f"$.terminal.input[{input_index}].param",
        )
        for input_index, input_binding in enumerate(terminal_input_bindings)
    ]
    validate_tool_inputs(
        tool_definition,
        terminal_parameters,
        call_location="$.terminal.input",
    )
    validate_terminal_binding_contracts(
        doc,
        subflows,
        tool_definition,
        terminal_input_bindings,
    )
    for state_key, result_path in terminal_definition.get("outputs", {}).items():
        validate_tool_result_path(
            tool_definition,
            cast(str, result_path),
            location=f"$.terminal.outputs[{state_key!r}]",
            allow_array_indices=False,
        )
