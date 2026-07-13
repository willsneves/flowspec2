"""Aggregate checker contracts and deterministic diagnostics."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import FrozenInstanceError
from typing import Any, cast
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from flowspec2 import (
    DiagnosticLocation,
    FlowCheckReport,
    FlowDiagnostic,
    check_flow,
    check_json,
    schema,
)


def test_diagnostic_contract_is_immutable_and_machine_readable() -> None:
    diagnostic = FlowDiagnostic(
        code="FLOWSPEC_TEST",
        severity="warning",
        path="/path/0",
        message="A deterministic finding.",
        related_locations=(DiagnosticLocation(path="/slots/category", message="Declared here."),),
        suggested_fix="Use the declared category slot.",
    )
    report = FlowCheckReport(diagnostics=(diagnostic,), compilation="not_requested")

    assert report.to_dict() == {
        "valid": True,
        "compilation": "not_requested",
        "diagnostics": [
            {
                "code": "FLOWSPEC_TEST",
                "severity": "warning",
                "path": "/path/0",
                "message": "A deterministic finding.",
                "related_locations": [{"path": "/slots/category", "message": "Declared here."}],
                "suggested_fix": "Use the declared category slot.",
            }
        ],
    }
    with pytest.raises(FrozenInstanceError):
        diagnostic.message = "mutated"  # type: ignore[misc]


def test_structural_check_aggregates_every_schema_error_deterministically(
    luminaria_doc: dict[str, object],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["flow"] = "Invalid Flow"
    invalid_flow["version"] = "2"
    invalid_flow["unknown"] = True

    expected_errors = tuple(Draft202012Validator(schema()).iter_errors(cast(Any, invalid_flow)))
    first_report = check_flow(invalid_flow, compile_document=False)
    second_report = check_flow(invalid_flow, compile_document=False)

    assert len(first_report.diagnostics) == len(expected_errors)
    assert first_report == second_report
    assert first_report.compilation == "not_requested"
    assert {diagnostic.path for diagnostic in first_report.diagnostics}.issuperset(
        {"", "/flow", "/version"}
    )
    assert all(
        diagnostic.code.startswith("FLOWSPEC_SCHEMA_") for diagnostic in first_report.diagnostics
    )


def test_missing_required_property_uses_the_missing_property_pointer() -> None:
    flow_report = check_flow({"schema": "flowspec/2"}, compile_document=False)

    assert "/flow" in {diagnostic.path for diagnostic in flow_report.diagnostics}
    assert "/path" in {diagnostic.path for diagnostic in flow_report.diagnostics}


def test_structural_errors_skip_the_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler_spy = Mock()
    monkeypatch.setattr("flowspec2.checker.compile_flow", compiler_spy)

    flow_report = check_flow({}, compile_document=True)

    assert flow_report.compilation == "skipped"
    compiler_spy.assert_not_called()


def test_structurally_valid_semantic_failure_skips_compilation(
    luminaria_doc: dict[str, object],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["path"][0] = {"use": "missing@1"}  # type: ignore[index]

    flow_report = check_flow(invalid_flow)

    assert flow_report.compilation == "skipped"
    assert [diagnostic.code for diagnostic in flow_report.diagnostics] == [
        "FLOWSPEC_SEMANTIC_USE_DECLARATION_MISSING"
    ]
    assert flow_report.diagnostics[0].path == "/path/0/use"
    assert "missing@1" in flow_report.diagnostics[0].message


def test_valid_flow_compiles_and_serialized_json_matches_object_check(
    luminaria_doc: dict[str, object],
) -> None:
    object_report = check_flow(luminaria_doc)
    serialized_report = check_json(json.dumps(luminaria_doc))

    assert object_report == serialized_report
    assert object_report.is_valid
    assert object_report.compilation == "succeeded"


def test_semantically_valid_compilation_failure_is_reported(
    luminaria_doc: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_compilation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("compiler integration failed")

    monkeypatch.setattr("flowspec2.checker.compile_flow", reject_compilation)

    flow_report = check_flow(luminaria_doc)

    assert flow_report.compilation == "failed"
    assert [diagnostic.code for diagnostic in flow_report.diagnostics] == [
        "FLOWSPEC_COMPILE_FAILED"
    ]


def test_invalid_json_is_a_root_diagnostic() -> None:
    flow_report = check_json('{"schema":')

    assert flow_report.compilation == "skipped"
    assert flow_report.diagnostics[0].code == "FLOWSPEC_JSON_INVALID"
    assert flow_report.diagnostics[0].path == ""


@pytest.mark.parametrize(
    "serialized_flow",
    [
        '{"schema":"flowspec/2","value":NaN}',
        '{"schema":"flowspec/2","value":Infinity}',
        '{"schema":"flowspec/2","schema":"flowspec/2"}',
    ],
)
def test_nonstandard_or_ambiguous_json_is_rejected_at_parse_boundary(
    serialized_flow: str,
) -> None:
    flow_report = check_json(serialized_flow)

    assert flow_report.compilation == "skipped"
    assert flow_report.diagnostics[0].code == "FLOWSPEC_JSON_INVALID"


@pytest.mark.parametrize(
    ("invalid_value", "expected_path", "expected_detail"),
    [
        (math.nan, "/domains/Distance/minimum", "non-finite number"),
        (object(), "/domains/Distance/minimum", "unsupported Python type"),
    ],
)
def test_object_check_rejects_non_json_values_before_semantic_linking(
    luminaria_doc: dict[str, Any],
    invalid_value: object,
    expected_path: str,
    expected_detail: str,
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["domains"]["Distance"] = {
        "type": "number",
        "minimum": invalid_value,
    }

    flow_report = check_flow(invalid_flow)

    assert flow_report.compilation == "skipped"
    assert flow_report.diagnostics[0].code == "FLOWSPEC_JSON_VALUE_INVALID"
    assert flow_report.diagnostics[0].path == expected_path
    assert expected_detail in flow_report.diagnostics[0].message
