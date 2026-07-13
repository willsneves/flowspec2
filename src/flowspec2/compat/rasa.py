"""Bounded interoperability with the portable Rasa CALM collection subset.

The converter deliberately does not model arbitrary Rasa projects.  It projects
linear ``collect`` steps (including ordinary bool slots), their slot domains, one
unconditional prompt response, and static ``SetSlots`` buttons.  Confirmation
steps are blocked because their exhaustion behavior has no equivalent Rasa
``collect`` contract.  Profile ``rasa-calm/1`` targets Rasa 3.11.0 or newer.
Every omitted source construct is represented by a compatibility diagnostic
before policy is enforced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from flowspec2.compat.tool_profiles import (
    compatibility_tool_names,
    synthetic_compatibility_tool_definition,
)
from flowspec2.compiler import compile_flow
from flowspec2.domains import interactive_options_for_domain
from flowspec2.schema import schema as flowspec_schema
from flowspec2.tools import ToolRegistry, default_tool_registry

from .models import (
    CompatibilityDiagnostic,
    CompatibilityReport,
    ConversionOutcome,
    DiagnosticSeverity,
    enforce_compatibility_policy,
)

RASA_PROFILE_VERSION = "1"
RASA_FORMAT = f"rasa-calm/{RASA_PROFILE_VERSION}"
RASA_MINIMUM_VERSION = "3.11.0"
FLOWSPEC_FORMAT = "flowspec/2"
RASA_DOMAIN_VERSION = "3.1"

_FLOWSPEC_FLOW_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_RASA_ACTION_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RASA_SET_SLOTS_NAME_FORBIDDEN = frozenset("=,()")
_RASA_SET_SLOTS_VALUE_FORBIDDEN = frozenset(",()")
_RASA_RESPONSE_TEMPLATE_CHARACTERS = frozenset("{}")


@dataclass(frozen=True, init=False)
class RasaBundle:
    """An immutable pair of Rasa ``flows.yml`` and ``domain.yml`` documents.

    Documents are held as private JSON snapshots.  Each public accessor returns a
    fresh dictionary, so mutating a caller-owned object can never mutate the
    bundle or a later serialization.
    """

    _flows_snapshot: str = field(repr=False)
    _domain_snapshot: str = field(repr=False)

    def __init__(
        self,
        flows: Mapping[str, Any],
        domain: Mapping[str, Any],
    ) -> None:
        object.__setattr__(self, "_flows_snapshot", _snapshot_mapping(flows))
        object.__setattr__(self, "_domain_snapshot", _snapshot_mapping(domain))

    @property
    def flows(self) -> dict[str, Any]:
        """Return a defensive copy of the ``flows.yml`` document."""

        return cast(dict[str, Any], json.loads(self._flows_snapshot))

    @property
    def domain(self) -> dict[str, Any]:
        """Return a defensive copy of the ``domain.yml`` document."""

        return cast(dict[str, Any], json.loads(self._domain_snapshot))

    @property
    def flows_document(self) -> dict[str, Any]:
        """Alias for callers that prefer an explicit document name."""

        return self.flows

    @property
    def domain_document(self) -> dict[str, Any]:
        """Alias for callers that prefer an explicit document name."""

        return self.domain


def _snapshot_mapping(document: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(document),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


async def _compatibility_profile_tool(**_inputs: Any) -> dict[str, Any]:
    """Stand in for a declared action while the adapter checks compilation only."""

    return {}


def _compatibility_tool_registry(flow_document: Mapping[str, Any]) -> ToolRegistry:
    """Register only tool names explicitly present in the document being reviewed."""

    registry = ToolRegistry()
    known_definitions = default_tool_registry().definitions
    for tool_name in compatibility_tool_names(flow_document):
        registry.register(
            tool_name,
            _compatibility_profile_tool,
            definition=known_definitions.get(tool_name)
            or synthetic_compatibility_tool_definition(
                tool_name,
                flow_document,
                version="rasa-compatibility-profile",
                description=(
                    "Synthetic compile-only contract derived from the Rasa compatibility artifact."
                ),
            ),
        )
    return registry


def _diagnostic(
    severity: DiagnosticSeverity,
    code: str,
    source_path: str,
    message: str,
) -> CompatibilityDiagnostic:
    return CompatibilityDiagnostic(
        severity=severity,
        code=code,
        source_path=source_path,
        message=message,
    )


def _validation_path(validation_error: ValidationError) -> str:
    path = "$"
    for segment in validation_error.absolute_path:
        path += f"[{segment}]" if isinstance(segment, int) else f".{segment}"
    return path


def _flowspec_validation_diagnostics(
    flow_document: Mapping[str, Any],
    code: str,
) -> tuple[CompatibilityDiagnostic, ...]:
    validator = Draft202012Validator(flowspec_schema())
    return tuple(
        _diagnostic(
            "error",
            code,
            _validation_path(validation_error),
            validation_error.message,
        )
        for validation_error in validator.iter_errors(dict(flow_document))
    )


def _flowspec_compilation_diagnostic(
    flow_document: Mapping[str, Any],
    code: str,
) -> CompatibilityDiagnostic | None:
    try:
        compile_flow(
            dict(flow_document),
            tools=_compatibility_tool_registry(flow_document),
        )
    except (KeyError, StopIteration, TypeError, ValueError) as compilation_error:
        return _diagnostic(
            "error",
            code,
            "$",
            "flowspec2 compiler rejected the document: "
            f"{str(compilation_error) or compilation_error.__class__.__name__}",
        )
    return None


def _mapping(value: object) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return cast(Mapping[str, Any], value)


def _sequence(value: object) -> Sequence[Any] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return None
    return value


def _step_identifier_diagnostics(
    steps: Sequence[Any],
    source_path: str,
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    first_indexes_by_identifier: dict[str, int] = {}
    for step_index, step_value in enumerate(steps):
        step = _mapping(step_value)
        if step is None or "id" not in step:
            continue
        step_identifier = step.get("id")
        if not isinstance(step_identifier, str) or not step_identifier:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "RASA_STEP_ID_INVALID",
                    f"{source_path}[{step_index}].id",
                    "a Rasa step id must be a non-empty string",
                )
            )
            continue
        if step_identifier in first_indexes_by_identifier:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "RASA_DUPLICATE_STEP_ID",
                    f"{source_path}[{step_index}].id",
                    f"step id {step_identifier!r} was already used at index "
                    f"{first_indexes_by_identifier[step_identifier]}",
                )
            )
        else:
            first_indexes_by_identifier[step_identifier] = step_index


def _rasa_response_key(flow_identifier: str, slot_name: str, step_index: int) -> str:
    normalized_slot_name = re.sub(r"[^A-Za-z0-9_]", "_", slot_name)
    return f"utter_ask_{flow_identifier}_{normalized_slot_name}_{step_index + 1}"


def _rasa_set_slots_name_supported(slot_name: str) -> bool:
    return bool(slot_name) and not any(
        character in slot_name
        for character in (_RASA_SET_SLOTS_NAME_FORBIDDEN | _RASA_RESPONSE_TEMPLATE_CHARACTERS)
    )


def _rasa_set_slots_value_supported(slot_value: str) -> bool:
    return bool(slot_value) and not any(
        character in slot_value
        for character in (_RASA_SET_SLOTS_VALUE_FORBIDDEN | _RASA_RESPONSE_TEMPLATE_CHARACTERS)
    )


def _rasa_response_text_supported(response_text: str) -> bool:
    return not any(character in response_text for character in _RASA_RESPONSE_TEMPLATE_CHARACTERS)


def _set_slots_payload(slot_name: str, slot_value: str) -> str:
    return f"/SetSlots({slot_name}={slot_value})"


def _domain_button_options(
    domain_specification: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(domain_option.value).lower(), domain_option.title)
        for domain_option in interactive_options_for_domain(domain_specification)
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
        raw_values = _sequence(domain_specification.get("values"))
        if raw_values is None or not raw_values:
            diagnostics.append(
                _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
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
    elif domain_type in {"cpf", "email", "name"}:
        rasa_slot = {"type": "text"}
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_DOMAIN_VALIDATION_UNSUPPORTED",
                f"{source_path}.type",
                f"flowspec2 {domain_type!r} validation is not enforced by a Rasa text slot",
            )
        )
    else:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_NORMALIZATION_UNSUPPORTED",
                f"{source_path}.normalize",
                "flowspec2 deterministic normalization has no equivalent Rasa slot contract",
            )
        )
    if domain_specification.get("rows"):
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_LIST_ROW_DESCRIPTIONS_UNSUPPORTED",
                f"{source_path}.rows",
                "Rasa static buttons do not preserve flowspec2 list-row descriptions",
            )
        )
    if domain_specification.get("optional"):
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_SLOT_PARTITION_UNSUPPORTED",
                f"{source_path}.persist",
                "Rasa slots cannot preserve flowspec2 internal or payload partitions",
            )
        )
    if slot_declaration.get("nullable"):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_NULLABLE_SLOT_UNSUPPORTED",
                f"{source_path}.nullable",
                "the Rasa portable profile does not preserve explicit-null collection semantics",
            )
        )
    if slot_declaration.get("requires"):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_SLOT_DEPENDENCY_UNSUPPORTED",
                f"{source_path}.requires",
                "slot precedence and correction invalidation are control-flow semantics",
            )
        )
    if slot_declaration.get("prefill_sources"):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_PREFILL_SOURCES_UNSUPPORTED",
                f"{source_path}.prefill_sources",
                "Rasa from_llm mappings do not restrict prefill by flowspec2 source channel",
            )
        )
    if "max_attempts" in slot_declaration:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_MAX_ATTEMPTS_UNSUPPORTED",
                f"{source_path}.max_attempts",
                "Rasa collect steps do not preserve flowspec2 exhaustion control flow",
            )
        )
    if "on_exhaust" in slot_declaration:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_EXHAUSTION_TRANSITION_UNSUPPORTED",
                f"{source_path}.on_exhaust",
                "Rasa collect steps do not preserve flowspec2 exhaustion transitions",
            )
        )
    if "default" in slot_declaration:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_SLOT_DEFAULT_UNSUPPORTED",
                f"{source_path}.default",
                "a Rasa initial value is not equivalent to a flowspec2 exhaustion default",
            )
        )
    if slot_declaration.get("fill_only_when_asked"):
        diagnostics.append(
            _diagnostic(
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
    button_options = _domain_button_options(domain_specification)
    if not button_options:
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_STATIC_BUTTONS_UNSUPPORTED",
                source_path,
                "this domain cannot be materialized as static Rasa SetSlots buttons",
            )
        )
        return None
    if not _rasa_response_text_supported(slot_name):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                f"{source_path}.field",
                "curly braces in a SetSlots name trigger Rasa response interpolation",
            )
        )
        return None
    if not _rasa_set_slots_name_supported(slot_name):
        diagnostics.append(
            _diagnostic(
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
        if not _rasa_response_text_supported(button_value)
    ]
    for button_value in interpolated_values:
        diagnostics.append(
            _diagnostic(
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
        if _rasa_response_text_supported(button_value)
        and not _rasa_set_slots_value_supported(button_value)
    ]
    for button_value in unsupported_values:
        diagnostics.append(
            _diagnostic(
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
            "payload": _set_slots_payload(slot_name, button_value),
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
                _diagnostic(
                    "error",
                    "RASA_INTERACTIVE_CONTROL_UNSUPPORTED",
                    f"{source_path}.{property_name}",
                    f"interactive control {property_name!r} has no portable Rasa equivalent",
                )
            )
    if interactive_kind != "buttons":
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_INTERACTIVE_KIND_UNSUPPORTED",
                f"{source_path}.kind",
                f"interactive kind {interactive_kind!r} is reduced to a text response",
            )
        )
        return None
    if interactive.get("field") != slot_name:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_INTERACTIVE_FIELD_MISMATCH",
                f"{source_path}.field",
                "a Rasa collect response may set only the slot being collected",
            )
        )
        return None
    if interactive.get("from_domain") != domain_name:
        diagnostics.append(
            _diagnostic(
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
    slot_declaration = _mapping(slots.get(slot_name))
    if slot_declaration is None:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_SLOT_DECLARATION_MISSING",
                f"{source_path}.{'confirm' if is_confirmation else 'slot'}",
                f"slot {slot_name!r} has no usable declaration",
            )
        )
        return None
    domain_name = slot_declaration.get("domain")
    domain_specification = (
        _mapping(domains.get(domain_name)) if isinstance(domain_name, str) else None
    )
    if domain_specification is None:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_DOMAIN_REFERENCE_MISSING",
                f"$.slots.{slot_name}.domain",
                f"domain {domain_name!r} has no usable declaration",
            )
        )
        return None
    if is_confirmation and domain_specification.get("type", "categorical") != "bool":
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_CONFIRM_DOMAIN_UNSUPPORTED",
                f"{source_path}.confirm",
                "only a bool-domain confirmation is portable to Rasa",
            )
        )
    if is_confirmation:
        diagnostics.append(
            _diagnostic(
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
                _diagnostic(
                    "error",
                    "RASA_STEP_CONTROL_UNSUPPORTED",
                    f"{source_path}.{control_property}",
                    f"step control {control_property!r} is not part of the linear portable profile",
                )
            )
    if step.get("correctable"):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_CORRECTION_HUB_UNSUPPORTED",
                f"{source_path}.correctable",
                "flowspec2 correction routing cannot be represented as a plain Rasa collect step",
            )
        )

    rasa_step: dict[str, Any] = {"collect": slot_name}
    if isinstance(step.get("step"), str):
        rasa_step["id"] = step["step"]
    prompt = _mapping(step.get("prompt")) or {}
    if isinstance(prompt.get("extract_hint"), str):
        rasa_step["description"] = prompt["extract_hint"]
    if prompt.get("verbatim"):
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_VERBATIM_PROMPT_UNSUPPORTED",
                f"{source_path}.prompt.verbatim",
                "Rasa does not carry flowspec2's prohibition on prompt rephrasing",
            )
        )

    response_key = _rasa_response_key(flow_identifier, slot_name, step_index)
    rasa_step["utter"] = response_key
    response_text_value = prompt.get(
        "text",
        "Confirma?" if is_confirmation else f"Informe {slot_name}.",
    )
    if not isinstance(response_text_value, str) or not response_text_value:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_RESPONSE_TEXT_REQUIRED",
                f"{source_path}.prompt.text",
                "the portable Rasa response requires non-empty prompt text",
            )
        )
        response_text = ""
    else:
        response_text = response_text_value
    if response_text and not _rasa_response_text_supported(response_text):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED",
                f"{source_path}.prompt.text",
                "curly braces are literal in flowspec2 but trigger Rasa response interpolation",
            )
        )
    response_variation: dict[str, Any] = {"text": response_text}
    interactive = _mapping(step.get("interactive"))
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
        _diagnostic(
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
            _diagnostic(
                "warning",
                "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
                "$.path",
                "Rasa CALM collection can fill or correct slots opportunistically "
                "and processes interruption or repair commands during collection; "
                "flowspec2 collection is sequential with explicit correction and "
                "transition routing",
            )
        )
    route = _mapping(flow_document.get("route")) or {}
    if route.get("trigger_phrases"):
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_TRIGGER_PHRASES_UNSUPPORTED",
                "$.route.trigger_phrases",
                "free-text trigger phrases are not Rasa nlu_trigger intent identifiers",
            )
        )
    if "entry_args_schema" in route:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_ENTRY_ARGUMENTS_UNSUPPORTED",
                "$.route.entry_args_schema",
                "Rasa flows do not preserve flowspec2's closed entry-argument schema",
            )
        )
    if flow_document.get("service"):
        diagnostics.append(
            _diagnostic(
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
            diagnostics.append(_diagnostic("error", code, f"$.{property_name}", message))

    capabilities = _mapping(flow_document.get("capabilities")) or {}
    for capability_name, capability_value in capabilities.items():
        if not capability_value:
            continue
        if capability_name == "await_external":
            diagnostics.append(
                _diagnostic(
                    "error",
                    "RASA_EXTERNAL_WAIT_UNSUPPORTED",
                    "$.capabilities.await_external",
                    "suspend/resume and recovery transitions require profile-aware runtime support",
                )
            )
        else:
            diagnostics.append(
                _diagnostic(
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
        _flowspec_validation_diagnostics(
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

    if compilation_diagnostic := _flowspec_compilation_diagnostic(
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
        if (slot_declaration := _mapping(slot_value)) is not None
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
                _diagnostic(
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
            _mapping(domains.get(referenced_domain_name))
            if isinstance(referenced_domain_name, str)
            else None
        )
        if referenced_domain_specification is None:
            diagnostics.append(
                _diagnostic(
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
            terminal = _mapping(source_document.get("terminal"))
            if terminal is None:
                diagnostics.append(
                    _diagnostic(
                        "error",
                        "RASA_TERMINAL_DECLARATION_MISSING",
                        f"$.path[{step_index}].terminal",
                        "the terminal marker has no usable terminal declaration",
                    )
                )
                continue
            tool_name = terminal.get("tool")
            if not isinstance(tool_name, str) or not _RASA_ACTION_IDENTIFIER.fullmatch(tool_name):
                diagnostics.append(
                    _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
                    "error",
                    "RASA_PATH_STEP_UNSUPPORTED",
                    f"$.path[{step_index}]",
                    f"path step kind {step_kind!r} is outside the linear Rasa portable profile",
                )
            )

    _step_identifier_diagnostics(rasa_steps, "$.path", diagnostics)
    if source_document.get("terminal") and not terminal_marker_seen:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_TERMINAL_UNREACHABLE",
                "$.terminal",
                "the terminal declaration has no marker in the source path",
            )
        )
    for unreferenced_slot_name in sorted(set(slots) - set(collected_slot_names)):
        diagnostics.append(
            _diagnostic(
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
        if (slot_declaration := _mapping(slot_value)) is not None
        and slot_declaration.get("required") is True
    }
    uncollected_required_slot_names = required_slot_names - set(collected_slot_names)
    if uncollected_required_slot_names:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_REQUIRED_SLOT_NOT_COLLECTED",
                "$.slots",
                "required slots have no portable collect step: "
                f"{sorted(uncollected_required_slot_names)!r}",
            )
        )
    if not collected_slot_names:
        diagnostics.append(
            _diagnostic(
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


def _import_slot_mapping_diagnostics(
    slot_name: str,
    slot_declaration: Mapping[str, Any],
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    source_path = f"$.slots.{slot_name}"
    if "mappings" in slot_declaration:
        mappings = _sequence(slot_declaration.get("mappings"))
        if mappings is None or len(mappings) != 1 or _mapping(mappings[0]) != {"type": "from_llm"}:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "RASA_SLOT_MAPPING_UNSUPPORTED",
                    f"{source_path}.mappings",
                    "only an omitted mapping or one exact from_llm mapping is portable",
                )
            )
    allowed_properties = {"type", "values", "mappings"}
    for property_name in slot_declaration.keys() - allowed_properties:
        diagnostics.append(
            _diagnostic(
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
        values = _sequence(slot_declaration.get("values"))
        if values is None or not values or not all(isinstance(value, str) for value in values):
            diagnostics.append(
                _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
                    "error",
                    "RASA_SLOT_PROPERTY_UNSUPPORTED",
                    f"{source_path}.values",
                    "text slots do not use categorical values in the portable profile",
                )
            )
        return {"type": "free_text"}
    diagnostics.append(
        _diagnostic(
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
    if not _rasa_set_slots_name_supported(slot_name):
        return None
    if not _rasa_set_slots_value_supported(slot_value):
        return None
    return slot_name, slot_value


def _import_buttons(
    buttons_value: object,
    slot_name: str,
    domain_specification: Mapping[str, Any],
    source_path: str,
    diagnostics: list[CompatibilityDiagnostic],
) -> dict[str, Any] | None:
    buttons = _sequence(buttons_value)
    if buttons is None or not buttons:
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_STATIC_BUTTONS_UNSUPPORTED",
                source_path,
                "buttons must be a non-empty list of title/payload mappings",
            )
        )
        return None
    if not _rasa_set_slots_name_supported(slot_name):
        diagnostics.append(
            _diagnostic(
                "warning",
                "RASA_SETSLOTS_SLOT_NAME_UNSUPPORTED",
                source_path,
                "portable SetSlots names must be non-empty and forbid '=,()'",
            )
        )
        return None

    expected_options = _domain_button_options(domain_specification)
    expected_titles = dict(expected_options)
    accepts_legacy_boolean_titles = domain_specification.get("type") == "bool"
    parsed_values: list[str] = []
    portable = True
    for button_index, button_value in enumerate(buttons):
        button = _mapping(button_value)
        button_path = f"{source_path}[{button_index}]"
        if button is None or set(button) != {"title", "payload"}:
            diagnostics.append(
                _diagnostic(
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
            isinstance(button_text, str) and not _rasa_response_text_supported(button_text)
            for button_text in (button_title, button_payload)
        ):
            diagnostics.append(
                _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
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
            _diagnostic(
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
    variations = _sequence(responses.get(response_key))
    if variations is None or len(variations) != 1:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_RESPONSE_VARIATIONS_UNSUPPORTED",
                source_path,
                "the portable profile requires exactly one unconditional response variation",
            )
        )
        return None, None
    variation = _mapping(variations[0])
    if variation is None or not isinstance(variation.get("text"), str) or not variation["text"]:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_RESPONSE_PROPERTY_UNSUPPORTED",
                f"{source_path}[0].{property_name}",
                f"conditional or rich response property {property_name!r} is not portable",
            )
        )
    response_text = cast(str, variation["text"])
    if not _rasa_response_text_supported(response_text):
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_COLLECT_SLOT_INVALID",
                f"{source_path}.collect",
                "collect must name a non-empty domain slot",
            )
        )
        return None
    if _mapping(slot_declarations.get(slot_name)) is None or slot_name not in domain_names:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_STEP_CONTROL_UNSUPPORTED",
                f"{source_path}.{property_name}",
                f"collect property {property_name!r} changes control flow "
                "outside the portable profile",
            )
        )
    if "description" in step and not isinstance(step.get("description"), str):
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_UTTER_KEY_INVALID",
                f"{source_path}.utter",
                "utter must name one domain response",
            )
        )
        response_key_value = f"utter_ask_{slot_name}"
    elif not response_key_value.startswith("utter_"):
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_ASK_BEFORE_FILLING_INVALID",
                f"{source_path}.ask_before_filling",
                "ask_before_filling must be boolean",
            )
        )
        ask_before_filling = False
    if ask_before_filling:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_ACTION_POSITION_UNSUPPORTED",
                source_path,
                "only one final custom action can represent a flowspec2 terminal",
            )
        )
        return None
    for property_name in step.keys() - {"action", "id"}:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_STEP_CONTROL_UNSUPPORTED",
                f"{source_path}.{property_name}",
                f"terminal action property {property_name!r} changes control flow",
            )
        )
    action_name = step.get("action")
    if not isinstance(action_name, str) or not _RASA_ACTION_IDENTIFIER.fullmatch(action_name):
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_ACTION_NAME_UNSUPPORTED",
                f"{source_path}.action",
                "the final action must be a valid Rasa custom-action identifier",
            )
        )
        return None
    if action_name not in declared_actions:
        diagnostics.append(
            _diagnostic(
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
        _diagnostic(
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
    flows = _mapping(flows_document.get("flows"))
    if flows is None or not flows:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
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
                _diagnostic(
                    "error",
                    "RASA_FLOW_SELECTION_REQUIRED",
                    "$.flows",
                    "flow_id is required when flows.yml contains more than one flow",
                )
            )
        selected_flow_id = string_flow_ids[0] if string_flow_ids else None
    if selected_flow_id is None or selected_flow_id not in flows:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_FLOW_NOT_FOUND",
                "$.flows",
                f"selected flow {selected_flow_id!r} does not exist",
            )
        )
        return None
    selected_flow = _mapping(flows[selected_flow_id])
    if selected_flow is None:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
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
            _diagnostic(
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
            _diagnostic(
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
            _diagnostic(
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
                _diagnostic(
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
                _diagnostic(
                    "warning",
                    "RASA_COLLECTED_SLOT_RESET_UNSUPPORTED",
                    source_path,
                    "Rasa resets collected slots by default, while flowspec2 data slots persist",
                )
            )
        return
    persisted_slots = _sequence(flow_definition.get("persisted_slots"))
    if persisted_slots is None or not all(
        isinstance(slot_name, str) for slot_name in persisted_slots
    ):
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_PERSISTED_SLOTS_INVALID",
                source_path,
                "persisted_slots must not repeat a slot name",
            )
        )
    unexpected_slot_names = set(persisted_slot_names) - referenced_slot_names
    if unexpected_slot_names:
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
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
    if not _FLOWSPEC_FLOW_IDENTIFIER.fullmatch(selected_flow_identifier):
        diagnostics.append(
            _diagnostic(
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
            _diagnostic(
                "error",
                "RASA_FLOW_DESCRIPTION_REQUIRED",
                f"$.flows.{selected_flow_identifier}.description",
                "the selected flow requires a non-empty description",
            )
        )
        description = "Invalid imported Rasa flow"
    steps = _sequence(flow_definition.get("steps"))
    if steps is None or not steps:
        diagnostics.append(
            _diagnostic(
                "error",
                "RASA_FLOW_STEPS_REQUIRED",
                f"$.flows.{selected_flow_identifier}.steps",
                "the selected flow requires a non-empty steps list",
            )
        )
        steps = ()
    _step_identifier_diagnostics(
        steps,
        f"$.flows.{selected_flow_identifier}.steps",
        diagnostics,
    )

    referenced_slot_names = {
        slot_name
        for step_value in steps
        if (step := _mapping(step_value)) is not None
        and isinstance((slot_name := step.get("collect")), str)
        and slot_name
    }
    if referenced_slot_names:
        diagnostics.append(
            _diagnostic(
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
    slot_declarations = _mapping(domain_document.get("slots"))
    if slot_declarations is None:
        if referenced_slot_names:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "RASA_DOMAIN_SLOTS_REQUIRED",
                    "$.slots",
                    "domain.yml must contain a slots mapping for every collected slot",
                )
            )
        slot_declarations = {}
    responses = _mapping(domain_document.get("responses"))
    if responses is None:
        if referenced_slot_names:
            diagnostics.append(
                _diagnostic(
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
        slot_declaration = _mapping(slot_declarations.get(slot_name))
        if slot_declaration is None:
            diagnostics.append(
                _diagnostic(
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
    actions = _sequence(actions_value)
    if actions is None or not all(isinstance(action_name, str) for action_name in actions):
        diagnostics.append(
            _diagnostic(
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
        step = _mapping(step_value)
        source_path = f"$.flows.{selected_flow_identifier}.steps[{step_index}]"
        if step is None:
            diagnostics.append(
                _diagnostic(
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
                _diagnostic(
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
    generated_validation_diagnostics = _flowspec_validation_diagnostics(
        imported_flow,
        "RASA_GENERATED_FLOWSPEC_INVALID",
    )
    diagnostics.extend(generated_validation_diagnostics)
    if not generated_validation_diagnostics and (
        compilation_diagnostic := _flowspec_compilation_diagnostic(
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


__all__ = [
    "RASA_FORMAT",
    "RASA_MINIMUM_VERSION",
    "RASA_PROFILE_VERSION",
    "RasaBundle",
    "export_rasa",
    "import_rasa",
]
