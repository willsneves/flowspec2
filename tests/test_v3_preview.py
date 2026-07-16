from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

import flowspec2.experimental.v3_preview as v3_preview_module
from flowspec2 import load_flow
from flowspec2.experimental import (
    V3PreviewDocumentMode,
    V3PreviewLossCategory,
    V3PreviewLossDisposition,
    V3PreviewLossHandling,
    V3PreviewMigrationError,
    V3PreviewValidationError,
    check_v3_preview,
    compact_json_bytes,
    migrate_v2_to_v3_preview,
    migrate_v2_to_v3_preview_report,
    preview_loss_policy,
    preview_schema,
    validate_v3_preview,
)

EXAMPLES_DIRECTORY = Path(__file__).resolve().parents[1] / "examples"


def _step_with_kind(preview_document: dict[str, Any], step_kind: str) -> dict[str, Any]:
    return next(
        preview_step for preview_step in preview_document["steps"] if step_kind in preview_step
    )


def _loss_entries_by_path(preview_document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        loss_entry["source_path"]: loss_entry
        for loss_entry in preview_document.get("v2_passthrough", {}).get("entries", [])
    }


@pytest.mark.parametrize(
    "example_filename",
    ["pothole_repair.flow.json", "streetlight_repair.flow.json"],
)
def test_real_v2_examples_migrate_to_schema_valid_previews(example_filename: str) -> None:
    source_document = load_flow(EXAMPLES_DIRECTORY / example_filename)

    preview_document = migrate_v2_to_v3_preview(source_document)

    validate_v3_preview(preview_document, mode=V3PreviewDocumentMode.V2_MIGRATION)
    assert preview_document["schema"] == "flowspec/3-draft"
    assert "domains" not in preview_document
    assert "slots" not in preview_document
    assert "path" not in preview_document


def test_migration_is_pure_deterministic_and_report_is_immutable(
    streetlight_document: dict[str, Any],
) -> None:
    source_snapshot = copy.deepcopy(streetlight_document)

    first_report = migrate_v2_to_v3_preview_report(streetlight_document)
    second_report = migrate_v2_to_v3_preview_report(streetlight_document)

    assert streetlight_document == source_snapshot
    assert first_report == second_report
    mutable_preview = first_report.preview_document
    mutable_preview["flow"] = "changed_by_caller"
    assert first_report.preview_document["flow"] == "streetlight_repair"
    mutable_fragment = first_report.loss_entries[0].source_fragment
    assert isinstance(mutable_fragment, dict)
    mutable_fragment["changed_by_caller"] = True
    assert "changed_by_caller" not in first_report.loss_entries[0].source_fragment


def test_migration_inlines_stable_options_and_typed_references(
    streetlight_document: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    collect_defect = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("collect") == "streetlight_issue"
    )
    not_working_option = next(
        domain_option
        for domain_option in collect_defect["domain"]["options"]
        if domain_option["value"] == "Not working"
    )
    derive_step = _step_with_kind(preview_document, "derive")
    submit_step = _step_with_kind(preview_document, "submit")

    assert not_working_option == {
        "value": "Not working",
        "label": "Not working",
        "aliases": ["burned out", "does not turn on", "no light"],
        "description": "The streetlight does not turn on",
    }
    assert derive_step["from"][0] == {"$slot": "streetlight_issue"}
    assert derive_step["default"] == {"$slot": "streetlight_issue"}
    assert submit_step["input"]["streetlightIssue"] == {"$derive": "classified_streetlight_issue"}
    assert submit_step["outputs"]["protocol_id"] == {"$result": "protocol_id"}


