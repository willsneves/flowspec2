"""Bounded Rasa CALM to FlowSpec2 import conversion."""

from __future__ import annotations

from typing import Any, Mapping, Sequence, cast

from .models import (
    CompatibilityDiagnostic,
    CompatibilityReport,
    ConversionOutcome,
    DiagnosticSeverity,
    enforce_compatibility_policy,
)
from .rasa_shared import (
    FLOWSPEC_FLOW_IDENTIFIER_PATTERN,
    FLOWSPEC_FORMAT,
    RASA_ACTION_IDENTIFIER_PATTERN,
    RASA_DOMAIN_VERSION,
    RASA_FORMAT,
    compatibility_diagnostic,
    domain_button_options,
    flowspec_compilation_diagnostic,
    flowspec_validation_diagnostics,
    mapping_or_none,
    rasa_response_text_supported,
    rasa_set_slots_name_supported,
    rasa_set_slots_value_supported,
    sequence_or_none,
    step_identifier_diagnostics,
)


def _import_slot_mapping_diagnostics(
    slot_name: str,
    slot_declaration: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    source_path = f"$.slots.{slot_name}"
    if "mappings" in slot_declaration:
        mappings = sequence_or_none(slot_declaration.get("mappings"))
        if (
            mappings is None
            or len(mappings) != 1
            or mapping_or_none(mappings[0]) != {"type": "from_llm"}
        ):
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_SLOT_MAPPING_UNSUPPORTED",
                    f"{source_path}.mappings",
                    "only an omitted mapping or one exact from_llm mapping is portable",
                )
            )
    allowed_properties = {"type", "values", "mappings"}
    for property_name in slot_declaration.keys() - allowed_properties:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_SLOT_PROPERTY_UNSUPPORTED",
                f"{source_path}.{property_name}",
                f"Rasa slot property {property_name!r} has no flowspec2 portable equivalent",
            )
        )


def _import_domain_specification(
    slot_name: str,
    slot_declaration: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> dict[str, Any] | None:
    source_path = f"$.slots.{slot_name}"
    _import_slot_mapping_diagnostics(slot_name, slot_declaration, diagnostics)
    slot_type = slot_declaration.get("type")
    if slot_type == "categorical":
        values = sequence_or_none(slot_declaration.get("values"))
        if values is None or not values or not all(isinstance(value, str) for value in values):
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_CATEGORICAL_VALUES_INVALID",
                    f"{source_path}.values",
                    "a portable categorical slot requires a non-empty list of strings",
                )
            )
            return None
        string_values = cast(Sequence[str], values)
        if len({value.casefold() for value in string_values}) != len(string_values):
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_CATEGORICAL_CASE_COLLISION",
                    f"{source_path}.values",
                    "case-insensitive Rasa categories cannot become distinct flowspec2 tokens",
                )
            )
        return {"type": "categorical", "values": list(string_values)}
    if slot_type == "bool":
        if "values" in slot_declaration:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_SLOT_PROPERTY_UNSUPPORTED",
                    f"{source_path}.values",
                    "bool slots do not use categorical values in the portable profile",
                )
            )
        return {"type": "bool"}
    if slot_type == "text":
        if "values" in slot_declaration:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_SLOT_PROPERTY_UNSUPPORTED",
                    f"{source_path}.values",
                    "text slots do not use categorical values in the portable profile",
                )
            )
        return {"type": "free_text"}
    diagnostics.append(
        compatibility_diagnostic(
            "error",
            "RASA_SLOT_TYPE_UNSUPPORTED",
            f"{source_path}.type",
            f"slot type {slot_type!r} is outside categorical, bool and text",
        )
    )
    return None


def _parse_set_slots_payload(payload: object) -> tuple[str, str] | None:
    if (
        not isinstance(payload, str)
        or not payload.startswith("/SetSlots(")
        or not payload.endswith(")")
    ):
        return None
    assignment = payload[len("/SetSlots(") : -1]
    if "=" not in assignment or "," in assignment:
        return None
    slot_name, slot_value = assignment.split("=", 1)
    if not slot_name or not slot_value:
        return None
    if not rasa_set_slots_name_supported(slot_name):
        return None
    if not rasa_set_slots_value_supported(slot_value):
        return None
    return slot_name, slot_value


