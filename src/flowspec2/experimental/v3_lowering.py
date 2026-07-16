"""Deterministic lowering from the experimental v3 preview to FlowSpec2."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, cast

from ..checker import check_flow
from ..diagnostics import FlowCheckReport, FlowDiagnostic
from ..profiles import FlowProfile
from .v3_preview import (
    V2_SCHEMA_IDENTIFIER,
    V3PreviewDocumentMode,
    V3PreviewLossCategory,
    V3PreviewLossHandling,
    migrate_v2_to_v3_preview,
    preview_loss_policy,
    validate_v3_preview,
)

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
_PREDICATE_REFERENCE_NAMESPACES = {
    "$config": "config",
    "$payload": "payload",
    "$internal": "internal",
    "$address": "address",
}


class V3PreviewLoweringError(ValueError):
    """A valid preview cannot produce an executable FlowSpec2 document."""

    def __init__(self, diagnostics: tuple[FlowDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        summary = "; ".join(
            f"{diagnostic.code} at {diagnostic.path or '/'}: {diagnostic.message}"
            for diagnostic in diagnostics
        )
        super().__init__(summary)


@dataclass(frozen=True, slots=True)
class V3PreviewLoweringReport:
    """Immutable executable lowering result with fixed-point evidence."""

    _source_json: str = field(repr=False)
    rehydrated_paths: tuple[str, ...]
    flow_check: FlowCheckReport

    @property
    def source_document(self) -> dict[str, Any]:
        """Return a fresh lowered document so callers cannot mutate the report."""
        return cast(dict[str, Any], json.loads(self._source_json))


@dataclass(slots=True)
class _LoweringContext:
    source_document: dict[str, Any]
    reserved_domain_names: set[str]
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: list[dict[str, Any]] = field(default_factory=list)
    uses: list[dict[str, Any]] = field(default_factory=list)
    derives: list[dict[str, Any]] = field(default_factory=list)
    override_gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    confirmation: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    await_capability: dict[str, Any] | None = None


def lower_v3_preview_to_v2(
    preview_document: Mapping[str, Any],
    *,
    profile: FlowProfile | None = None,
) -> dict[str, Any]:
    """Lower a preview and return a fresh executable FlowSpec2 document."""
    return lower_v3_preview_to_v2_report(preview_document, profile=profile).source_document


def lower_v3_preview_to_v2_report(
    preview_document: Mapping[str, Any],
    *,
    profile: FlowProfile | None = None,
) -> V3PreviewLoweringReport:
    """Lower, compile-check, and prove the exact v3-to-v2-to-v3 fixed point."""
    private_preview = _canonical_clone(preview_document)
    if not isinstance(private_preview, dict):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_INVALID_DOCUMENT",
            "",
            "preview source must be a JSON object",
        )
    validation_mode = (
        V3PreviewDocumentMode.V2_MIGRATION
        if "v2_passthrough" in private_preview
        else V3PreviewDocumentMode.AUTHORING
    )
    validate_v3_preview(private_preview, mode=validation_mode)
    loss_entries = _loss_entries(private_preview)
    _reject_excluded_loss_entries(loss_entries)
    _validate_lowerable_surface(private_preview)

    source_document = _lower_native_preview(private_preview, loss_entries)
    rehydrated_paths = _rehydrate_loss_entries(source_document, loss_entries)
    if not source_document.get("domains"):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_DOMAINS_REQUIRED",
            "/steps",
            "FlowSpec2 requires at least one local or rehydrated domain declaration",
        )
    flow_check = check_flow(source_document, profile=profile)
    if not flow_check.is_valid or flow_check.compilation != "succeeded":
        raise V3PreviewLoweringError(flow_check.diagnostics)

    round_trip_preview = migrate_v2_to_v3_preview(source_document)
    if round_trip_preview != private_preview:
        mismatch_path = _first_difference_path(private_preview, round_trip_preview)
        _raise_lowering(
            "FLOWSPEC3_LOWERING_FIXED_POINT_MISMATCH",
            mismatch_path,
            "lowered FlowSpec2 does not migrate back to the exact preview source",
        )

    return V3PreviewLoweringReport(
        _source_json=_canonical_json(source_document),
        rehydrated_paths=rehydrated_paths,
        flow_check=flow_check,
    )


def _loss_entries(preview_document: dict[str, Any]) -> list[dict[str, Any]]:
    passthrough = cast(dict[str, Any] | None, preview_document.get("v2_passthrough"))
    return [] if passthrough is None else cast(list[dict[str, Any]], passthrough["entries"])


def _reject_excluded_loss_entries(loss_entries: list[dict[str, Any]]) -> None:
    policy_categories = cast(dict[str, dict[str, str]], preview_loss_policy()["categories"])
    diagnostics = tuple(
        FlowDiagnostic(
            code="FLOWSPEC3_LOWERING_EXCLUDED_LOSS",
            severity="error",
            path=f"/v2_passthrough/entries/{loss_index}",
            message=(
                f"loss category {loss_entry['category']!r} requires rejection and cannot be "
                "rehydrated"
            ),
        )
        for loss_index, loss_entry in enumerate(loss_entries)
        if V3PreviewLossHandling(policy_categories[cast(str, loss_entry["category"])]["handling"])
        is V3PreviewLossHandling.REJECT
    )
    if diagnostics:
        raise V3PreviewLoweringError(diagnostics)


def _validate_lowerable_surface(preview_document: dict[str, Any]) -> None:
    for step_index, preview_step in enumerate(
        cast(list[dict[str, Any]], preview_document["steps"])
    ):
        step_path = f"/steps/{step_index}"
        if domain := preview_step.get("domain"):
            _validate_lowerable_domain(cast(dict[str, Any], domain), f"{step_path}/domain")
        preview_ui = preview_step.get("ui")
        if isinstance(preview_ui, dict) and preview_ui.get("kind") == "flow":
            _raise_lowering(
                "FLOWSPEC3_LOWERING_UNSUPPORTED_FLOW_UI",
                f"{step_path}/ui",
                "FlowSpec2 path steps have no equivalent Meta Flow UI contract",
            )
        if "derive" in preview_step:
            lookup = cast(dict[str, Any], preview_step["lookup"])
            if any(not isinstance(lookup_value, str) for lookup_value in lookup.values()):
                _raise_lowering(
                    "FLOWSPEC3_LOWERING_NON_STRING_DERIVE_VALUE",
                    f"{step_path}/lookup",
                    "FlowSpec2 derive lookup values must be strings",
                )
            derive_default = preview_step.get("default")
            if derive_default is not None and (
                not isinstance(derive_default, (str, dict))
                or isinstance(derive_default, dict)
                and set(derive_default) not in ({"$slot"}, {"$derive"})
            ):
                _raise_lowering(
                    "FLOWSPEC3_LOWERING_NON_STRING_DERIVE_DEFAULT",
                    f"{step_path}/default",
                    "FlowSpec2 derive defaults must be strings or source references",
                )
            if isinstance(derive_default, str) and re.fullmatch(
                r"\$from\[[0-9]+\]", derive_default
            ):
                _raise_lowering(
                    "FLOWSPEC3_LOWERING_AMBIGUOUS_DERIVE_DEFAULT",
                    f"{step_path}/default",
                    "FlowSpec2 interprets this string as a derive source reference",
                )
        for predicate_key in ("ask_when", "skip_when", "gate"):
            if predicate_key in preview_step:
                _validate_lowerable_predicate_references(
                    cast(dict[str, Any], preview_step[predicate_key]),
                    f"{step_path}/{predicate_key}",
                )
        preview_ui = cast(dict[str, Any] | None, preview_step.get("ui"))
        if isinstance(preview_ui, dict):
            for option_index, option_gate in enumerate(
                cast(list[dict[str, Any]], preview_ui.get("options_when", []))
            ):
                _validate_lowerable_predicate_references(
                    cast(dict[str, Any], option_gate["gate"]),
                    f"{step_path}/ui/options_when/{option_index}/gate",
                )
        if on_resume := preview_step.get("on_resume"):
            _validate_lowerable_bindings(
                cast(dict[str, Any], on_resume).get("set"),
                "$token.",
                f"{step_path}/on_resume/set",
            )
            enrichment = cast(dict[str, Any], on_resume).get("enrich")
            if isinstance(enrichment, dict):
                _validate_lowerable_bindings(
                    enrichment.get("input"), "$token.", f"{step_path}/on_resume/enrich/input"
                )
                _validate_lowerable_bindings(
                    enrichment.get("set"), "$result.", f"{step_path}/on_resume/enrich/set"
                )


def _validate_lowerable_domain(domain: dict[str, Any], domain_path: str) -> None:
    domain_type = cast(str, domain["type"])
    if domain_type not in {"categorical", "bool"}:
        return
    if (
        domain_type == "categorical"
        and domain.get("accepts_null") is True
        and cast(dict[str, Any], domain.get("normalize", {})).get("number_words") is True
    ):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_NULLABLE_NUMBER_WORDS",
            f"{domain_path}/normalize/number_words",
            "nullable categorical number-word normalization has no stable FlowSpec2 round-trip",
        )
    for option_index, option in enumerate(cast(list[dict[str, Any]], domain["options"])):
        option_value = option["value"]
        expected_label = (
            "Yes" if option_value is True else "No" if option_value is False else str(option_value)
        )
        if option["label"] != expected_label:
            _raise_lowering(
                "FLOWSPEC3_LOWERING_CUSTOM_OPTION_LABEL",
                f"{domain_path}/options/{option_index}/label",
                f"FlowSpec2 derives label {expected_label!r} from the canonical option value",
            )
        if domain_type == "bool" and "description" in option:
            _raise_lowering(
                "FLOWSPEC3_LOWERING_BOOLEAN_OPTION_DESCRIPTION",
                f"{domain_path}/options/{option_index}/description",
                "FlowSpec2 boolean domains cannot represent per-option descriptions",
            )
        aliases = cast(list[str], option.get("aliases", []))
        if aliases != sorted(aliases):
            _raise_lowering(
                "FLOWSPEC3_LOWERING_UNORDERED_ALIASES",
                f"{domain_path}/options/{option_index}/aliases",
                "aliases must use canonical lexical order for an exact round-trip",
            )
    null_aliases = cast(list[str], domain.get("null_aliases", []))
    if null_aliases != sorted(null_aliases):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_UNORDERED_ALIASES",
            f"{domain_path}/null_aliases",
            "null aliases must use canonical lexical order for an exact round-trip",
        )


def _validate_lowerable_bindings(bindings: Any, reserved_prefix: str, path: str) -> None:
    if not isinstance(bindings, dict):
        return
    for binding_name, binding_value in bindings.items():
        reference_key = reserved_prefix.removesuffix(".")
        if isinstance(binding_value, (dict, list)) and not (
            isinstance(binding_value, dict) and set(binding_value) == {reference_key}
        ):
            _raise_lowering(
                "FLOWSPEC3_LOWERING_COMPOSITE_BINDING_LITERAL",
                f"{path}/{_encode_pointer_segment(binding_name)}",
                "FlowSpec2 resume and enrichment binding literals must be JSON scalars",
            )
        if isinstance(binding_value, str) and binding_value.startswith(reserved_prefix):
            _raise_lowering(
                "FLOWSPEC3_LOWERING_AMBIGUOUS_BINDING_LITERAL",
                f"{path}/{_encode_pointer_segment(binding_name)}",
                f"FlowSpec2 interprets strings beginning with {reserved_prefix!r} as references",
            )


def _validate_lowerable_predicate_references(predicate: dict[str, Any], path: str) -> None:
    operator, operand = next(iter(predicate.items()))
    if operator in {"and", "or"}:
        for child_index, child_predicate in enumerate(cast(list[dict[str, Any]], operand)):
            _validate_lowerable_predicate_references(
                child_predicate, f"{path}/{operator}/{child_index}"
            )
        return
    if operator == "not":
        _validate_lowerable_predicate_references(cast(dict[str, Any], operand), f"{path}/not")
        return
    operands = [operand] if operator == "is_present" else cast(list[Any], operand)
    for operand_index, predicate_operand in enumerate(operands):
        if not isinstance(predicate_operand, dict):
            continue
        reference_key, reference_path = next(iter(predicate_operand.items()))
        if reference_key in {"$config", "$payload", "$internal"} and "." in reference_path:
            _raise_lowering(
                "FLOWSPEC3_LOWERING_NESTED_STATE_REFERENCE",
                f"{path}/{operator}/{operand_index}",
                f"FlowSpec2 supports only one path segment for {reference_key} references",
            )


def _lower_native_preview(
    preview_document: dict[str, Any], loss_entries: list[dict[str, Any]]
) -> dict[str, Any]:
    source_document: dict[str, Any] = {
        "schema": V2_SCHEMA_IDENTIFIER,
        "flow": preview_document["flow"],
        "version": preview_document["version"],
        "route": _canonical_clone(preview_document["route"]),
    }
    for top_level_key in ("service", "config"):
        if top_level_key in preview_document:
            source_document[top_level_key] = _canonical_clone(preview_document[top_level_key])
    lowering_context = _LoweringContext(
        source_document=source_document,
        reserved_domain_names={
            _decode_pointer(cast(str, loss_entry["source_path"]))[-1]
            for loss_entry in loss_entries
            if loss_entry["category"] == V3PreviewLossCategory.UNUSED_DOMAIN_DECLARATION.value
        },
    )
    for step_index, preview_step in enumerate(
        cast(list[dict[str, Any]], preview_document["steps"])
    ):
        _lower_step(preview_step, step_index, lowering_context)
    source_document["path"] = lowering_context.path
    for key, section in (
        ("domains", lowering_context.domains),
        ("slots", lowering_context.slots),
        ("uses", lowering_context.uses),
        ("derive", lowering_context.derives),
    ):
        if section:
            source_document[key] = section
    if lowering_context.confirmation is not None:
        source_document["confirm"] = lowering_context.confirmation
    if lowering_context.terminal is not None:
        source_document["terminal"] = lowering_context.terminal
    if lowering_context.override_gates:
        source_document["overrides"] = {"gates": lowering_context.override_gates}
    if lowering_context.await_capability is not None:
        source_document["capabilities"] = {"await_external": lowering_context.await_capability}
    return cast(dict[str, Any], _canonical_clone(source_document))


def _lower_step(
    preview_step: dict[str, Any], step_index: int, lowering_context: _LoweringContext
) -> None:
    if "collect" in preview_step:
        _lower_slot_step("collect", "slot", preview_step, step_index, lowering_context)
        return
    if "confirm" in preview_step:
        _lower_slot_step("confirm", "confirm", preview_step, step_index, lowering_context)
        return
    if "use" in preview_step:
        use_reference = cast(str, preview_step["use"])
        lowering_context.path.append({"use": use_reference})
        use_declaration: dict[str, Any] = {"ref": use_reference}
        if "with" in preview_step:
            use_declaration["with"] = _canonical_clone(preview_step["with"])
        lowering_context.uses.append(use_declaration)
        return
    if "derive" in preview_step:
        _lower_derive_step(preview_step, step_index, lowering_context)
        return
    if "submit" in preview_step:
        _lower_submit_step(preview_step, lowering_context)
        return
    if "await" in preview_step:
        _lower_await_step(preview_step, lowering_context)
        return
    _raise_lowering(
        "FLOWSPEC3_LOWERING_UNKNOWN_STEP",
        f"/steps/{step_index}",
        "preview step has no lowerable kind",
    )


def _lower_slot_step(
    preview_kind: str,
    v2_kind: str,
    preview_step: dict[str, Any],
    step_index: int,
    lowering_context: _LoweringContext,
) -> None:
    slot_name = cast(str, preview_step[preview_kind])
    domain_name = _available_domain_name(slot_name, lowering_context)
    lowering_context.domains[domain_name] = _lower_domain(
        cast(dict[str, Any], preview_step["domain"])
    )
    slot_definition = {
        "domain": domain_name,
        **{
            contract_key: _canonical_clone(preview_step[contract_key])
            for contract_key in _SLOT_CONTRACT_KEYS
            if contract_key in preview_step
        },
    }
    if "requires" in preview_step:
        slot_definition["requires"] = [
            _reference_target(slot_reference, "$slot")
            for slot_reference in cast(list[dict[str, str]], preview_step["requires"])
        ]
    lowering_context.slots[slot_name] = slot_definition

    path_step: dict[str, Any] = {v2_kind: slot_name}
    if "id" in preview_step:
        path_step["step"] = preview_step["id"]
    if "prompt" in preview_step:
        path_step["prompt"] = _canonical_clone(preview_step["prompt"])
    if "ui" in preview_step:
        path_step["interactive"] = _lower_ui(cast(dict[str, Any], preview_step["ui"]), domain_name)
    for predicate_key in ("ask_when", "skip_when"):
        if predicate_key in preview_step:
            path_step[predicate_key] = _lower_predicate(
                cast(dict[str, Any], preview_step[predicate_key])
            )
    if "gate" in preview_step:
        step_identifier = cast(str, preview_step.get("id", slot_name))
        lowering_context.override_gates[step_identifier] = _lower_predicate(
            cast(dict[str, Any], preview_step["gate"])
        )
    if "on_reject" in preview_step:
        path_step["on_reject"] = _canonical_clone(preview_step["on_reject"])
    if preview_step.get("correction_hub") is True:
        path_step["correctable"] = True
        step_identifier = cast(str, preview_step.get("id", slot_name))
        lowering_context.confirmation = {
            "step": step_identifier,
            "slot": slot_name,
            "correctable": [
                _reference_target(reference, "$slot")
                for reference in cast(list[dict[str, str]], preview_step["correctable"])
            ],
            "on_confirm": _reference_target(
                cast(dict[str, str], preview_step["on_confirm"]), "$step"
            ),
        }
    lowering_context.path.append(path_step)


def _available_domain_name(slot_name: str, lowering_context: _LoweringContext) -> str:
    domain_name_base = f"domain_{slot_name}"
    domain_name = domain_name_base
    suffix = 2
    unavailable_names = {
        *lowering_context.reserved_domain_names,
        *lowering_context.domains,
    }
    while domain_name in unavailable_names:
        domain_name = f"{domain_name_base}_{suffix}"
        suffix += 1
    return domain_name


def _lower_domain(preview_domain: dict[str, Any]) -> dict[str, Any]:
    domain_type = cast(str, preview_domain["type"])
    domain_definition: dict[str, Any] = {"type": domain_type}
    for domain_key in ("optional", "minimum", "maximum"):
        if domain_key in preview_domain:
            domain_definition[domain_key] = preview_domain[domain_key]
    normalization = cast(dict[str, Any], _canonical_clone(preview_domain.get("normalize", {})))
    if domain_type in {"categorical", "bool"}:
        options = cast(list[dict[str, Any]], preview_domain["options"])
        if domain_type == "categorical":
            domain_definition["values"] = [option["value"] for option in options]
            if preview_domain.get("accepts_null") is True:
                cast(list[Any], domain_definition["values"]).append(None)
        synonyms = {
            alias: option["value"]
            for option in options
            for alias in cast(list[str], option.get("aliases", []))
        }
        if domain_type == "categorical" and preview_domain.get("accepts_null") is True:
            synonyms.update(
                {alias: None for alias in cast(list[str], preview_domain.get("null_aliases", []))}
            )
        if synonyms:
            normalization["synonyms"] = synonyms
        if domain_type == "categorical":
            rows = [
                {"value": option["value"], "description": option["description"]}
                for option in options
                if "description" in option
            ]
            if rows:
                domain_definition["rows"] = rows
    if normalization:
        domain_definition["normalize"] = normalization
    return domain_definition


def _lower_ui(preview_ui: dict[str, Any], domain_name: str | None) -> dict[str, Any]:
    interactive = {
        interactive_key: _canonical_clone(interactive_value)
        for interactive_key, interactive_value in preview_ui.items()
        if interactive_key not in {"next_step", "prefill_from", "options_when"}
    }
    if preview_ui["kind"] in {"buttons", "list"}:
        if domain_name is None:
            _raise_lowering(
                "FLOWSPEC3_LOWERING_MISSING_UI_DOMAIN",
                "",
                "choice UI requires a lowered domain",
            )
        interactive["from_domain"] = domain_name
    if "next_step" in preview_ui:
        interactive["next_step"] = _reference_target(
            cast(dict[str, str], preview_ui["next_step"]), "$step"
        )
    if "prefill_from" in preview_ui:
        interactive["prefill_from"] = [
            _reference_target(reference, "$slot")
            for reference in cast(list[dict[str, str]], preview_ui["prefill_from"])
        ]
    if "options_when" in preview_ui:
        interactive["options_when"] = [
            {
                "value": option_gate["value"],
                "gate": _lower_predicate(cast(dict[str, Any], option_gate["gate"])),
            }
            for option_gate in cast(list[dict[str, Any]], preview_ui["options_when"])
        ]
    return interactive


def _lower_derive_step(
    preview_step: dict[str, Any], step_index: int, lowering_context: _LoweringContext
) -> None:
    source_references = cast(list[dict[str, str]], preview_step["from"])
    source_names = [_source_reference_target(reference) for reference in source_references]
    derive_definition: dict[str, Any] = {
        "writes": preview_step["derive"],
        "from": source_names,
        "lookup": _canonical_clone(preview_step["lookup"]),
    }
    if "default" in preview_step:
        derive_default = preview_step["default"]
        if isinstance(derive_default, dict):
            source_index = next(
                (
                    candidate_index
                    for candidate_index, source_reference in enumerate(source_references)
                    if source_reference == cast(dict[str, str], derive_default)
                ),
                None,
            )
            if source_index is None:
                _raise_lowering(
                    "FLOWSPEC3_LOWERING_DERIVE_DEFAULT_OUTSIDE_SOURCES",
                    f"/steps/{step_index}/default",
                    "derive default reference must also appear in the ordered from list",
                )
            derive_definition["default"] = f"$from[{source_index}]"
        else:
            derive_definition["default"] = derive_default
    lowering_context.derives.append(derive_definition)
    path_step: dict[str, Any] = {"derive": preview_step["derive"]}
    if "id" in preview_step:
        path_step["step"] = preview_step["id"]
    lowering_context.path.append(path_step)


def _lower_submit_step(preview_step: dict[str, Any], lowering_context: _LoweringContext) -> None:
    terminal: dict[str, Any] = {
        "step": preview_step["id"],
        "tool": preview_step["submit"],
        "idempotent": preview_step["idempotent"],
        "outcomes": _canonical_clone(preview_step["outcomes"]),
    }
    if "input" in preview_step:
        terminal["input"] = [
            {"param": parameter_name, "slot": _source_reference_target(source_reference)}
            for parameter_name, source_reference in cast(
                dict[str, dict[str, str]], preview_step["input"]
            ).items()
        ]
    if "outputs" in preview_step:
        terminal["outputs"] = {
            state_key: f"result.{_reference_target(result_reference, '$result')}"
            for state_key, result_reference in cast(
                dict[str, dict[str, str]], preview_step["outputs"]
            ).items()
        }
    if "empty_payload" in preview_step:
        terminal["empty_payload"] = _canonical_clone(preview_step["empty_payload"])
    lowering_context.terminal = terminal
    lowering_context.path.append({"terminal": True})


def _lower_await_step(preview_step: dict[str, Any], lowering_context: _LoweringContext) -> None:
    await_capability: dict[str, Any] = {
        "kind": preview_step["await"],
        "step": preview_step["id"],
        "resume_on": preview_step["resume_on"],
    }
    if "on_resume" in preview_step:
        await_capability["on_resume"] = _lower_on_resume(
            cast(dict[str, Any], preview_step["on_resume"])
        )
    if "timeout" in preview_step:
        await_capability["timeout"] = _lower_transition(
            cast(dict[str, Any], preview_step["timeout"])
        )
    if "recovery" in preview_step:
        await_capability["recovery"] = {
            recovery_event: _lower_transition(transition)
            for recovery_event, transition in cast(
                dict[str, dict[str, Any]], preview_step["recovery"]
            ).items()
        }
    path_step: dict[str, Any] = {
        "step": preview_step["id"],
        "await_external": True,
        "interactive": _lower_ui(cast(dict[str, Any], preview_step["ui"]), None),
    }
    if "prompt" in preview_step:
        path_step["prompt"] = _canonical_clone(preview_step["prompt"])
    lowering_context.await_capability = await_capability
    lowering_context.path.append(path_step)


def _lower_on_resume(preview_on_resume: dict[str, Any]) -> dict[str, Any]:
    on_resume: dict[str, Any] = {}
    if "set" in preview_on_resume:
        on_resume["set"] = _lower_binding_map(
            cast(dict[str, Any], preview_on_resume["set"]), "$token"
        )
    if "enrich" in preview_on_resume:
        preview_enrichment = cast(dict[str, Any], preview_on_resume["enrich"])
        enrichment: dict[str, Any] = {"tool": preview_enrichment["tool"]}
        if "optional" in preview_enrichment:
            enrichment["optional"] = preview_enrichment["optional"]
        if "input" in preview_enrichment:
            enrichment["input"] = _lower_binding_map(
                cast(dict[str, Any], preview_enrichment["input"]), "$token"
            )
        if "set" in preview_enrichment:
            enrichment["set"] = _lower_binding_map(
                cast(dict[str, Any], preview_enrichment["set"]), "$result"
            )
        on_resume["enrich"] = enrichment
    return on_resume


def _lower_binding_map(bindings: dict[str, Any], reference_key: str) -> dict[str, Any]:
    return {
        binding_name: (
            f"{reference_key}.{_reference_target(binding_value, reference_key)}"
            if isinstance(binding_value, dict) and reference_key in binding_value
            else _canonical_clone(binding_value)
        )
        for binding_name, binding_value in bindings.items()
    }


def _lower_transition(preview_transition: dict[str, Any]) -> dict[str, Any]:
    transition_target = preview_transition["goto"]
    transition: dict[str, Any] = {
        "goto": transition_target
        if transition_target == "END"
        else _reference_target(cast(dict[str, str], transition_target), "$step")
    }
    if "set" in preview_transition:
        transition["set"] = _canonical_clone(preview_transition["set"])
    return transition


def _lower_predicate(preview_predicate: dict[str, Any]) -> dict[str, Any]:
    operator, operand = next(iter(preview_predicate.items()))
    if operator in {"and", "or"}:
        return {
            operator: [
                _lower_predicate(child_predicate)
                for child_predicate in cast(list[dict[str, Any]], operand)
            ]
        }
    if operator == "not":
        return {operator: _lower_predicate(cast(dict[str, Any], operand))}
    if operator == "is_present":
        return {operator: _lower_predicate_reference(cast(dict[str, str], operand))}
    if operator == "in":
        membership_operands = cast(list[Any], operand)
        return {
            operator: [
                _lower_predicate_reference(cast(dict[str, str], membership_operands[0])),
                _canonical_clone(membership_operands[1]),
            ]
        }
    return {
        operator: [
            _lower_predicate_operand(predicate_operand)
            for predicate_operand in cast(list[Any], operand)
        ]
    }


def _lower_predicate_operand(predicate_operand: Any) -> Any:
    if isinstance(predicate_operand, dict):
        return _lower_predicate_reference(cast(dict[str, str], predicate_operand))
    if isinstance(predicate_operand, str) and _looks_like_v2_reference(predicate_operand):
        return {"literal": predicate_operand}
    return _canonical_clone(predicate_operand)


def _lower_predicate_reference(reference: dict[str, str]) -> str:
    reference_key, reference_path = next(iter(reference.items()))
    if reference_key in {"$slot", "$derive"}:
        return f"slots.{reference_path}"
    return f"{_PREDICATE_REFERENCE_NAMESPACES[reference_key]}.{reference_path}"


def _looks_like_v2_reference(value: str) -> bool:
    namespace, separator, reference_path = value.partition(".")
    return bool(
        separator
        and reference_path
        and namespace in {"slots", "internal", "payload", "config", "address"}
    )


def _source_reference_target(reference: dict[str, str]) -> str:
    reference_key, reference_target = next(iter(reference.items()))
    if reference_key not in {"$slot", "$derive"}:
        _raise_lowering(
            "FLOWSPEC3_LOWERING_INVALID_SOURCE_REFERENCE",
            "",
            f"source reference key {reference_key!r} is not lowerable",
        )
    return reference_target


def _reference_target(reference: dict[str, str], expected_key: str) -> str:
    return reference[expected_key]


def _rehydrate_loss_entries(
    source_document: dict[str, Any], loss_entries: list[dict[str, Any]]
) -> tuple[str, ...]:
    rehydratable_entries = [
        loss_entry
        for loss_entry in loss_entries
        if loss_entry["disposition"] == "compatibility_only"
    ]
    use_entries = sorted(
        (
            loss_entry
            for loss_entry in rehydratable_entries
            if loss_entry["category"] == V3PreviewLossCategory.UNUSED_SUBFLOW_DECLARATION.value
        ),
        key=lambda loss_entry: int(cast(str, loss_entry["source_path"]).rsplit("/", 1)[1]),
    )
    for loss_entry in rehydratable_entries:
        if loss_entry in use_entries:
            continue
        _prepare_rehydration_parent(source_document, loss_entry)
        _set_pointer(
            source_document,
            cast(str, loss_entry["source_path"]),
            _canonical_clone(loss_entry["source_fragment"]),
        )
    for loss_entry in use_entries:
        _insert_array_pointer(
            source_document,
            cast(str, loss_entry["source_path"]),
            _canonical_clone(loss_entry["source_fragment"]),
        )
    return tuple(cast(str, loss_entry["source_path"]) for loss_entry in rehydratable_entries)


def _prepare_rehydration_parent(
    source_document: dict[str, Any], loss_entry: dict[str, Any]
) -> None:
    category = V3PreviewLossCategory(cast(str, loss_entry["category"]))
    if category in {
        V3PreviewLossCategory.AGENT_CAPABILITY,
        V3PreviewLossCategory.IMPLICIT_AWAIT_CAPABILITY,
    }:
        source_document.setdefault("capabilities", {})
    elif category is V3PreviewLossCategory.UNUSED_DOMAIN_DECLARATION:
        source_document.setdefault("domains", {})
    elif category is V3PreviewLossCategory.UNUSED_SLOT_DECLARATION:
        source_document.setdefault("slots", {})
    elif category is V3PreviewLossCategory.UNUSED_OVERRIDE_GATE:
        overrides = source_document.setdefault("overrides", {})
        if isinstance(overrides, dict):
            overrides.setdefault("gates", {})


def _set_pointer(document: dict[str, Any], pointer: str, fragment: Any) -> None:
    segments = _decode_pointer(pointer)
    parent: Any = document
    for segment in segments[:-1]:
        if not isinstance(parent, dict) or segment not in parent:
            _raise_lowering(
                "FLOWSPEC3_LOWERING_REHYDRATION_PARENT_MISSING",
                pointer,
                "loss entry parent does not exist in the native lowered document",
            )
        parent = parent[segment]
    final_segment = segments[-1]
    if not isinstance(parent, dict):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_REHYDRATION_PARENT_INVALID",
            pointer,
            "loss entry parent is not an object",
        )
    if final_segment in parent:
        _raise_lowering(
            "FLOWSPEC3_LOWERING_REHYDRATION_CONFLICT",
            pointer,
            "loss entry would overwrite native lowered content",
        )
    parent[final_segment] = fragment


def _insert_array_pointer(document: dict[str, Any], pointer: str, fragment: Any) -> None:
    segments = _decode_pointer(pointer)
    if len(segments) != 2 or segments[0] != "uses":
        _raise_lowering(
            "FLOWSPEC3_LOWERING_REHYDRATION_ARRAY_PATH_INVALID",
            pointer,
            "only unused subflow declarations may rehydrate into arrays",
        )
    uses = document.setdefault("uses", [])
    if not isinstance(uses, list):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_REHYDRATION_PARENT_INVALID",
            pointer,
            "uses rehydration parent is not an array",
        )
    insertion_index = int(segments[1])
    if insertion_index > len(uses):
        _raise_lowering(
            "FLOWSPEC3_LOWERING_REHYDRATION_INDEX_INVALID",
            pointer,
            "unused subflow declaration index is beyond the reconstructed uses array",
        )
    uses.insert(insertion_index, fragment)


def _decode_pointer(pointer: str) -> list[str]:
    return [segment.replace("~1", "/").replace("~0", "~") for segment in pointer[1:].split("/")]


def _first_difference_path(expected: Any, actual: Any, segments: tuple[str | int, ...] = ()) -> str:
    if type(expected) is not type(actual):
        return _pointer(segments)
    if isinstance(expected, dict):
        expected_keys = set(expected)
        actual_keys = set(cast(dict[str, Any], actual))
        if expected_keys != actual_keys:
            differing_key = sorted(expected_keys ^ actual_keys)[0]
            return _pointer((*segments, differing_key))
        for key in sorted(expected):
            difference = _first_difference_path(expected[key], actual[key], (*segments, key))
            if difference:
                return difference
        return ""
    if isinstance(expected, list):
        actual_list = cast(list[Any], actual)
        if len(expected) != len(actual_list):
            return _pointer((*segments, min(len(expected), len(actual_list))))
        for index, expected_item in enumerate(expected):
            difference = _first_difference_path(
                expected_item, actual_list[index], (*segments, index)
            )
            if difference:
                return difference
        return ""
    return "" if expected == actual else _pointer(segments)


def _pointer(segments: tuple[str | int, ...]) -> str:
    return "".join(f"/{_encode_pointer_segment(str(segment))}" for segment in segments)


def _encode_pointer_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _raise_lowering(code: str, path: str, message: str) -> None:
    raise V3PreviewLoweringError(
        (FlowDiagnostic(code=code, severity="error", path=path, message=message),)
    )


def _canonical_clone(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