def test_migration_localizes_subflow_confirmation_derive_and_submit(
    streetlight_document: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    preview_steps = preview_document["steps"]
    address_use = next(
        preview_step for preview_step in preview_steps if preview_step.get("use") == "address@1"
    )
    confirmation_hub = next(
        preview_step
        for preview_step in preview_steps
        if preview_step.get("confirm") == "ticket_data_confirmed"
    )
    derive_index = next(
        step_index
        for step_index, preview_step in enumerate(preview_steps)
        if "derive" in preview_step
    )
    localization_index = next(
        step_index
        for step_index, preview_step in enumerate(preview_steps)
        if preview_step.get("collect") == "streetlight_location"
    )

    assert address_use["with"] == {
        "required": True,
        "needs_confirmation": True,
        "max_attempts": 3,
    }
    assert confirmation_hub["correctable"][0] == {"$slot": "streetlight_issue"}
    assert confirmation_hub["on_confirm"] == {"$step": "open_ticket"}
    assert derive_index == localization_index + 1
    assert preview_steps[-1]["submit"] == "open_service_request"


def test_non_native_v2_fragments_are_preserved_and_reported(
    streetlight_document: dict[str, Any],
) -> None:
    migration_report = migrate_v2_to_v3_preview_report(streetlight_document)
    preview_document = migration_report.preview_document
    loss_entries = _loss_entries_by_path(preview_document)
    report_entries = {
        loss_entry.source_path: loss_entry for loss_entry in migration_report.loss_entries
    }

    assert report_entries["/entry"].category is V3PreviewLossCategory.ENTRY_CONTRACT
    assert report_entries["/auto_flow"].category is V3PreviewLossCategory.AUTOMATIC_FLOW_CONTRACT
    assert (
        report_entries["/capabilities/await_external"].category
        is V3PreviewLossCategory.IMPLICIT_AWAIT_CAPABILITY
    )
    assert loss_entries["/entry"]["source_fragment"] == streetlight_document["entry"]
    assert loss_entries["/auto_flow"]["source_fragment"] == streetlight_document["auto_flow"]
    assert (
        loss_entries["/capabilities/await_external"]["source_fragment"]
        == (streetlight_document["capabilities"]["await_external"])
    )
    for capability_name, capability_definition in streetlight_document["capabilities"].items():
        capability_path = f"/capabilities/{capability_name}"
        if capability_name == "await_external":
            continue
        assert report_entries[capability_path].category is V3PreviewLossCategory.AGENT_CAPABILITY
        assert loss_entries[capability_path]["source_fragment"] == capability_definition
    native_v2_prefixes = (
        "/domains",
        "/slots",
        "/path",
        "/uses",
        "/confirm",
        "/derive",
        "/terminal",
        "/overrides",
    )
    assert not any(
        loss_entry.source_path.startswith(native_v2_prefixes)
        for loss_entry in migration_report.loss_entries
    )


def test_compact_byte_report_is_neutral_measurement(
    streetlight_document: dict[str, Any],
) -> None:
    migration_report = migrate_v2_to_v3_preview_report(streetlight_document)
    compact_measurements = migration_report.compact_bytes

    assert compact_measurements.source_bytes == compact_json_bytes(streetlight_document)
    assert compact_measurements.preview_bytes == compact_json_bytes(
        migration_report.preview_document
    )
    assert compact_measurements.delta_bytes == (
        compact_measurements.preview_bytes - compact_measurements.source_bytes
    )
    assert compact_measurements.as_dict() == {
        "source_bytes": compact_measurements.source_bytes,
        "preview_bytes": compact_measurements.preview_bytes,
        "delta_bytes": compact_measurements.delta_bytes,
    }


def test_explicit_external_wait_becomes_a_local_typed_step() -> None:
    source_document = _external_wait_source()

    preview_document = migrate_v2_to_v3_preview(source_document)
    await_step = _step_with_kind(preview_document, "await")

    assert await_step["await"] == "cta_url"
    assert await_step["id"] == "wait_payment"
    assert await_step["on_resume"]["set"]["payment_id"] == {"$token": "payment.id"}
    assert await_step["on_resume"]["enrich"]["input"]["payment_id"] == {"$token": "payment.id"}
    assert await_step["on_resume"]["enrich"]["set"]["receipt_code"] == {"$result": "receipt.code"}
    assert await_step["timeout"]["goto"] == {"$step": "wait_payment"}
    loss_entries = _loss_entries_by_path(preview_document)
    assert "/capabilities/await_external" not in loss_entries
    assert loss_entries["/capabilities/await_external/resume"] == {
        "source_path": "/capabilities/await_external/resume",
        "category": "await_resume_contract",
        "disposition": "compatibility_only",
        "source_fragment": source_document["capabilities"]["await_external"]["resume"],
    }
    assert loss_entries["/capabilities/await_external/timeout_seconds"] == {
        "source_path": "/capabilities/await_external/timeout_seconds",
        "category": "await_timeout_duration",
        "disposition": "compatibility_only",
        "source_fragment": 300,
    }
    assert loss_entries["/domains/Placeholder"]["category"] == "unused_domain_declaration"


def test_shadowed_await_presentation_is_classified_for_rehydration() -> None:
    source_document = _external_wait_source()
    source_document["path"][0]["prompt"] = {
        "text": "Path-owned payment instructions.",
        "verbatim": True,
    }

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    loss_entries = {
        loss_entry.source_path: loss_entry for loss_entry in migration_report.loss_entries
    }
    prompt_entry = loss_entries["/capabilities/await_external/prompt"]

    assert prompt_entry.category is V3PreviewLossCategory.SHADOWED_AWAIT_PRESENTATION
    assert prompt_entry.handling is V3PreviewLossHandling.REHYDRATE
    assert (
        prompt_entry.source_fragment == source_document["capabilities"]["await_external"]["prompt"]
    )


def test_mismatched_interactive_domain_is_classified_as_excluded(
    streetlight_document: dict[str, Any],
) -> None:
    streetlight_document["path"][1]["interactive"]["from_domain"] = "YesNo"

    migration_report = migrate_v2_to_v3_preview_report(streetlight_document)
    mismatch_entry = next(
        loss_entry
        for loss_entry in migration_report.loss_entries
        if loss_entry.source_path == "/path/1/interactive/from_domain"
    )

    assert mismatch_entry.category is V3PreviewLossCategory.MISMATCHED_INTERACTIVE_DOMAIN
    assert mismatch_entry.disposition is V3PreviewLossDisposition.EXCLUDED
    assert mismatch_entry.handling is V3PreviewLossHandling.REJECT


def test_unused_v2_declarations_receive_specific_loss_categories() -> None:
    source_document = _external_wait_source()
    source_document["slots"] = {"unused_answer": {"domain": "Placeholder"}}

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    categories_by_path = {
        loss_entry.source_path: loss_entry.category for loss_entry in migration_report.loss_entries
    }

    assert categories_by_path["/domains/Placeholder"] is (
        V3PreviewLossCategory.UNUSED_DOMAIN_DECLARATION
    )
    assert categories_by_path["/slots/unused_answer"] is (
        V3PreviewLossCategory.UNUSED_SLOT_DECLARATION
    )


def test_duplicate_terminal_parameter_is_classified_as_excluded() -> None:
    source_document = _external_wait_source()
    source_document["slots"] = {"payment_reference": {"domain": "Placeholder"}}
    source_document["path"].insert(
        0,
        {
            "step": "collect_payment_reference",
            "slot": "payment_reference",
            "prompt": {"text": "Provide the payment reference."},
        },
    )
    source_document["terminal"]["input"] = [
        {"param": "paymentReference", "slot": "payment_reference"},
        {"param": "paymentReference", "slot": "payment_reference"},
    ]

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    duplicate_entry = next(
        loss_entry
        for loss_entry in migration_report.loss_entries
        if loss_entry.source_path == "/terminal/input/0"
    )

    assert duplicate_entry.category is V3PreviewLossCategory.DUPLICATE_TERMINAL_INPUT_PARAMETER
    assert duplicate_entry.handling is V3PreviewLossHandling.REJECT


def test_loss_registration_rejects_a_repeated_source_path() -> None:
    migration_context = v3_preview_module._new_migration_context(_external_wait_source())
    v3_preview_module._record_loss(
        migration_context,
        V3PreviewLossCategory.ENTRY_CONTRACT,
        "/entry",
        {"slot": "answer"},
    )

    with pytest.raises(V3PreviewMigrationError, match="classified more than once"):
        v3_preview_module._record_loss(
            migration_context,
            V3PreviewLossCategory.ENTRY_CONTRACT,
            "/entry",
            {"slot": "answer"},
        )


def test_legacy_await_enrichment_is_accounted_instead_of_kept_ambiguous() -> None:
    source_document = _external_wait_source()
    source_document["capabilities"]["await_external"]["on_resume"]["enrich"] = "lookup_receipt"

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    preview_document = migration_report.preview_document
    await_step = _step_with_kind(preview_document, "await")
    enrichment_pointer = "/capabilities/await_external/on_resume/enrich"

    assert "enrich" not in await_step["on_resume"]
    report_entries = {
        loss_entry.source_path: loss_entry for loss_entry in migration_report.loss_entries
    }
    assert (
        report_entries[enrichment_pointer].category is V3PreviewLossCategory.LEGACY_AWAIT_ENRICHMENT
    )
    assert report_entries[enrichment_pointer].handling is V3PreviewLossHandling.REJECT
    assert report_entries[enrichment_pointer].disposition is V3PreviewLossDisposition.EXCLUDED
    assert _loss_entries_by_path(preview_document)[enrichment_pointer]["source_fragment"] == (
        "lookup_receipt"
    )


def test_legacy_interactive_gate_is_loss_accounted_not_promoted() -> None:
    source_document = _external_wait_source()
    source_document["capabilities"]["await_external"]["interactive"]["gate"] = "payment_cta_enabled"

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    preview_document = migration_report.preview_document
    await_step = _step_with_kind(preview_document, "await")
    gate_pointer = "/capabilities/await_external/interactive/gate"

    assert "gate" not in await_step["ui"]
    report_entries = {
        loss_entry.source_path: loss_entry for loss_entry in migration_report.loss_entries
    }
    assert report_entries[gate_pointer].category is V3PreviewLossCategory.LEGACY_INTERACTIVE_GATE
    assert report_entries[gate_pointer].handling is V3PreviewLossHandling.REJECT
    assert report_entries[gate_pointer].disposition is V3PreviewLossDisposition.EXCLUDED
    assert _loss_entries_by_path(preview_document)[gate_pointer]["source_fragment"] == (
        "payment_cta_enabled"
    )


def test_preview_schema_is_closed_and_references_are_single_key(
    streetlight_document: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    unexpected_top_level = copy.deepcopy(preview_document)
    unexpected_top_level["capabilities"] = {}
    unexpected_step_field = copy.deepcopy(preview_document)
    unexpected_step_field["steps"][1]["domain_name"] = "StreetlightIssue"
    malformed_reference = copy.deepcopy(preview_document)
    derive_step = _step_with_kind(malformed_reference, "derive")
    derive_step["from"][0]["label"] = "ambiguous"

    with pytest.raises(jsonschema.ValidationError):
        validate_v3_preview(unexpected_top_level)
    with pytest.raises(jsonschema.ValidationError):
        validate_v3_preview(unexpected_step_field)
    with pytest.raises(jsonschema.ValidationError):
        validate_v3_preview(malformed_reference)


def test_preview_boolean_domain_requires_canonical_option_values(
    streetlight_document: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    confirmation_step = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("confirm") == "service_confirmed"
    )
    confirmation_step["domain"]["options"][0]["value"] = "yes"

    with pytest.raises(jsonschema.ValidationError):
        validate_v3_preview(preview_document)


def test_schema_asset_is_valid_draft_2020_12() -> None:
    experimental_schema = preview_schema()

    jsonschema.Draft202012Validator.check_schema(experimental_schema)
    assert experimental_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert experimental_schema["properties"]["schema"]["const"] == "flowspec/3-draft"


def test_loss_policy_is_closed_and_matches_public_enums() -> None:
    policy_document = preview_loss_policy()
    category_documents = policy_document["categories"]

    assert set(category_documents) == {category.value for category in V3PreviewLossCategory}
    assert {
        category_document["disposition"] for category_document in category_documents.values()
    } == {disposition.value for disposition in V3PreviewLossDisposition}
    assert {category_document["handling"] for category_document in category_documents.values()} == {
        handling.value for handling in V3PreviewLossHandling
    }
    assert all(
        category_document["source_path_pattern"]
        for category_document in category_documents.values()
    )


def test_authored_preview_rejects_migration_loss_accounting(
    streetlight_document: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)

    with pytest.raises(V3PreviewValidationError) as validation_error:
        validate_v3_preview(preview_document)

    assert validation_error.value.diagnostics[0].code == "FLOWSPEC3_AUTHORED_LOSS_ACCOUNTING"
    validate_v3_preview(preview_document, mode=V3PreviewDocumentMode.V2_MIGRATION)


@pytest.mark.parametrize("validation_function", [validate_v3_preview, check_v3_preview])
def test_preview_validation_rejects_unknown_document_mode(validation_function: Any) -> None:
    with pytest.raises(ValueError, match="not_a_mode"):
        validation_function(_minimal_preview(), mode=cast(Any, "not_a_mode"))


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("unknown_category", "FLOWSPEC3_UNKNOWN_LOSS_CATEGORY"),
        ("disposition_mismatch", "FLOWSPEC3_LOSS_DISPOSITION_MISMATCH"),
        ("source_path_mismatch", "FLOWSPEC3_LOSS_SOURCE_PATH_MISMATCH"),
        ("unordered", "FLOWSPEC3_UNORDERED_LOSS_ENTRIES"),
        ("duplicate_source_path", "FLOWSPEC3_DUPLICATE_LOSS_SOURCE_PATH"),
    ],
)
def test_migration_loss_accounting_enforces_policy_and_canonical_order(
    streetlight_document: dict[str, Any], mutation: str, expected_code: str
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    loss_entries = preview_document["v2_passthrough"]["entries"]
    if mutation == "unknown_category":
        loss_entries[0]["category"] = "unknown_category"
    elif mutation == "disposition_mismatch":
        loss_entries[0]["disposition"] = "excluded"
    elif mutation == "source_path_mismatch":
        loss_entries[0]["category"] = "entry_contract"
    elif mutation == "unordered":
        loss_entries.reverse()
    elif mutation == "duplicate_source_path":
        duplicate_entry = copy.deepcopy(loss_entries[0])
        duplicate_entry["category"] = "unused_domain_declaration"
        loss_entries.insert(1, duplicate_entry)

    diagnostic_codes = {
        diagnostic.code
        for diagnostic in check_v3_preview(
            preview_document, mode=V3PreviewDocumentMode.V2_MIGRATION
        )
    }

    assert expected_code in diagnostic_codes


def test_migration_loss_entries_are_ordered_and_unique(
    streetlight_document: dict[str, Any],
) -> None:
    migration_report = migrate_v2_to_v3_preview_report(streetlight_document)
    source_paths = [loss_entry.source_path for loss_entry in migration_report.loss_entries]

    assert source_paths == sorted(source_paths)
    assert len(source_paths) == len(set(source_paths))
    assert source_paths == [
        loss_entry["source_path"]
        for loss_entry in migration_report.preview_document["v2_passthrough"]["entries"]
    ]


def test_migration_rejects_a_non_v2_source(streetlight_document: dict[str, Any]) -> None:
    streetlight_document["schema"] = "flowspec/3-draft"

    with pytest.raises(V3PreviewMigrationError, match="source schema must be"):
        migrate_v2_to_v3_preview(streetlight_document)


@pytest.mark.parametrize("domain_type", ["integer", "number"])
def test_numeric_domains_preserve_inclusive_bounds(domain_type: str) -> None:
    source_document = {
        "schema": "flowspec/2",
        "flow": "numeric_preview",
        "version": "1.0.0",
        "route": {"description": "Collect a bounded numeric value."},
        "domains": {
            "Quantity": {
                "type": domain_type,
                "minimum": 1,
                "maximum": 10,
            }
        },
        "slots": {"quantity": {"domain": "Quantity"}},
        "path": [{"slot": "quantity", "prompt": {"text": "Quantity?"}}],
    }

    preview_document = migrate_v2_to_v3_preview(source_document)
    migrated_domain = preview_document["steps"][0]["domain"]

    assert migrated_domain == {
        "type": domain_type,
        "minimum": 1,
        "maximum": 10,
    }


def test_implicit_derives_sharing_an_anchor_preserve_source_order() -> None:
    source_document = {
        "schema": "flowspec/2",
        "flow": "derive_order_preview",
        "version": "1.0.0",
        "route": {"description": "Preserve deterministic derive order."},
        "domains": {"Answer": {"type": "free_text"}},
        "slots": {"answer": {"domain": "Answer"}},
        "path": [
            {
                "step": "collect_answer",
                "slot": "answer",
                "prompt": {"text": "Answer?"},
            }
        ],
        "derive": [
            {
                "writes": "first",
                "from": ["answer"],
                "lookup": {"input": "first-output"},
                "after": "collect_answer",
            },
            {
                "writes": "second",
                "from": ["first"],
                "lookup": {"first-output": "second-output"},
                "after": "collect_answer",
            },
        ],
    }

    preview_document = migrate_v2_to_v3_preview(source_document)

    assert [step.get("derive") for step in preview_document["steps"]] == [
        None,
        "first",
        "second",
    ]


def test_nested_terminal_literals_preserve_dollar_prefixed_keys() -> None:
    source_document = _external_wait_source()
    source_document["terminal"]["outcomes"]["success"]["set"] = {
        "metadata": {"$raw": "literal", "$result": "also-literal"}
    }

    preview_document = migrate_v2_to_v3_preview(source_document)
    submit_step = _step_with_kind(preview_document, "submit")

    assert submit_step["outcomes"]["success"]["set"] == {
        "metadata": {"$raw": "literal", "$result": "also-literal"}
    }


def test_migration_materializes_implicit_confirmation_target(
    streetlight_document: dict[str, Any],
) -> None:
    del streetlight_document["confirm"]["on_confirm"]

    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    confirmation_step = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("correction_hub") is True
    )

    assert confirmation_step["on_confirm"] == {"$step": "open_ticket"}


