"""FlowSpec2 to bounded Rasa CALM export conversion."""

from __future__ import annotations

from typing import Any, Mapping, Sequence, cast

from .models import (
    CompatibilityDiagnostic,
    CompatibilityReport,
    ConversionOutcome,
    enforce_compatibility_policy,
)
from .rasa_shared import (
    FLOWSPEC_FORMAT,
    RASA_ACTION_IDENTIFIER_PATTERN,
    RASA_DOMAIN_VERSION,
    RASA_FORMAT,
    RasaBundle,
    compatibility_diagnostic,
    domain_button_options,
    flowspec_compilation_diagnostic,
    flowspec_validation_diagnostics,
    mapping_or_none,
    rasa_response_key,
    rasa_response_text_supported,
    rasa_set_slots_name_supported,
    rasa_set_slots_value_supported,
    sequence_or_none,
    set_slots_payload,
    step_identifier_diagnostics,
)


def _export_domain_specification(
    domain_name: str,
    domain_specification: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> dict[str, Any] | None:
    source_path = f"$.domains.{domain_name}"
    domain_type = domain_specification.get("type", "categorical")
    rasa_slot: dict[str, Any]

    if domain_type == "categorical":
        raw_values = sequence_or_none(domain_specification.get("values"))
        if raw_values is None or not raw_values:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_CATEGORICAL_VALUES_INVALID",
                    f"{source_path}.values",
                    "a Rasa categorical slot requires a non-empty list of string values",
                )
            )
            return None
        invalid_value_indexes = [
            value_index
            for value_index, category_value in enumerate(raw_values)
            if not isinstance(category_value, str)
        ]
        for value_index in invalid_value_indexes:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_CATEGORICAL_VALUE_UNSUPPORTED",
                    f"{source_path}.values[{value_index}]",
                    "Rasa categorical values in this profile must be strings; null is not portable",
                )
            )
        string_values = [
            category_value for category_value in raw_values if isinstance(category_value, str)
        ]
        normalized_values = [category_value.casefold() for category_value in string_values]
        if len(normalized_values) != len(set(normalized_values)):
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_CATEGORICAL_CASE_COLLISION",
                    f"{source_path}.values",
                    "Rasa coerces categorical values case-insensitively, so "
                    "these values are not distinct",
                )
            )
        rasa_slot = {"type": "categorical", "values": string_values}
    elif domain_type == "bool":
        rasa_slot = {"type": "bool"}
    elif domain_type == "free_text":
        rasa_slot = {"type": "text"}
    elif domain_type in {"brazilian_tax_id", "email", "name"}:
        rasa_slot = {"type": "text"}
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_DOMAIN_VALIDATION_UNSUPPORTED",
                f"{source_path}.type",
                f"flowspec2 {domain_type!r} validation is not enforced by a Rasa text slot",
            )
        )
    else:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_DOMAIN_TYPE_UNSUPPORTED",
                f"{source_path}.type",
                f"domain type {domain_type!r} is outside the Rasa portable profile",
            )
        )
        return None

    rasa_slot["mappings"] = [{"type": "from_llm"}]
    if domain_specification.get("normalize"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_NORMALIZATION_UNSUPPORTED",
                f"{source_path}.normalize",
                "flowspec2 deterministic normalization has no equivalent Rasa slot contract",
            )
        )
    if domain_specification.get("rows"):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_LIST_ROW_DESCRIPTIONS_UNSUPPORTED",
                f"{source_path}.rows",
                "Rasa static buttons do not preserve flowspec2 list-row descriptions",
            )
        )
    if domain_specification.get("optional"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_OPTIONAL_EMPTY_TEXT_UNSUPPORTED",
                f"{source_path}.optional",
                "Rasa text slots do not preserve flowspec2's empty-string skip contract",
            )
        )
    return rasa_slot