def _import_buttons(
    buttons_value: object,
    slot_name: str,
    domain_specification: Mapping[str, Any],
    source_path: str,
    diagnostics: list[CompatibilityDiagnostic],
) -> dict[str, Any] | None:
    buttons = sequence_or_none(buttons_value)
    if buttons is None or not buttons:
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_STATIC_BUTTONS_UNSUPPORTED",
                source_path,
                "buttons must be a non-empty list of title/payload mappings",
            )
        )
        return None
    if not rasa_set_slots_name_supported(slot_name):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_SETSLOTS_SLOT_NAME_UNSUPPORTED",
                source_path,
                "portable SetSlots names must be non-empty and forbid '=,()'",
            )
        )
        return None

    expected_options = domain_button_options(domain_specification)
    expected_titles = dict(expected_options)
    accepts_legacy_boolean_titles = domain_specification.get("type") == "bool"
    parsed_values: list[str] = []
    portable = True
    for button_index, button_value in enumerate(buttons):
        button = mapping_or_none(button_value)
        button_path = f"{source_path}[{button_index}]"
        if button is None or set(button) != {"title", "payload"}:
            diagnostics.append(
                compatibility_diagnostic(
                    "warning",
                    "RASA_STATIC_BUTTONS_UNSUPPORTED",
                    button_path,
                    "each portable button must contain exactly title and payload",
                )
            )
            portable = False
            continue
        button_title = button.get("title")
        button_payload = button.get("payload")
        if any(
            isinstance(button_text, str) and not rasa_response_text_supported(button_text)
            for button_text in (button_title, button_payload)
        ):
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                    button_path,
                    "curly braces in a button title or payload trigger Rasa response interpolation",
                )
            )
            portable = False
            continue
        parsed_assignment = _parse_set_slots_payload(button.get("payload"))
        if parsed_assignment is None:
            diagnostics.append(
                compatibility_diagnostic(
                    "warning",
                    "RASA_SETSLOTS_PAYLOAD_UNSUPPORTED",
                    f"{button_path}.payload",
                    "the payload must be one valid case-sensitive /SetSlots(slot=value) command",
                )
            )
            portable = False
            continue
        assigned_slot, assigned_value = parsed_assignment
        if assigned_slot != slot_name:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_BUTTON_SLOT_MISMATCH",
                    f"{button_path}.payload",
                    "a collect response button may set only its collected slot",
                )
            )
            portable = False
        accepted_titles = {expected_titles.get(assigned_value)}
        if accepts_legacy_boolean_titles:
            accepted_titles.add(assigned_value)
        if button.get("title") not in accepted_titles:
            diagnostics.append(
                compatibility_diagnostic(
                    "warning",
                    "RASA_BUTTON_TITLE_UNSUPPORTED",
                    f"{button_path}.title",
                    "the button title must match the domain's canonical presentation",
                )
            )
            portable = False
        parsed_values.append(assigned_value)

    expected_values = [button_value for button_value, _button_title in expected_options]
    if parsed_values != expected_values:
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_BUTTON_VALUES_MISMATCH",
                source_path,
                "buttons must cover the slot domain exactly and in domain order",
            )
        )
        portable = False
    if not portable:
        return None
    return {"kind": "buttons", "field": slot_name}


def _import_prompt(
    response_key: str,
    slot_name: str,
    responses: Mapping[str, Any],
    domain_name: str,
    domain_specification: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_path = f"$.responses.{response_key}"
    variations = sequence_or_none(responses.get(response_key))
    if variations is None or len(variations) != 1:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_VARIATIONS_UNSUPPORTED",
                source_path,
                "the portable profile requires exactly one unconditional response variation",
            )
        )
        return None, None
    variation = mapping_or_none(variations[0])
    if variation is None or not isinstance(variation.get("text"), str) or not variation["text"]:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_TEXT_REQUIRED",
                f"{source_path}[0].text",
                "the portable response variation requires non-empty text",
            )
        )
        return None, None

    allowed_properties = {"text", "buttons"}
    for property_name in variation.keys() - allowed_properties:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_PROPERTY_UNSUPPORTED",
                f"{source_path}[0].{property_name}",
                f"conditional or rich response property {property_name!r} is not portable",
            )
        )
    response_text = cast(str, variation["text"])
    if not rasa_response_text_supported(response_text):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                f"{source_path}[0].text",
                "Rasa response interpolation has no literal flowspec2 prompt equivalent",
            )
        )
    prompt = {"text": response_text}
    interactive: dict[str, Any] | None = None
    if "buttons" in variation:
        interactive = _import_buttons(
            variation["buttons"],
            slot_name,
            domain_specification,
            f"{source_path}[0].buttons",
            diagnostics,
        )
        if interactive is not None:
            interactive["from_domain"] = domain_name
    return prompt, interactive


