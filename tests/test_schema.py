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


def test_examples_validate(luminaria_doc, buraco_doc):
    validate_flow(luminaria_doc)
    validate_flow(buraco_doc)


def test_validate_flow_preserves_best_match_validation_error(luminaria_doc) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
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
    luminaria_doc,
    buraco_doc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_module = import_module("flowspec2.schema")
    cached_schema = schema_module._cached_schema()
    validator_factory = Mock(wraps=jsonschema.Draft202012Validator)
    monkeypatch.setattr(schema_module, "_schema_cache", cached_schema)
    monkeypatch.setattr(schema_module, "_validator_cache", None)
    monkeypatch.setattr(schema_module.jsonschema, "Draft202012Validator", validator_factory)

    schema_module.validate_flow(luminaria_doc)
    schema_module.validate_flow(buraco_doc)
    schema_module.validate_flow(luminaria_doc)

    validator_factory.assert_called_once_with(cached_schema)


def test_implicit_categorical_domain_requires_its_closed_values(luminaria_doc):
    implicit_categorical = copy.deepcopy(luminaria_doc)
    implicit_categorical["domains"]["LuminariaDefeito"].pop("type")
    validate_flow(implicit_categorical)

    missing_values = copy.deepcopy(implicit_categorical)
    missing_values["domains"]["LuminariaDefeito"].pop("values")
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(missing_values)


@pytest.mark.parametrize(
    ("domain_name", "irrelevant_field", "irrelevant_value"),
    [
        ("PontoReferencia", "values", ["ignored"]),
        ("SimNao", "minimum", 0),
        ("LuminariaDefeito", "optional", True),
    ],
)
def test_domain_kind_rejects_fields_its_validator_does_not_consume(
    luminaria_doc,
    domain_name: str,
    irrelevant_field: str,
    irrelevant_value: object,
):
    bad = copy.deepcopy(luminaria_doc)
    bad["domains"][domain_name][irrelevant_field] = irrelevant_value

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_boolean_emoji_veto_requires_affirmation_normalization(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["domains"]["SimNao"]["normalize"]["affirmation"] = False

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_unknown_top_level_key(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["wat"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_step_both_slot_and_confirm(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["path"][1] = {"step": "x", "slot": "luminaria_defeito", "confirm": "service_confirmed"}
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_terminal_marker_must_be_true(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["path"][-1] = {"terminal": False}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_step_rejects_fields_the_kind_does_not_consume(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["path"][1]["on_reject"] = {"end": "ignored"}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_confirmation_rejection_contract_requires_a_message(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    summary_step = next(path_step for path_step in bad["path"] if "on_reject" in path_step)
    summary_step["on_reject"] = {}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


@pytest.mark.parametrize("ignored_field", ["ask_when", "correctable"])
def test_summary_confirmation_rejects_incompatible_variants(
    luminaria_doc,
    ignored_field: str,
):
    bad = copy.deepcopy(luminaria_doc)
    summary_step = next(path_step for path_step in bad["path"] if "on_reject" in path_step)
    summary_step[ignored_field] = (
        {"is_present": "slots.luminaria_defeito"} if ignored_field == "ask_when" else True
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


@pytest.mark.parametrize("ignored_field", ["ask_when", "skip_when", "on_reject"])
def test_correction_hub_rejects_fields_it_cannot_execute(
    luminaria_doc,
    ignored_field: str,
):
    bad = copy.deepcopy(luminaria_doc)
    correction_hub = next(path_step for path_step in bad["path"] if path_step.get("correctable"))
    correction_hub[ignored_field] = (
        {"end": "Stop."}
        if ignored_field == "on_reject"
        else {"is_present": "slots.luminaria_defeito"}
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_version_not_semver(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["version"] = "1.4"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_predicate_two_operators(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["overrides"]["gates"]["collect_quadra_esportes"] = {
        "eq": ["slots.x", "y"],
        "ne": ["slots.a", "b"],
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_predicate_accepts_explicit_namespaced_literal(luminaria_doc) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["overrides"]["gates"]["collect_quantidade"] = {
        "eq": ["slots.luminaria_defeito", {"literal": "config.flag"}]
    }

    validate_flow(flow_document)


def test_predicate_presence_requires_a_namespaced_reference(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["auto_flow"]["send_when"] = {"is_present": "not_a_reference"}

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_flow_id_uppercase(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["flow"] = "ReparoLuminaria"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_subflow_ref_missing_major(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["uses"][0]["ref"] = "address"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_reference_point_requirement_uses_only_the_slot_declaration(luminaria_doc):
    assert luminaria_doc["slots"]["ponto_referencia"]["required"] is True
    assert "reference_point_required" not in luminaria_doc["config"]
    assert "reference_point_required" not in luminaria_doc["uses"][0]["with"]


def test_obsolete_reference_point_requirement_config_is_rejected(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["config"]["reference_point_required"] = True

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_subflow_configuration_keys_are_owned_by_the_resolved_manifest(luminaria_doc):
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["uses"][0]["with"]["custom_manifest_key"] = True

    validate_flow(flow_document)


def test_slot_default_is_rejected_without_default_exhaustion(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["slots"]["luminaria_defeito"]["default"] = "Apagada"

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_nullable_slot_requires_a_required_collector(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["slots"]["luminaria_localizacao"]["nullable"] = True

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_prefill_sources_require_prompt_bound_collection(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    del bad["slots"]["luminaria_defeito"]["fill_only_when_asked"]

    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)
