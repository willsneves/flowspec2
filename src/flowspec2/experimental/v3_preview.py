"""Experimental flowspec/3-draft source preview and v2 migration helpers.

The preview is deliberately isolated from runtime compilation. Its only purpose
is to make the proposed authoring surface executable as a schema and measurable
against existing flowspec/2 documents before any stable format decision.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, cast

import jsonschema

from ..diagnostics import DiagnosticLocation, FlowDiagnostic
from ..domains import categorical_number_bindings, parse_affirmation
from ..schema import validate_flow
from ..semantics import semantic_diagnostics

V2_SCHEMA_IDENTIFIER = "flowspec/2"
V3_PREVIEW_SCHEMA_IDENTIFIER = "flowspec/3-draft"
V3_PREVIEW_LOSS_POLICY_CONTRACT = "flowspec2/v3-preview-loss-policy@1"

_SCHEMA_PATH = Path(__file__).with_name("flowspec-3-draft.schema.json")
_LOSS_POLICY_PATH = Path(__file__).with_name("v3-preview-loss-policy.json")
_PREDICATE_REFERENCE_NAMESPACES = {
    "internal": "$internal",
    "payload": "$payload",
    "config": "$config",
    "address": "$address",
}
_SLOT_CONTRACT_KEYS = (
    "persist",
    "required",
    "nullable",
    "prefill_sources",
    "max_attempts",
    "on_exhaust",
    "default",
    "fill_only_when_asked",
)
_TOP_LEVEL_NATIVE_KEYS = {
    "schema",
    "flow",
    "version",
    "service",
    "route",
    "config",
    "domains",
    "slots",
    "path",
    "uses",
    "derive",
    "confirm",
    "terminal",
    "overrides",
    "entry",
    "auto_flow",
    "capabilities",
}

_schema_cache: dict[str, Any] | None = None
_loss_policy_cache: dict[V3PreviewLossCategory, _V3PreviewLossRule] | None = None


class V3PreviewDocumentMode(StrEnum):
    """Validation boundary for authored sources and v2 migration artifacts."""

    AUTHORING = "authoring"
    V2_MIGRATION = "v2_migration"


class V3PreviewLossCategory(StrEnum):
    """Closed reasons why the v2 migrator cannot emit native preview syntax."""

    AGENT_CAPABILITY = "agent_capability"
    AUTOMATIC_FLOW_CONTRACT = "automatic_flow_contract"
    AWAIT_RESUME_CONTRACT = "await_resume_contract"
    AWAIT_TIMEOUT_DURATION = "await_timeout_duration"
    DUPLICATE_DOMAIN_ROW = "duplicate_domain_row"
    DUPLICATE_TERMINAL_INPUT_PARAMETER = "duplicate_terminal_input_parameter"
    ENTRY_CONTRACT = "entry_contract"
    IMPLICIT_AWAIT_CAPABILITY = "implicit_await_capability"
    INVALID_BOOLEAN_DOMAIN_FIELD = "invalid_boolean_domain_field"
    INVALID_NON_CHOICE_DOMAIN_FIELD = "invalid_non_choice_domain_field"
    INVALID_NON_CHOICE_SYNONYMS = "invalid_non_choice_synonyms"
    INVALID_SYNONYM_TARGET = "invalid_synonym_target"
    LEGACY_AWAIT_ENRICHMENT = "legacy_await_enrichment"
    LEGACY_INTERACTIVE_GATE = "legacy_interactive_gate"
    MISMATCHED_INTERACTIVE_DOMAIN = "mismatched_interactive_domain"
    ORPHAN_DOMAIN_ROW = "orphan_domain_row"
    SHADOWED_AWAIT_PRESENTATION = "shadowed_await_presentation"
    UNKNOWN_DERIVE_ANCHOR = "unknown_derive_anchor"
    UNKNOWN_TOP_LEVEL_FRAGMENT = "unknown_top_level_fragment"
    UNUSED_CONFIRMATION_CONTRACT = "unused_confirmation_contract"
    UNUSED_DOMAIN_DECLARATION = "unused_domain_declaration"
    UNUSED_OVERRIDE_GATE = "unused_override_gate"
    UNUSED_SLOT_DECLARATION = "unused_slot_declaration"
    UNUSED_SUBFLOW_DECLARATION = "unused_subflow_declaration"
    UNUSED_TERMINAL_CONTRACT = "unused_terminal_contract"


class V3PreviewLossDisposition(StrEnum):
    """Normative status assigned to a non-native source fragment."""

    COMPATIBILITY_ONLY = "compatibility_only"
    EXCLUDED = "excluded"


class V3PreviewLossHandling(StrEnum):
    """Required lowerer behavior for a loss-accounting category."""

    REHYDRATE = "rehydrate"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class _V3PreviewLossRule:
    disposition: V3PreviewLossDisposition
    handling: V3PreviewLossHandling
    description: str
    source_path_pattern: str


class V3PreviewMigrationError(ValueError):
    """A source construct cannot be represented as a valid preview document."""


class V3PreviewValidationError(ValueError):
    """A structurally valid preview violates one or more semantic contracts."""

    def __init__(self, diagnostics: tuple[FlowDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        summary = "; ".join(
            f"{diagnostic.code} at {diagnostic.path or '/'}: {diagnostic.message}"
            for diagnostic in diagnostics
        )
        super().__init__(summary)


@dataclass(frozen=True, slots=True)
class CompactByteComparison:
    """UTF-8 byte sizes of canonical compact JSON for a migration pair."""

    source_bytes: int
    preview_bytes: int

    @property
    def delta_bytes(self) -> int:
        """Return preview bytes minus source bytes without interpreting the sign."""
        return self.preview_bytes - self.source_bytes

    def as_dict(self) -> dict[str, int]:
        """Return a serialization-friendly measurement report."""
        return {
            "source_bytes": self.source_bytes,
            "preview_bytes": self.preview_bytes,
            "delta_bytes": self.delta_bytes,
        }


@dataclass(frozen=True, slots=True)
class V3PreviewLossEntry:
    """Immutable classification of one non-native v2 source fragment."""

    source_path: str
    category: V3PreviewLossCategory
    disposition: V3PreviewLossDisposition
    _source_fragment_json: str = field(repr=False)

    @property
    def source_fragment(self) -> Any:
        """Return a fresh JSON value so callers cannot mutate the entry."""
        return json.loads(self._source_fragment_json)

    @property
    def handling(self) -> V3PreviewLossHandling:
        """Return the policy-mandated lowerer behavior."""
        return _loss_policy_rules()[self.category].handling

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical document representation of this entry."""
        return {
            "source_path": self.source_path,
            "category": self.category.value,
            "disposition": self.disposition.value,
            "source_fragment": self.source_fragment,
        }


@dataclass(frozen=True, slots=True)
class V3PreviewMigrationReport:
    """Immutable migration artifact with explicit non-native source locations."""

    _preview_json: str = field(repr=False)
    loss_entries: tuple[V3PreviewLossEntry, ...]
    compact_bytes: CompactByteComparison

    @property
    def preview_document(self) -> dict[str, Any]:
        """Return a fresh preview document so callers cannot mutate the report."""
        return cast(dict[str, Any], json.loads(self._preview_json))


@dataclass(slots=True)
class _MigrationContext:
    source_document: dict[str, Any]
    domains: dict[str, dict[str, Any]]
    slots: dict[str, dict[str, Any]]
    use_declarations: list[dict[str, Any]]
    derive_definitions: list[dict[str, Any]]
    confirmation_definition: dict[str, Any] | None
    terminal_definition: dict[str, Any] | None
    override_gates: dict[str, dict[str, Any]]
    await_capability: dict[str, Any] | None
    used_domain_names: set[str] = field(default_factory=set)
    placed_slot_names: set[str] = field(default_factory=set)
    consumed_use_indexes: set[int] = field(default_factory=set)
    consumed_derive_indexes: set[int] = field(default_factory=set)
    consumed_override_gate_names: set[str] = field(default_factory=set)
    confirmation_consumed: bool = False
    terminal_consumed: bool = False
    await_capability_consumed: bool = False
    loss_entries_by_path: dict[str, V3PreviewLossEntry] = field(default_factory=dict)


def preview_loss_policy() -> dict[str, Any]:
    """Return a private copy of the authoritative preview loss policy."""
    policy_document = cast(
        dict[str, Any], json.loads(_LOSS_POLICY_PATH.read_text(encoding="utf-8"))
    )
    _validate_loss_policy_document(policy_document)
    return cast(dict[str, Any], _canonical_clone(policy_document))


def _loss_policy_rules() -> dict[V3PreviewLossCategory, _V3PreviewLossRule]:
    global _loss_policy_cache
    cached_rules = _loss_policy_cache
    if cached_rules is not None:
        return cached_rules
    policy_document = preview_loss_policy()
    category_documents = cast(dict[str, dict[str, str]], policy_document["categories"])
    cached_rules = {
        V3PreviewLossCategory(category_name): _V3PreviewLossRule(
            disposition=V3PreviewLossDisposition(category_document["disposition"]),
            handling=V3PreviewLossHandling(category_document["handling"]),
            description=category_document["description"],
            source_path_pattern=category_document["source_path_pattern"],
        )
        for category_name, category_document in category_documents.items()
    }
    _loss_policy_cache = cached_rules
    return cached_rules


def _validate_loss_policy_document(policy_document: dict[str, Any]) -> None:
    if set(policy_document) != {"_sections", "contract", "categories"}:
        raise RuntimeError("preview loss policy has an invalid top-level shape")
    section_index = policy_document.get("_sections")
    if not isinstance(section_index, dict) or set(section_index) != {"loss-policy"}:
        raise RuntimeError("preview loss policy has an invalid section index")
    if policy_document.get("contract") != V3_PREVIEW_LOSS_POLICY_CONTRACT:
        raise RuntimeError("preview loss policy has an unsupported contract")
    category_documents = policy_document.get("categories")
    if not isinstance(category_documents, dict):
        raise RuntimeError("preview loss policy categories must be an object")
    expected_categories = {category.value for category in V3PreviewLossCategory}
    if set(category_documents) != expected_categories:
        raise RuntimeError("preview loss policy categories do not match the closed category enum")
    for category_name, category_document in category_documents.items():
        if not isinstance(category_document, dict) or set(category_document) != {
            "description",
            "disposition",
            "handling",
            "source_path_pattern",
        }:
            raise RuntimeError(f"preview loss policy category {category_name!r} is malformed")
        try:
            disposition = V3PreviewLossDisposition(category_document["disposition"])
            handling = V3PreviewLossHandling(category_document["handling"])
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"preview loss policy category {category_name!r} has an invalid decision"
            ) from error
        expected_handling = (
            V3PreviewLossHandling.REHYDRATE
            if disposition is V3PreviewLossDisposition.COMPATIBILITY_ONLY
            else V3PreviewLossHandling.REJECT
        )
        if handling is not expected_handling:
            raise RuntimeError(
                f"preview loss policy category {category_name!r} has an inconsistent decision"
            )
        if (
            not isinstance(category_document["description"], str)
            or not category_document["description"].strip()
        ):
            raise RuntimeError(
                f"preview loss policy category {category_name!r} needs a description"
            )
        source_path_pattern = category_document["source_path_pattern"]
        if not isinstance(source_path_pattern, str):
            raise RuntimeError(
                f"preview loss policy category {category_name!r} needs a source path pattern"
            )
        try:
            re.compile(source_path_pattern)
        except re.error as error:
            raise RuntimeError(
                f"preview loss policy category {category_name!r} has an invalid source path pattern"
            ) from error