def _import_collect_step(
    step: Mapping[str, Any],
    flow_identifier: str,
    step_index: int,
    slot_declarations: Mapping[str, Any],
    domain_names: Mapping[str, str],
    flowspec_domains: Mapping[str, Any],
    responses: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> dict[str, Any] | None:
    source_path = f"$.flows.{flow_identifier}.steps[{step_index}]"
    slot_name = step.get("collect")
    if not isinstance(slot_name, str) or not slot_name:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_COLLECT_SLOT_INVALID",
                f"{source_path}.collect",
                "collect must name a non-empty domain slot",
            )
        )
        return None
    if mapping_or_none(slot_declarations.get(slot_name)) is None or slot_name not in domain_names:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_COLLECT_SLOT_MISSING",
                f"{source_path}.collect",
                f"slot {slot_name!r} has no portable domain declaration",
            )
        )
        return None

    allowed_properties = {"collect", "id", "description", "utter", "ask_before_filling"}
    for property_name in step.keys() - allowed_properties:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_STEP_CONTROL_UNSUPPORTED",
                f"{source_path}.{property_name}",
                f"collect property {property_name!r} changes control flow "
                "outside the portable profile",
            )
        )
    if "description" in step and not isinstance(step.get("description"), str):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_COLLECT_DESCRIPTION_INVALID",
                f"{source_path}.description",
                "a collect description must be a string",
            )
        )

    domain_name = domain_names[slot_name]
    domain_specification = cast(Mapping[str, Any], flowspec_domains[domain_name])
    response_key_value = step.get("utter", f"utter_ask_{slot_name}")
    if not isinstance(response_key_value, str):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_UTTER_KEY_INVALID",
                f"{source_path}.utter",
                "utter must name one domain response",
            )
        )
        response_key_value = f"utter_ask_{slot_name}"
    elif not response_key_value.startswith("utter_"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_KEY_UNSUPPORTED",
                f"{source_path}.utter",
                "Rasa response keys referenced by collect steps must start with 'utter_'",
            )
        )
    prompt, interactive = _import_prompt(
        response_key_value,
        slot_name,
        responses,
        domain_name,
        domain_specification,
        diagnostics,
    )

    ask_before_filling = step.get("ask_before_filling", False)
    if not isinstance(ask_before_filling, bool):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ASK_BEFORE_FILLING_INVALID",
                f"{source_path}.ask_before_filling",
                "ask_before_filling must be boolean",
            )
        )
        ask_before_filling = False
    if ask_before_filling:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ASK_BEFORE_FILLING_UNSUPPORTED",
                f"{source_path}.ask_before_filling",
                "Rasa clears a prefilled slot before asking, which flowspec2 "
                "portable collect does not do",
            )
        )

    flowspec_step: dict[str, Any] = {"slot": slot_name}
    if isinstance(step.get("id"), str) and step["id"]:
        flowspec_step["step"] = step["id"]
    if prompt is not None:
        if isinstance(step.get("description"), str):
            prompt["extract_hint"] = step["description"]
        flowspec_step["prompt"] = prompt
    if interactive is not None:
        flowspec_step["interactive"] = interactive
    return flowspec_step


def _import_terminal_action(
    step: Mapping[str, Any],
    flow_identifier: str,
    step_index: int,
    step_count: int,
    declared_actions: frozenset[str],
    diagnostics: list[CompatibilityDiagnostic],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    source_path = f"$.flows.{flow_identifier}.steps[{step_index}]"
    if step_index != step_count - 1:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ACTION_POSITION_UNSUPPORTED",
                source_path,
                "only one final custom action can represent a flowspec2 terminal",
            )
        )
        return None
    for property_name in step.keys() - {"action", "id"}:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_STEP_CONTROL_UNSUPPORTED",
                f"{source_path}.{property_name}",
                f"terminal action property {property_name!r} changes control flow",
            )
        )
    action_name = step.get("action")
    if not isinstance(action_name, str) or not RASA_ACTION_IDENTIFIER_PATTERN.fullmatch(
        action_name
    ):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ACTION_NAME_UNSUPPORTED",
                f"{source_path}.action",
                "the final action must be a valid Rasa custom-action identifier",
            )
        )
        return None
    if action_name not in declared_actions:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ACTION_DECLARATION_MISSING",
                f"{source_path}.action",
                "the final custom action must be listed in domain.yml actions",
            )
        )
    terminal_step_identifier = step.get("id", action_name)
    if not isinstance(terminal_step_identifier, str) or not terminal_step_identifier:
        terminal_step_identifier = action_name

    diagnostics.append(
        compatibility_diagnostic(
            "warning",
            "RASA_ACTION_ADAPTER_REQUIRED",
            source_path,
            "the imported terminal uses conservative lifecycle defaults: "
            "non-idempotent and reset after every outcome",
        )
    )
    terminal = {
        "step": terminal_step_identifier,
        "tool": action_name,
        "idempotent": False,
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": False},
            "fatal": {"reset_next": True},
        },
    }
    return {"terminal": True}, terminal


