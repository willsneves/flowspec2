"""Entry, state-writer, and rail-reference semantic contracts."""

from __future__ import annotations

from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .diagnostics import DiagnosticLocation, FlowDiagnostic
from .semantic_support import json_pointer, semantic_diagnostic


def entry_schema_contracts(
    document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    entry_schema = cast(dict[str, Any] | None, document["route"].get("entry_args_schema"))
    if entry_schema is None:
        return []
    try:
        Draft202012Validator.check_schema(entry_schema)
    except SchemaError as schema_error:
        return [
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_INVALID_ENTRY_SCHEMA",
                "/route/entry_args_schema",
                f"entry_args_schema is not a valid Draft 2020-12 schema: {schema_error.message}",
            )
        ]

    diagnostics: list[FlowDiagnostic] = []
    if entry_schema.get("type") != "object":
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_ENTRY_SCHEMA_NOT_OBJECT",
                "/route/entry_args_schema/type",
                "entry_args_schema must describe one object of initial slot values",
                suggested_fix="Set type to object and declare the permitted slot properties.",
            )
        )
    if entry_schema.get("additionalProperties") is not False:
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_ENTRY_SCHEMA_OPEN",
                "/route/entry_args_schema/additionalProperties",
                "entry_args_schema must reject undeclared entry fields",
                suggested_fix="Set additionalProperties to false.",
            )
        )
    available_slots = set(document.get("slots", {}))
    if external_slots is not None:
        available_slots.update(external_slots)
    for property_name in cast(dict[str, Any], entry_schema.get("properties", {})):
        if property_name not in available_slots:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_ENTRY_SLOT",
                    json_pointer("route", "entry_args_schema", "properties", property_name),
                    f"entry argument {property_name!r} is not a declared or exposed slot",
                )
            )
    return diagnostics


def state_writer_contracts(
    document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    """Reject state keys whose ownership or write phase is ambiguous."""

    diagnostics: list[FlowDiagnostic] = []
    declared_slots = set(document.get("slots", {}))
    state_slots = declared_slots | set(external_slots or ())
    derive_locations = {
        cast(str, derive_definition["writes"]): json_pointer("derive", derive_index, "writes")
        for derive_index, derive_definition in enumerate(document.get("derive", []))
    }
    await_locations: dict[str, str] = {}
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    if await_definition:
        on_resume = cast(dict[str, Any], await_definition.get("on_resume", {}))
        for state_key in cast(dict[str, Any], on_resume.get("set", {})):
            await_locations[state_key] = json_pointer(
                "capabilities", "await_external", "on_resume", "set", state_key
            )
        enrichment = on_resume.get("enrich")
        if isinstance(enrichment, dict):
            for state_key in cast(dict[str, Any], enrichment.get("set", {})):
                await_locations[state_key] = json_pointer(
                    "capabilities",
                    "await_external",
                    "on_resume",
                    "enrich",
                    "set",
                    state_key,
                )
        for transition_name, transition in {
            "timeout": await_definition.get("timeout"),
            **cast(dict[str, Any], await_definition.get("recovery", {})),
        }.items():
            if not isinstance(transition, dict):
                continue
            for state_key in cast(dict[str, Any], transition.get("set", {})):
                await_locations.setdefault(
                    state_key,
                    json_pointer(
                        "capabilities",
                        "await_external",
                        transition_name if transition_name == "timeout" else "recovery",
                        *(() if transition_name == "timeout" else (transition_name,)),
                        "set",
                        state_key,
                    ),
                )

    terminal_locations: dict[str, str] = {}
    terminal = cast(dict[str, Any], document.get("terminal", {}))
    for state_key in terminal.get("outputs", {}):
        terminal_locations[state_key] = json_pointer("terminal", "outputs", state_key)
    success_set = cast(
        dict[str, Any],
        cast(dict[str, Any], terminal.get("outcomes", {})).get("success", {}).get("set", {}),
    )
    for state_key in success_set:
        if state_key in terminal_locations:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_TERMINAL_WRITE",
                    json_pointer("terminal", "outcomes", "success", "set", state_key),
                    f"terminal writes state key {state_key!r} through outputs and success.set",
                    related=(DiagnosticLocation(path=terminal_locations[state_key]),),
                )
            )
        terminal_locations.setdefault(
            state_key,
            json_pointer("terminal", "outcomes", "success", "set", state_key),
        )

    entry = cast(dict[str, Any], document.get("entry", {}))
    entry_key = cast(str | None, entry.get("writes"))
    protected_keys = (
        state_slots | set(derive_locations) | set(await_locations) | set(terminal_locations)
    )
    reserved_service_key = {"service"} if document.get("service") else set()
    if entry_key is not None and entry_key in protected_keys | reserved_service_key:
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_ENTRY_WRITE_COLLISION",
                "/entry/writes",
                f"entry result key {entry_key!r} has another state owner",
            )
        )

    if document.get("service") and "service" in state_slots | set(derive_locations) | set(
        await_locations
    ) | set(terminal_locations):
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_SERVICE_STATE_COLLISION",
                "/service",
                "state key 'service' is reserved for namespaced service metadata",
            )
        )

    for derived_key, derive_path in derive_locations.items():
        conflicting_path = await_locations.get(derived_key) or terminal_locations.get(derived_key)
        if conflicting_path is not None:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DERIVE_WRITE_COLLISION",
                    derive_path,
                    f"derived key {derived_key!r} is also written by another phase",
                    related=(DiagnosticLocation(path=conflicting_path),),
                )
            )

    for terminal_key, terminal_path in terminal_locations.items():
        if terminal_key in state_slots:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_TERMINAL_SLOT_COLLISION",
                    terminal_path,
                    f"terminal write {terminal_key!r} would bypass its slot domain",
                    suggested_fix="Write a distinct result key.",
                )
            )
        if terminal_key in await_locations:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_PHASE_WRITE_COLLISION",
                    terminal_path,
                    f"state key {terminal_key!r} is also written by await_external",
                    related=(DiagnosticLocation(path=await_locations[terminal_key]),),
                )
            )
    return diagnostics


