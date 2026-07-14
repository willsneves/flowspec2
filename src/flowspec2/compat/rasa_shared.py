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
    DiagnosticSeverity,
)

RASA_PROFILE_VERSION = "1"
RASA_FORMAT = f"rasa-calm/{RASA_PROFILE_VERSION}"
RASA_MINIMUM_VERSION = "3.11.0"
FLOWSPEC_FORMAT = "flowspec/2"
RASA_DOMAIN_VERSION = "3.1"

FLOWSPEC_FLOW_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
RASA_ACTION_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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


def compatibility_diagnostic(
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


def flowspec_validation_diagnostics(
    flow_document: Mapping[str, Any],
    code: str,
) -> tuple[CompatibilityDiagnostic, ...]:
    validator = Draft202012Validator(flowspec_schema())
    return tuple(
        compatibility_diagnostic(
            "error",
            code,
            _validation_path(validation_error),
            validation_error.message,
        )
        for validation_error in validator.iter_errors(dict(flow_document))
    )


def flowspec_compilation_diagnostic(
    flow_document: Mapping[str, Any],
    code: str,
) -> CompatibilityDiagnostic | None:
    try:
        compile_flow(
            dict(flow_document),
            tools=_compatibility_tool_registry(flow_document),
        )
    except (KeyError, StopIteration, TypeError, ValueError) as compilation_error:
        return compatibility_diagnostic(
            "error",
            code,
            "$",
            "flowspec2 compiler rejected the document: "
            f"{str(compilation_error) or compilation_error.__class__.__name__}",
        )
    return None


def mapping_or_none(value: object) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return cast(Mapping[str, Any], value)


def sequence_or_none(value: object) -> Sequence[Any] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return None
    return value


def step_identifier_diagnostics(
    steps: Sequence[Any],
    source_path: str,
    diagnostics: list[CompatibilityDiagnostic],
) -> None:
    first_indexes_by_identifier: dict[str, int] = {}
    for step_index, step_value in enumerate(steps):
        step = mapping_or_none(step_value)
        if step is None or "id" not in step:
            continue
        step_identifier = step.get("id")
        if not isinstance(step_identifier, str) or not step_identifier:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_STEP_ID_INVALID",
                    f"{source_path}[{step_index}].id",
                    "a Rasa step id must be a non-empty string",
                )
            )
            continue
        if step_identifier in first_indexes_by_identifier:
            diagnostics.append(
                compatibility_diagnostic(
                    "error",
                    "RASA_DUPLICATE_STEP_ID",
                    f"{source_path}[{step_index}].id",
                    f"step id {step_identifier!r} was already used at index "
                    f"{first_indexes_by_identifier[step_identifier]}",
                )
            )
        else:
            first_indexes_by_identifier[step_identifier] = step_index


def rasa_response_key(flow_identifier: str, slot_name: str, step_index: int) -> str:
    normalized_slot_name = re.sub(r"[^A-Za-z0-9_]", "_", slot_name)
    return f"utter_ask_{flow_identifier}_{normalized_slot_name}_{step_index + 1}"


def rasa_set_slots_name_supported(slot_name: str) -> bool:
    return bool(slot_name) and not any(
        character in slot_name
        for character in (_RASA_SET_SLOTS_NAME_FORBIDDEN | _RASA_RESPONSE_TEMPLATE_CHARACTERS)
    )


def rasa_set_slots_value_supported(slot_value: str) -> bool:
    return bool(slot_value) and not any(
        character in slot_value
        for character in (_RASA_SET_SLOTS_VALUE_FORBIDDEN | _RASA_RESPONSE_TEMPLATE_CHARACTERS)
    )


def rasa_response_text_supported(response_text: str) -> bool:
    return not any(character in response_text for character in _RASA_RESPONSE_TEMPLATE_CHARACTERS)


def set_slots_payload(slot_name: str, slot_value: str) -> str:
    return f"/SetSlots({slot_name}={slot_value})"


def domain_button_options(
    domain_specification: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(domain_option.value).lower(), domain_option.title)
        for domain_option in interactive_options_for_domain(domain_specification)
    )