def _export_slot_declaration_diagnostics(
    slot_name: str,
    slot_declaration: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    source_path = f"$.slots.{slot_name}"
    if slot_declaration.get("persist", "data") != "data":
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_SLOT_PARTITION_UNSUPPORTED",
                f"{source_path}.persist",
                "Rasa slots cannot preserve flowspec2 internal or payload partitions",
            )
        )
    if slot_declaration.get("nullable"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_NULLABLE_SLOT_UNSUPPORTED",
                f"{source_path}.nullable",
                "the Rasa portable profile does not preserve explicit-null collection semantics",
            )
        )
    if slot_declaration.get("requires"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_SLOT_DEPENDENCY_UNSUPPORTED",
                f"{source_path}.requires",
                "slot precedence and correction invalidation are control-flow semantics",
            )
        )
    if slot_declaration.get("prefill_sources"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_PREFILL_SOURCES_UNSUPPORTED",
                f"{source_path}.prefill_sources",
                "Rasa from_llm mappings do not restrict prefill by flowspec2 source channel",
            )
        )
    if "max_attempts" in slot_declaration:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_MAX_ATTEMPTS_UNSUPPORTED",
                f"{source_path}.max_attempts",
                "Rasa collect steps do not preserve flowspec2 exhaustion control flow",
            )
        )
    if "on_exhaust" in slot_declaration:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_EXHAUSTION_TRANSITION_UNSUPPORTED",
                f"{source_path}.on_exhaust",
                "Rasa collect steps do not preserve flowspec2 exhaustion transitions",
            )
        )
    if "default" in slot_declaration:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_SLOT_DEFAULT_UNSUPPORTED",
                f"{source_path}.default",
                "a Rasa initial value is not equivalent to a flowspec2 exhaustion default",
            )
        )
    if slot_declaration.get("fill_only_when_asked"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_FILL_ONLY_WHEN_ASKED_UNSUPPORTED",
                f"{source_path}.fill_only_when_asked",
                "Rasa from_llm mappings may fill the slot outside its collect step",
            )
        )


def _export_buttons(
    slot_name: str,
    domain_specification: Mapping[str, Any],
    source_path: str,
    diagnostics: list[CompatibilityDiagnostic],
) -> list[dict[str, str]] | None:
    button_options = domain_button_options(domain_specification)
    if not button_options:
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_STATIC_BUTTONS_UNSUPPORTED",
                source_path,
                "this domain cannot be materialized as static Rasa SetSlots buttons",
            )
        )
        return None
    if not rasa_response_text_supported(slot_name):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                f"{source_path}.field",
                "curly braces in a SetSlots name trigger Rasa response interpolation",
            )
        )
        return None
    if not rasa_set_slots_name_supported(slot_name):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_SETSLOTS_SLOT_NAME_UNSUPPORTED",
                f"{source_path}.field",
                "portable SetSlots names must be non-empty and forbid '=,()'",
            )
        )
        return None

    interpolated_values = [
        button_value
        for button_value, _button_title in button_options
        if not rasa_response_text_supported(button_value)
    ]
    for button_value in interpolated_values:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                source_path,
                "curly braces in a button title or payload trigger Rasa response "
                f"interpolation: {button_value!r}",
            )
        )
    unsupported_values = [
        button_value
        for button_value, _button_title in button_options
        if rasa_response_text_supported(button_value)
        and not rasa_set_slots_value_supported(button_value)
    ]
    for button_value in unsupported_values:
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_SETSLOTS_VALUE_UNSUPPORTED",
                source_path,
                "portable SetSlots values must be non-empty and forbid ',()'; "
                f"'=' remains valid: {button_value!r}",
            )
        )
    if interpolated_values or unsupported_values:
        return None
    return [
        {
            "title": button_title,
            "payload": set_slots_payload(slot_name, button_value),
        }
        for button_value, button_title in button_options
    ]