def test_migration_separates_nullable_acceptance_from_rendered_options(
    streetlight_document: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(streetlight_document)
    location_step = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("collect") == "streetlight_location"
    )

    assert location_step["domain"]["accepts_null"] is True
    assert all(option["value"] is not None for option in location_step["domain"]["options"])


def test_migration_materializes_number_aliases_when_null_is_not_rendered() -> None:
    source_document = {
        "schema": "flowspec/2",
        "flow": "nullable_number_aliases",
        "version": "1.0.0",
        "route": {"description": "Keep positional aliases stable."},
        "domains": {
            "Choice": {
                "type": "categorical",
                "values": ["First", None, "Third"],
                "normalize": {"number_words": True},
            }
        },
        "slots": {"choice": {"domain": "Choice"}},
        "path": [{"slot": "choice"}],
    }

    preview_document = migrate_v2_to_v3_preview(source_document)
    domain = preview_document["steps"][0]["domain"]

    assert domain["options"][0]["aliases"] == ["1", "one"]
    assert domain["null_aliases"] == ["2", "two"]
    assert domain["options"][1]["aliases"] == ["3", "three"]
    assert "normalize" not in domain


def test_migration_requires_semantically_linked_v2_source(
    streetlight_document: dict[str, Any],
) -> None:
    streetlight_document["confirm"]["on_confirm"] = "missing_step"

    with pytest.raises(V3PreviewMigrationError, match="not semantically linked"):
        migrate_v2_to_v3_preview(streetlight_document)


