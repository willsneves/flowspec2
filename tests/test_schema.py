"""The schema is a valid Draft 2020-12 document; examples validate; mutations fail."""

from __future__ import annotations

import copy

import jsonschema
import pytest

from flowspec2 import schema, validate_flow


def test_schema_is_valid_draft202012():
    jsonschema.Draft202012Validator.check_schema(schema())


def test_examples_validate(luminaria_doc, buraco_doc):
    validate_flow(luminaria_doc)
    validate_flow(buraco_doc)


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


def test_mutation_version_not_semver(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["version"] = "1.4"
    with pytest.raises(jsonschema.ValidationError):
        validate_flow(bad)


def test_mutation_predicate_two_operators(luminaria_doc):
    bad = copy.deepcopy(luminaria_doc)
    bad["overrides"]["gates"]["collect_quadra_esportes"] = {"eq": ["slots.x", "y"], "ne": ["slots.a", "b"]}
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