def _export_interactive(
    interactive: Mapping[str, Any],
    slot_name: str,
    domain_name: str,
    domain_specification: Mapping[str, Any],
    source_path: str,
    diagnostics: list[CompatibilityDiagnostic],
) -> list[dict[str, str]] | None:
    interactive_kind = interactive.get("kind")
    control_properties = (
        "options_when",
        "out_of_band",
        "next_step",
        "meta_flow_ref",
        "prefill_from",
    )
    for property_name in control_properties:
        if interactive.get(property_name):
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_INTERACTIVE_CONTROL_UNSUPPORTED",
                    f"{source_path}.{property_name}",
                    f"interactive control {property_name!r} has no portable Rasa equivalent",
                )
            )
    if interactive_kind != "buttons":
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_INTERACTIVE_KIND_UNSUPPORTED",
                f"{source_path}.kind",
                f"interactive kind {interactive_kind!r} is reduced to a text response",
            )
        )
        return None
    if interactive.get("field") != slot_name:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_INTERACTIVE_FIELD_MISMATCH",
                f"{source_path}.field",
                "a Rasa collect response may set only the slot being collected",
            )
        )
        return None
    if interactive.get("from_domain") != domain_name:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_INTERACTIVE_DOMAIN_MISMATCH",
                f"{source_path}.from_domain",
                "static buttons must be materialized from the collected slot's domain",
            )
        )
        return None
    if any(interactive.get(property_name) for property_name in control_properties):
        return None
    return _export_buttons(
        slot_name,
        domain_specification,
        source_path,
        diagnostics,
    )


def _export_collection_step(
    flow_identifier: str,
    step_index: int,
    step: Mapping[str, Any],
    slot_name: str,
    is_confirmation: bool,
    slots: Mapping[str, Any],
    domains: Mapping[str, Any],
    responses: dict[str, list[dict[str, Any]]],
    diagnostics: list[CompatibilityDiagnostic],
) -> dict[str, Any] | None:
    source_path = f"$.path[{step_index}]"
    slot_declaration = mapping_or_none(slots.get(slot_name))
    if slot_declaration is None:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_SLOT_DECLARATION_MISSING",
                f"{source_path}.{'confirm' if is_confirmation else 'slot'}",
                f"slot {slot_name!r} has no usable declaration",
            )
        )
        return None
    domain_name = slot_declaration.get("domain")
    domain_specification = (
        mapping_or_none(domains.get(domain_name)) if isinstance(domain_name, str) else None
    )
    if domain_specification is None:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_DOMAIN_REFERENCE_MISSING",
                f"$.slots.{slot_name}.domain",
                f"domain {domain_name!r} has no usable declaration",
            )
        )
        return None
    if is_confirmation and domain_specification.get("type", "categorical") != "bool":
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_CONFIRM_DOMAIN_UNSUPPORTED",
                f"{source_path}.confirm",
                "only a bool-domain confirmation is portable to Rasa",
            )
        )
    if is_confirmation:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_CONFIRM_SEMANTICS_UNSUPPORTED",
                f"{source_path}.confirm",
                "Rasa collect cannot preserve flowspec2 confirmation exhaustion "
                "without changing skip-if-filled behavior",
            )
        )

    for control_property in ("ask_when", "skip_when", "on_reject"):
        if control_property in step:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_STEP_CONTROL_UNSUPPORTED",
                    f"{source_path}.{control_property}",
                    f"step control {control_property!r} is not part of the linear portable profile",
                )
            )
    if step.get("correctable"):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_CORRECTION_HUB_UNSUPPORTED",
                f"{source_path}.correctable",
                "flowspec2 correction routing cannot be represented as a plain Rasa collect step",
            )
        )

    rasa_step: dict[str, Any] = {"collect": slot_name}
    if isinstance(step.get("step"), str):
        rasa_step["id"] = step["step"]
    prompt = mapping_or_none(step.get("prompt")) or {}
    if isinstance(prompt.get("extract_hint"), str):
        rasa_step["description"] = prompt["extract_hint"]
    if prompt.get("verbatim"):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_VERBATIM_PROMPT_UNSUPPORTED",
                f"{source_path}.prompt.verbatim",
                "Rasa does not carry flowspec2's prohibition on prompt rephrasing",
            )
        )

    response_key = rasa_response_key(flow_identifier, slot_name, step_index)
    rasa_step["utter"] = response_key
    response_text_value = prompt.get(
        "text",
        "Do you confirm?" if is_confirmation else f"Provide {slot_name}.",
    )
    if not isinstance(response_text_value, str) or not response_text_value:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_TEXT_REQUIRED",
                f"{source_path}.prompt.text",
                "the portable Rasa response requires non-empty prompt text",
            )
        )
        response_text = ""
    else:
        response_text = response_text_value
    if response_text and not rasa_response_text_supported(response_text):
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                f"{source_path}.prompt.text",
                "curly braces are literal in flowspec2 but trigger Rasa response interpolation",
            )
        )
    response_variation: dict[str, Any] = {"text": response_text}
    interactive = mapping_or_none(step.get("interactive"))
    if interactive is not None:
        buttons = _export_interactive(
            interactive,
            slot_name,
            cast(str, domain_name),
            domain_specification,
            f"{source_path}.interactive",
            diagnostics,
        )
        if buttons is not None:
            response_variation["buttons"] = buttons
    responses[response_key] = [response_variation]
    return rasa_step