def _select_rasa_flow(
    flows_document: Mapping[str, Any],
    flow_id: str | None,
    diagnostics: list[CompatibilityDiagnostic],
) -> tuple[str, Mapping[str, Any]] | None:
    flows = mapping_or_none(flows_document.get("flows"))
    if flows is None or not flows:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOWS_MAPPING_REQUIRED",
                "$.flows",
                "flows.yml must contain a non-empty flows mapping",
            )
        )
        return None
    string_flow_ids = [candidate_id for candidate_id in flows if isinstance(candidate_id, str)]
    if len(string_flow_ids) != len(flows):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOW_ID_INVALID",
                "$.flows",
                "every Rasa flow id must be a string",
            )
        )
    selected_flow_id = flow_id
    if selected_flow_id is None:
        if len(string_flow_ids) != 1:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_FLOW_SELECTION_REQUIRED",
                    "$.flows",
                    "flow_id is required when flows.yml contains more than one flow",
                )
            )
        selected_flow_id = string_flow_ids[0] if string_flow_ids else None
    if selected_flow_id is None or selected_flow_id not in flows:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOW_NOT_FOUND",
                "$.flows",
                f"selected flow {selected_flow_id!r} does not exist",
            )
        )
        return None
    selected_flow = mapping_or_none(flows[selected_flow_id])
    if selected_flow is None:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOW_DEFINITION_INVALID",
                f"$.flows.{selected_flow_id}",
                "the selected flow must be a mapping",
            )
        )
        return None
    return selected_flow_id, selected_flow


def _import_flow_property_diagnostics(
    flow_identifier: str,
    flow_definition: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    allowed_properties = {
        "description",
        "steps",
        "persisted_slots",
        "run_pattern_completed",
    }
    for property_name in flow_definition.keys() - allowed_properties:
        severity: DiagnosticSeverity = "warning" if property_name == "name" else "error"
        code = (
            "RASA_FLOW_NAME_UNSUPPORTED"
            if property_name == "name"
            else "RASA_FLOW_CONTROL_UNSUPPORTED"
        )
        diagnostics.append(
            compatibility_diagnostic(
                severity,
                code,
                f"$.flows.{flow_identifier}.{property_name}",
                (
                    "flowspec2 has no separate human-readable flow-name field"
                    if property_name == "name"
                    else f"Rasa flow property {property_name!r} is outside the "
                    "linear portable profile"
                ),
            )
        )
    if flow_definition.get("run_pattern_completed") is not False:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_COMPLETION_PATTERN_UNSUPPORTED",
                f"$.flows.{flow_identifier}.run_pattern_completed",
                "the portable profile requires run_pattern_completed: false because "
                "Rasa otherwise runs completion-pattern behavior with no flowspec2 equivalent",
            )
        )


def _import_document_property_diagnostics(
    flows_document: Mapping[str, Any],
    domain_document: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    for property_name in flows_document.keys() - {"flows", "version"}:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOWS_DOCUMENT_PROPERTY_UNSUPPORTED",
                f"$.{property_name}",
                f"flows.yml property {property_name!r} is outside the portable profile",
            )
        )
    for property_name in domain_document.keys() - {
        "version",
        "slots",
        "responses",
        "actions",
    }:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_DOMAIN_DOCUMENT_PROPERTY_UNSUPPORTED",
                f"$.{property_name}",
                f"domain.yml property {property_name!r} is outside the portable profile",
            )
        )
    for document_name, document, virtual_root in (
        ("flows.yml", flows_document, "flows_document"),
        ("domain.yml", domain_document, "domain_document"),
    ):
        if "version" in document and document.get("version") != RASA_DOMAIN_VERSION:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_DOCUMENT_VERSION_UNSUPPORTED",
                    f"$.{virtual_root}.version",
                    f"{document_name} version must be {RASA_DOMAIN_VERSION!r} when declared",
                )
            )


