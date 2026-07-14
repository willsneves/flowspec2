"""Aggregate semantic linking for structurally valid FlowSpec2 documents."""

from __future__ import annotations

from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from .diagnostics import DiagnosticLocation, FlowDiagnostic
from .ir import normalize_flow
from .profiles import FlowProfile, capability_is_requested
from .semantic_source_contracts import (
    derive_contracts,
    domain_contracts,
    entry_schema_contracts,
    path_contracts,
    predicate_contracts,
    rail_reference_contracts,
    slot_contracts,
    state_writer_contracts,
)
from .semantic_support import json_pointer, mutable_json, semantic_diagnostic


class FlowLinkError(ValueError):
    """Raised when a structurally valid flow fails semantic or profile linking."""

    def __init__(self, diagnostics: tuple[FlowDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        rendered_diagnostics = "; ".join(
            f"{diagnostic.code} at {diagnostic.path or '/'}: {diagnostic.message}"
            for diagnostic in diagnostics
        )
        super().__init__(f"FlowSpec2 linking failed: {rendered_diagnostics}")


def profile_contracts(
    document: dict[str, Any],
    profile: FlowProfile,
) -> tuple[list[FlowDiagnostic], frozenset[str] | None, dict[str, int]]:
    diagnostics: list[FlowDiagnostic] = []
    exposed_slots: set[str] = set()
    exposed_slot_positions: dict[str, int] = {}
    exposed_slot_owners: dict[str, tuple[str, int]] = {}
    state_key_owners: dict[str, tuple[str, int, bool]] = {}
    has_open_manifest = False
    required_capabilities: set[tuple[str, str]] = set()
    use_anchor_positions = {
        cast(str, path_step["use"]): path_index
        for path_index, path_step in enumerate(document["path"])
        if "use" in path_step
    }
    authored_state_writers: dict[str, list[tuple[str, str]]] = {}

    def add_authored_state_writer(state_key: str, path: str, writer_kind: str) -> None:
        authored_state_writers.setdefault(state_key, []).append((path, writer_kind))

    for slot_name in document.get("slots", {}):
        add_authored_state_writer(slot_name, json_pointer("slots", slot_name), "slot")
    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        add_authored_state_writer(
            cast(str, derive_definition["writes"]),
            json_pointer("derive", derive_index, "writes"),
            "derive",
        )
    if (entry_definition := document.get("entry")) is not None and entry_definition.get("writes"):
        add_authored_state_writer(
            cast(str, entry_definition["writes"]),
            "/entry/writes",
            "entry",
        )
    if (terminal_definition := document.get("terminal")) is not None:
        for output_key in terminal_definition.get("outputs", {}):
            add_authored_state_writer(
                output_key,
                json_pointer("terminal", "outputs", output_key),
                "terminal",
            )
        success_set = cast(
            dict[str, Any],
            cast(dict[str, Any], terminal_definition.get("outcomes", {}))
            .get("success", {})
            .get("set", {}),
        )
        for state_key in success_set:
            add_authored_state_writer(
                state_key,
                json_pointer("terminal", "outcomes", "success", "set", state_key),
                "terminal",
            )

    for domain_name, domain_definition in document.get("domains", {}).items():
        domain_type = domain_definition["type"]
        if domain_type not in profile.domain_types:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_DOMAIN_UNSUPPORTED",
                    json_pointer("domains", domain_name, "type"),
                    f"profile {profile.identifier!r} does not implement domain type {domain_type!r}",
                )
            )

    for use_index, use_definition in enumerate(document.get("uses", [])):
        reference = use_definition["ref"]
        if not profile.subflows.has(reference):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_SUBFLOW_UNAVAILABLE",
                    json_pointer("uses", use_index, "ref"),
                    f"profile {profile.identifier!r} does not register subflow {reference!r}",
                )
            )
            has_open_manifest = True
            continue
        definition = profile.subflows.definition(reference)
        if definition.legacy_manifest and not profile.allow_legacy_contracts:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_LEGACY_SUBFLOW_MANIFEST_FORBIDDEN",
                    json_pointer("uses", use_index, "ref"),
                    f"profile {profile.identifier!r} requires a typed manifest for "
                    f"subflow {reference!r}",
                )
            )
        if definition.exposed_slots is None:
            has_open_manifest = True
        else:
            exposed_slots.update(definition.exposed_slots)
            for exposed_slot in definition.exposed_slots:
                if (first_owner := exposed_slot_owners.get(exposed_slot)) is not None:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_PROFILE_SUBFLOW_SLOT_COLLISION",
                            json_pointer("uses", use_index, "ref"),
                            f"subflow slot {exposed_slot!r} is also owned by {first_owner[0]!r}",
                            related=(
                                DiagnosticLocation(
                                    path=json_pointer("uses", first_owner[1], "ref"),
                                    message="First owning subflow declaration.",
                                ),
                            ),
                        )
                    )
                else:
                    exposed_slot_owners[exposed_slot] = (reference, use_index)
                if reference in use_anchor_positions:
                    exposed_slot_positions[exposed_slot] = use_anchor_positions[reference]
            for colliding_slot in sorted(set(document.get("slots", {})) & definition.exposed_slots):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_SLOT_COLLISION",
                        json_pointer("slots", colliding_slot),
                        f"slot {colliding_slot!r} is owned by subflow {reference!r}",
                        related=(
                            DiagnosticLocation(
                                path=json_pointer("uses", use_index, "ref"),
                                message="Owning subflow declaration.",
                            ),
                        ),
                        suggested_fix="Remove the duplicate top-level slot declaration.",
                    )
                )
        for state_key in sorted(definition.owned_state_keys):
            state_key_is_exposed = (
                definition.exposed_slots is not None and state_key in definition.exposed_slots
            )
            if (first_state_owner := state_key_owners.get(state_key)) is not None:
                if not (state_key_is_exposed and first_state_owner[2]):
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_PROFILE_SUBFLOW_STATE_COLLISION",
                            json_pointer("uses", use_index, "ref"),
                            f"subflow state key {state_key!r} is also owned by "
                            f"{first_state_owner[0]!r}",
                            related=(
                                DiagnosticLocation(
                                    path=json_pointer("uses", first_state_owner[1], "ref"),
                                    message="First owning subflow declaration.",
                                ),
                            ),
                        )
                    )
            else:
                state_key_owners[state_key] = (
                    reference,
                    use_index,
                    state_key_is_exposed,
                )
            for writer_path, writer_kind in authored_state_writers.get(state_key, []):
                if writer_kind == "slot" and state_key_is_exposed:
                    continue
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_STATE_COLLISION",
                        writer_path,
                        f"{writer_kind} writes state key {state_key!r}, which is owned by "
                        f"subflow {reference!r}",
                        related=(
                            DiagnosticLocation(
                                path=json_pointer("uses", use_index, "ref"),
                                message="Owning subflow declaration.",
                            ),
                        ),
                        suggested_fix="Remove the duplicate writer or use a distinct state key.",
                    )
                )
        required_capabilities.update(
            (capability, json_pointer("uses", use_index, "ref"))
            for capability in definition.capabilities
        )
        for tool_name, required_version in definition.required_tools.items():
            tool_path = json_pointer("uses", use_index, "ref")
            if not profile.tools.has(tool_name):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_TOOL_UNAVAILABLE",
                        tool_path,
                        f"subflow {reference!r} requires tool {tool_name!r} at version "
                        f"{required_version!r}, but the profile does not register it",
                    )
                )
                continue
            actual_version = profile.tools.definition(tool_name).version
            if (
                profile.tools.definition(tool_name).legacy_contract
                and not profile.allow_legacy_contracts
            ):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_PROFILE_LEGACY_TOOL_CONTRACT_FORBIDDEN",
                        tool_path,
                        f"profile {profile.identifier!r} requires a typed contract for "
                        f"tool {tool_name!r}",
                    )
                )
            if actual_version != required_version:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_TOOL_VERSION_MISMATCH",
                        tool_path,
                        f"subflow {reference!r} requires tool {tool_name!r} at version "
                        f"{required_version!r}, but the profile provides {actual_version!r}",
                    )
                )
        configuration_schema = mutable_json(definition.configuration_schema)
        configuration_validator = Draft202012Validator(
            configuration_schema,
            format_checker=FormatChecker(),
        )
        for configuration_error in sorted(
            configuration_validator.iter_errors(use_definition.get("with", {})),
            key=lambda error: tuple(str(segment) for segment in error.absolute_path),
        ):
            error_path = json_pointer(
                "uses",
                use_index,
                "with",
                *tuple(configuration_error.absolute_path),
            )
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_SUBFLOW_CONFIGURATION_INVALID",
                    error_path,
                    f"configuration for {definition.ref!r}: {configuration_error.message}",
                )
            )

    tool_references: list[tuple[str, str]] = []
    if (entry := document.get("entry")) is not None:
        tool_references.append((entry["tool"], "/entry/tool"))
    if (terminal := document.get("terminal")) is not None:
        tool_references.append((terminal["tool"], "/terminal/tool"))
    await_external = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external"),
    )
    if await_external is not None:
        enrichment = cast(
            str | dict[str, Any] | None,
            cast(dict[str, Any], await_external.get("on_resume", {})).get("enrich"),
        )
        if not profile.allow_legacy_contracts and (
            not isinstance(await_external.get("resume"), dict) or isinstance(enrichment, str)
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_LEGACY_AWAIT_CONTRACT_FORBIDDEN",
                    "/capabilities/await_external",
                    f"profile {profile.identifier!r} requires a versioned typed resume "
                    "contract and object-form enrichment",
                )
            )
        if isinstance(enrichment, str):
            tool_references.append((enrichment, "/capabilities/await_external/on_resume/enrich"))
        elif isinstance(enrichment, dict):
            tool_references.append(
                (
                    enrichment["tool"],
                    "/capabilities/await_external/on_resume/enrich/tool",
                )
            )
    for tool_name, tool_path in tool_references:
        if not profile.tools.has(tool_name):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_TOOL_UNAVAILABLE",
                    tool_path,
                    f"tool {tool_name!r} is not registered in profile {profile.identifier!r}",
                )
            )
        elif (
            profile.tools.definition(tool_name).legacy_contract
            and not profile.allow_legacy_contracts
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_LEGACY_TOOL_CONTRACT_FORBIDDEN",
                    tool_path,
                    f"profile {profile.identifier!r} requires a typed contract for "
                    f"tool {tool_name!r}",
                )
            )

    for capability_name, capability_value in document.get("capabilities", {}).items():
        if capability_is_requested(capability_value):
            required_capabilities.add(
                (capability_name, json_pointer("capabilities", capability_name))
            )
    if "auto_flow" in document:
        required_capabilities.add(("auto_flow", "/auto_flow"))
    for capability_name, capability_path in sorted(required_capabilities):
        if capability_name not in profile.capabilities:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_PROFILE_CAPABILITY_UNAVAILABLE",
                    capability_path,
                    f"profile {profile.identifier!r} does not provide capability {capability_name!r}",
                )
            )

    return (
        diagnostics,
        None if has_open_manifest else frozenset(exposed_slots),
        exposed_slot_positions,
    )


