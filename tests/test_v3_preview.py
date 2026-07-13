from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from flowspec2 import load_flow
from flowspec2.experimental import (
    V3PreviewMigrationError,
    V3PreviewValidationError,
    check_v3_preview,
    compact_json_bytes,
    migrate_v2_to_v3_preview,
    migrate_v2_to_v3_preview_report,
    preview_schema,
    validate_v3_preview,
)

EXAMPLES_DIRECTORY = Path(__file__).resolve().parents[1] / "examples"


def _step_with_kind(preview_document: dict[str, Any], step_kind: str) -> dict[str, Any]:
    return next(
        preview_step for preview_step in preview_document["steps"] if step_kind in preview_step
    )


@pytest.mark.parametrize(
    "example_filename",
    ["reparo_buraco.flow.json", "reparo_luminaria.flow.json"],
)
def test_real_v2_examples_migrate_to_schema_valid_previews(example_filename: str) -> None:
    source_document = load_flow(EXAMPLES_DIRECTORY / example_filename)

    preview_document = migrate_v2_to_v3_preview(source_document)

    validate_v3_preview(preview_document)
    assert preview_document["schema"] == "flowspec/3-draft"
    assert "domains" not in preview_document
    assert "slots" not in preview_document
    assert "path" not in preview_document


def test_migration_is_pure_deterministic_and_report_is_immutable(
    luminaria_doc: dict[str, Any],
) -> None:
    source_snapshot = copy.deepcopy(luminaria_doc)

    first_report = migrate_v2_to_v3_preview_report(luminaria_doc)
    second_report = migrate_v2_to_v3_preview_report(luminaria_doc)

    assert luminaria_doc == source_snapshot
    assert first_report == second_report
    mutable_preview = first_report.preview_document
    mutable_preview["flow"] = "changed_by_caller"
    assert first_report.preview_document["flow"] == "reparo_luminaria"


def test_migration_inlines_stable_options_and_typed_references(
    luminaria_doc: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
    collect_defect = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("collect") == "luminaria_defeito"
    )
    apagada_option = next(
        domain_option
        for domain_option in collect_defect["domain"]["options"]
        if domain_option["value"] == "Apagada"
    )
    derive_step = _step_with_kind(preview_document, "derive")
    submit_step = _step_with_kind(preview_document, "submit")

    assert apagada_option == {
        "value": "Apagada",
        "label": "Apagada",
        "aliases": ["nao acende", "queimada", "sem luz"],
        "description": "A luminária não acende / está sem luz",
    }
    assert derive_step["from"][0] == {"$slot": "luminaria_defeito"}
    assert derive_step["default"] == {"$slot": "luminaria_defeito"}
    assert submit_step["input"]["defeitoLuminaria"] == {"$derive": "luminaria_defeito_classificado"}
    assert submit_step["outputs"]["protocol_id"] == {"$result": "protocolo"}


def test_migration_localizes_subflow_confirmation_derive_and_submit(
    luminaria_doc: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
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
        if preview_step.get("collect") == "luminaria_localizacao"
    )

    assert address_use["with"] == {
        "required": True,
        "needs_confirmation": True,
        "max_attempts": 3,
    }
    assert confirmation_hub["correctable"][0] == {"$slot": "luminaria_defeito"}
    assert confirmation_hub["on_confirm"] == {"$step": "open_ticket"}
    assert derive_index == localization_index + 1
    assert preview_steps[-1]["submit"] == "sgrc_open_ticket"


def test_non_native_v2_fragments_are_preserved_and_reported(
    luminaria_doc: dict[str, Any],
) -> None:
    migration_report = migrate_v2_to_v3_preview_report(luminaria_doc)
    preview_document = migration_report.preview_document
    unmapped_fragments = preview_document["v2_passthrough"]["unmapped"]

    assert "/entry" in migration_report.passthrough_paths
    assert "/auto_flow" in migration_report.passthrough_paths
    assert "/capabilities/await_external" in migration_report.passthrough_paths
    assert unmapped_fragments["/entry"] == luminaria_doc["entry"]
    assert unmapped_fragments["/auto_flow"] == luminaria_doc["auto_flow"]
    assert (
        unmapped_fragments["/capabilities/await_external"]
        == (luminaria_doc["capabilities"]["await_external"])
    )
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
        source_path.startswith(native_v2_prefixes)
        for source_path in migration_report.passthrough_paths
    )


