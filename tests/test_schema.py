"""The schema is a valid Draft 2020-12 document; examples validate; mutations fail."""

from __future__ import annotations

import copy
from importlib import import_module
from typing import Any, cast
from unittest.mock import Mock

import jsonschema
import pytest
from jsonschema.exceptions import best_match

from flowspec2 import schema, validate_flow

PUBLIC_SCHEMA_NAMESPACE = "https://wllsena.github.io/flowspec2/schemas/"


def test_schema_is_valid_draft202012():
    jsonschema.Draft202012Validator.check_schema(schema())


def test_schema_uses_the_project_owned_public_namespace() -> None:
    assert schema()["$id"] == f"{PUBLIC_SCHEMA_NAMESPACE}flowspec-2.json"


def test_schema_returns_an_owned_copy() -> None:
    caller_schema = schema()
    caller_schema["type"] = "string"

    assert schema()["type"] == "object"


def test_examples_validate(streetlight_document, pothole_document):
    validate_flow(streetlight_document)
    validate_flow(pothole_document)


def test_validate_flow_preserves_best_match_validation_error(streetlight_document) -> None:
    invalid_flow = copy.deepcopy(streetlight_document)
    invalid_flow["flow"] = "Invalid Flow"
    invalid_flow["version"] = "2"
    invalid_flow["unknown"] = True
    expected_error = best_match(
        jsonschema.Draft202012Validator(schema()).iter_errors(cast(Any, invalid_flow))
    )
    assert expected_error is not None

    with pytest.raises(jsonschema.ValidationError) as raised_error:
        validate_flow(invalid_flow)

    actual_error = raised_error.value
    assert actual_error.validator == expected_error.validator
    assert actual_error.message == expected_error.message
    assert tuple(actual_error.absolute_path) == tuple(expected_error.absolute_path)
    assert tuple(actual_error.absolute_schema_path) == tuple(expected_error.absolute_schema_path)
    assert tuple(
        (context_error.validator, context_error.message, tuple(context_error.absolute_path))
        for context_error in actual_error.context
    ) == tuple(
        (context_error.validator, context_error.message, tuple(context_error.absolute_path))
        for context_error in expected_error.context
    )


def test_validate_flow_reuses_the_compiled_validator(
    streetlight_document,
    pothole_document,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_module = import_module("flowspec2.schema")
    cached_schema = schema_module._cached_schema()
    validator_factory = Mock(wraps=jsonschema.Draft202012Validator)
    monkeypatch.setattr(schema_module, "_schema_cache", cached_schema)
    monkeypatch.setattr(schema_module, "_validator_cache", None)
    monkeypatch.setattr(schema_module.jsonschema, "Draft202012Validator", validator_factory)

    schema_module.validate_flow(streetlight_document)
    schema_module.validate_flow(pothole_document)
    schema_module.validate_flow(streetlight_document)

    validator_factory.assert_called_once_with(cached_schema)


def test_implicit_categorical_domain_requires_its_closed_values(streetlight_document):
    implicit_categorical = copy.deepcopy(streetlight_document)
    implicit_categorical["domains"]["StreetlightIssue"].pop("type")
    validate_flow(implicit_categorical)

    missing_values = copy.deepcopy(implicit_categorical)
    missing_values["domains"]["StreetlightIssue"].pop("values")
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(missing_values)


@pytest.mark.parametrize(
    ("domain_name", "irrelevant_field", "irrelevant_value"),
    [
        ("ReferencePoint", "values", ["ignored"]),
        ("YesNo", "minimum", 0),
        ("StreetlightIssue", "optional", True),
    ],
)
def test_domain_kind_rejects_fields_its_validator_does_not_consume(
    streetlight_document,
    domain_name: str,
    irrelevant_field: str,
    irrelevant_value: object,
):
    bad = copy.deepcopy(streetlight_document)
    bad["domains"][domain_name][irrelevant_field] = irrelevant_value

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_boolean_emoji_veto_requires_affirmation_normalization(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["domains"]["YesNo"]["normalize"]["affirmation"] = False

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_unknown_top_level_key(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["wat"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_step_both_slot_and_confirm(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["path"][1] = {"step": "x", "slot": "streetlight_issue", "confirm": "service_confirmed"}
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_terminal_marker_must_be_true(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["path"][-1] = {"terminal": False}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_step_rejects_fields_the_kind_does_not_consume(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["path"][1]["on_reject"] = {"end": "ignored"}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_confirmation_rejection_contract_requires_a_message(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    summary_step = next(path_step for path_step in bad["path"] if "on_reject" in path_step)
    summary_step["on_reject"] = {}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


@pytest.mark.parametrize("ignored_field", ["ask_when", "correctable"])
def test_summary_confirmation_rejects_incompatible_variants(
    streetlight_document,
    ignored_field: str,
):
    bad = copy.deepcopy(streetlight_document)
    summary_step = next(path_step for path_step in bad["path"] if "on_reject" in path_step)
    summary_step[ignored_field] = (
        {"is_present": "slots.streetlight_issue"} if ignored_field == "ask_when" else True
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


@pytest.mark.parametrize("ignored_field", ["ask_when", "skip_when", "on_reject"])
def test_correction_hub_rejects_fields_it_cannot_execute(
    streetlight_document,
    ignored_field: str,
):
    bad = copy.deepcopy(streetlight_document)
    correction_hub = next(path_step for path_step in bad["path"] if path_step.get("correctable"))
    correction_hub[ignored_field] = (
        {"end": "Stop."}
        if ignored_field == "on_reject"
        else {"is_present": "slots.streetlight_issue"}
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_version_not_semver(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["version"] = "1.4"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_predicate_two_operators(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["overrides"]["gates"]["collect_near_sports_court"] = {
        "eq": ["slots.x", "y"],
        "ne": ["slots.a", "b"],
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_predicate_accepts_explicit_namespaced_literal(streetlight_document) -> None:
    flow_document = copy.deepcopy(streetlight_document)
    flow_document["overrides"]["gates"]["collect_count"] = {
        "eq": ["slots.streetlight_issue", {"literal": "config.flag"}]
    }

    validate_flow(flow_document)


def test_predicate_presence_requires_a_namespaced_reference(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["auto_flow"]["send_when"] = {"is_present": "not_a_reference"}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_flow_id_uppercase(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["flow"] = "ReparoLuminaria"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_subflow_ref_missing_major(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["uses"][0]["ref"] = "address"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_reference_point_requirement_uses_only_the_slot_declaration(streetlight_document):
    assert streetlight_document["slots"]["reference_point"]["required"] is True
    assert "reference_point_required" not in streetlight_document["config"]
    assert "reference_point_required" not in streetlight_document["uses"][0]["with"]


def test_obsolete_reference_point_requirement_config_is_rejected(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["config"]["reference_point_required"] = True

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_subflow_configuration_keys_are_owned_by_the_resolved_manifest(streetlight_document):
    flow_document = copy.deepcopy(streetlight_document)
    flow_document["uses"][0]["with"]["custom_manifest_key"] = True

    validate_flow(flow_document)


def test_slot_default_is_rejected_without_default_exhaustion(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["slots"]["streetlight_issue"]["default"] = "Not working"

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_nullable_slot_requires_a_required_collector(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    bad["slots"]["streetlight_location"]["nullable"] = True

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_prefill_sources_require_prompt_bound_collection(streetlight_document):
    bad = copy.deepcopy(streetlight_document)
    del bad["slots"]["streetlight_issue"]["fill_only_when_asked"]

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)