def _export_top_level_diagnostics(
    flow_document: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    diagnostics.append(
        compatibility_diagnostic(
            "warning",
            "RASA_FLOW_VERSION_UNSUPPORTED",
            "$.version",
            "Rasa flows.yml has no canonical field for the flowspec2 document version",
        )
    )
    if any(
        isinstance(step, Mapping) and ("slot" in step or "confirm" in step)
        for step in cast(Sequence[Any], flow_document.get("path", ()))
    ):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
                "$.path",
                "Rasa CALM collection can fill or correct slots opportunistically "
                "and processes interruption or repair commands during collection; "
                "flowspec2 collection is sequential with explicit correction and "
                "transition routing",
            )
        )
    route = mapping_or_none(flow_document.get("route")) or {}
    if route.get("trigger_phrases"):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_TRIGGER_PHRASES_UNSUPPORTED",
                "$.route.trigger_phrases",
                "free-text trigger phrases are not Rasa nlu_trigger intent identifiers",
            )
        )
    if "entry_args_schema" in route:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_ENTRY_ARGUMENTS_UNSUPPORTED",
                "$.route.entry_args_schema",
                "Rasa flows do not preserve flowspec2's closed entry-argument schema",
            )
        )
    if flow_document.get("service"):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_SERVICE_METADATA_UNSUPPORTED",
                "$.service",
                "municipal service identity metadata is not represented in Rasa flows.yml",
            )
        )
    for property_name, code, message in (
        (
            "config",
            "RASA_CONFIG_CONTROL_UNSUPPORTED",
            "flow guardrail configuration has no portable Rasa equivalent",
        ),
        (
            "entry",
            "RASA_ENTRY_ACTION_UNSUPPORTED",
            "the best-effort entry side effect is not a linear collect step",
        ),
        (
            "uses",
            "RASA_SUBFLOW_UNSUPPORTED",
            "versioned built-in subflows cannot be expanded without changing their contracts",
        ),
        (
            "derive",
            "RASA_DERIVATION_UNSUPPORTED",
            "lookup derivations are deterministic control-flow constructs",
        ),
        (
            "confirm",
            "RASA_CORRECTION_ROUTER_UNSUPPORTED",
            "the top-level correction router has no plain collect equivalent",
        ),
        (
            "overrides",
            "RASA_OVERRIDE_GATES_UNSUPPORTED",
            "override gates require conditional Rasa control flow",
        ),
        (
            "auto_flow",
            "RASA_AUTO_FLOW_UNSUPPORTED",
            "WhatsApp Flow send/resume behavior is outside the Rasa portable profile",
        ),
    ):
        if flow_document.get(property_name):
            diagnostics.append(
                compatibility_diagnostic("error", code, f"$.{property_name}", message)
            )

    capabilities = mapping_or_none(flow_document.get("capabilities")) or {}
    for capability_name, capability_value in capabilities.items():
        if not capability_value:
            continue
        if capability_name == "await_external":
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_EXTERNAL_WAIT_UNSUPPORTED",
                    "$.capabilities.await_external",
                    "suspend/resume and recovery transitions require profile-aware runtime support",
                )
            )
        else:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_CAPABILITY_UNSUPPORTED",
                    f"$.capabilities.{capability_name}",
                    f"agent capability {capability_name!r} is omitted from the Rasa bundle",
                )
            )