def test_migration_rejects_ephemeral_collected_slot_persistence(
    streetlight_document: dict[str, Any],
) -> None:
    streetlight_document["slots"]["streetlight_issue"]["persist"] = "payload"

    with pytest.raises(V3PreviewMigrationError, match="cannot survive a conversational pause"):
        migrate_v2_to_v3_preview(streetlight_document)


@pytest.mark.parametrize(
    "mutation",
    [
        "non_boolean_confirmation",
        "emoji_veto_without_affirmation",
        "duplicate_gate_sources",
        "invalid_confirmation_variant",
        "result_in_predicate",
        "categorical_boolean_value",
        "dead_ui_field",
        "orphan_default",
        "ambiguous_nullable_slot",
        "unbounded_prefill",
        "ephemeral_persistence",
        "legacy_enrichment_name",
        "missing_submit_id",
        "missing_await_cta",
        "invalid_cta_contract",
        "terminal_array_index",
        "invalid_passthrough_pointer",
    ],
)
def test_preview_schema_closes_local_contradictions(mutation: str) -> None:
    preview_document = _minimal_preview()
    collect_step = preview_document["steps"][0]
    if mutation == "non_boolean_confirmation":
        preview_document["steps"].insert(
            1,
            {
                "confirm": "confirmed",
                "domain": copy.deepcopy(collect_step["domain"]),
            },
        )
    elif mutation == "emoji_veto_without_affirmation":
        preview_document["steps"].insert(1, _confirmation_step())
        preview_document["steps"][1]["domain"]["normalize"] = {"emoji_veto": True}
    elif mutation == "duplicate_gate_sources":
        collect_step["ask_when"] = {"is_present": {"$slot": "answer"}}
        collect_step["gate"] = {"is_present": {"$slot": "answer"}}
    elif mutation == "invalid_confirmation_variant":
        confirmation_step = _confirmation_step()
        confirmation_step.update(
            {
                "correction_hub": True,
                "correctable": [{"$slot": "answer"}],
                "on_confirm": {"$step": "submit_answer"},
                "on_reject": {"end": "Cancelled"},
            }
        )
        preview_document["steps"].insert(1, confirmation_step)
    elif mutation == "result_in_predicate":
        collect_step["ask_when"] = {"is_present": {"$result": "answer"}}
    elif mutation == "categorical_boolean_value":
        collect_step["domain"]["options"][0]["value"] = True
    elif mutation == "dead_ui_field":
        collect_step["ui"] = {
            "kind": "buttons",
            "field": "answer_button",
            "meta_flow_ref": "dead",
        }
    elif mutation == "orphan_default":
        collect_step["default"] = "yes"
    elif mutation == "ambiguous_nullable_slot":
        collect_step["nullable"] = True
    elif mutation == "unbounded_prefill":
        collect_step["prefill_sources"] = ["whatsapp_flow"]
    elif mutation == "ephemeral_persistence":
        collect_step["persist"] = "payload"
    elif mutation == "legacy_enrichment_name":
        preview_document["steps"].insert(1, _await_step())
        preview_document["steps"][1]["on_resume"] = {"enrich": "lookup_answer"}
    elif mutation == "missing_submit_id":
        submit_step = _submit_step()
        del submit_step["id"]
        preview_document["steps"].append(submit_step)
    elif mutation == "missing_await_cta":
        await_step = _await_step()
        del await_step["ui"]
        preview_document["steps"].append(await_step)
    elif mutation == "invalid_cta_contract":
        await_step = _await_step()
        await_step["ui"]["out_of_band"] = False
        preview_document["steps"].append(await_step)
    elif mutation == "terminal_array_index":
        preview_document["steps"].append(_submit_step())
        preview_document["steps"][-1]["outputs"] = {"protocol_id": {"$result": "tickets.0.id"}}
    elif mutation == "invalid_passthrough_pointer":
        preview_document["v2_passthrough"] = {
            "contract": "flowspec2/v3-preview-loss-policy@1",
            "entries": [
                {
                    "source_path": "/~2",
                    "category": "entry_contract",
                    "disposition": "compatibility_only",
                    "source_fragment": True,
                }
            ],
        }

    with pytest.raises(jsonschema.ValidationError):
        validate_v3_preview(preview_document)