def _import_persisted_slots_diagnostics(
    flow_identifier: str,
    flow_definition: Mapping[str, Any],
    referenced_slot_names: set[str],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    source_path = f"$.flows.{flow_identifier}.persisted_slots"
    if "persisted_slots" not in flow_definition:
        if referenced_slot_names:
            diagnostics.append(
                compatibility_diagnostic(
                    "warning",
                    "RASA_COLLECTED_SLOT_RESET_UNSUPPORTED",
                    source_path,
                    "Rasa resets collected slots by default, while flowspec2 data slots persist",
                )
            )
        return
    persisted_slots = sequence_or_none(flow_definition.get("persisted_slots"))
    if persisted_slots is None or not all(
        isinstance(slot_name, str) for slot_name in persisted_slots
    ):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_PERSISTED_SLOTS_INVALID",
                source_path,
                "persisted_slots must be a list of collected slot names",
            )
        )
        return
    persisted_slot_names = cast(Sequence[str], persisted_slots)
    if len(persisted_slot_names) != len(set(persisted_slot_names)):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_PERSISTED_SLOTS_INVALID",
                source_path,
                "persisted_slots must not repeat a slot name",
            )
        )
    unexpected_slot_names = set(persisted_slot_names) - referenced_slot_names
    if unexpected_slot_names:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_PERSISTED_SLOT_NOT_COLLECTED",
                source_path,
                "persisted slots are not collected by the selected flow: "
                f"{sorted(unexpected_slot_names)!r}",
            )
        )
    reset_slot_names = referenced_slot_names - set(persisted_slot_names)
    if reset_slot_names:
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_COLLECTED_SLOT_RESET_UNSUPPORTED",
                source_path,
                "these collected slots reset in Rasa but persist in flowspec2: "
                f"{sorted(reset_slot_names)!r}",
            )
        )