def export_rasa(
    flow_document: Mapping[str, Any],
    *,
    allow_lossy: bool = False,
) -> ConversionOutcome[RasaBundle]:
    """Project one flowspec2 document into the bounded Rasa CALM profile."""

    source_document = dict(flow_document)
    diagnostics = list(
        flowspec_validation_diagnostics(
            source_document,
            "FLOWSPEC_INVALID_DOCUMENT",
        )
    )
    if diagnostics:
        report = CompatibilityReport(
            source_format=FLOWSPEC_FORMAT,
            target_format=RASA_FORMAT,
            diagnostics=tuple(diagnostics),
        )
        enforce_compatibility_policy(report, allow_lossy=allow_lossy)
        raise AssertionError("an invalid source document must block Rasa export")

    if compilation_diagnostic := flowspec_compilation_diagnostic(
        source_document,
        "FLOWSPEC_NON_EXECUTABLE",
    ):
        diagnostics.append(compilation_diagnostic)

    _export_top_level_diagnostics(source_document, diagnostics)
    flow_identifier = cast(str, source_document["flow"])
    route = cast(Mapping[str, Any], source_document["route"])
    domains = cast(Mapping[str, Any], source_document["domains"])
    slots = cast(Mapping[str, Any], source_document.get("slots", {}))

    referenced_domain_names = {
        slot_declaration["domain"]
        for slot_value in slots.values()
        if (slot_declaration := mapping_or_none(slot_value)) is not None
        and isinstance(slot_declaration.get("domain"), str)
    }
    converted_domains: dict[str, dict[str, Any]] = {}
    for domain_name, domain_value in domains.items():
        domain_specification = cast(Mapping[str, Any], domain_value)
        converted_domain = _export_domain_specification(
            domain_name,
            domain_specification,
            diagnostics,
        )
        if converted_domain is not None:
            converted_domains[domain_name] = converted_domain
        if domain_name not in referenced_domain_names:
            diagnostics.append(
                compatibility_diagnostic(
                    "warning",
                    "RASA_UNREFERENCED_DOMAIN_UNSUPPORTED",
                    f"$.domains.{domain_name}",
                    "Rasa domain.yml has no standalone registry for an "
                    "unreferenced flowspec2 domain",
                )
            )

    rasa_slots: dict[str, Any] = {}
    for slot_name, slot_value in slots.items():
        slot_declaration = cast(Mapping[str, Any], slot_value)
        _export_slot_declaration_diagnostics(slot_name, slot_declaration, diagnostics)
        referenced_domain_name = slot_declaration.get("domain")
        referenced_domain_specification = (
            mapping_or_none(domains.get(referenced_domain_name))
            if isinstance(referenced_domain_name, str)
            else None
        )
        if referenced_domain_specification is None:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_DOMAIN_REFERENCE_MISSING",
                    f"$.slots.{slot_name}.domain",
                    f"domain {referenced_domain_name!r} has no usable declaration",
                )
            )
            continue
        converted_slot = converted_domains.get(cast(str, referenced_domain_name))
        if converted_slot is not None:
            rasa_slots[slot_name] = converted_slot

    responses: dict[str, list[dict[str, Any]]] = {}
    rasa_steps: list[dict[str, Any]] = []
    collected_slot_names: list[str] = []
    actions: list[str] = []
    terminal_marker_seen = False
    for step_index, step_value in enumerate(cast(Sequence[Any], source_document["path"])):
        step = cast(Mapping[str, Any], step_value)
        if isinstance(step.get("slot"), str):
            converted_step = _export_collection_step(
                flow_identifier,
                step_index,
                step,
                cast(str, step["slot"]),
                False,
                slots,
                domains,
                responses,
                diagnostics,
            )
            if converted_step is not None:
                rasa_steps.append(converted_step)
                collected_slot_names.append(cast(str, step["slot"]))
        elif isinstance(step.get("confirm"), str):
            converted_step = _export_collection_step(
                flow_identifier,
                step_index,
                step,
                cast(str, step["confirm"]),
                True,
                slots,
                domains,
                responses,
                diagnostics,
            )
            if converted_step is not None:
                rasa_steps.append(converted_step)
                collected_slot_names.append(cast(str, step["confirm"]))
        elif step.get("terminal") is True:
            terminal_marker_seen = True
            terminal = mapping_or_none(source_document.get("terminal"))
            if terminal is None:
                diagnostics.append(
                    compatibility_diagnostic(
                        "error",
                        "RASA_TERMINAL_DECLARATION_MISSING",
                        f"$.path[{step_index}].terminal",
                        "the terminal marker has no usable terminal declaration",
                    )
                )
                continue
            tool_name = terminal.get("tool")
            if not isinstance(tool_name, str) or not RASA_ACTION_IDENTIFIER_PATTERN.fullmatch(
                tool_name
            ):
                diagnostics.append(
                    compatibility_diagnostic(
                        "error",
                        "RASA_ACTION_NAME_UNSUPPORTED",
                        "$.terminal.tool",
                        "the terminal tool is not a valid portable Rasa custom-action name",
                    )
                )
                continue
            terminal_step: dict[str, Any] = {"action": tool_name}
            if isinstance(terminal.get("step"), str):
                terminal_step["id"] = terminal["step"]
            rasa_steps.append(terminal_step)
            actions.append(tool_name)
            diagnostics.append(
                compatibility_diagnostic(
                    "warning",
                    "RASA_ACTION_ADAPTER_REQUIRED",
                    "$.terminal",
                    "the custom action must implement flowspec2 inputs, "
                    "idempotency, outputs and outcome lifecycle",
                )
            )
        else:
            step_kind = next(
                (
                    property_name
                    for property_name in ("use", "derive", "await_external")
                    if property_name in step
                ),
                "unknown",
            )
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_PATH_STEP_UNSUPPORTED",
                    f"$.path[{step_index}]",
                    f"path step kind {step_kind!r} is outside the linear Rasa portable profile",
                )
            )

    step_identifier_diagnostics(rasa_steps, "$.path", diagnostics)
    if source_document.get("terminal") and not terminal_marker_seen:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_TERMINAL_UNREACHABLE",
                "$.terminal",
                "the terminal declaration has no marker in the source path",
            )
        )
    for unreferenced_slot_name in sorted(set(slots) - set(collected_slot_names)):
        diagnostics.append(
            compatibility_diagnostic(
                "warning",
                "RASA_UNREFERENCED_SLOT_UNSUPPORTED",
                f"$.slots.{unreferenced_slot_name}",
                "Rasa import scope is limited to slots collected by the selected "
                "flow, so this declaration will not survive a round trip",
            )
        )
    required_slot_names = {
        slot_name
        for slot_name, slot_value in slots.items()
        if (slot_declaration := mapping_or_none(slot_value)) is not None
        and slot_declaration.get("required") is True
    }
    uncollected_required_slot_names = required_slot_names - set(collected_slot_names)
    if uncollected_required_slot_names:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_REQUIRED_SLOT_NOT_COLLECTED",
                "$.slots",
                "required slots have no portable collect step: "
                f"{sorted(uncollected_required_slot_names)!r}",
            )
        )
    if not collected_slot_names:
        diagnostics.append(
            compatibility_diagnostic(
                "error",
                "RASA_NO_PORTABLE_STEPS",
                "$.path",
                "the source flow produced no portable Rasa collect steps",
            )
        )

    rasa_flow: dict[str, Any] = {
        "description": route["description"],
        "run_pattern_completed": False,
        "persisted_slots": list(dict.fromkeys(collected_slot_names)),
        "steps": rasa_steps,
    }
    flows_document = {"flows": {flow_identifier: rasa_flow}}
    domain_document: dict[str, Any] = {
        "version": RASA_DOMAIN_VERSION,
        "slots": rasa_slots,
        "responses": responses,
    }
    if actions:
        domain_document["actions"] = list(dict.fromkeys(actions))

    report = CompatibilityReport(
        source_format=FLOWSPEC_FORMAT,
        target_format=RASA_FORMAT,
        diagnostics=tuple(diagnostics),
    )
    enforce_compatibility_policy(report, allow_lossy=allow_lossy)
    return ConversionOutcome(
        artifact=RasaBundle(flows_document, domain_document),
        report=report,
    )