def rail_reference_contracts(
    document: dict[str, Any],
    identifiers: dict[str, str],
    slot_positions: dict[str, int],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    declared_slots = set(document.get("slots", {}))
    available_slots = set(declared_slots)
    if external_slots is not None:
        available_slots.update(external_slots)
    derive_targets = {definition["writes"] for definition in document.get("derive", [])}
    available_inputs = available_slots | derive_targets

    for path_index, path_step in enumerate(document["path"]):
        for step_kind in ("slot", "confirm"):
            if step_kind in path_step and path_step[step_kind] not in declared_slots:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_NATIVE_PATH_SLOT",
                        json_pointer("path", path_index, step_kind),
                        f"native {step_kind} step must reference a top-level declared slot",
                    )
                )
        if "confirm" in path_step and path_step["confirm"] in declared_slots:
            confirmation_slot = cast(dict[str, Any], document["slots"][path_step["confirm"]])
            confirmation_domain = cast(
                dict[str, Any] | None,
                cast(dict[str, Any], document.get("domains", {})).get(confirmation_slot["domain"]),
            )
            if confirmation_domain is not None and confirmation_domain["type"] != "bool":
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_DOMAIN_NOT_BOOLEAN",
                        json_pointer("path", path_index, "confirm"),
                        f"confirmation slot {path_step['confirm']!r} must use a bool domain",
                    )
                )
    gates = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("overrides", {})).get("gates", {}),
    )
    gateable_identifiers = {
        cast(str, path_step["step"])
        for path_step in document["path"]
        if "slot" in path_step or "confirm" in path_step
    }
    for gate_identifier in gates:
        if gate_identifier not in gateable_identifiers:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_GATE_STEP",
                    json_pointer("overrides", "gates", gate_identifier),
                    f"gate target {gate_identifier!r} is not a native collect or confirm step",
                )
            )
            continue
        gated_step = next(
            path_step
            for path_step in document["path"]
            if path_step.get("step") == gate_identifier
            and ("slot" in path_step or "confirm" in path_step)
        )
        if "ask_when" in gated_step:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_GATE_SOURCE",
                    json_pointer("overrides", "gates", gate_identifier),
                    "step has both inline ask_when and an override gate",
                    related=(
                        DiagnosticLocation(
                            path=json_pointer(
                                "path", document["path"].index(gated_step), "ask_when"
                            )
                        ),
                    ),
                    suggested_fix="Keep one gate source for the step.",
                )
            )
        if "confirm" in gated_step and (
            gated_step.get("correctable") is True or "on_reject" in gated_step
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_GATE_UNSUPPORTED_FOR_CONFIRMATION_VARIANT",
                    json_pointer("overrides", "gates", gate_identifier),
                    "summary and correction-hub confirmations do not support override gates",
                )
            )

    if (confirmation := document.get("confirm")) is not None:
        if confirmation["step"] not in identifiers:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIRM_STEP",
                    "/confirm/step",
                    f"confirmation hub step {confirmation['step']!r} is not in path",
                )
            )
        if confirmation["slot"] not in declared_slots:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIRM_SLOT",
                    "/confirm/slot",
                    f"confirmation hub slot {confirmation['slot']!r} is not declared",
                )
            )
        else:
            confirmation_slot = cast(dict[str, Any], document["slots"][confirmation["slot"]])
            confirmation_domain = cast(
                dict[str, Any] | None,
                cast(dict[str, Any], document.get("domains", {})).get(confirmation_slot["domain"]),
            )
            if confirmation_domain is not None and confirmation_domain["type"] != "bool":
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_DOMAIN_NOT_BOOLEAN",
                        "/confirm/slot",
                        f"confirmation slot {confirmation['slot']!r} must use a bool domain",
                    )
                )
        matching_confirmation_steps = [
            path_step
            for path_step in document["path"]
            if path_step.get("step") == confirmation["step"] and "confirm" in path_step
        ]
        if matching_confirmation_steps:
            matching_step = matching_confirmation_steps[0]
            if matching_step["confirm"] != confirmation["slot"]:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_SLOT_MISMATCH",
                        "/confirm/slot",
                        "top-level confirmation slot differs from its path step",
                    )
                )
            if matching_step.get("correctable") is not True:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_HUB_NOT_CORRECTABLE",
                        "/confirm/step",
                        "top-level confirmation must bind a path step marked correctable",
                    )
                )
        if (
            on_confirm := confirmation.get("on_confirm")
        ) is not None and on_confirm not in identifiers:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIRM_TARGET",
                    "/confirm/on_confirm",
                    f"confirmation target {on_confirm!r} is not an executable step",
                )
            )
        elif on_confirm is not None:
            confirmation_path = identifiers.get(confirmation["step"], "")
            target_path = identifiers[on_confirm]
            if confirmation_path.startswith("/path/") and target_path.startswith("/path/"):
                confirmation_index = int(confirmation_path.split("/")[2])
                target_index = int(target_path.split("/")[2])
                if target_index <= confirmation_index:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_CONFIRM_TARGET_NOT_FORWARD",
                            "/confirm/on_confirm",
                            "confirmation target must execute after the confirmation hub",
                            suggested_fix="Target a later path step, normally the terminal step.",
                        )
                    )
        for correctable_index, correctable_slot in enumerate(confirmation["correctable"]):
            if external_slots is not None and correctable_slot not in available_slots:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_CORRECTABLE_SLOT",
                        json_pointer("confirm", "correctable", correctable_index),
                        f"correctable slot {correctable_slot!r} is not declared or exposed",
                    )
                )
            confirmation_source = identifiers.get(confirmation["step"], "")
            if (
                confirmation_source.startswith("/path/")
                and correctable_slot in slot_positions
                and slot_positions[correctable_slot] >= int(confirmation_source.split("/")[2])
            ):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_CORRECTABLE_SLOT_ORDER",
                        json_pointer("confirm", "correctable", correctable_index),
                        f"correctable slot {correctable_slot!r} is not produced before the hub",
                        suggested_fix="Move its producer before the confirmation hub.",
                    )
                )

    if (terminal := document.get("terminal")) is not None:
        for binding_index, binding in enumerate(terminal.get("input", [])):
            if external_slots is not None and binding["slot"] not in available_inputs:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_TERMINAL_INPUT",
                        json_pointer("terminal", "input", binding_index, "slot"),
                        f"terminal input slot {binding['slot']!r} is not declared, derived, or exposed",
                    )
                )

    if (auto_flow := document.get("auto_flow")) is not None:
        native_path_identifiers = {
            identifier
            for identifier, source_path in identifiers.items()
            if source_path.startswith("/path/")
        }
        if auto_flow.get("resume_at") not in {None, *native_path_identifiers}:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_AUTO_FLOW_RESUME_STEP",
                    "/auto_flow/resume_at",
                    f"auto_flow.resume_at {auto_flow['resume_at']!r} is not a native path step",
                )
            )
        for prefill_index, prefill_slot in enumerate(auto_flow.get("prefill_from", [])):
            if prefill_slot not in declared_slots:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_NON_NATIVE_AUTO_FLOW_PREFILL_SLOT",
                        json_pointer("auto_flow", "prefill_from", prefill_index),
                        f"auto-flow prefill slot {prefill_slot!r} is not a top-level slot",
                    )
                )
        for flow_field, mapping in auto_flow.get("alias_map", {}).items():
            candidate_mappings = (
                [mapping]
                if all(not isinstance(value, dict) for value in mapping.values())
                else [value for value in mapping.values() if isinstance(value, dict)]
            )
            for candidate_mapping in candidate_mappings:
                for destination_slot in candidate_mapping:
                    if destination_slot not in declared_slots:
                        diagnostics.append(
                            semantic_diagnostic(
                                "FLOWSPEC_SEMANTIC_NON_NATIVE_AUTO_FLOW_DESTINATION",
                                json_pointer("auto_flow", "alias_map", flow_field),
                                f"auto-flow alias destination {destination_slot!r} is not a "
                                "top-level slot",
                            )
                        )
    return diagnostics