def test_preview_schema_allows_array_index_only_for_enrichment_results() -> None:
    preview_document = _minimal_preview()
    await_step = _await_step()
    await_step["on_resume"] = {
        "enrich": {
            "tool": "lookup_answer",
            "set": {"answer_code": {"$result": "answers.0.code"}},
        }
    }
    preview_document["steps"].append(await_step)

    validate_v3_preview(preview_document)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("reversed_range", "FLOWSPEC3_INVALID_NUMERIC_RANGE"),
        ("duplicate_step_id", "FLOWSPEC3_DUPLICATE_STEP_ID"),
        ("unknown_derive", "FLOWSPEC3_UNKNOWN_DERIVE_REFERENCE"),
        ("unknown_slot", "FLOWSPEC3_UNKNOWN_SLOT_REFERENCE"),
        ("future_derive", "FLOWSPEC3_SOURCE_NOT_AVAILABLE"),
        ("unknown_step", "FLOWSPEC3_UNKNOWN_STEP_REFERENCE"),
        ("nonforward_confirm", "FLOWSPEC3_NON_FORWARD_CONFIRM_TARGET"),
        ("await_field", "FLOWSPEC3_AWAIT_FIELD_MISMATCH"),
        ("await_target", "FLOWSPEC3_AWAIT_NEXT_STEP_MISMATCH"),
        ("await_resend_target", "FLOWSPEC3_AWAIT_RESEND_TARGET_MISMATCH"),
        ("duplicate_option", "FLOWSPEC3_DUPLICATE_OPTION_VALUE"),
        ("ambiguous_alias", "FLOWSPEC3_AMBIGUOUS_OPTION_INPUT"),
        ("blank_alias", "FLOWSPEC3_EMPTY_OPTION_INPUT"),
        ("blank_label", "FLOWSPEC3_EMPTY_OPTION_LABEL"),
        ("normalized_duplicate_alias", "FLOWSPEC3_DUPLICATE_OPTION_INPUT"),
        ("number_alias_collision", "FLOWSPEC3_AMBIGUOUS_OPTION_INPUT"),
        ("boolean_alias_conflict", "FLOWSPEC3_BOOLEAN_ALIAS_CONFLICT"),
        ("choice_ui_without_options", "FLOWSPEC3_CHOICE_UI_REQUIRES_OPTIONS"),
        ("unknown_conditional_option", "FLOWSPEC3_UNKNOWN_CONDITIONAL_OPTION"),
        ("duplicate_conditional_option", "FLOWSPEC3_DUPLICATE_CONDITIONAL_OPTION"),
        ("invalid_default", "FLOWSPEC3_INVALID_DEFAULT"),
        ("await_write_collision", "FLOWSPEC3_AWAIT_WRITE_COLLISION"),
        ("submit_write_collision", "FLOWSPEC3_SUBMIT_WRITE_COLLISION"),
        ("multiple_submit", "FLOWSPEC3_MULTIPLE_SUBMIT_STEPS"),
        ("multiple_await", "FLOWSPEC3_MULTIPLE_AWAIT_STEPS"),
        ("multiple_hub", "FLOWSPEC3_MULTIPLE_CORRECTION_HUBS"),
        ("duplicate_subflow", "FLOWSPEC3_DUPLICATE_SUBFLOW_USE"),
        ("service_writer_collision", "FLOWSPEC3_STATE_WRITER_COLLISION"),
    ],
)
def test_check_v3_preview_reports_cross_step_and_value_contracts(
    mutation: str, expected_code: str
) -> None:
    preview_document = _minimal_preview()
    collect_step = preview_document["steps"][0]
    if mutation == "reversed_range":
        collect_step["domain"] = {"type": "integer", "minimum": 2, "maximum": 1}
    elif mutation == "duplicate_step_id":
        preview_document["steps"].append(
            {"collect": "second", "id": "collect_answer", "domain": _categorical_domain()}
        )
    elif mutation == "unknown_derive":
        preview_document["steps"].append(
            {
                "derive": "normalized",
                "from": [{"$derive": "missing"}],
                "lookup": {},
            }
        )
    elif mutation == "unknown_slot":
        preview_document["steps"].append(
            {
                "derive": "normalized",
                "from": [{"$slot": "missing"}],
                "lookup": {},
            }
        )
    elif mutation == "future_derive":
        preview_document["steps"].extend(
            [
                {
                    "derive": "consumer",
                    "from": [{"$derive": "producer"}],
                    "lookup": {},
                },
                {
                    "derive": "producer",
                    "from": [{"$slot": "answer"}],
                    "lookup": {},
                },
            ]
        )
    elif mutation == "unknown_step":
        confirmation_step = _confirmation_step()
        confirmation_step.update(
            {
                "correction_hub": True,
                "correctable": [{"$slot": "answer"}],
                "on_confirm": {"$step": "missing"},
            }
        )
        preview_document["steps"].append(confirmation_step)
    elif mutation == "nonforward_confirm":
        confirmation_step = _confirmation_step()
        confirmation_step.update(
            {
                "correction_hub": True,
                "correctable": [{"$slot": "answer"}],
                "on_confirm": {"$step": "collect_answer"},
            }
        )
        preview_document["steps"].append(confirmation_step)
    elif mutation in {"await_field", "await_target"}:
        await_step = _await_step()
        if mutation == "await_field":
            await_step["ui"]["field"] = "other_token"
        else:
            await_step["ui"]["next_step"] = {"$step": "collect_answer"}
        preview_document["steps"].append(await_step)
    elif mutation == "await_resend_target":
        await_step = _await_step()
        await_step["recovery"] = {"resend": {"goto": {"$step": "collect_answer"}}}
        preview_document["steps"].append(await_step)
    elif mutation == "duplicate_option":
        collect_step["domain"]["options"].append({"value": "yes", "label": "Duplicate yes"})
    elif mutation == "ambiguous_alias":
        collect_step["domain"]["options"][1]["aliases"] = ["YES"]
    elif mutation == "blank_alias":
        collect_step["domain"]["options"][0]["aliases"] = ["   "]
    elif mutation == "blank_label":
        collect_step["domain"]["options"][0]["label"] = "   "
    elif mutation == "normalized_duplicate_alias":
        collect_step["domain"]["options"][0]["aliases"] = ["affirmative", " AFFIRMATIVE "]
    elif mutation == "number_alias_collision":
        collect_step["domain"] = {
            "type": "categorical",
            "options": [
                {"value": "first", "label": "First"},
                {"value": "1", "label": "Literal one"},
            ],
            "normalize": {"number_words": True},
        }
    elif mutation == "boolean_alias_conflict":
        collect_step["domain"] = {
            "type": "bool",
            "options": [
                {"value": True, "label": "Yes"},
                {"value": False, "label": "No", "aliases": ["yes"]},
            ],
            "normalize": {"affirmation": True},
        }
    elif mutation == "choice_ui_without_options":
        collect_step["domain"] = {"type": "free_text"}
        collect_step["ui"] = {"kind": "buttons", "field": "answer_button"}
    elif mutation == "unknown_conditional_option":
        collect_step["ui"] = {
            "kind": "buttons",
            "field": "answer_button",
            "options_when": [
                {
                    "value": "missing",
                    "gate": {"is_present": {"$config": "feature_enabled"}},
                }
            ],
        }
    elif mutation == "duplicate_conditional_option":
        collect_step["ui"] = {
            "kind": "buttons",
            "field": "answer_button",
            "options_when": [
                {
                    "value": "yes",
                    "gate": {"is_present": {"$config": "first_gate"}},
                },
                {
                    "value": "yes",
                    "gate": {"is_present": {"$config": "second_gate"}},
                },
            ],
        }
    elif mutation == "invalid_default":
        collect_step.update({"on_exhaust": "default", "default": "missing"})
    elif mutation == "await_write_collision":
        await_step = _await_step()
        await_step["on_resume"] = {
            "set": {"answer_code": {"$token": "answer.code"}},
            "enrich": {
                "tool": "lookup_answer",
                "set": {"answer_code": {"$result": "answer.code"}},
            },
        }
        preview_document["steps"].append(await_step)
    elif mutation == "submit_write_collision":
        submit_step = _submit_step()
        submit_step["outputs"] = {"protocol_id": {"$result": "protocol"}}
        submit_step["outcomes"]["success"]["set"] = {"protocol_id": "fallback"}
        preview_document["steps"].append(submit_step)
    elif mutation == "multiple_submit":
        preview_document["steps"].extend([_submit_step(), _submit_step("submit_again")])
    elif mutation == "multiple_await":
        preview_document["steps"].extend(
            [_await_step(), _await_step_with_identifier("wait_again", "other_token")]
        )
    elif mutation == "multiple_hub":
        first_hub = _confirmation_step()
        first_hub.update(
            {
                "correction_hub": True,
                "correctable": [{"$slot": "answer"}],
                "on_confirm": {"$step": "submit_answer"},
            }
        )
        second_hub = _confirmation_step()
        second_hub.update(
            {
                "confirm": "confirmed_again",
                "id": "confirm_again",
                "correction_hub": True,
                "correctable": [{"$slot": "answer"}],
                "on_confirm": {"$step": "submit_answer"},
            }
        )
        preview_document["steps"].extend([first_hub, second_hub, _submit_step()])
    elif mutation == "duplicate_subflow":
        preview_document["steps"].extend([{"use": "address@1"}, {"use": "address@1"}])
    elif mutation == "service_writer_collision":
        preview_document["service"] = {"id": "service-id"}
        collect_step["collect"] = "service"

    diagnostic_codes = {diagnostic.code for diagnostic in check_v3_preview(preview_document)}

    assert expected_code in diagnostic_codes