def import_rasa(
    flows_document: Mapping[str, Any],
    domain_document: Mapping[str, Any],
    *,
    flow_id: str | None = None,
    flow_version: str = "1.0.0",
    allow_lossy: bool = False,
) -> ConversionOutcome[dict[str, Any]]:
    """Convert one selected Rasa CALM flow from the bounded portable subset."""

    diagnostics: list[CompatibilityDiagnostic] = []
    _import_document_property_diagnostics(
        flows_document,
        domain_document,
        diagnostics,
    )
    selected_flow = _select_rasa_flow(flows_document, flow_id, diagnostics)
    selected_flow_identifier = (
        selected_flow[0] if selected_flow is not None else flow_id or "invalid_flow"
    )
    flow_definition = selected_flow[1] if selected_flow is not None else {}
    _import_flow_property_diagnostics(selected_flow_identifier, flow_definition, diagnostics)
    if not FLOWSPEC_FLOW_IDENTIFIER_PATTERN.fullmatch(selected_flow_identifier):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOW_ID_UNSUPPORTED",
                f"$.flows.{selected_flow_identifier}",
                "the selected id must already satisfy flowspec2's lowercase "
                "underscore identifier contract",
            )
        )

    description = flow_definition.get("description")
    if not isinstance(description, str) or not description:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOW_DESCRIPTION_REQUIRED",
                f"$.flows.{selected_flow_identifier}.description",
                "the selected flow requires a non-empty description",
            )
        )
        description = "Invalid imported Rasa flow"
    steps = sequence_or_none(flow_definition.get("steps"))
    if steps is None or not steps:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FLOW_STEPS_REQUIRED",
                f"$.flows.{selected_flow_identifier}.steps",
                "the selected flow requires a non-empty steps list",
            )
        )
        steps = ()
    step_identifier_diagnostics(
        steps,
        f"$.flows.{selected_flow_identifier}.steps",
        diagnostics,
    )

    referenced_slot_names = {
        slot_name
        for step_value in steps
        if (step := mapping_or_none(step_value)) is not None
        and isinstance((slot_name := step.get("collect")), str)
        and slot_name
    }
    if referenced_slot_names:
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
                f"$.flows.{selected_flow_identifier}.steps",
                "Rasa CALM collection can fill or correct slots opportunistically "
                "and processes interruption or repair commands during collection; "
                "imported flowspec2 collection is sequential with explicit "
                "correction and transition routing",
            )
        )
    _import_persisted_slots_diagnostics(
        selected_flow_identifier,
        flow_definition,
        referenced_slot_names,
        diagnostics,
    )
    slot_declarations = mapping_or_none(domain_document.get("slots"))
    if slot_declarations is None:
        if referenced_slot_names:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_DOMAIN_SLOTS_REQUIRED",
                    "$.slots",
                    "domain.yml must contain a slots mapping for every collected slot",
                )
            )
        slot_declarations = {}
    responses = mapping_or_none(domain_document.get("responses"))
    if responses is None:
        if referenced_slot_names:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_RESPONSES_MAPPING_REQUIRED",
                    "$.responses",
                    "domain.yml must contain responses for every collected slot",
                )
            )
        responses = {}

    flowspec_domains: dict[str, Any] = {}
    flowspec_slots: dict[str, Any] = {}
    domain_names: dict[str, str] = {}
    for slot_name in sorted(referenced_slot_names):
        slot_declaration = mapping_or_none(slot_declarations.get(slot_name))
        if slot_declaration is None:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_SLOT_DEFINITION_INVALID",
                    f"$.slots.{slot_name}",
                    "a collected slot requires a mapping declaration in domain.yml",
                )
            )
            continue
        domain_specification = _import_domain_specification(
            slot_name,
            slot_declaration,
            diagnostics,
        )
        if domain_specification is None:
            continue
        domain_name = f"rasa_{slot_name}"
        domain_names[slot_name] = domain_name
        flowspec_domains[domain_name] = domain_specification
        flowspec_slots[slot_name] = {
            "domain": domain_name,
            "persist": "data",
            "required": True,
        }

    actions_value = domain_document.get("actions", [])
    actions = sequence_or_none(actions_value)
    if actions is None or not all(isinstance(action_name, str) for action_name in actions):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ACTIONS_DECLARATION_INVALID",
                "$.actions",
                "domain.yml actions must be a list of custom-action names",
            )
        )
        declared_actions: frozenset[str] = frozenset()
    else:
        declared_actions = frozenset(cast(Sequence[str], actions))

    flowspec_path: list[dict[str, Any]] = []
    imported_terminal: dict[str, Any] | None = None
    for step_index, step_value in enumerate(steps):
        step = mapping_or_none(step_value)
        source_path = f"$.flows.{selected_flow_identifier}.steps[{step_index}]"
        if step is None:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_STEP_DEFINITION_INVALID",
                    source_path,
                    "each Rasa flow step must be a mapping",
                )
            )
            continue
        if "collect" in step:
            imported_step = _import_collect_step(
                step,
                selected_flow_identifier,
                step_index,
                slot_declarations,
                domain_names,
                flowspec_domains,
                responses,
                diagnostics,
            )
            if imported_step is not None:
                flowspec_path.append(imported_step)
            continue
        if "action" in step:
            imported_action = _import_terminal_action(
                step,
                selected_flow_identifier,
                step_index,
                len(steps),
                declared_actions,
                diagnostics,
            )
            if imported_action is not None:
                terminal_marker, imported_terminal = imported_action
                flowspec_path.append(terminal_marker)
            continue
        if "collect" not in step:
            step_type = next(
                (
                    property_name
                    for property_name in ("set_slots", "call", "link", "noop")
                    if property_name in step
                ),
                "unknown",
            )
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_STEP_TYPE_UNSUPPORTED",
                    source_path,
                    f"Rasa step type {step_type!r} cannot be imported without "
                    "inventing flowspec2 semantics",
                )
            )
            continue

    imported_flow: dict[str, Any] = {
        "schema": FLOWSPEC_FORMAT,
        "flow": selected_flow_identifier,
        "version": flow_version,
        "route": {"description": description},
        "domains": flowspec_domains,
        "slots": flowspec_slots,
        "path": flowspec_path,
    }
    if imported_terminal is not None:
        imported_flow["terminal"] = imported_terminal
    generated_validation_diagnostics = flowspec_validation_diagnostics(
        imported_flow,
        "RASA_GENERATED_FLOWSPEC_INVALID",
    )
    diagnostics.extend(generated_validation_diagnostics)
    if not generated_validation_diagnostics and (
        compilation_diagnostic := flowspec_compilation_diagnostic(
            imported_flow,
            "RASA_GENERATED_FLOWSPEC_NON_EXECUTABLE",
        )
    ):
        diagnostics.append(compilation_diagnostic)

    report = CompatibilityReport(
        source_format=RASA_FORMAT,
        target_format=FLOWSPEC_FORMAT,
        diagnostics=tuple(diagnostics),
    )
    enforce_compatibility_policy(report, allow_lossy=allow_lossy)
    return ConversionOutcome(artifact=imported_flow, report=report)