def preview_schema() -> dict[str, Any]:
    """Return a private copy of the closed experimental Draft 2020-12 schema."""
    global _schema_cache
    cached_schema = _schema_cache
    if cached_schema is None:
        cached_schema = cast(dict[str, Any], json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))
        jsonschema.Draft202012Validator.check_schema(cached_schema)
        _schema_cache = cached_schema
    return cast(dict[str, Any], _canonical_clone(cached_schema))


def validate_v3_preview(
    preview_document: Mapping[str, Any],
    *,
    mode: V3PreviewDocumentMode = V3PreviewDocumentMode.AUTHORING,
) -> None:
    """Raise when a document violates structural or semantic preview contracts."""
    mode = V3PreviewDocumentMode(mode)
    jsonschema.Draft202012Validator(preview_schema()).validate(preview_document)
    if diagnostics := _semantic_v3_diagnostics(cast(dict[str, Any], preview_document), mode):
        raise V3PreviewValidationError(diagnostics)


def check_v3_preview(
    preview_document: object,
    *,
    mode: V3PreviewDocumentMode = V3PreviewDocumentMode.AUTHORING,
) -> tuple[FlowDiagnostic, ...]:
    """Return all deterministic structural or semantic preview diagnostics."""
    mode = V3PreviewDocumentMode(mode)
    validator = jsonschema.Draft202012Validator(preview_schema())
    structural_findings = tuple(
        sorted(
            (
                _schema_diagnostic(validation_error)
                for validation_error in validator.iter_errors(cast(Any, preview_document))
            ),
            key=lambda diagnostic: (diagnostic.path, diagnostic.code, diagnostic.message),
        )
    )
    if structural_findings:
        return structural_findings
    return _semantic_v3_diagnostics(cast(dict[str, Any], preview_document), mode)


def _schema_diagnostic(validation_error: jsonschema.ValidationError) -> FlowDiagnostic:
    validator_name = re.sub(
        r"[^A-Za-z0-9]+", "_", str(validation_error.validator or "violation")
    ).strip("_")
    return FlowDiagnostic(
        code=f"FLOWSPEC3_SCHEMA_{validator_name.upper() or 'VIOLATION'}",
        severity="error",
        path=_validation_error_pointer(validation_error),
        message=validation_error.message,
        related_locations=_validation_context_locations(validation_error),
    )


def _validation_error_pointer(validation_error: jsonschema.ValidationError) -> str:
    path_segments = list(validation_error.absolute_path)
    if missing_property := _required_property(validation_error):
        path_segments.append(missing_property)
    return _pointer_from_segments(path_segments)


def _required_property(validation_error: jsonschema.ValidationError) -> str | None:
    if validation_error.validator != "required":
        return None
    required_properties = validation_error.validator_value
    instance = validation_error.instance
    if not isinstance(required_properties, list) or not isinstance(instance, Mapping):
        return None
    missing_properties = [
        property_name
        for property_name in required_properties
        if isinstance(property_name, str) and property_name not in instance
    ]
    return missing_properties[0] if len(missing_properties) == 1 else None


def _validation_context_locations(
    validation_error: jsonschema.ValidationError,
) -> tuple[DiagnosticLocation, ...]:
    locations = {
        DiagnosticLocation(
            path=_validation_error_pointer(context_error),
            message=context_error.message,
        )
        for context_error in validation_error.context
    }
    return tuple(sorted(locations, key=lambda location: (location.path, location.message or "")))


def _pointer_from_segments(path_segments: Iterable[str | int]) -> str:
    encoded_segments = (
        str(path_segment).replace("~", "~0").replace("/", "~1") for path_segment in path_segments
    )
    return "".join(f"/{path_segment}" for path_segment in encoded_segments)


def _v3_diagnostic(code: str, path: str, message: str) -> FlowDiagnostic:
    return FlowDiagnostic(code=code, severity="error", path=path, message=message)


def _loss_accounting_diagnostics(
    preview_document: dict[str, Any], mode: V3PreviewDocumentMode
) -> list[FlowDiagnostic]:
    passthrough = cast(dict[str, Any] | None, preview_document.get("v2_passthrough"))
    if passthrough is None:
        return []
    if mode == V3PreviewDocumentMode.AUTHORING:
        return [
            _v3_diagnostic(
                "FLOWSPEC3_AUTHORED_LOSS_ACCOUNTING",
                "/v2_passthrough",
                "v2 loss accounting is allowed only on v2 migration artifacts",
            )
        ]

    diagnostics: list[FlowDiagnostic] = []
    loss_entries = cast(list[dict[str, Any]], passthrough["entries"])
    source_paths = [cast(str, loss_entry["source_path"]) for loss_entry in loss_entries]
    if source_paths != sorted(source_paths):
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_UNORDERED_LOSS_ENTRIES",
                "/v2_passthrough/entries",
                "loss entries must be ordered by source_path",
            )
        )
    first_indexes_by_path: dict[str, int] = {}
    policy_rules = _loss_policy_rules()
    for loss_index, loss_entry in enumerate(loss_entries):
        loss_path = f"/v2_passthrough/entries/{loss_index}"
        source_path = cast(str, loss_entry["source_path"])
        if (first_index := first_indexes_by_path.get(source_path)) is not None:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_DUPLICATE_LOSS_SOURCE_PATH",
                    f"{loss_path}/source_path",
                    f"source path {source_path!r} is already accounted at "
                    f"/v2_passthrough/entries/{first_index}",
                )
            )
        else:
            first_indexes_by_path[source_path] = loss_index
        try:
            category = V3PreviewLossCategory(cast(str, loss_entry["category"]))
        except ValueError:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_UNKNOWN_LOSS_CATEGORY",
                    f"{loss_path}/category",
                    f"unknown loss category {loss_entry['category']!r}",
                )
            )
            continue
        expected_disposition = policy_rules[category].disposition
        if loss_entry["disposition"] != expected_disposition.value:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_LOSS_DISPOSITION_MISMATCH",
                    f"{loss_path}/disposition",
                    f"category {category.value!r} requires disposition "
                    f"{expected_disposition.value!r}",
                )
            )
        if re.fullmatch(policy_rules[category].source_path_pattern, source_path) is None:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_LOSS_SOURCE_PATH_MISMATCH",
                    f"{loss_path}/source_path",
                    f"source path {source_path!r} is not allowed for category {category.value!r}",
                )
            )
    return diagnostics


def _semantic_v3_diagnostics(
    preview_document: dict[str, Any], mode: V3PreviewDocumentMode
) -> tuple[FlowDiagnostic, ...]:
    diagnostics = _loss_accounting_diagnostics(preview_document, mode)
    steps = cast(list[dict[str, Any]], preview_document["steps"])
    identifiers: dict[str, tuple[int, str]] = {}
    slot_positions: dict[str, int] = {}
    derive_positions: dict[str, int] = {}
    use_positions: dict[str, int] = {}
    has_profile_owned_slots = any("use" in preview_step for preview_step in steps)

    for step_index, preview_step in enumerate(steps):
        step_path = _json_pointer("steps", step_index)
        if subflow_reference := preview_step.get("use"):
            subflow_reference = cast(str, subflow_reference)
            previous_use_position = use_positions.get(subflow_reference)
            if previous_use_position is not None:
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_DUPLICATE_SUBFLOW_USE",
                        f"{step_path}/use",
                        f"subflow {subflow_reference!r} is already used at "
                        f"/steps/{previous_use_position}",
                    )
                )
            else:
                use_positions[subflow_reference] = step_index
        step_identifier = _preview_step_identifier(preview_step)
        if step_identifier is not None:
            identifier_path = f"{step_path}/id" if "id" in preview_step else step_path
            if previous_identifier := identifiers.get(step_identifier):
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_DUPLICATE_STEP_ID",
                        identifier_path,
                        f"step identifier {step_identifier!r} is already declared at "
                        f"{previous_identifier[1]}",
                    )
                )
            else:
                identifiers[step_identifier] = (step_index, identifier_path)

        for slot_kind in ("collect", "confirm"):
            if slot_name := preview_step.get(slot_kind):
                slot_name = cast(str, slot_name)
                previous_position = slot_positions.get(slot_name)
                if previous_position is not None:
                    diagnostics.append(
                        _v3_diagnostic(
                            "FLOWSPEC3_DUPLICATE_SLOT_WRITER",
                            f"{step_path}/{slot_kind}",
                            f"slot {slot_name!r} is already written by /steps/{previous_position}",
                        )
                    )
                else:
                    slot_positions[slot_name] = step_index

        if derive_name := preview_step.get("derive"):
            derive_name = cast(str, derive_name)
            if derive_name in derive_positions:
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_DUPLICATE_DERIVE_WRITER",
                        f"{step_path}/derive",
                        f"derive target {derive_name!r} is already written",
                    )
                )
            elif derive_name in slot_positions:
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_STATE_WRITER_COLLISION",
                        f"{step_path}/derive",
                        f"derive target {derive_name!r} collides with a slot writer",
                    )
                )
            else:
                derive_positions[derive_name] = step_index

    for slot_name, slot_position in slot_positions.items():
        if slot_name in derive_positions:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_STATE_WRITER_COLLISION",
                    f"/steps/{slot_position}",
                    f"slot {slot_name!r} collides with a derive target",
                )
            )

    _check_state_writer_collisions(preview_document, steps, diagnostics)
    _check_step_cardinality(steps, diagnostics)
    for step_index, preview_step in enumerate(steps):
        _check_step_semantics(
            preview_step,
            step_index,
            identifiers,
            slot_positions,
            derive_positions,
            has_profile_owned_slots,
            diagnostics,
        )

    return tuple(
        sorted(
            set(diagnostics),
            key=lambda diagnostic: (diagnostic.path, diagnostic.code, diagnostic.message),
        )
    )


