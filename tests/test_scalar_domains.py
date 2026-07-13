"""Deterministic integer and number domain contracts."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from flowspec2.domains import make_slot_model
from flowspec2.semantics import semantic_diagnostics


def test_integer_domain_coerces_exact_text_and_enforces_bounds() -> None:
    model = make_slot_model(
        "quantity",
        "Quantity",
        {"Quantity": {"type": "integer", "minimum": 1, "maximum": 5}},
    )

    assert model.model_validate({"quantity": " 3 "}).model_dump()["quantity"] == 3
    quantity_schema = model.model_json_schema()["properties"]["quantity"]
    assert quantity_schema["type"] == "integer"
    assert quantity_schema["minimum"] == 1
    assert quantity_schema["maximum"] == 5
    with pytest.raises(ValidationError):
        model.model_validate({"quantity": "3.5"})
    with pytest.raises(ValidationError):
        model.model_validate({"quantity": 8})


def test_number_domain_rejects_non_finite_and_boolean_values() -> None:
    model = make_slot_model(
        "distance",
        "Distance",
        {"Distance": {"type": "number", "minimum": 0}},
    )

    assert model.model_validate({"distance": "2.5"}).model_dump()["distance"] == 2.5
    with pytest.raises(ValidationError):
        model.model_validate({"distance": "NaN"})
    with pytest.raises(ValidationError):
        model.model_validate({"distance": "1e9999"})
    with pytest.raises(ValidationError):
        model.model_validate({"distance": True})

    distance_schema = model.model_json_schema()["properties"]["distance"]
    assert distance_schema["minimum"] == 0


def test_semantic_linker_rejects_inverted_numeric_range(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["domains"]["Tamanho"] = {
        "type": "integer",
        "minimum": 5,
        "maximum": 1,
    }

    assert "FLOWSPEC_SEMANTIC_INVALID_NUMERIC_RANGE" in {
        diagnostic.code for diagnostic in semantic_diagnostics(invalid_flow)
    }