def test_compact_byte_report_is_neutral_measurement(
    luminaria_doc: dict[str, Any],
) -> None:
    migration_report = migrate_v2_to_v3_preview_report(luminaria_doc)
    compact_measurements = migration_report.compact_bytes

    assert compact_measurements.source_bytes == compact_json_bytes(luminaria_doc)
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
    assert "/capabilities/await_external" not in preview_document["v2_passthrough"]["unmapped"]


def test_legacy_await_enrichment_is_accounted_instead_of_kept_ambiguous() -> None:
    source_document = _external_wait_source()
    source_document["capabilities"]["await_external"]["on_resume"]["enrich"] = "lookup_receipt"

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    preview_document = migration_report.preview_document
    await_step = _step_with_kind(preview_document, "await")
    enrichment_pointer = "/capabilities/await_external/on_resume/enrich"

    assert "enrich" not in await_step["on_resume"]
    assert enrichment_pointer in migration_report.passthrough_paths
    assert preview_document["v2_passthrough"]["unmapped"][enrichment_pointer] == "lookup_receipt"


def test_legacy_interactive_gate_is_loss_accounted_not_promoted() -> None:
    source_document = _external_wait_source()
    source_document["capabilities"]["await_external"]["interactive"]["gate"] = "payment_cta_enabled"

    migration_report = migrate_v2_to_v3_preview_report(source_document)
    preview_document = migration_report.preview_document
    await_step = _step_with_kind(preview_document, "await")
    gate_pointer = "/capabilities/await_external/interactive/gate"

    assert "gate" not in await_step["ui"]
    assert gate_pointer in migration_report.passthrough_paths
    assert preview_document["v2_passthrough"]["unmapped"][gate_pointer] == "payment_cta_enabled"


def test_preview_schema_is_closed_and_references_are_single_key(
    luminaria_doc: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
    unexpected_top_level = copy.deepcopy(preview_document)
    unexpected_top_level["capabilities"] = {}
    unexpected_step_field = copy.deepcopy(preview_document)
    unexpected_step_field["steps"][1]["domain_name"] = "LuminariaDefeito"
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
    luminaria_doc: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
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


def test_migration_rejects_a_non_v2_source(luminaria_doc: dict[str, Any]) -> None:
    luminaria_doc["schema"] = "flowspec/3-draft"

    with pytest.raises(V3PreviewMigrationError, match="source schema must be"):
        migrate_v2_to_v3_preview(luminaria_doc)


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
    luminaria_doc: dict[str, Any],
) -> None:
    del luminaria_doc["confirm"]["on_confirm"]

    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
    confirmation_step = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("correction_hub") is True
    )

    assert confirmation_step["on_confirm"] == {"$step": "open_ticket"}


def test_migration_separates_nullable_acceptance_from_rendered_options(
    luminaria_doc: dict[str, Any],
) -> None:
    preview_document = migrate_v2_to_v3_preview(luminaria_doc)
    location_step = next(
        preview_step
        for preview_step in preview_document["steps"]
        if preview_step.get("collect") == "luminaria_localizacao"
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

    assert domain["options"][0]["aliases"] == ["1", "um", "uma"]
    assert domain["null_aliases"] == ["2", "dois", "duas"]
    assert domain["options"][1]["aliases"] == ["3", "tres"]
    assert "normalize" not in domain


def test_migration_requires_semantically_linked_v2_source(
    luminaria_doc: dict[str, Any],
) -> None:
    luminaria_doc["confirm"]["on_confirm"] = "missing_step"

    with pytest.raises(V3PreviewMigrationError, match="not semantically linked"):
        migrate_v2_to_v3_preview(luminaria_doc)


def test_migration_rejects_ephemeral_collected_slot_persistence(
    luminaria_doc: dict[str, Any],
) -> None:
    luminaria_doc["slots"]["luminaria_defeito"]["persist"] = "payload"

    with pytest.raises(V3PreviewMigrationError, match="cannot survive a conversational pause"):
        migrate_v2_to_v3_preview(luminaria_doc)


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
        preview_document["v2_passthrough"] = {"unmapped": {"/~2": True}}

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
                {"value": False, "label": "No", "aliases": ["sim"]},
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
                "recovery": {
                    "abort": {"goto": "END"},
                    "resend": {"goto": "wait_payment"},
                    "switch": {"goto": "wait_payment", "set": {"channel": "manual"}},
                },
            }
        },
    }