def _check_state_writer_collisions(
    preview_document: dict[str, Any],
    steps: list[dict[str, Any]],
    diagnostics: list[FlowDiagnostic],
) -> None:
    first_writer_by_key: dict[str, str] = {}

    if "service" in preview_document:
        _register_state_writer(
            "service",
            "/service",
            first_writer_by_key,
            diagnostics,
        )
    for step_index, preview_step in enumerate(steps):
        step_path = _json_pointer("steps", step_index)
        for writer_kind in ("collect", "confirm", "derive"):
            if state_key := preview_step.get(writer_kind):
                _register_state_writer(
                    cast(str, state_key),
                    f"{step_path}/{writer_kind}",
                    first_writer_by_key,
                    diagnostics,
                )
        if "await" in preview_step:
            on_resume = cast(dict[str, Any], preview_step.get("on_resume", {}))
            for state_key in cast(dict[str, Any], on_resume.get("set", {})):
                _register_state_writer(
                    state_key,
                    f"{step_path}/on_resume/set/{_pointer_segment(state_key)}",
                    first_writer_by_key,
                    diagnostics,
                )
            enrichment = cast(dict[str, Any], on_resume.get("enrich", {}))
            for state_key in cast(dict[str, Any], enrichment.get("set", {})):
                _register_state_writer(
                    state_key,
                    f"{step_path}/on_resume/enrich/set/{_pointer_segment(state_key)}",
                    first_writer_by_key,
                    diagnostics,
                )
        if "submit" in preview_step:
            for state_key in cast(dict[str, Any], preview_step.get("outputs", {})):
                _register_state_writer(
                    state_key,
                    f"{step_path}/outputs/{_pointer_segment(state_key)}",
                    first_writer_by_key,
                    diagnostics,
                )
            success_set = cast(dict[str, Any], preview_step["outcomes"]["success"].get("set", {}))
            for state_key in success_set:
                _register_state_writer(
                    state_key,
                    f"{step_path}/outcomes/success/set/{_pointer_segment(state_key)}",
                    first_writer_by_key,
                    diagnostics,
                )


def _register_state_writer(
    state_key: str,
    writer_path: str,
    first_writer_by_key: dict[str, str],
    diagnostics: list[FlowDiagnostic],
) -> None:
    previous_writer = first_writer_by_key.get(state_key)
    if previous_writer is None:
        first_writer_by_key[state_key] = writer_path
        return
    diagnostics.append(
        _v3_diagnostic(
            "FLOWSPEC3_STATE_WRITER_COLLISION",
            writer_path,
            f"state key {state_key!r} is already written at {previous_writer}",
        )
    )


def _check_step_cardinality(steps: list[dict[str, Any]], diagnostics: list[FlowDiagnostic]) -> None:
    cardinality_contracts = (
        ("submit", "FLOWSPEC3_MULTIPLE_SUBMIT_STEPS"),
        ("await", "FLOWSPEC3_MULTIPLE_AWAIT_STEPS"),
        ("correction_hub", "FLOWSPEC3_MULTIPLE_CORRECTION_HUBS"),
    )
    for step_key, diagnostic_code in cardinality_contracts:
        matching_indexes = [
            step_index for step_index, preview_step in enumerate(steps) if step_key in preview_step
        ]
        for duplicate_index in matching_indexes[1:]:
            diagnostics.append(
                _v3_diagnostic(
                    diagnostic_code,
                    _json_pointer("steps", duplicate_index, step_key),
                    f"{step_key!r} is a singular flow-level construct",
                )
            )


def _check_step_semantics(
    preview_step: dict[str, Any],
    step_index: int,
    identifiers: dict[str, tuple[int, str]],
    slot_positions: dict[str, int],
    derive_positions: dict[str, int],
    has_profile_owned_slots: bool,
    diagnostics: list[FlowDiagnostic],
) -> None:
    step_path = _json_pointer("steps", step_index)
    domain = cast(dict[str, Any] | None, preview_step.get("domain"))
    if domain is not None:
        _check_domain_semantics(domain, preview_step, step_path, diagnostics)

    for required_index, required_reference in enumerate(preview_step.get("requires", [])):
        _check_source_reference(
            cast(dict[str, str], required_reference),
            f"{step_path}/requires/{required_index}",
            step_index,
            slot_positions,
            derive_positions,
            has_profile_owned_slots,
            diagnostics,
            slots_only=True,
        )

    if "derive" in preview_step:
        for source_index, source_reference in enumerate(preview_step["from"]):
            _check_source_reference(
                cast(dict[str, str], source_reference),
                f"{step_path}/from/{source_index}",
                step_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
            )
        if isinstance(preview_step.get("default"), dict):
            default_reference = cast(dict[str, str], preview_step["default"])
            if set(default_reference) & {"$slot", "$derive"}:
                _check_source_reference(
                    default_reference,
                    f"{step_path}/default",
                    step_index,
                    slot_positions,
                    derive_positions,
                    has_profile_owned_slots,
                    diagnostics,
                )

    if "submit" in preview_step:
        for parameter_name, source_reference in preview_step.get("input", {}).items():
            _check_source_reference(
                cast(dict[str, str], source_reference),
                f"{step_path}/input/{_pointer_segment(parameter_name)}",
                step_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
            )
        output_keys = set(cast(dict[str, Any], preview_step.get("outputs", {})))
        success_keys = set(cast(dict[str, Any], preview_step["outcomes"]["success"].get("set", {})))
        for colliding_key in sorted(output_keys & success_keys):
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_SUBMIT_WRITE_COLLISION",
                    f"{step_path}/outcomes/success/set/{_pointer_segment(colliding_key)}",
                    f"state key {colliding_key!r} is also written by submit outputs",
                )
            )

    if "confirm" in preview_step:
        _check_confirmation_semantics(
            preview_step,
            step_index,
            identifiers,
            slot_positions,
            derive_positions,
            has_profile_owned_slots,
            diagnostics,
        )

    if "await" in preview_step:
        _check_await_semantics(preview_step, step_index, identifiers, diagnostics)

    ui = cast(dict[str, Any] | None, preview_step.get("ui"))
    if ui is not None:
        _check_ui_semantics(
            ui,
            domain,
            step_index,
            slot_positions,
            derive_positions,
            has_profile_owned_slots,
            diagnostics,
        )

    for predicate_key in ("ask_when", "skip_when", "gate"):
        if predicate := preview_step.get(predicate_key):
            _check_predicate_references(
                cast(dict[str, Any], predicate),
                f"{step_path}/{predicate_key}",
                step_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
            )


def _check_domain_semantics(
    domain: dict[str, Any],
    preview_step: dict[str, Any],
    step_path: str,
    diagnostics: list[FlowDiagnostic],
) -> None:
    domain_type = cast(str, domain["type"])
    minimum = domain.get("minimum")
    maximum = domain.get("maximum")
    if minimum is not None and maximum is not None and minimum > maximum:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_INVALID_NUMERIC_RANGE",
                f"{step_path}/domain/minimum",
                "minimum must not exceed maximum",
            )
        )

    if domain_type in {"categorical", "bool"}:
        _check_domain_options(domain, step_path, diagnostics)

    if "default" in preview_step and not _domain_accepts_value(
        domain, preview_step["default"], nullable=preview_step.get("nullable") is True
    ):
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_INVALID_DEFAULT",
                f"{step_path}/default",
                "default is outside the inline domain or nullability contract",
            )
        )


def _check_domain_options(
    domain: dict[str, Any], step_path: str, diagnostics: list[FlowDiagnostic]
) -> None:
    option_owner_by_identity: dict[str, int] = {}
    normalized_owner: dict[str, tuple[int | None, str]] = {}
    accent_fold = cast(dict[str, Any], domain.get("normalize", {})).get("accent_fold") is True
    for option_index, option in enumerate(cast(list[dict[str, Any]], domain["options"])):
        option_value = option["value"]
        option_identity = _json_identity(option_value)
        option_path = f"{step_path}/domain/options/{option_index}"
        if not cast(str, option["label"]).strip():
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_EMPTY_OPTION_LABEL",
                    f"{option_path}/label",
                    "option label must contain non-whitespace text",
                )
            )
        if option_identity in option_owner_by_identity:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_DUPLICATE_OPTION_VALUE",
                    f"{option_path}/value",
                    f"option value duplicates /domain/options/{option_owner_by_identity[option_identity]}",
                )
            )
        else:
            option_owner_by_identity[option_identity] = option_index

        candidate_inputs = [str(option_value), *cast(list[str], option.get("aliases", []))]
        for candidate_index, candidate_input in enumerate(candidate_inputs):
            candidate_path = (
                f"{option_path}/value"
                if candidate_index == 0
                else f"{option_path}/aliases/{candidate_index - 1}"
            )
            _register_normalized_input(
                candidate_input,
                option_index,
                candidate_path,
                accent_fold,
                normalized_owner,
                diagnostics,
            )

    for null_alias_index, null_alias in enumerate(domain.get("null_aliases", [])):
        _register_normalized_input(
            cast(str, null_alias),
            None,
            f"{step_path}/domain/null_aliases/{null_alias_index}",
            accent_fold,
            normalized_owner,
            diagnostics,
        )

    normalization = cast(dict[str, Any], domain.get("normalize", {}))
    if domain["type"] == "categorical" and normalization.get("number_words") is True:
        option_values = [
            option["value"] for option in cast(list[dict[str, Any]], domain["options"])
        ]
        for number_input, target_value in categorical_number_bindings(option_values).items():
            option_index = next(
                index
                for index, option_value in enumerate(option_values)
                if _same_json_scalar(option_value, target_value)
            )
            _register_normalized_input(
                number_input,
                option_index,
                f"{step_path}/domain/normalize/number_words",
                accent_fold,
                normalized_owner,
                diagnostics,
            )

    if domain["type"] == "bool" and normalization.get("affirmation") is True:
        for option_index, option in enumerate(cast(list[dict[str, Any]], domain["options"])):
            for alias_index, option_alias in enumerate(option.get("aliases", [])):
                parsed_alias = parse_affirmation(
                    option_alias,
                    emoji_veto=normalization.get("emoji_veto") is True,
                )
                if parsed_alias is not None and parsed_alias is not option["value"]:
                    diagnostics.append(
                        _v3_diagnostic(
                            "FLOWSPEC3_BOOLEAN_ALIAS_CONFLICT",
                            f"{step_path}/domain/options/{option_index}/aliases/{alias_index}",
                            "alias conflicts with the enabled affirmation normalization",
                        )
                    )


