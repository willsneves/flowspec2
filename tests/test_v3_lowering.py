from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from flowspec2 import FlowProfile, load_flow, reference_profile
from flowspec2.experimental import (
    V3PreviewLoweringError,
    lower_v3_preview_to_v2,
    lower_v3_preview_to_v2_report,
    migrate_v2_to_v3_preview,
)

EXAMPLES_DIRECTORY = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize(
    "example_filename",
    ["reparo_buraco.flow.json", "reparo_luminaria.flow.json"],
)
def test_migrated_examples_lower_compile_and_close_the_exact_fixed_point(
    example_filename: str,
) -> None:
    source_document = load_flow(EXAMPLES_DIRECTORY / example_filename)
    preview_document = migrate_v2_to_v3_preview(source_document)

    lowering_report = lower_v3_preview_to_v2_report(preview_document)
    lowered_document = lowering_report.source_document

    assert lowering_report.flow_check.compilation == "succeeded"
    assert migrate_v2_to_v3_preview(lowered_document) == preview_document
    assert lowering_report.rehydrated_paths == tuple(
        loss_entry["source_path"] for loss_entry in preview_document["v2_passthrough"]["entries"]
    )


def test_lowering_is_pure_deterministic_and_report_is_immutable(
    luminaria_doc: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
    preview_snapshot = copy.deepcopy(preview_document)

    first_report = lower_v3_preview_to_v2_report(preview_document)
    second_report = lower_v3_preview_to_v2_report(preview_document)

    assert preview_document == preview_snapshot
    assert first_report == second_report
    mutable_source = first_report.source_document
    mutable_source["flow"] = "changed_by_caller"
    assert first_report.source_document["flow"] == "reparo_luminaria"


def test_authored_lowerable_preview_compiles_without_passthrough() -> None:
    preview_document = _minimal_preview()

    lowered_document = lower_v3_preview_to_v2(preview_document)

    assert lowered_document["schema"] == "flowspec/2"
    assert lowered_document["slots"]["answer"]["domain"] == "domain_answer"
    assert migrate_v2_to_v3_preview(lowered_document) == preview_document


def test_await_lifecycle_loss_entries_are_rehydrated_before_compilation() -> None:
    preview_document = _preview_with_external_wait()

    lowering_report = lower_v3_preview_to_v2_report(preview_document)
    await_capability = lowering_report.source_document["capabilities"]["await_external"]

    assert await_capability["resume"] == _resume_contract()
    assert await_capability["timeout_seconds"] == 300
    assert lowering_report.rehydrated_paths == (
        "/capabilities/await_external/resume",
        "/capabilities/await_external/timeout_seconds",
    )
    assert migrate_v2_to_v3_preview(lowering_report.source_document) == preview_document


def test_shadowed_await_presentation_rehydrates_capability_ownership() -> None:
    preview_document = _preview_with_external_wait()
    preview_document["steps"][1]["prompt"] = {"text": "Path-owned instructions."}
    preview_document["v2_passthrough"]["entries"].insert(
        0,
        {
            "source_path": "/capabilities/await_external/prompt",
            "category": "shadowed_await_presentation",
            "disposition": "compatibility_only",
            "source_fragment": {"text": "Capability-owned instructions."},
        },
    )

    lowering_report = lower_v3_preview_to_v2_report(preview_document)

    assert lowering_report.source_document["path"][1]["prompt"] == {
        "text": "Path-owned instructions."
    }
    assert lowering_report.source_document["capabilities"]["await_external"]["prompt"] == {
        "text": "Capability-owned instructions."
    }
    assert migrate_v2_to_v3_preview(lowering_report.source_document) == preview_document


def test_unused_domain_names_are_reserved_before_native_name_synthesis() -> None:
    preview_document = _minimal_preview()
    preview_document["v2_passthrough"] = {
        "contract": "flowspec2/v3-preview-loss-policy@1",
        "entries": [
            {
                "source_path": "/domains/domain_answer",
                "category": "unused_domain_declaration",
                "disposition": "compatibility_only",
                "source_fragment": {"type": "free_text"},
            },
            {
                "source_path": "/slots/unused_answer",
                "category": "unused_slot_declaration",
                "disposition": "compatibility_only",
                "source_fragment": {"domain": "domain_answer"},
            },
        ],
    }

    lowering_report = lower_v3_preview_to_v2_report(preview_document)

    assert lowering_report.source_document["slots"]["answer"]["domain"] == "domain_answer_2"
    assert lowering_report.source_document["slots"]["unused_answer"]["domain"] == ("domain_answer")
    assert migrate_v2_to_v3_preview(lowering_report.source_document) == preview_document


def test_excluded_loss_entry_rejects_before_lowering() -> None:
    preview_document = _preview_with_external_wait()
    preview_document["v2_passthrough"]["entries"].insert(
        2,
        {
            "source_path": "/path/1/interactive/gate",
            "category": "legacy_interactive_gate",
            "disposition": "excluded",
            "source_fragment": "legacy_gate",
        },
    )

    with pytest.raises(V3PreviewLoweringError) as lowering_error:
        lower_v3_preview_to_v2(preview_document)

    assert lowering_error.value.diagnostics[0].code == "FLOWSPEC3_LOWERING_EXCLUDED_LOSS"


def test_lowering_checks_the_selected_runtime_profile() -> None:
    preview_document = _preview_with_external_wait()
    reference = reference_profile()
    restricted_profile = FlowProfile(
        identifier="test/no-capabilities@1",
        tools=reference.tools,
        subflows=reference.subflows,
        capabilities=frozenset(),
        domain_types=reference.domain_types,
    )

    with pytest.raises(V3PreviewLoweringError) as lowering_error:
        lower_v3_preview_to_v2(preview_document, profile=restricted_profile)

    assert "FLOWSPEC_PROFILE_CAPABILITY_UNAVAILABLE" in {
        diagnostic.code for diagnostic in lowering_error.value.diagnostics
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("custom_label", "FLOWSPEC3_LOWERING_CUSTOM_OPTION_LABEL"),
        ("flow_ui", "FLOWSPEC3_LOWERING_UNSUPPORTED_FLOW_UI"),
        ("composite_binding", "FLOWSPEC3_LOWERING_COMPOSITE_BINDING_LITERAL"),
        ("ambiguous_binding", "FLOWSPEC3_LOWERING_AMBIGUOUS_BINDING_LITERAL"),
        ("ambiguous_derive", "FLOWSPEC3_LOWERING_AMBIGUOUS_DERIVE_DEFAULT"),
        ("non_string_derive", "FLOWSPEC3_LOWERING_NON_STRING_DERIVE_VALUE"),
        ("nested_reference", "FLOWSPEC3_LOWERING_NESTED_STATE_REFERENCE"),
        ("unordered_aliases", "FLOWSPEC3_LOWERING_UNORDERED_ALIASES"),
        ("missing_domains", "FLOWSPEC3_LOWERING_DOMAINS_REQUIRED"),
    ],
)
def test_authored_non_lowerable_surfaces_have_specific_diagnostics(
    mutation: str, expected_code: str
) -> None:
    preview_document = _minimal_preview()
    if mutation == "custom_label":
        preview_document["steps"][0]["domain"]["options"][0]["label"] = "Custom label"
    elif mutation == "flow_ui":
        preview_document["steps"][0]["ui"] = {
            "kind": "flow",
            "field": "answer_flow",
            "meta_flow_ref": "answer_flow_id",
        }
    elif mutation in {"composite_binding", "ambiguous_binding"}:
        preview_document = _preview_with_external_wait(include_loss_entries=False)
        preview_document["steps"][1]["on_resume"] = {
            "set": {
                "resumed_answer": {"nested": True}
                if mutation == "composite_binding"
                else "$token.literal"
            }
        }
    elif mutation in {"ambiguous_derive", "non_string_derive"}:
        preview_document["steps"].append(
            {
                "derive": "derived_answer",
                "from": [{"$slot": "answer"}],
                "lookup": {"yes": 1 if mutation == "non_string_derive" else "yes"},
                "default": "$from[0]",
            }
        )
    elif mutation == "nested_reference":
        preview_document["steps"][0]["ask_when"] = {"is_present": {"$internal": "nested.value"}}
    elif mutation == "unordered_aliases":
        preview_document["steps"][0]["domain"]["options"][0]["aliases"] = ["z", "a"]
    elif mutation == "missing_domains":
        preview_document["steps"] = [{"use": "address@1"}]

    with pytest.raises(V3PreviewLoweringError) as lowering_error:
        lower_v3_preview_to_v2(preview_document)

    assert lowering_error.value.diagnostics[0].code == expected_code