def semantic_diagnostics(
    flow_document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None = None,
    external_steps: frozenset[str] = frozenset(),
    profile: FlowProfile | None = None,
) -> tuple[FlowDiagnostic, ...]:
    """Link all source contracts and return every deterministic finding.

    ``external_slots=None`` defers profile-owned references to compilation.  A
    profile-aware caller passes the complete exposed-slot set to make unknown
    references an aggregate semantic error instead.
    """

    document = normalize_flow(flow_document)
    profile_diagnostics: list[FlowDiagnostic] = []
    profile_slot_positions: dict[str, int] = {}
    if profile is not None:
        profile_diagnostics, profile_slots, profile_slot_positions = profile_contracts(
            document, profile
        )
        if profile_slots is not None:
            external_slots = profile_slots
    diagnostics, identifiers, slot_positions = path_contracts(document)
    slot_positions = {**profile_slot_positions, **slot_positions}
    diagnostics.extend(profile_diagnostics)
    diagnostics.extend(domain_contracts(document))
    diagnostics.extend(slot_contracts(document, slot_positions, external_slots=external_slots))
    diagnostics.extend(
        derive_contracts(
            document,
            identifiers,
            external_slots=external_slots,
            external_steps=external_steps,
        )
    )
    diagnostics.extend(entry_schema_contracts(document, external_slots=external_slots))
    diagnostics.extend(state_writer_contracts(document, external_slots=external_slots))
    diagnostics.extend(
        predicate_contracts(
            document,
            external_slots=external_slots,
            profile=profile,
        )
    )
    diagnostics.extend(
        rail_reference_contracts(
            document,
            identifiers,
            slot_positions,
            external_slots=external_slots,
        )
    )
    return tuple(
        sorted(
            set(diagnostics),
            key=lambda diagnostic: (
                diagnostic.path,
                diagnostic.code,
                diagnostic.message,
            ),
        )
    )