def _register_normalized_input(
    candidate_input: str,
    option_index: int | None,
    candidate_path: str,
    accent_fold: bool,
    normalized_owner: dict[str, tuple[int | None, str]],
    diagnostics: list[FlowDiagnostic],
) -> None:
    normalized_input = _normalize_domain_input(candidate_input, accent_fold=accent_fold)
    if not normalized_input:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_EMPTY_OPTION_INPUT",
                candidate_path,
                "option values and aliases must contain non-whitespace input",
            )
        )
        return
    if previous_owner := normalized_owner.get(normalized_input):
        if previous_owner[0] == option_index:
            if not previous_owner[1].endswith("/value"):
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_DUPLICATE_OPTION_INPUT",
                        candidate_path,
                        f"normalized input duplicates {previous_owner[1]}",
                    )
                )
            return
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_AMBIGUOUS_OPTION_INPUT",
                candidate_path,
                f"normalized input collides with {previous_owner[1]}",
            )
        )
        return
    normalized_owner[normalized_input] = (option_index, candidate_path)


def _normalize_domain_input(candidate_input: str, *, accent_fold: bool) -> str:
    normalized_input = " ".join(candidate_input.split()).casefold()
    if accent_fold:
        normalized_input = "".join(
            character
            for character in unicodedata.normalize("NFKD", normalized_input)
            if not unicodedata.combining(character)
        )
    return normalized_input


def _domain_accepts_value(domain: dict[str, Any], candidate: Any, *, nullable: bool) -> bool:
    if candidate is None:
        return nullable
    domain_type = domain["type"]
    if domain_type == "categorical":
        return any(
            _same_json_scalar(candidate, option["value"])
            for option in cast(list[dict[str, Any]], domain["options"])
        )
    if domain_type == "bool":
        return isinstance(candidate, bool)
    if domain_type == "integer":
        valid_type = isinstance(candidate, int) and not isinstance(candidate, bool)
    elif domain_type == "number":
        valid_type = isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
    else:
        valid_type = isinstance(candidate, str)
    if not valid_type:
        return False
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        minimum = domain.get("minimum")
        maximum = domain.get("maximum")
        return (minimum is None or candidate >= minimum) and (
            maximum is None or candidate <= maximum
        )
    return True


def _check_source_reference(
    source_reference: dict[str, str],
    reference_path: str,
    consumer_index: int,
    slot_positions: dict[str, int],
    derive_positions: dict[str, int],
    has_profile_owned_slots: bool,
    diagnostics: list[FlowDiagnostic],
    *,
    slots_only: bool = False,
) -> None:
    reference_kind, source_name = next(iter(source_reference.items()))
    if reference_kind == "$slot":
        producer_index = slot_positions.get(source_name)
        if producer_index is None:
            if not has_profile_owned_slots:
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_UNKNOWN_SLOT_REFERENCE",
                        reference_path,
                        f"slot {source_name!r} has no local or profile-owned producer",
                    )
                )
            return
    elif reference_kind == "$derive" and not slots_only:
        producer_index = derive_positions.get(source_name)
        if producer_index is None:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_UNKNOWN_DERIVE_REFERENCE",
                    reference_path,
                    f"derive target {source_name!r} is not declared",
                )
            )
            return
    else:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_INVALID_SOURCE_REFERENCE",
                reference_path,
                "this contract requires a slot reference",
            )
        )
        return
    if producer_index >= consumer_index:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_SOURCE_NOT_AVAILABLE",
                reference_path,
                f"source {source_name!r} must be produced by an earlier step",
            )
        )


def _check_confirmation_semantics(
    preview_step: dict[str, Any],
    step_index: int,
    identifiers: dict[str, tuple[int, str]],
    slot_positions: dict[str, int],
    derive_positions: dict[str, int],
    has_profile_owned_slots: bool,
    diagnostics: list[FlowDiagnostic],
) -> None:
    step_path = _json_pointer("steps", step_index)
    if target_reference := preview_step.get("on_confirm"):
        _check_step_reference(
            cast(dict[str, str], target_reference),
            f"{step_path}/on_confirm",
            identifiers,
            diagnostics,
            after_index=step_index,
        )
    for correctable_index, correctable_reference in enumerate(preview_step.get("correctable", [])):
        _check_source_reference(
            cast(dict[str, str], correctable_reference),
            f"{step_path}/correctable/{correctable_index}",
            step_index,
            slot_positions,
            derive_positions,
            has_profile_owned_slots,
            diagnostics,
            slots_only=True,
        )


def _check_await_semantics(
    preview_step: dict[str, Any],
    step_index: int,
    identifiers: dict[str, tuple[int, str]],
    diagnostics: list[FlowDiagnostic],
) -> None:
    step_path = _json_pointer("steps", step_index)
    if ui := preview_step.get("ui"):
        await_ui = cast(dict[str, Any], ui)
        if await_ui["field"] != preview_step["resume_on"]:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_AWAIT_FIELD_MISMATCH",
                    f"{step_path}/ui/field",
                    "CTA field must equal the await resume_on payload key",
                )
            )
        if await_ui["next_step"]["$step"] != preview_step["id"]:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_AWAIT_NEXT_STEP_MISMATCH",
                    f"{step_path}/ui/next_step",
                    "CTA next_step must target its enclosing await step",
                )
            )

    transition_locations: list[tuple[str, dict[str, Any]]] = []
    if timeout_transition := preview_step.get("timeout"):
        transition_locations.append((f"{step_path}/timeout", timeout_transition))
    transition_locations.extend(
        (f"{step_path}/recovery/{event_name}", transition)
        for event_name, transition in preview_step.get("recovery", {}).items()
    )
    for transition_path, transition in transition_locations:
        if isinstance(transition["goto"], dict):
            _check_step_reference(
                cast(dict[str, str], transition["goto"]),
                f"{transition_path}/goto",
                identifiers,
                diagnostics,
            )
    resend_transition = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], preview_step.get("recovery", {})).get("resend"),
    )
    if resend_transition is not None and resend_transition["goto"] != {"$step": preview_step["id"]}:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_AWAIT_RESEND_TARGET_MISMATCH",
                f"{step_path}/recovery/resend/goto",
                "resend must target its enclosing await step",
            )
        )

    on_resume = cast(dict[str, Any], preview_step.get("on_resume", {}))
    direct_writes = set(cast(dict[str, Any], on_resume.get("set", {})))
    enrichment = cast(dict[str, Any], on_resume.get("enrich", {}))
    enrichment_writes = set(cast(dict[str, Any], enrichment.get("set", {})))
    for colliding_key in sorted(direct_writes & enrichment_writes):
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_AWAIT_WRITE_COLLISION",
                f"{step_path}/on_resume/enrich/set/{_pointer_segment(colliding_key)}",
                f"state key {colliding_key!r} is also written by on_resume.set",
            )
        )


def _check_ui_semantics(
    ui: dict[str, Any],
    domain: dict[str, Any] | None,
    step_index: int,
    slot_positions: dict[str, int],
    derive_positions: dict[str, int],
    has_profile_owned_slots: bool,
    diagnostics: list[FlowDiagnostic],
) -> None:
    step_path = _json_pointer("steps", step_index)
    if ui["kind"] in {"buttons", "list"} and (
        domain is None or domain["type"] not in {"categorical", "bool"}
    ):
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_CHOICE_UI_REQUIRES_OPTIONS",
                f"{step_path}/ui/kind",
                "buttons and list UI require a categorical or boolean inline domain",
            )
        )
    if (
        ui["kind"] in {"buttons", "list"}
        and domain is not None
        and domain["type"] in {"categorical", "bool"}
    ):
        allowed_values = [option["value"] for option in domain.get("options", [])]
        if not allowed_values:
            diagnostics.append(
                _v3_diagnostic(
                    "FLOWSPEC3_CHOICE_UI_REQUIRES_OPTIONS",
                    f"{step_path}/ui/kind",
                    "buttons and list UI require renderable domain options",
                )
            )
        seen_values: list[Any] = []
        for condition_index, conditional_option in enumerate(ui.get("options_when", [])):
            condition_value = conditional_option["value"]
            condition_path = f"{step_path}/ui/options_when/{condition_index}/value"
            if any(_same_json_scalar(condition_value, seen_value) for seen_value in seen_values):
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_DUPLICATE_CONDITIONAL_OPTION",
                        condition_path,
                        "options_when repeats an option value",
                    )
                )
            seen_values.append(condition_value)
            if not any(
                _same_json_scalar(condition_value, allowed_value)
                for allowed_value in allowed_values
            ):
                diagnostics.append(
                    _v3_diagnostic(
                        "FLOWSPEC3_UNKNOWN_CONDITIONAL_OPTION",
                        condition_path,
                        "options_when value is not rendered by the inline domain",
                    )
                )
            _check_predicate_references(
                cast(dict[str, Any], conditional_option["gate"]),
                f"{step_path}/ui/options_when/{condition_index}/gate",
                step_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
            )
    if ui["kind"] == "flow":
        for prefill_index, prefill_reference in enumerate(ui.get("prefill_from", [])):
            _check_source_reference(
                cast(dict[str, str], prefill_reference),
                f"{step_path}/ui/prefill_from/{prefill_index}",
                step_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
                slots_only=True,
            )


def _check_predicate_references(
    predicate: dict[str, Any],
    predicate_path: str,
    consumer_index: int,
    slot_positions: dict[str, int],
    derive_positions: dict[str, int],
    has_profile_owned_slots: bool,
    diagnostics: list[FlowDiagnostic],
) -> None:
    operator, operand = next(iter(predicate.items()))
    if operator in {"and", "or"}:
        for child_index, child_predicate in enumerate(operand):
            _check_predicate_references(
                cast(dict[str, Any], child_predicate),
                f"{predicate_path}/{operator}/{child_index}",
                consumer_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
            )
        return
    if operator == "not":
        _check_predicate_references(
            cast(dict[str, Any], operand),
            f"{predicate_path}/not",
            consumer_index,
            slot_positions,
            derive_positions,
            has_profile_owned_slots,
            diagnostics,
        )
        return
    operands = [operand] if operator == "is_present" else cast(list[Any], operand)
    for operand_index, candidate_operand in enumerate(operands):
        if isinstance(candidate_operand, dict) and set(candidate_operand) & {"$slot", "$derive"}:
            _check_source_reference(
                cast(dict[str, str], candidate_operand),
                f"{predicate_path}/{operator}/{operand_index}",
                consumer_index,
                slot_positions,
                derive_positions,
                has_profile_owned_slots,
                diagnostics,
            )


