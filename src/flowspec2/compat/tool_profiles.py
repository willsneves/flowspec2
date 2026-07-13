"""Synthetic tool contracts used only while compatibility artifacts are checked."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from flowspec2.subflows import default_subflows
from flowspec2.tools import ToolDefinition, ToolEffects

_OPEN_OBJECT_SCHEMA = {"type": "object", "additionalProperties": True}
_TERMINAL_STATUSES = ("success", "retryable", "fatal")


def _enrichment_tool_name(flow_document: Mapping[str, Any]) -> object:
    capabilities = flow_document.get("capabilities")
    await_external = (
        capabilities.get("await_external") if isinstance(capabilities, Mapping) else None
    )
    on_resume = await_external.get("on_resume") if isinstance(await_external, Mapping) else None
    enrichment = on_resume.get("enrich") if isinstance(on_resume, Mapping) else None
    return enrichment.get("tool") if isinstance(enrichment, Mapping) else enrichment


def compatibility_tool_names(flow_document: Mapping[str, Any]) -> tuple[str, ...]:
    """Return direct and transitively required tools for compile-only checking."""

    tool_names: set[str] = set()
    for section_name in ("entry", "terminal"):
        section = flow_document.get(section_name)
        if isinstance(section, Mapping) and isinstance(section.get("tool"), str):
            tool_names.add(section["tool"])
    enrichment_tool_name = _enrichment_tool_name(flow_document)
    if isinstance(enrichment_tool_name, str):
        tool_names.add(enrichment_tool_name)

    uses = flow_document.get("uses")
    subflow_registry = default_subflows()
    if isinstance(uses, list):
        for use_contract in uses:
            if not isinstance(use_contract, Mapping):
                continue
            subflow_reference = use_contract.get("ref")
            if not isinstance(subflow_reference, str) or not subflow_registry.has(
                subflow_reference
            ):
                continue
            tool_names.update(subflow_registry.definition(subflow_reference).required_tools)
    return tuple(sorted(tool_names))


def _add_required_output_path(
    output_schema: dict[str, Any],
    result_path: str,
) -> None:
    if not result_path.startswith("result."):
        raise ValueError(f"invalid terminal result path in compatibility profile: {result_path!r}")
    path_segments = result_path.removeprefix("result.").split(".")
    if any(not path_segment for path_segment in path_segments):
        raise ValueError(f"invalid terminal result path in compatibility profile: {result_path!r}")

    current_schema = output_schema
    for path_index, path_segment in enumerate(path_segments):
        required_properties = cast(list[str], current_schema.setdefault("required", []))
        if path_segment not in required_properties:
            required_properties.append(path_segment)
        property_schemas = cast(
            dict[str, Any],
            current_schema.setdefault("properties", {}),
        )
        is_leaf = path_index == len(path_segments) - 1
        if is_leaf:
            property_schemas.setdefault(path_segment, {})
            continue
        nested_schema = property_schemas.get(path_segment)
        if nested_schema == {}:
            nested_schema = None
        if nested_schema is None:
            nested_schema = {
                "type": "object",
                "additionalProperties": True,
                "properties": {},
            }
            property_schemas[path_segment] = nested_schema
        if not isinstance(nested_schema, dict) or nested_schema.get("type") != "object":
            raise ValueError(
                f"terminal result paths conflict in compatibility profile: {result_path!r}"
            )
        current_schema = nested_schema


def _terminal_output_schema(
    terminal_contract: Mapping[str, Any],
) -> dict[str, Any]:
    output_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "status": {
                "type": "string",
                "enum": list(_TERMINAL_STATUSES),
            }
        },
    }
    output_bindings = terminal_contract.get("outputs")
    if isinstance(output_bindings, Mapping):
        for result_path in output_bindings.values():
            if isinstance(result_path, str):
                _add_required_output_path(output_schema, result_path)
    return output_schema


def synthetic_compatibility_tool_definition(
    tool_name: str,
    flow_document: Mapping[str, Any],
    *,
    version: str,
    description: str,
) -> ToolDefinition:
    """Derive the minimum truthful compile-only contract from authored roles."""

    entry_contract = flow_document.get("entry")
    entry_tool_name = entry_contract.get("tool") if isinstance(entry_contract, Mapping) else None
    terminal_contract = flow_document.get("terminal")
    terminal_tool_name = (
        terminal_contract.get("tool") if isinstance(terminal_contract, Mapping) else None
    )
    read_only = tool_name in {entry_tool_name, _enrichment_tool_name(flow_document)}
    terminal_idempotent = (
        bool(terminal_contract.get("idempotent", False))
        if isinstance(terminal_contract, Mapping) and terminal_tool_name == tool_name
        else False
    )
    return ToolDefinition(
        name=tool_name,
        version=version,
        description=description,
        input_schema=_OPEN_OBJECT_SCHEMA,
        output_schema=(
            _terminal_output_schema(terminal_contract)
            if isinstance(terminal_contract, Mapping) and terminal_tool_name == tool_name
            else _OPEN_OBJECT_SCHEMA
        ),
        effects=ToolEffects(
            read_only=read_only,
            idempotent=read_only or terminal_idempotent,
            open_world=True,
        ),
    )