def test_namespaced_predicate_literal_round_trips_through_v2_escape_wrapper() -> None:
    preview_document = _minimal_preview()
    preview_document["steps"][0]["ask_when"] = {"eq": ["slots.literal", "slots.literal"]}

    lowered_document = lower_v3_preview_to_v2(preview_document)

    assert lowered_document["path"][0]["ask_when"] == {
        "eq": [{"literal": "slots.literal"}, {"literal": "slots.literal"}]
    }
    assert migrate_v2_to_v3_preview(lowered_document) == preview_document


def _minimal_preview() -> dict[str, Any]:
    return {
        "schema": "flowspec/3-draft",
        "flow": "lowering_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise deterministic lowering."},
        "steps": [
            {
                "collect": "answer",
                "id": "collect_answer",
                "domain": {
                    "type": "categorical",
                    "options": [{"value": "yes", "label": "yes", "aliases": ["y"]}],
                },
                "prompt": {"text": "Answer yes."},
            }
        ],
    }


def _preview_with_external_wait(*, include_loss_entries: bool = True) -> dict[str, Any]:
    preview_document = _minimal_preview()
    preview_document["steps"].append(
        {
            "await": "cta_url",
            "id": "wait_answer",
            "resume_on": "answer_token",
            "ui": {
                "kind": "cta_url",
                "field": "answer_token",
                "out_of_band": True,
                "next_step": {"$step": "wait_answer"},
            },
            "timeout": {"goto": "END"},
        }
    )
    if include_loss_entries:
        preview_document["v2_passthrough"] = {
            "contract": "flowspec2/v3-preview-loss-policy@1",
            "entries": [
                {
                    "source_path": "/capabilities/await_external/resume",
                    "category": "await_resume_contract",
                    "disposition": "compatibility_only",
                    "source_fragment": _resume_contract(),
                },
                {
                    "source_path": "/capabilities/await_external/timeout_seconds",
                    "category": "await_timeout_duration",
                    "disposition": "compatibility_only",
                    "source_fragment": 300,
                },
            ],
        }
    return preview_document


def _resume_contract() -> dict[str, Any]:
    return {
        "version": "1",
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {"id": {"type": "string", "minLength": 1}},
            "required": ["id"],
        },
        "correlation": "$token.id",
        "duplicate": "ignore",
        "late": "reject",
    }