def _check_step_reference(
    step_reference: dict[str, str],
    reference_path: str,
    identifiers: dict[str, tuple[int, str]],
    diagnostics: list[FlowDiagnostic],
    *,
    after_index: int | None = None,
) -> None:
    target_identifier = step_reference["$step"]
    target = identifiers.get(target_identifier)
    if target is None:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_UNKNOWN_STEP_REFERENCE",
                reference_path,
                f"step {target_identifier!r} is not declared",
            )
        )
    elif after_index is not None and target[0] <= after_index:
        diagnostics.append(
            _v3_diagnostic(
                "FLOWSPEC3_NON_FORWARD_CONFIRM_TARGET",
                reference_path,
                "confirmation target must be a later step",
            )
        )


def _pointer_segment(path_segment: str) -> str:
    return path_segment.replace("~", "~0").replace("/", "~1")


def compact_json_bytes(flow_document: Mapping[str, Any]) -> int:
    """Measure canonical compact JSON in UTF-8 bytes."""
    return len(_canonical_json(flow_document).encode("utf-8"))


def migrate_v2_to_v3_preview(source_document: Mapping[str, Any]) -> dict[str, Any]:
    """Purely migrate a valid flowspec/2 source into the non-stable preview."""
    return migrate_v2_to_v3_preview_report(source_document).preview_document


def migrate_v2_to_v3_preview_report(
    source_document: Mapping[str, Any],
) -> V3PreviewMigrationReport:
    """Migrate and report classified source losses plus neutral byte measurements."""
    private_source = _canonical_clone(source_document)
    if not isinstance(private_source, dict):
        raise V3PreviewMigrationError("flowspec/2 source must be a JSON object")
    if private_source.get("schema") != V2_SCHEMA_IDENTIFIER:
        raise V3PreviewMigrationError(
            f"source schema must be {V2_SCHEMA_IDENTIFIER!r}, got {private_source.get('schema')!r}"
        )
    _reject_ephemeral_slot_persistence(private_source)
    validation_source = _without_legacy_interactive_gates(private_source)
    validate_flow(validation_source)
    if source_diagnostics := semantic_diagnostics(validation_source):
        first_diagnostic = source_diagnostics[0]
        raise V3PreviewMigrationError(
            "flowspec/2 source is not semantically linked: "
            f"{first_diagnostic.code} at {first_diagnostic.path or '/'}: "
            f"{first_diagnostic.message}"
        )

    migration_context = _new_migration_context(private_source)
    preview_document = _migrate_document(migration_context)
    validate_v3_preview(preview_document, mode=V3PreviewDocumentMode.V2_MIGRATION)
    canonical_preview = _canonical_json(preview_document)
    compact_bytes = CompactByteComparison(
        source_bytes=compact_json_bytes(private_source),
        preview_bytes=len(canonical_preview.encode("utf-8")),
    )
    return V3PreviewMigrationReport(
        _preview_json=canonical_preview,
        loss_entries=tuple(
            sorted(
                migration_context.loss_entries_by_path.values(),
                key=lambda loss_entry: loss_entry.source_path,
            )
        ),
        compact_bytes=compact_bytes,
    )


def _reject_ephemeral_slot_persistence(source_document: dict[str, Any]) -> None:
    slots = source_document.get("slots")
    if not isinstance(slots, dict):
        return
    for slot_name, slot_definition in slots.items():
        if isinstance(slot_definition, dict) and slot_definition.get("persist") == "payload":
            raise V3PreviewMigrationError(
                f"{_json_pointer('slots', slot_name, 'persist')} uses ephemeral payload "
                "persistence, which cannot survive a conversational pause"
            )


def _without_legacy_interactive_gates(
    source_document: dict[str, Any],
) -> dict[str, Any]:
    cleaned_document = cast(dict[str, Any], _canonical_clone(source_document))
    interactive_definitions: list[Any] = [
        path_step.get("interactive")
        for path_step in cast(list[dict[str, Any]], cleaned_document.get("path", []))
    ]
    interactive_definitions.extend(
        [
            cast(dict[str, Any], cleaned_document.get("confirm", {})).get("interactive"),
            cast(dict[str, Any], cleaned_document.get("auto_flow", {})).get("interactive"),
            cast(
                dict[str, Any],
                cast(dict[str, Any], cleaned_document.get("capabilities", {})).get(
                    "await_external", {}
                ),
            ).get("interactive"),
        ]
    )
    for interactive_definition in interactive_definitions:
        if isinstance(interactive_definition, dict):
            interactive_definition.pop("gate", None)
    return cleaned_document


def _new_migration_context(source_document: dict[str, Any]) -> _MigrationContext:
    capabilities = cast(dict[str, Any], source_document.get("capabilities") or {})
    overrides = cast(dict[str, Any], source_document.get("overrides") or {})
    return _MigrationContext(
        source_document=source_document,
        domains=cast(dict[str, dict[str, Any]], source_document.get("domains") or {}),
        slots=cast(dict[str, dict[str, Any]], source_document.get("slots") or {}),
        use_declarations=cast(list[dict[str, Any]], source_document.get("uses") or []),
        derive_definitions=cast(list[dict[str, Any]], source_document.get("derive") or []),
        confirmation_definition=cast(dict[str, Any] | None, source_document.get("confirm")),
        terminal_definition=cast(dict[str, Any] | None, source_document.get("terminal")),
        override_gates=cast(dict[str, dict[str, Any]], overrides.get("gates") or {}),
        await_capability=cast(dict[str, Any] | None, capabilities.get("await_external")),
    )


def _migrate_document(migration_context: _MigrationContext) -> dict[str, Any]:
    source_document = migration_context.source_document
    preview_document: dict[str, Any] = {
        "schema": V3_PREVIEW_SCHEMA_IDENTIFIER,
        "flow": source_document["flow"],
        "version": source_document["version"],
        "route": _canonical_clone(source_document["route"]),
    }
    for native_top_level_key in ("service", "config"):
        if native_top_level_key in source_document:
            preview_document[native_top_level_key] = _canonical_clone(
                source_document[native_top_level_key]
            )

    preview_steps = [
        _migrate_path_step(path_step, path_index, migration_context)
        for path_index, path_step in enumerate(cast(list[dict[str, Any]], source_document["path"]))
    ]
    _insert_unanchored_derives(preview_steps, migration_context)
    preview_document["steps"] = preview_steps
    _collect_loss_entries(migration_context)
    if migration_context.loss_entries_by_path:
        ordered_loss_entries = sorted(
            migration_context.loss_entries_by_path.values(),
            key=lambda loss_entry: loss_entry.source_path,
        )
        preview_document["v2_passthrough"] = {
            "contract": V3_PREVIEW_LOSS_POLICY_CONTRACT,
            "entries": [loss_entry.as_dict() for loss_entry in ordered_loss_entries],
        }
    return cast(dict[str, Any], _canonical_clone(preview_document))