def test_validate_v3_preview_raises_semantic_error_with_diagnostics() -> None:
    preview_document = _minimal_preview()
    preview_document["steps"][0]["domain"] = {
        "type": "number",
        "minimum": 2,
        "maximum": 1,
    }

    with pytest.raises(V3PreviewValidationError) as validation_error:
        validate_v3_preview(preview_document)

    assert validation_error.value.diagnostics[0].code == "FLOWSPEC3_INVALID_NUMERIC_RANGE"


def _minimal_preview() -> dict[str, Any]:
    return {
        "schema": "flowspec/3-draft",
        "flow": "preview_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise preview contracts."},
        "steps": [
            {
                "collect": "answer",
                "id": "collect_answer",
                "domain": _categorical_domain(),
            }
        ],
    }


def _categorical_domain() -> dict[str, Any]:
    return {
        "type": "categorical",
        "options": [
            {"value": "yes", "label": "Yes"},
            {"value": "no", "label": "No"},
        ],
    }


def _confirmation_step() -> dict[str, Any]:
    return {
        "confirm": "confirmed",
        "id": "confirm_answer",
        "domain": {
            "type": "bool",
            "options": [
                {"value": True, "label": "Yes"},
                {"value": False, "label": "No"},
            ],
        },
    }


def _submit_step(step_identifier: str = "submit_answer") -> dict[str, Any]:
    return {
        "submit": "submit_answer",
        "id": step_identifier,
        "idempotent": True,
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    }


