"""Contract tests for the bounded Rasa CALM compatibility profile."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from flowspec2.compat.models import CompatibilityError
from flowspec2.compat.rasa import (
    RASA_FORMAT,
    RASA_MINIMUM_VERSION,
    RASA_PROFILE_VERSION,
    RasaBundle,
    export_rasa,
    import_rasa,
)
from flowspec2.compat.yaml import load_yaml_mapping, loads_yaml_mapping
from flowspec2.compiler import compile_flow
from flowspec2.schema import validate_flow

RASA_FIXTURES = Path(__file__).parent / "fixtures" / "rasa"


def portable_flow() -> dict[str, Any]:
    """Return a fresh flowspec2 document entirely inside the portable subset."""

    return {
        "schema": "flowspec/2",
        "flow": "portable_report",
        "version": "1.0.0",
        "route": {"description": "Collect a category and confirmation."},
        "domains": {
            "Category": {"type": "categorical", "values": ["a=b", "lighting"]},
            "Confirmation": {"type": "bool"},
            "Details": {"type": "free_text"},
        },
        "slots": {
            "category": {"domain": "Category"},
            "details": {"domain": "Details"},
            "confirmed": {"domain": "Confirmation"},
        },
        "path": [
            {
                "step": "collect_category",
                "slot": "category",
                "prompt": {
                    "text": "Which category applies?",
                    "extract_hint": "One declared category token.",
                },
                "interactive": {
                    "kind": "buttons",
                    "field": "category",
                    "from_domain": "Category",
                },
            },
            {
                "step": "collect_details",
                "slot": "details",
                "prompt": {"text": "Describe the issue."},
            },
            {
                "step": "confirm_report",
                "slot": "confirmed",
                "prompt": {"text": "Do you confirm the report?"},
                "interactive": {
                    "kind": "buttons",
                    "field": "confirmed",
                    "from_domain": "Confirmation",
                },
            },
        ],
    }


def diagnostic_codes(compatibility_error: CompatibilityError) -> set[str]:
    return {diagnostic.code for diagnostic in compatibility_error.report.diagnostics}


def test_profile_identifiers_define_the_public_rasa_contract() -> None:
    assert RASA_PROFILE_VERSION == "1"
    assert RASA_FORMAT == "rasa-calm/1"
    assert RASA_MINIMUM_VERSION == "3.11.0"


def test_export_projects_linear_collection_profile() -> None:
    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(portable_flow())
    assert diagnostic_codes(raised_error.value) == {
        "RASA_FLOW_VERSION_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }

    conversion = export_rasa(portable_flow(), allow_lossy=True)

    exported_flow = conversion.artifact.flows["flows"]["portable_report"]
    assert {diagnostic.code for diagnostic in conversion.report.diagnostics} == {
        "RASA_FLOW_VERSION_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }
    assert exported_flow["description"] == "Collect a category and confirmation."
    assert exported_flow["run_pattern_completed"] is False
    assert exported_flow["persisted_slots"] == [
        "category",
        "details",
        "confirmed",
    ]
    assert exported_flow["steps"] == [
        {
            "collect": "category",
            "id": "collect_category",
            "description": "One declared category token.",
            "utter": "utter_ask_portable_report_category_1",
        },
        {
            "collect": "details",
            "id": "collect_details",
            "utter": "utter_ask_portable_report_details_2",
        },
        {
            "collect": "confirmed",
            "id": "confirm_report",
            "utter": "utter_ask_portable_report_confirmed_3",
        },
    ]
    exported_domain = conversion.artifact.domain
    assert exported_domain["version"] == "3.1"
    assert exported_domain["slots"]["details"] == {
        "type": "text",
        "mappings": [{"type": "from_llm"}],
    }
    assert exported_domain["responses"]["utter_ask_portable_report_category_1"][0]["buttons"][
        0
    ] == {
        "title": "a=b",
        "payload": "/SetSlots(category=a=b)",
    }
    assert exported_domain["responses"]["utter_ask_portable_report_confirmed_3"][0]["buttons"] == [
        {"title": "Sim", "payload": "/SetSlots(confirmed=true)"},
        {"title": "Não", "payload": "/SetSlots(confirmed=false)"},
    ]


def test_runtime_semantics_warning_covers_interruptions_and_repairs() -> None:
    conversion = export_rasa(portable_flow(), allow_lossy=True)

    semantics_diagnostic = next(
        diagnostic
        for diagnostic in conversion.report.diagnostics
        if diagnostic.code == "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED"
    )
    assert "interruption" in semantics_diagnostic.message
    assert "repair" in semantics_diagnostic.message


def test_export_aggregates_all_flowspec_schema_failures() -> None:
    invalid_flow = {
        "schema": "wrong",
        "flow": "Invalid-ID",
        "version": "invalid",
        "route": {},
        "domains": {},
        "path": [],
        "unexpected": True,
    }

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(invalid_flow, allow_lossy=True)

    schema_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "FLOWSPEC_INVALID_DOCUMENT"
    ]
    assert len(schema_diagnostics) >= 6
    assert len({diagnostic.source_path for diagnostic in schema_diagnostics}) >= 5


def test_export_treats_removed_interactive_gate_as_invalid_source() -> None:
    flow_document = portable_flow()
    flow_document["path"][0]["interactive"]["gate"] = {"literal": True}

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert diagnostic_codes(raised_error.value) == {"FLOWSPEC_INVALID_DOCUMENT"}


def test_rasa_bundle_is_deeply_immutable_by_defensive_copy() -> None:
    source_flows = {"flows": {"sample": {"description": "Sample", "steps": []}}}
    source_domain = {"version": "3.1", "slots": {}}
    bundle = RasaBundle(source_flows, source_domain)

    source_flows["flows"]["sample"]["description"] = "Changed outside"
    first_copy = bundle.flows
    first_copy["flows"]["sample"]["description"] = "Changed copy"

    assert bundle.flows["flows"]["sample"]["description"] == "Sample"
    with pytest.raises(FrozenInstanceError):
        bundle._flows_snapshot = "{}"  # type: ignore[misc]


def test_export_strict_policy_blocks_warnings_and_reports_all_findings() -> None:
    flow_document = portable_flow()
    flow_document["service"] = {"id": "18131"}
    flow_document["route"]["trigger_phrases"] = ["report a light"]
    flow_document["domains"]["Category"]["rows"] = [
        {"value": "lighting", "description": "Public lighting"}
    ]

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document)

    assert diagnostic_codes(raised_error.value) == {
        "RASA_FLOW_VERSION_UNSUPPORTED",
        "RASA_TRIGGER_PHRASES_UNSUPPORTED",
        "RASA_SERVICE_METADATA_UNSUPPORTED",
        "RASA_LIST_ROW_DESCRIPTIONS_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }
    assert all(
        diagnostic.severity == "warning" for diagnostic in raised_error.value.report.diagnostics
    )


def test_export_allow_lossy_never_permits_executable_contract_loss() -> None:
    flow_document = portable_flow()
    flow_document["domains"]["Details"]["optional"] = True
    flow_document["domains"]["Category"]["normalize"] = {"synonyms": {"lamp": "lighting"}}
    flow_document["slots"]["category"]["prefill_sources"] = ["trusted_source"]
    flow_document["slots"]["category"]["fill_only_when_asked"] = True
    flow_document["route"]["entry_args_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"category": {"enum": ["a=b", "lighting"]}},
    }
    flow_document["capabilities"] = {"media_out": ["location"]}

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert diagnostic_codes(raised_error.value) == {
        "RASA_FLOW_VERSION_UNSUPPORTED",
        "RASA_FILL_ONLY_WHEN_ASKED_UNSUPPORTED",
        "RASA_ENTRY_ARGUMENTS_UNSUPPORTED",
        "RASA_CAPABILITY_UNSUPPORTED",
        "RASA_NORMALIZATION_UNSUPPORTED",
        "RASA_OPTIONAL_EMPTY_TEXT_UNSUPPORTED",
        "RASA_PREFILL_SOURCES_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }
    assert {
        diagnostic.code
        for diagnostic in raised_error.value.report.blocking_diagnostics(allow_lossy=True)
    } == {
        "RASA_ENTRY_ARGUMENTS_UNSUPPORTED",
        "RASA_FILL_ONLY_WHEN_ASKED_UNSUPPORTED",
        "RASA_CAPABILITY_UNSUPPORTED",
        "RASA_NORMALIZATION_UNSUPPORTED",
        "RASA_OPTIONAL_EMPTY_TEXT_UNSUPPORTED",
        "RASA_PREFILL_SOURCES_UNSUPPORTED",
    }


def test_export_rejects_present_empty_entry_arguments_schema() -> None:
    flow_document = portable_flow()
    flow_document["route"]["entry_args_schema"] = {}

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    entry_argument_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_ENTRY_ARGUMENTS_UNSUPPORTED"
    ]
    assert len(entry_argument_diagnostics) == 1
    assert entry_argument_diagnostics[0].source_path == "$.route.entry_args_schema"


def test_export_control_flow_errors_block_even_when_lossy_is_allowed() -> None:
    flow_document = portable_flow()
    flow_document["config"] = {"max_attempts": 3}
    flow_document["path"][0]["ask_when"] = {"eq": ["slots.confirmed", True]}
    flow_document["capabilities"] = {
        "await_external": {"kind": "cta_url", "resume_on": "payment_token"}
    }

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert {
        "RASA_CONFIG_CONTROL_UNSUPPORTED",
        "RASA_STEP_CONTROL_UNSUPPORTED",
        "RASA_EXTERNAL_WAIT_UNSUPPORTED",
    }.issubset(diagnostic_codes(raised_error.value))
    assert len(raised_error.value.report.blocking_diagnostics(allow_lossy=True)) >= 3


def test_export_checks_transitive_subflow_tools_before_reporting_profile_gap() -> None:
    flow_document = portable_flow()
    flow_document["uses"] = [{"ref": "address@1", "with": {"required": False}}]
    flow_document["path"].insert(2, {"use": "address@1"})

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert "RASA_SUBFLOW_UNSUPPORTED" in diagnostic_codes(raised_error.value)
    assert "FLOWSPEC_NON_EXECUTABLE" not in diagnostic_codes(raised_error.value)


def test_export_terminal_requires_explicit_custom_action_adapter() -> None:
    flow_document = portable_flow()
    flow_document["path"].append({"terminal": True})
    flow_document["terminal"] = {
        "step": "submit_report",
        "tool": "action_submit_report",
        "idempotent": True,
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    }

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document)
    assert diagnostic_codes(raised_error.value) == {
        "RASA_ACTION_ADAPTER_REQUIRED",
        "RASA_FLOW_VERSION_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }

    conversion = export_rasa(flow_document, allow_lossy=True)
    assert conversion.artifact.flows["flows"]["portable_report"]["steps"][-1] == {
        "action": "action_submit_report",
        "id": "submit_report",
    }
    assert conversion.artifact.domain["actions"] == ["action_submit_report"]


def test_export_rejects_terminal_only_flow_that_cannot_be_reimported() -> None:
    flow_document = portable_flow()
    flow_document["slots"] = {}
    flow_document["path"] = [{"terminal": True}]
    flow_document["terminal"] = {
        "step": "submit_report",
        "tool": "action_submit_report",
        "idempotent": True,
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    }

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    no_collect_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_NO_PORTABLE_STEPS"
    ]
    assert len(no_collect_diagnostics) == 1
    assert no_collect_diagnostics[0].source_path == "$.path"


def test_export_rejects_confirmation_semantics_even_when_lossy_is_allowed() -> None:
    flow_document = portable_flow()
    flow_document["path"][2]["confirm"] = flow_document["path"][2].pop("slot")

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert "RASA_CONFIRM_SEMANTICS_UNSUPPORTED" in diagnostic_codes(raised_error.value)


def test_export_drops_all_buttons_when_any_setslots_value_is_not_encodable() -> None:
    flow_document = portable_flow()
    flow_document["domains"]["Category"]["values"] = [
        "a=b",
        "comma,value",
    ]

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document)
    assert diagnostic_codes(raised_error.value) == {
        "RASA_FLOW_VERSION_UNSUPPORTED",
        "RASA_SETSLOTS_VALUE_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }

    conversion = export_rasa(flow_document, allow_lossy=True)
    response = conversion.artifact.domain["responses"]["utter_ask_portable_report_category_1"][0]
    assert response == {"text": "Which category applies?"}


def test_export_rejects_empty_categorical_token_as_structurally_invalid() -> None:
    flow_document = portable_flow()
    flow_document["domains"]["Category"]["values"] = ["a=b", ""]

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert diagnostic_codes(raised_error.value) == {"FLOWSPEC_INVALID_DOCUMENT"}
    assert raised_error.value.report.diagnostics[0].source_path == "$.domains.Category"


def test_export_rejects_rasa_response_interpolation_in_text_and_buttons() -> None:
    flow_document = portable_flow()
    flow_document["path"][0]["prompt"]["text"] = "Which {category} applies?"
    flow_document["domains"]["Category"]["values"] = [
        "{dynamic_category}",
        "lighting",
    ]

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    interpolation_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED"
    ]
    assert len(interpolation_diagnostics) == 2
    assert {diagnostic.source_path for diagnostic in interpolation_diagnostics} == {
        "$.path[0].prompt.text",
        "$.path[0].interactive",
    }


def test_export_rejects_empty_prompt_before_emitting_unimportable_response() -> None:
    flow_document = portable_flow()
    flow_document["path"][0]["prompt"]["text"] = ""

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    response_text_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_RESPONSE_TEXT_REQUIRED"
    ]
    assert len(response_text_diagnostics) == 1
    assert response_text_diagnostics[0].source_path == "$.path[0].prompt.text"


def test_export_reports_slot_that_selected_flow_does_not_collect() -> None:
    flow_document = portable_flow()
    flow_document["slots"]["unused"] = {"domain": "Details"}

    conversion = export_rasa(flow_document, allow_lossy=True)

    assert "RASA_UNREFERENCED_SLOT_UNSUPPORTED" in {
        diagnostic.code for diagnostic in conversion.report.diagnostics
    }


def test_export_rejects_duplicate_derived_compiler_node_ids() -> None:
    flow_document = portable_flow()
    flow_document["path"][0].pop("step")
    flow_document["path"].insert(
        1,
        {
            "slot": "category",
            "prompt": {"text": "Choose the category again."},
        },
    )

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert "FLOWSPEC_NON_EXECUTABLE" in diagnostic_codes(raised_error.value)


def test_export_rejects_duplicate_explicit_step_ids() -> None:
    flow_document = portable_flow()
    flow_document["path"][1]["step"] = flow_document["path"][0]["step"]

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert {
        "FLOWSPEC_NON_EXECUTABLE",
        "RASA_DUPLICATE_STEP_ID",
    }.issubset(diagnostic_codes(raised_error.value))


def test_export_rejects_empty_step_id_as_structurally_invalid() -> None:
    flow_document = portable_flow()
    flow_document["path"][2]["step"] = ""

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert diagnostic_codes(raised_error.value) == {"FLOWSPEC_INVALID_DOCUMENT"}
    assert raised_error.value.report.diagnostics[0].source_path == "$.path[2]"


def test_export_rejects_forbidden_slot_name_as_structurally_invalid() -> None:
    flow_document = portable_flow()
    slot_declaration = flow_document["slots"].pop("category")
    flow_document["slots"]["category=bad"] = slot_declaration
    flow_document["path"][0]["slot"] = "category=bad"
    flow_document["path"][0]["interactive"]["field"] = "category=bad"

    with pytest.raises(CompatibilityError) as raised_error:
        export_rasa(flow_document, allow_lossy=True)

    assert diagnostic_codes(raised_error.value) == {"FLOWSPEC_INVALID_DOCUMENT"}


def test_import_portable_fixture_and_validate_generated_flowspec() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")

    conversion = import_rasa(
        flows_document,
        domain_document,
        flow_version="2.3.4",
        allow_lossy=True,
    )

    assert {diagnostic.code for diagnostic in conversion.report.diagnostics} == {
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED"
    }
    assert conversion.artifact["flow"] == "report_issue"
    assert conversion.artifact["version"] == "2.3.4"
    assert conversion.artifact["domains"]["rasa_category"]["values"] == [
        "a=b",
        "lighting",
    ]
    assert conversion.artifact["path"][0] == {
        "slot": "category",
        "step": "collect_category",
        "prompt": {
            "text": "Which category applies?",
            "extract_hint": "One of the declared category tokens.",
        },
        "interactive": {
            "kind": "buttons",
            "field": "category",
            "from_domain": "rasa_category",
        },
    }
    assert conversion.artifact["path"][2]["slot"] == "confirmed"
    assert conversion.artifact["path"][2]["interactive"] == {
        "kind": "buttons",
        "field": "confirmed",
        "from_domain": "rasa_confirmed",
    }
    validate_flow(conversion.artifact)
    assert compile_flow(conversion.artifact).entry_node_id == "__init__"


def test_portable_export_import_round_trip_preserves_executable_subset() -> None:
    exported = export_rasa(portable_flow(), allow_lossy=True).artifact

    imported = import_rasa(
        exported.flows,
        exported.domain,
        allow_lossy=True,
    ).artifact

    assert imported["flow"] == "portable_report"
    assert [step.get("slot", step.get("confirm")) for step in imported["path"]] == [
        "category",
        "details",
        "confirmed",
    ]
    assert imported["path"][0]["prompt"]["extract_hint"] == ("One declared category token.")
    assert imported["path"][2]["slot"] == "confirmed"


def test_import_requires_explicit_selection_for_multiple_flows() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    flows_document["flows"]["another_flow"] = {
        "description": "Another flow",
        "run_pattern_completed": False,
        "persisted_slots": ["details"],
        "steps": [{"collect": "details"}],
    }
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document)

    assert "RASA_FLOW_SELECTION_REQUIRED" in diagnostic_codes(raised_error.value)

    selected = import_rasa(
        flows_document,
        domain_document,
        flow_id="another_flow",
        allow_lossy=True,
    )
    assert selected.artifact["flow"] == "another_flow"


def test_import_ignores_unreferenced_global_domain_artifacts() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    domain_document["slots"]["unrelated_legacy_slot"] = {
        "type": "list",
        "mappings": [{"type": "from_entity", "entity": "legacy"}],
    }
    domain_document["responses"]["utter_unrelated"] = [
        {"text": "One"},
        {"text": "Two"},
    ]
    domain_document["actions"] = ["action_unrelated"]

    conversion = import_rasa(
        flows_document,
        domain_document,
        allow_lossy=True,
    )

    assert {diagnostic.code for diagnostic in conversion.report.diagnostics} == {
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED"
    }
    assert "unrelated_legacy_slot" not in conversion.artifact["slots"]


def test_import_validates_top_level_profile_properties() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    flows_document["metadata"] = {"owner": "outside-profile"}
    domain_document["intents"] = ["report_issue"]

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert {
        "RASA_FLOWS_DOCUMENT_PROPERTY_UNSUPPORTED",
        "RASA_DOMAIN_DOCUMENT_PROPERTY_UNSUPPORTED",
    }.issubset(diagnostic_codes(raised_error.value))


def test_import_version_diagnostics_identify_each_source_document() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    flows_document["version"] = "wrong"
    domain_document["version"] = "wrong"

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    version_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_DOCUMENT_VERSION_UNSUPPORTED"
    ]
    assert {diagnostic.source_path for diagnostic in version_diagnostics} == {
        "$.flows_document.version",
        "$.domain_document.version",
    }


def test_import_rejects_ask_before_filling_with_selected_flow_path() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    flows_document["flows"]["report_issue"]["steps"][2]["ask_before_filling"] = True

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    diagnostic = next(
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_ASK_BEFORE_FILLING_UNSUPPORTED"
    )
    assert diagnostic.source_path == ("$.flows.report_issue.steps[2].ask_before_filling")


def test_import_rejects_non_utter_response_key() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    category_response = domain_document["responses"].pop("utter_ask_report_category")
    domain_document["responses"]["ask_report_category"] = category_response
    flows_document["flows"]["report_issue"]["steps"][0]["utter"] = "ask_report_category"

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert "RASA_RESPONSE_KEY_UNSUPPORTED" in diagnostic_codes(raised_error.value)


def test_import_rejects_response_interpolation_in_text_and_buttons() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    category_response = domain_document["responses"]["utter_ask_report_category"][0]
    category_response["text"] = "Which {category} applies?"
    category_response["buttons"][0] = {
        "title": "{dynamic_category}",
        "payload": "/SetSlots(category={dynamic_category})",
    }

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    interpolation_diagnostics = [
        diagnostic
        for diagnostic in raised_error.value.report.diagnostics
        if diagnostic.code == "RASA_RESPONSE_INTERPOLATION_UNSUPPORTED"
    ]
    assert len(interpolation_diagnostics) == 2
    assert {diagnostic.source_path for diagnostic in interpolation_diagnostics} == {
        "$.responses.utter_ask_report_category[0].text",
        "$.responses.utter_ask_report_category[0].buttons[0]",
    }


def test_import_rejects_duplicate_and_empty_explicit_step_ids() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    selected_steps = flows_document["flows"]["report_issue"]["steps"]
    selected_steps[1]["id"] = selected_steps[0]["id"]
    selected_steps[2]["id"] = ""

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert {
        "RASA_DUPLICATE_STEP_ID",
        "RASA_STEP_ID_INVALID",
        "RASA_GENERATED_FLOWSPEC_NON_EXECUTABLE",
    }.issubset(diagnostic_codes(raised_error.value))


def test_import_rejects_duplicate_derived_compiler_node_ids() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    selected_steps = flows_document["flows"]["report_issue"]["steps"]
    selected_steps[0].pop("id")
    selected_steps.insert(
        1,
        {
            "collect": "category",
            "utter": "utter_ask_report_category",
        },
    )

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert "RASA_GENERATED_FLOWSPEC_NON_EXECUTABLE" in diagnostic_codes(raised_error.value)


def test_import_requires_persistence_to_match_flowspec_data_lifecycle() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    del flows_document["flows"]["report_issue"]["persisted_slots"]

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document)
    assert diagnostic_codes(raised_error.value) == {
        "RASA_COLLECTED_SLOT_RESET_UNSUPPORTED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }

    conversion = import_rasa(
        flows_document,
        domain_document,
        allow_lossy=True,
    )
    assert conversion.artifact["slots"]["category"]["persist"] == "data"


def test_import_rejects_persisted_slot_not_collected_by_selected_flow() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    flows_document["flows"]["report_issue"]["persisted_slots"].append("other")

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert "RASA_PERSISTED_SLOT_NOT_COLLECTED" in diagnostic_codes(raised_error.value)


def test_import_aggregates_control_mapping_response_and_step_errors() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "unsupported_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "unsupported_domain.yml")

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert {
        "RASA_FLOW_CONTROL_UNSUPPORTED",
        "RASA_SLOT_MAPPING_UNSUPPORTED",
        "RASA_STEP_CONTROL_UNSUPPORTED",
        "RASA_RESPONSE_PROPERTY_UNSUPPORTED",
        "RASA_ACTION_ADAPTER_REQUIRED",
    }.issubset(diagnostic_codes(raised_error.value))
    assert all(
        diagnostic.severity == "error"
        for diagnostic in raised_error.value.report.blocking_diagnostics(allow_lossy=True)
    )


def test_import_reports_generated_flowspec_validation_with_other_errors() -> None:
    flows_document = {
        "flows": {
            "action_only": {
                "description": "Only a custom action.",
                "run_pattern_completed": False,
                "steps": [{"action": "action_submit"}],
            }
        }
    }
    domain_document = {"version": "3.1", "slots": {}, "responses": {}}

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert {
        "RASA_ACTION_ADAPTER_REQUIRED",
        "RASA_GENERATED_FLOWSPEC_INVALID",
    }.issubset(diagnostic_codes(raised_error.value))


def test_import_final_action_uses_explicit_conservative_terminal_defaults() -> None:
    flows_document = {
        "flows": {
            "action_flow": {
                "description": "Collect and submit.",
                "run_pattern_completed": False,
                "persisted_slots": ["details"],
                "steps": [
                    {"collect": "details"},
                    {"action": "action_submit"},
                ],
            }
        }
    }
    domain_document = {
        "version": "3.1",
        "slots": {"details": {"type": "text"}},
        "responses": {"utter_ask_details": [{"text": "Details?"}]},
        "actions": ["action_submit"],
    }

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document)
    assert diagnostic_codes(raised_error.value) == {
        "RASA_ACTION_ADAPTER_REQUIRED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }

    conversion = import_rasa(
        flows_document,
        domain_document,
        allow_lossy=True,
    )
    assert conversion.artifact["path"][-1] == {"terminal": True}
    assert conversion.artifact["terminal"] == {
        "step": "action_submit",
        "tool": "action_submit",
        "idempotent": False,
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": False},
            "fatal": {"reset_next": True},
        },
    }


def test_terminal_bundle_round_trip_keeps_action_with_adapter_warning() -> None:
    flow_document = portable_flow()
    flow_document["path"].append({"terminal": True})
    flow_document["terminal"] = {
        "step": "submit_report",
        "tool": "action_submit_report",
        "idempotent": True,
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    }

    exported = export_rasa(flow_document, allow_lossy=True).artifact
    imported = import_rasa(
        exported.flows,
        exported.domain,
        allow_lossy=True,
    )

    assert imported.artifact["terminal"]["step"] == "submit_report"
    assert imported.artifact["terminal"]["tool"] == "action_submit_report"
    assert imported.artifact["terminal"]["idempotent"] is False
    assert {diagnostic.code for diagnostic in imported.report.diagnostics} == {
        "RASA_ACTION_ADAPTER_REQUIRED",
        "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED",
    }


def test_import_rejects_non_final_custom_action() -> None:
    flows_document = {
        "flows": {
            "action_flow": {
                "description": "Run an action before collection.",
                "run_pattern_completed": False,
                "persisted_slots": ["details"],
                "steps": [
                    {"action": "action_prepare"},
                    {"collect": "details"},
                ],
            }
        }
    }
    domain_document = {
        "version": "3.1",
        "slots": {"details": {"type": "text"}},
        "responses": {"utter_ask_details": [{"text": "Details?"}]},
        "actions": ["action_prepare"],
    }

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert "RASA_ACTION_POSITION_UNSUPPORTED" in diagnostic_codes(raised_error.value)


def test_import_button_title_loss_is_explicit_and_drops_interactive() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    domain_document["responses"]["utter_ask_report_category"][0]["buttons"][0]["title"] = (
        "Human label"
    )

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document)
    assert "RASA_BUTTON_TITLE_UNSUPPORTED" in diagnostic_codes(raised_error.value)

    conversion = import_rasa(
        flows_document,
        domain_document,
        allow_lossy=True,
    )
    assert "interactive" not in conversion.artifact["path"][0]


def test_import_rejects_multiple_or_conditional_response_variations() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    domain_document["responses"]["utter_ask_details"].append({"text": "Describe it another way."})

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert "RASA_RESPONSE_VARIATIONS_UNSUPPORTED" in diagnostic_codes(raised_error.value)


def test_import_button_payload_allows_equals_only_in_value() -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")

    conversion = import_rasa(
        flows_document,
        domain_document,
        allow_lossy=True,
    )

    assert conversion.artifact["domains"]["rasa_category"]["values"][0] == "a=b"
    assert conversion.artifact["path"][0]["interactive"]["field"] == "category"


def test_import_preserves_unquoted_yaml_1_2_string_tokens() -> None:
    flows_document = loads_yaml_mapping(
        "flows:\n"
        "  choose_token:\n"
        "    description: Choose one token.\n"
        "    run_pattern_completed: false\n"
        "    persisted_slots: [choice]\n"
        "    steps:\n"
        "      - collect: choice\n"
    )
    domain_document = loads_yaml_mapping(
        'version: "3.1"\n'
        "slots:\n"
        "  choice:\n"
        "    type: categorical\n"
        "    values: [yes, no, on, off, 12:34]\n"
        "responses:\n"
        "  utter_ask_choice:\n"
        "    - text: Choose one token.\n"
    )

    conversion = import_rasa(
        flows_document,
        domain_document,
        allow_lossy=True,
    )

    assert conversion.artifact["domains"]["rasa_choice"]["values"] == [
        "yes",
        "no",
        "on",
        "off",
        "12:34",
    ]


@pytest.mark.parametrize("completion_value", [None, True])
def test_import_rejects_default_or_enabled_rasa_completion_pattern(
    completion_value: bool | None,
) -> None:
    flows_document = load_yaml_mapping(RASA_FIXTURES / "portable_flows.yml")
    domain_document = load_yaml_mapping(RASA_FIXTURES / "portable_domain.yml")
    if completion_value is None:
        del flows_document["flows"]["report_issue"]["run_pattern_completed"]
    else:
        flows_document["flows"]["report_issue"]["run_pattern_completed"] = completion_value

    with pytest.raises(CompatibilityError) as raised_error:
        import_rasa(flows_document, domain_document, allow_lossy=True)

    assert "RASA_COMPLETION_PATTERN_UNSUPPORTED" in diagnostic_codes(raised_error.value)