def _migrate_path_step(
    path_step: dict[str, Any],
    path_index: int,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    if "slot" in path_step:
        return _migrate_collect_step(path_step, path_index, migration_context)
    if "confirm" in path_step:
        return _migrate_confirm_step(path_step, path_index, migration_context)
    if "use" in path_step:
        return _migrate_use_step(path_step, migration_context)
    if "derive" in path_step:
        return _migrate_explicit_derive_step(path_step, path_index, migration_context)
    if "terminal" in path_step:
        return _migrate_submit_step(path_index, migration_context)
    if "await_external" in path_step:
        return _migrate_await_step(path_step, path_index, migration_context)
    raise V3PreviewMigrationError(f"/path/{path_index} has no supported step kind")


def _migrate_collect_step(
    path_step: dict[str, Any],
    path_index: int,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    slot_name = cast(str, path_step["slot"])
    preview_step = _migrate_slot_step(
        kind="collect",
        slot_name=slot_name,
        path_step=path_step,
        path_index=path_index,
        migration_context=migration_context,
    )
    _migrate_path_presentation(path_step, path_index, preview_step, slot_name, migration_context)
    _apply_step_gate(path_step, slot_name, preview_step, migration_context)
    return preview_step


def _migrate_confirm_step(
    path_step: dict[str, Any],
    path_index: int,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    slot_name = cast(str, path_step["confirm"])
    preview_step = _migrate_slot_step(
        kind="confirm",
        slot_name=slot_name,
        path_step=path_step,
        path_index=path_index,
        migration_context=migration_context,
    )
    _migrate_path_presentation(path_step, path_index, preview_step, slot_name, migration_context)
    _apply_step_gate(path_step, slot_name, preview_step, migration_context)
    if "on_reject" in path_step:
        preview_step["on_reject"] = _canonical_clone(path_step["on_reject"])
    if "correctable" in path_step:
        preview_step["correction_hub"] = path_step["correctable"]
    if path_step.get("correctable") is True:
        _merge_confirmation_definition(path_step, path_index, preview_step, migration_context)
    return preview_step


def _migrate_slot_step(
    *,
    kind: str,
    slot_name: str,
    path_step: dict[str, Any],
    path_index: int,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    slot_definition = migration_context.slots.get(slot_name)
    if slot_definition is None:
        raise V3PreviewMigrationError(
            f"/path/{path_index}/{kind} references undeclared slot {slot_name!r}"
        )
    domain_name = cast(str, slot_definition["domain"])
    preview_step: dict[str, Any] = {
        kind: slot_name,
        "domain": _migrate_domain(domain_name, migration_context),
    }
    migration_context.placed_slot_names.add(slot_name)
    if "step" in path_step:
        preview_step["id"] = path_step["step"]
    for slot_contract_key in _SLOT_CONTRACT_KEYS:
        if slot_contract_key in slot_definition:
            preview_step[slot_contract_key] = _canonical_clone(slot_definition[slot_contract_key])
    if "requires" in slot_definition:
        preview_step["requires"] = [
            _slot_reference(required_slot_name)
            for required_slot_name in cast(list[str], slot_definition["requires"])
        ]
    return preview_step


def _migrate_domain(domain_name: str, migration_context: _MigrationContext) -> dict[str, Any]:
    domain_definition = migration_context.domains.get(domain_name)
    if domain_definition is None:
        raise V3PreviewMigrationError(f"slot references undeclared domain {domain_name!r}")
    migration_context.used_domain_names.add(domain_name)
    domain_type = cast(str, domain_definition.get("type", "categorical"))
    preview_domain: dict[str, Any] = {"type": domain_type}
    normalization = cast(dict[str, Any], domain_definition.get("normalize") or {})
    native_normalization = {
        normalization_key: normalization[normalization_key]
        for normalization_key in (
            "accent_fold",
            "number_words",
            "affirmation",
            "emoji_veto",
        )
        if normalization_key in normalization
    }
    if native_normalization:
        preview_domain["normalize"] = native_normalization
    if "optional" in domain_definition:
        preview_domain["optional"] = domain_definition["optional"]
    for numeric_constraint in ("minimum", "maximum"):
        if numeric_constraint in domain_definition:
            preview_domain[numeric_constraint] = domain_definition[numeric_constraint]
    if domain_type in {"categorical", "bool"}:
        migrated_options = _migrate_options(
            domain_name, domain_definition, normalization, migration_context
        )
        if domain_type == "categorical":
            if normalization.get("number_words") is True and any(
                option["value"] is None for option in migrated_options
            ):
                _materialize_nullable_number_aliases(migrated_options)
                cast(dict[str, Any], preview_domain.get("normalize", {})).pop("number_words", None)
                if not preview_domain.get("normalize"):
                    preview_domain.pop("normalize", None)
            null_option = next(
                (option for option in migrated_options if option["value"] is None), None
            )
            preview_domain["options"] = [
                option for option in migrated_options if option["value"] is not None
            ]
            if null_option is not None:
                preview_domain["accepts_null"] = True
                if null_aliases := null_option.get("aliases"):
                    preview_domain["null_aliases"] = null_aliases
        else:
            preview_domain["options"] = migrated_options
    if domain_type == "bool":
        for ignored_domain_key in ("values", "rows"):
            if ignored_domain_key in domain_definition:
                _record_loss(
                    migration_context,
                    V3PreviewLossCategory.INVALID_BOOLEAN_DOMAIN_FIELD,
                    _json_pointer("domains", domain_name, ignored_domain_key),
                    domain_definition[ignored_domain_key],
                )
    if domain_type not in {"categorical", "bool"}:
        for ignored_domain_key in ("values", "rows"):
            if ignored_domain_key in domain_definition:
                _record_loss(
                    migration_context,
                    V3PreviewLossCategory.INVALID_NON_CHOICE_DOMAIN_FIELD,
                    _json_pointer("domains", domain_name, ignored_domain_key),
                    domain_definition[ignored_domain_key],
                )
        if "synonyms" in normalization:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.INVALID_NON_CHOICE_SYNONYMS,
                _json_pointer("domains", domain_name, "normalize", "synonyms"),
                normalization["synonyms"],
            )
    return preview_domain


def _materialize_nullable_number_aliases(migrated_options: list[dict[str, Any]]) -> None:
    option_values = [option["value"] for option in migrated_options]
    for number_input, target_value in categorical_number_bindings(option_values).items():
        target_option = next(
            option
            for option in migrated_options
            if _same_json_scalar(option["value"], target_value)
        )
        target_option["aliases"] = sorted(
            {*cast(list[str], target_option.get("aliases", [])), number_input}
        )


def _migrate_options(
    domain_name: str,
    domain_definition: dict[str, Any],
    normalization: dict[str, Any],
    migration_context: _MigrationContext,
) -> list[dict[str, Any]]:
    domain_type = cast(str, domain_definition.get("type", "categorical"))
    option_values: list[Any] = (
        [True, False]
        if domain_type == "bool"
        else cast(list[Any], domain_definition.get("values") or [])
    )
    aliases_by_option = _aliases_by_option(
        domain_name, option_values, normalization, migration_context
    )
    domain_rows = cast(list[dict[str, Any]], domain_definition.get("rows") or [])
    preview_options: list[dict[str, Any]] = []
    for option_value in option_values:
        option_label = (
            "Yes"
            if option_value is True
            else "No"
            if option_value is False
            else ""
            if option_value is None
            else str(option_value)
        )
        preview_option: dict[str, Any] = {"value": option_value, "label": option_label}
        option_aliases = aliases_by_option.get(_json_identity(option_value), [])
        if option_aliases:
            preview_option["aliases"] = sorted(option_aliases)
        matching_rows = [
            (row_index, domain_row)
            for row_index, domain_row in enumerate(domain_rows)
            if _same_json_scalar(domain_row["value"], option_value)
        ]
        if matching_rows:
            selected_domain_row = matching_rows[-1][1]
            if "description" in selected_domain_row:
                preview_option["description"] = selected_domain_row["description"]
            for overridden_row_index, domain_row in matching_rows[:-1]:
                _record_loss(
                    migration_context,
                    V3PreviewLossCategory.DUPLICATE_DOMAIN_ROW,
                    _json_pointer("domains", domain_name, "rows", overridden_row_index),
                    domain_row,
                )
        preview_options.append(preview_option)

    for row_index, domain_row in enumerate(domain_rows):
        if not any(
            _same_json_scalar(domain_row["value"], option_value) for option_value in option_values
        ):
            _record_loss(
                migration_context,
                V3PreviewLossCategory.ORPHAN_DOMAIN_ROW,
                _json_pointer("domains", domain_name, "rows", row_index),
                domain_row,
            )
    return preview_options


def _aliases_by_option(
    domain_name: str,
    option_values: list[Any],
    normalization: dict[str, Any],
    migration_context: _MigrationContext,
) -> dict[str, list[str]]:
    aliases_by_option: dict[str, list[str]] = {}
    synonyms = cast(dict[str, Any], normalization.get("synonyms") or {})
    for option_alias, synonym_target in synonyms.items():
        matched_option = next(
            (
                option_value
                for option_value in option_values
                if _same_json_scalar(option_value, synonym_target)
            ),
            None,
        )
        matched_null = synonym_target is None and any(
            option_value is None for option_value in option_values
        )
        if matched_option is None and not matched_null:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.INVALID_SYNONYM_TARGET,
                _json_pointer("domains", domain_name, "normalize", "synonyms", option_alias),
                synonym_target,
            )
            continue
        aliases_by_option.setdefault(_json_identity(matched_option), []).append(option_alias)
    return aliases_by_option


def _migrate_path_presentation(
    path_step: dict[str, Any],
    path_index: int,
    preview_step: dict[str, Any],
    slot_name: str,
    migration_context: _MigrationContext,
) -> None:
    if "prompt" in path_step:
        preview_step["prompt"] = _canonical_clone(path_step["prompt"])
    if "interactive" in path_step:
        slot_definition = migration_context.slots[slot_name]
        preview_step["ui"] = _migrate_ui(
            cast(dict[str, Any], path_step["interactive"]),
            _json_pointer("path", path_index, "interactive"),
            cast(str, slot_definition["domain"]),
            migration_context,
        )
    for predicate_key in ("ask_when", "skip_when"):
        if predicate_key in path_step:
            preview_step[predicate_key] = _migrate_predicate(
                cast(dict[str, Any], path_step[predicate_key]), migration_context
            )


def _apply_step_gate(
    path_step: dict[str, Any],
    default_step_identifier: str,
    preview_step: dict[str, Any],
    migration_context: _MigrationContext,
) -> None:
    step_identifier = cast(str, path_step.get("step", default_step_identifier))
    gate_predicate = migration_context.override_gates.get(step_identifier)
    if gate_predicate is not None:
        preview_step["gate"] = _migrate_predicate(gate_predicate, migration_context)
        migration_context.consumed_override_gate_names.add(step_identifier)


def _merge_confirmation_definition(
    path_step: dict[str, Any],
    path_index: int,
    preview_step: dict[str, Any],
    migration_context: _MigrationContext,
) -> None:
    confirmation_definition = migration_context.confirmation_definition
    if confirmation_definition is None:
        raise V3PreviewMigrationError(
            f"/path/{path_index}/correctable requires the top-level /confirm definition"
        )
    path_step_identifier = cast(str, path_step.get("step", path_step["confirm"]))
    if confirmation_definition["step"] != path_step_identifier:
        raise V3PreviewMigrationError(
            f"/confirm/step does not match /path/{path_index}: "
            f"{confirmation_definition['step']!r} != {path_step_identifier!r}"
        )
    if confirmation_definition["slot"] != path_step["confirm"]:
        raise V3PreviewMigrationError(f"/confirm/slot does not match /path/{path_index}/confirm")
    for presentation_key, preview_key in (("prompt", "prompt"), ("interactive", "ui")):
        confirmation_presentation = confirmation_definition.get(presentation_key)
        path_presentation = path_step.get(presentation_key)
        if confirmation_presentation is None:
            continue
        if path_presentation is not None and path_presentation != confirmation_presentation:
            raise V3PreviewMigrationError(
                f"/confirm/{presentation_key} conflicts with /path/{path_index}/{presentation_key}"
            )
        if path_presentation is None:
            if presentation_key == "interactive":
                slot_definition = migration_context.slots[cast(str, path_step["confirm"])]
                preview_step[preview_key] = _migrate_ui(
                    cast(dict[str, Any], confirmation_presentation),
                    _json_pointer("confirm", "interactive"),
                    cast(str, slot_definition["domain"]),
                    migration_context,
                )
            else:
                preview_step[preview_key] = _canonical_clone(confirmation_presentation)
    preview_step["correctable"] = [
        _slot_reference(correctable_slot_name)
        for correctable_slot_name in cast(list[str], confirmation_definition["correctable"])
    ]
    confirmation_target = confirmation_definition.get("on_confirm")
    if confirmation_target is None:
        terminal_definition = migration_context.terminal_definition
        if terminal_definition is None:
            raise V3PreviewMigrationError(
                f"/path/{path_index}/correctable has no explicit or terminal confirmation target"
            )
        confirmation_target = terminal_definition.get("step", "terminal")
    preview_step["on_confirm"] = _step_reference(cast(str, confirmation_target))
    migration_context.confirmation_consumed = True


def _migrate_use_step(
    path_step: dict[str, Any], migration_context: _MigrationContext
) -> dict[str, Any]:
    subflow_reference = cast(str, path_step["use"])
    preview_step: dict[str, Any] = {"use": subflow_reference}
    declaration_match = next(
        (
            (declaration_index, use_declaration)
            for declaration_index, use_declaration in enumerate(migration_context.use_declarations)
            if declaration_index not in migration_context.consumed_use_indexes
            and use_declaration["ref"] == subflow_reference
        ),
        None,
    )
    if declaration_match is not None:
        declaration_index, use_declaration = declaration_match
        migration_context.consumed_use_indexes.add(declaration_index)
        if "with" in use_declaration:
            preview_step["with"] = _canonical_clone(use_declaration["with"])
    return preview_step


def _migrate_explicit_derive_step(
    path_step: dict[str, Any],
    path_index: int,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    derive_target = cast(str, path_step["derive"])
    definition_match = next(
        (
            (derive_index, derive_definition)
            for derive_index, derive_definition in enumerate(migration_context.derive_definitions)
            if derive_index not in migration_context.consumed_derive_indexes
            and derive_definition["writes"] == derive_target
        ),
        None,
    )
    if definition_match is None:
        raise V3PreviewMigrationError(
            f"/path/{path_index}/derive references missing definition {derive_target!r}"
        )
    derive_index, derive_definition = definition_match
    migration_context.consumed_derive_indexes.add(derive_index)
    preview_step = _migrate_derive_definition(derive_definition, migration_context)
    if "step" in path_step:
        preview_step["id"] = path_step["step"]
    return preview_step


def _migrate_derive_definition(
    derive_definition: dict[str, Any], migration_context: _MigrationContext
) -> dict[str, Any]:
    source_slots = cast(list[str], derive_definition["from"])
    preview_step: dict[str, Any] = {
        "derive": derive_definition["writes"],
        "from": [_source_reference(source_slot, migration_context) for source_slot in source_slots],
        "lookup": _canonical_clone(derive_definition["lookup"]),
    }
    if "default" in derive_definition:
        derive_default = derive_definition["default"]
        if isinstance(derive_default, str) and derive_default.startswith("$from["):
            try:
                source_index = int(derive_default.removeprefix("$from[").removesuffix("]"))
                preview_step["default"] = _source_reference(
                    source_slots[source_index], migration_context
                )
            except (ValueError, IndexError) as migration_error:
                raise V3PreviewMigrationError(
                    f"invalid derive fallback reference {derive_default!r}"
                ) from migration_error
        else:
            preview_step["default"] = _canonical_clone(derive_default)
    return preview_step


def _insert_unanchored_derives(
    preview_steps: list[dict[str, Any]], migration_context: _MigrationContext
) -> None:
    last_inserted_identifier_by_anchor: dict[str, str] = {}
    for derive_index, derive_definition in enumerate(migration_context.derive_definitions):
        if derive_index in migration_context.consumed_derive_indexes:
            continue
        preview_derive_step = _migrate_derive_definition(derive_definition, migration_context)
        migration_context.consumed_derive_indexes.add(derive_index)
        after_identifier = derive_definition.get("after")
        if after_identifier is None:
            preview_steps.append(preview_derive_step)
            continue
        effective_anchor = last_inserted_identifier_by_anchor.get(
            cast(str, after_identifier), cast(str, after_identifier)
        )
        anchor_index = next(
            (
                step_index
                for step_index, preview_step in enumerate(preview_steps)
                if _preview_step_identifier(preview_step) == effective_anchor
            ),
            None,
        )
        if anchor_index is None:
            preview_steps.append(preview_derive_step)
            _record_loss(
                migration_context,
                V3PreviewLossCategory.UNKNOWN_DERIVE_ANCHOR,
                _json_pointer("derive", derive_index, "after"),
                after_identifier,
            )
        else:
            preview_steps.insert(anchor_index + 1, preview_derive_step)
            inserted_identifier = _preview_step_identifier(preview_derive_step)
            if inserted_identifier is None:
                raise V3PreviewMigrationError("migrated derive has no stable identifier")
            last_inserted_identifier_by_anchor[cast(str, after_identifier)] = inserted_identifier


def _migrate_submit_step(path_index: int, migration_context: _MigrationContext) -> dict[str, Any]:
    terminal_definition = migration_context.terminal_definition
    if terminal_definition is None:
        raise V3PreviewMigrationError(
            f"/path/{path_index}/terminal has no top-level /terminal definition"
        )
    if migration_context.terminal_consumed:
        raise V3PreviewMigrationError("flowspec/3-draft preview supports a singular submit step")
    preview_inputs: dict[str, Any] = {}
    input_indexes_by_parameter: dict[str, list[int]] = {}
    for input_index, input_binding in enumerate(
        cast(list[dict[str, Any]], terminal_definition.get("input") or [])
    ):
        parameter_name = cast(str, input_binding["param"])
        input_indexes_by_parameter.setdefault(parameter_name, []).append(input_index)
        preview_inputs[parameter_name] = _source_reference(
            cast(str, input_binding["slot"]), migration_context
        )
    for duplicate_indexes in input_indexes_by_parameter.values():
        for duplicate_index in duplicate_indexes[:-1]:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.DUPLICATE_TERMINAL_INPUT_PARAMETER,
                _json_pointer("terminal", "input", duplicate_index),
                cast(list[dict[str, Any]], terminal_definition["input"])[duplicate_index],
            )

    preview_outputs = {
        state_key: _result_reference(cast(str, result_path))
        for state_key, result_path in cast(
            dict[str, Any], terminal_definition.get("outputs") or {}
        ).items()
    }
    preview_step: dict[str, Any] = {
        "submit": terminal_definition["tool"],
        "id": terminal_definition["step"],
        "idempotent": terminal_definition["idempotent"],
        "outcomes": _migrate_outcomes(cast(dict[str, Any], terminal_definition["outcomes"])),
    }
    if preview_inputs:
        preview_step["input"] = preview_inputs
    if preview_outputs:
        preview_step["outputs"] = preview_outputs
    if "empty_payload" in terminal_definition:
        preview_step["empty_payload"] = _canonical_clone(terminal_definition["empty_payload"])
    migration_context.terminal_consumed = True
    return preview_step


def _migrate_outcomes(outcomes: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _canonical_clone(outcomes))


def _migrate_await_step(
    path_step: dict[str, Any],
    path_index: int,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    await_capability = migration_context.await_capability
    if await_capability is None:
        raise V3PreviewMigrationError(
            f"/path/{path_index}/await_external has no /capabilities/await_external definition"
        )
    if migration_context.await_capability_consumed:
        raise V3PreviewMigrationError("flowspec/3-draft preview supports a singular await step")
    path_identifier = path_step.get("step")
    capability_identifier = await_capability.get("step")
    if (
        path_identifier is not None
        and capability_identifier is not None
        and path_identifier != capability_identifier
    ):
        raise V3PreviewMigrationError(
            f"/path/{path_index}/step conflicts with /capabilities/await_external/step"
        )
    step_identifier = cast(str, path_identifier or capability_identifier or "await_external")
    preview_step: dict[str, Any] = {
        "await": await_capability["kind"],
        "id": step_identifier,
        "resume_on": await_capability["resume_on"],
    }
    _merge_await_presentation(
        "prompt", "prompt", path_step, path_index, await_capability, preview_step, migration_context
    )
    _merge_await_presentation(
        "interactive",
        "ui",
        path_step,
        path_index,
        await_capability,
        preview_step,
        migration_context,
    )
    if "ui" not in preview_step:
        raise V3PreviewMigrationError(
            f"/path/{path_index}/await_external has no out-of-band CTA contract"
        )
    if "on_resume" in await_capability:
        preview_on_resume = _migrate_on_resume(
            cast(dict[str, Any], await_capability["on_resume"]), migration_context
        )
        if preview_on_resume:
            preview_step["on_resume"] = preview_on_resume
    if "resume" in await_capability:
        _record_loss(
            migration_context,
            V3PreviewLossCategory.AWAIT_RESUME_CONTRACT,
            _json_pointer("capabilities", "await_external", "resume"),
            await_capability["resume"],
        )
    if "timeout_seconds" in await_capability:
        _record_loss(
            migration_context,
            V3PreviewLossCategory.AWAIT_TIMEOUT_DURATION,
            _json_pointer("capabilities", "await_external", "timeout_seconds"),
            await_capability["timeout_seconds"],
        )
    if "timeout" in await_capability:
        preview_step["timeout"] = _migrate_transition(
            cast(dict[str, Any], await_capability["timeout"])
        )
    if "recovery" in await_capability:
        preview_step["recovery"] = {
            recovery_event: _migrate_transition(cast(dict[str, Any], transition))
            for recovery_event, transition in cast(
                dict[str, Any], await_capability["recovery"]
            ).items()
        }
    migration_context.await_capability_consumed = True
    return preview_step


def _merge_await_presentation(
    source_key: str,
    preview_key: str,
    path_step: dict[str, Any],
    path_index: int,
    await_capability: dict[str, Any],
    preview_step: dict[str, Any],
    migration_context: _MigrationContext,
) -> None:
    path_presentation = path_step.get(source_key)
    capability_presentation = await_capability.get(source_key)
    selected_presentation = path_presentation or capability_presentation
    if selected_presentation is None:
        return
    if (
        path_presentation is not None
        and capability_presentation is not None
        and capability_presentation != path_presentation
    ):
        _record_loss(
            migration_context,
            V3PreviewLossCategory.SHADOWED_AWAIT_PRESENTATION,
            _json_pointer("capabilities", "await_external", source_key),
            capability_presentation,
        )
    if source_key == "interactive":
        preview_step[preview_key] = _migrate_ui(
            cast(dict[str, Any], selected_presentation),
            _json_pointer("path", path_index, source_key)
            if path_presentation is not None
            else _json_pointer("capabilities", "await_external", source_key),
            None,
            migration_context,
        )
    else:
        preview_step[preview_key] = _canonical_clone(selected_presentation)


def _migrate_on_resume(
    on_resume: dict[str, Any], migration_context: _MigrationContext
) -> dict[str, Any]:
    preview_on_resume: dict[str, Any] = {}
    if "set" in on_resume:
        preview_on_resume["set"] = _migrate_binding_map(cast(dict[str, Any], on_resume["set"]))
    if "enrich" in on_resume:
        enrichment = on_resume["enrich"]
        if isinstance(enrichment, str):
            _record_loss(
                migration_context,
                V3PreviewLossCategory.LEGACY_AWAIT_ENRICHMENT,
                _json_pointer("capabilities", "await_external", "on_resume", "enrich"),
                enrichment,
            )
        else:
            enrichment_definition = cast(dict[str, Any], enrichment)
            preview_enrichment: dict[str, Any] = {"tool": enrichment_definition["tool"]}
            if "optional" in enrichment_definition:
                preview_enrichment["optional"] = enrichment_definition["optional"]
            if "input" in enrichment_definition:
                preview_enrichment["input"] = _migrate_binding_map(
                    cast(dict[str, Any], enrichment_definition["input"])
                )
            if "set" in enrichment_definition:
                preview_enrichment["set"] = _migrate_binding_map(
                    cast(dict[str, Any], enrichment_definition["set"])
                )
            preview_on_resume["enrich"] = preview_enrichment
    return preview_on_resume


def _migrate_transition(transition: dict[str, Any]) -> dict[str, Any]:
    transition_target = cast(str, transition["goto"])
    preview_transition: dict[str, Any] = {
        "goto": "END" if transition_target == "END" else _step_reference(transition_target)
    }
    if "set" in transition:
        preview_transition["set"] = _canonical_clone(transition["set"])
    return preview_transition


def _migrate_ui(
    interactive: dict[str, Any],
    source_pointer: str,
    expected_domain_name: str | None,
    migration_context: _MigrationContext,
) -> dict[str, Any]:
    preview_ui: dict[str, Any] = {}
    for interactive_key, interactive_value in interactive.items():
        if interactive_key == "gate":
            _record_loss(
                migration_context,
                V3PreviewLossCategory.LEGACY_INTERACTIVE_GATE,
                f"{source_pointer}/gate",
                interactive_value,
            )
            continue
        if interactive_key == "from_domain":
            if expected_domain_name != interactive_value:
                _record_loss(
                    migration_context,
                    V3PreviewLossCategory.MISMATCHED_INTERACTIVE_DOMAIN,
                    f"{source_pointer}/from_domain",
                    interactive_value,
                )
            continue
        if interactive_key == "next_step":
            preview_ui[interactive_key] = _step_reference(cast(str, interactive_value))
            continue
        if interactive_key == "prefill_from":
            preview_ui[interactive_key] = [
                _slot_reference(prefill_slot_name)
                for prefill_slot_name in cast(list[str], interactive_value)
            ]
            continue
        if interactive_key == "options_when":
            preview_ui[interactive_key] = [
                {
                    "value": option_gate["value"],
                    "gate": _migrate_predicate(
                        cast(dict[str, Any], option_gate["gate"]), migration_context
                    ),
                }
                for option_gate in cast(list[dict[str, Any]], interactive_value)
            ]
            continue
        preview_ui[interactive_key] = _canonical_clone(interactive_value)
    return preview_ui


def _migrate_predicate(
    predicate: dict[str, Any], migration_context: _MigrationContext
) -> dict[str, Any]:
    operator, operand = next(iter(predicate.items()))
    if operator in {"and", "or"}:
        return {
            operator: [
                _migrate_predicate(child_predicate, migration_context)
                for child_predicate in cast(list[dict[str, Any]], operand)
            ]
        }
    if operator == "not":
        return {operator: _migrate_predicate(cast(dict[str, Any], operand), migration_context)}
    if operator == "is_present":
        return {operator: _migrate_predicate_reference(cast(str, operand), migration_context)}
    if operator == "in":
        membership_operands = cast(list[Any], operand)
        return {
            operator: [
                _migrate_predicate_reference(cast(str, membership_operands[0]), migration_context),
                _canonical_clone(membership_operands[1]),
            ]
        }
    comparison_operands = cast(list[Any], operand)
    return {
        operator: [
            _migrate_predicate_operand(comparison_operands[0], migration_context),
            _migrate_predicate_operand(comparison_operands[1], migration_context),
        ]
    }


def _migrate_predicate_operand(predicate_operand: Any, migration_context: _MigrationContext) -> Any:
    if isinstance(predicate_operand, dict) and set(predicate_operand) == {"literal"}:
        return _canonical_clone(predicate_operand["literal"])
    if isinstance(predicate_operand, str):
        namespace, separator, reference_path = predicate_operand.partition(".")
        if separator and namespace == "slots" and reference_path:
            return _source_reference(reference_path, migration_context)
        if separator and namespace in _PREDICATE_REFERENCE_NAMESPACES and reference_path:
            return {_PREDICATE_REFERENCE_NAMESPACES[namespace]: reference_path}
    return _canonical_clone(predicate_operand)


def _migrate_predicate_reference(
    reference: str, migration_context: _MigrationContext
) -> dict[str, str]:
    migrated_reference = _migrate_predicate_operand(reference, migration_context)
    if not isinstance(migrated_reference, dict):
        raise V3PreviewMigrationError(f"predicate reference is not namespaced: {reference!r}")
    return cast(dict[str, str], migrated_reference)


def _migrate_binding_map(bindings: dict[str, Any]) -> dict[str, Any]:
    return {
        binding_target: _migrate_binding(binding_source)
        for binding_target, binding_source in bindings.items()
    }


def _migrate_binding(binding_source: Any) -> Any:
    if isinstance(binding_source, str):
        for source_prefix, reference_key in (
            ("$token.", "$token"),
            ("$result.", "$result"),
        ):
            if binding_source.startswith(source_prefix):
                return {reference_key: binding_source.removeprefix(source_prefix)}
    return _canonical_clone(binding_source)


def _collect_loss_entries(migration_context: _MigrationContext) -> None:
    source_document = migration_context.source_document
    if "entry" in source_document:
        _record_loss(
            migration_context,
            V3PreviewLossCategory.ENTRY_CONTRACT,
            _json_pointer("entry"),
            source_document["entry"],
        )
    if "auto_flow" in source_document:
        _record_loss(
            migration_context,
            V3PreviewLossCategory.AUTOMATIC_FLOW_CONTRACT,
            _json_pointer("auto_flow"),
            source_document["auto_flow"],
        )

    capabilities = cast(dict[str, Any], source_document.get("capabilities") or {})
    for capability_name, capability_definition in capabilities.items():
        if capability_name == "await_external" and migration_context.await_capability_consumed:
            continue
        _record_loss(
            migration_context,
            V3PreviewLossCategory.IMPLICIT_AWAIT_CAPABILITY
            if capability_name == "await_external"
            else V3PreviewLossCategory.AGENT_CAPABILITY,
            _json_pointer("capabilities", capability_name),
            capability_definition,
        )

    for domain_name, domain_definition in migration_context.domains.items():
        if domain_name not in migration_context.used_domain_names:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.UNUSED_DOMAIN_DECLARATION,
                _json_pointer("domains", domain_name),
                domain_definition,
            )
    for slot_name, slot_definition in migration_context.slots.items():
        if slot_name not in migration_context.placed_slot_names:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.UNUSED_SLOT_DECLARATION,
                _json_pointer("slots", slot_name),
                slot_definition,
            )
    for use_index, use_declaration in enumerate(migration_context.use_declarations):
        if use_index not in migration_context.consumed_use_indexes:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.UNUSED_SUBFLOW_DECLARATION,
                _json_pointer("uses", use_index),
                use_declaration,
            )
    if (
        migration_context.confirmation_definition is not None
        and not migration_context.confirmation_consumed
    ):
        _record_loss(
            migration_context,
            V3PreviewLossCategory.UNUSED_CONFIRMATION_CONTRACT,
            _json_pointer("confirm"),
            migration_context.confirmation_definition,
        )
    if (
        migration_context.terminal_definition is not None
        and not migration_context.terminal_consumed
    ):
        _record_loss(
            migration_context,
            V3PreviewLossCategory.UNUSED_TERMINAL_CONTRACT,
            _json_pointer("terminal"),
            migration_context.terminal_definition,
        )
    for gate_name, gate_predicate in migration_context.override_gates.items():
        if gate_name not in migration_context.consumed_override_gate_names:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.UNUSED_OVERRIDE_GATE,
                _json_pointer("overrides", "gates", gate_name),
                gate_predicate,
            )
    for top_level_key, top_level_fragment in source_document.items():
        if top_level_key not in _TOP_LEVEL_NATIVE_KEYS:
            _record_loss(
                migration_context,
                V3PreviewLossCategory.UNKNOWN_TOP_LEVEL_FRAGMENT,
                _json_pointer(top_level_key),
                top_level_fragment,
            )