def _await_step() -> dict[str, Any]:
    return _await_step_with_identifier("wait_answer", "answer_token")


def _await_step_with_identifier(step_identifier: str, resume_key: str) -> dict[str, Any]:
    return {
        "await": "cta_url",
        "id": step_identifier,
        "resume_on": resume_key,
        "ui": {
            "kind": "cta_url",
            "field": resume_key,
            "out_of_band": True,
            "next_step": {"$step": step_identifier},
        },
    }


def _external_wait_source() -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "payment_preview",
        "version": "1.0.0",
        "route": {"description": "Resume a payment after an external confirmation."},
        "domains": {"Placeholder": {"type": "free_text"}},
        "path": [
            {"step": "wait_payment", "await_external": True},
            {"terminal": True},
        ],
        "terminal": {
            "step": "submit_payment",
            "tool": "submit_payment",
            "idempotent": True,
            "outputs": {"protocol_id": "result.protocol"},
            "outcomes": {
                "success": {"reset_next": True},
                "retryable": {"preserve_state": True},
                "fatal": {"reset_next": True},
            },
        },
        "capabilities": {
            "await_external": {
                "kind": "cta_url",
                "step": "wait_payment",
                "resume_on": "payment_token",
                "resume": {
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
                },
                "prompt": {"text": "Complete payment to continue.", "verbatim": True},
                "interactive": {
                    "kind": "cta_url",
                    "field": "payment_token",
                    "out_of_band": True,
                    "next_step": "wait_payment",
                },
                "on_resume": {
                    "set": {"payment_id": "$token.payment.id"},
                    "enrich": {
                        "tool": "lookup_receipt",
                        "optional": True,
                        "input": {"payment_id": "$token.payment.id"},
                        "set": {"receipt_code": "$result.receipt.code"},
                    },
                },
                "timeout": {"goto": "wait_payment", "set": {"timed_out": True}},
                "timeout_seconds": 300,
                "recovery": {
                    "abort": {"goto": "END"},
                    "resend": {"goto": "wait_payment"},
                    "switch": {"goto": "wait_payment", "set": {"channel": "manual"}},
                },
            }
        },
    }