def _record_loss(
    migration_context: _MigrationContext,
    category: V3PreviewLossCategory,
    source_path: str,
    source_fragment: Any,
) -> None:
    if source_path in migration_context.loss_entries_by_path:
        raise V3PreviewMigrationError(f"source path {source_path!r} was classified more than once")
    migration_context.loss_entries_by_path[source_path] = V3PreviewLossEntry(
        source_path=source_path,
        category=category,
        disposition=_loss_policy_rules()[category].disposition,
        _source_fragment_json=_canonical_json(source_fragment),
    )


def _preview_step_identifier(preview_step: dict[str, Any]) -> str | None:
    explicit_identifier = preview_step.get("id")
    if isinstance(explicit_identifier, str):
        return explicit_identifier
    for step_kind in ("collect", "confirm"):
        step_target = preview_step.get(step_kind)
        if isinstance(step_target, str):
            return step_target
    derive_target = preview_step.get("derive")
    if isinstance(derive_target, str):
        return f"derive_{derive_target}"
    return None


def _slot_reference(slot_path: str) -> dict[str, str]:
    return {"$slot": slot_path}


def _source_reference(source_name: str, migration_context: _MigrationContext) -> dict[str, str]:
    derive_targets = {
        cast(str, derive_definition["writes"])
        for derive_definition in migration_context.derive_definitions
    }
    return (
        {"$derive": source_name}
        if source_name in derive_targets and source_name not in migration_context.slots
        else _slot_reference(source_name)
    )


def _step_reference(step_identifier: str) -> dict[str, str]:
    return {"$step": step_identifier}


def _result_reference(result_path: str) -> dict[str, str]:
    return {"$result": result_path.removeprefix("result.") if result_path != "result" else "$"}


def _same_json_scalar(left_scalar: Any, right_scalar: Any) -> bool:
    return type(left_scalar) is type(right_scalar) and left_scalar == right_scalar


def _json_identity(json_scalar: Any) -> str:
    return json.dumps(json_scalar, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_pointer(*path_segments: str | int) -> str:
    escaped_segments = [
        str(path_segment).replace("~", "~0").replace("/", "~1") for path_segment in path_segments
    ]
    return "/" + "/".join(escaped_segments)


def _canonical_json(json_value: Any) -> str:
    return json.dumps(
        json_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_clone(json_value: Any) -> Any:
    return json.loads(_canonical_json(json_value))
