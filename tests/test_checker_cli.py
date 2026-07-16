"""CLI coverage for aggregate check and validate reports."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from flowspec2.cli import main

STREETLIGHT_FLOW = Path(__file__).resolve().parents[1] / "examples" / "streetlight_repair.flow.json"


def test_check_json_output_is_deterministic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", str(STREETLIGHT_FLOW), "--json"]) == 0
    first_output = capsys.readouterr()
    assert main(["check", str(STREETLIGHT_FLOW), "--json"]) == 0
    second_output = capsys.readouterr()

    assert first_output.err == second_output.err == ""
    assert first_output.out == second_output.out
    assert json.loads(first_output.out) == {
        "compilation": "succeeded",
        "diagnostics": [],
        "valid": True,
    }


def test_check_json_aggregates_structural_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid_flow_path = tmp_path / "invalid.flow.json"
    invalid_flow_path.write_text('{"schema":"wrong"}', encoding="utf-8")

    assert main(["check", str(invalid_flow_path), "--json"]) == 1

    captured_output = capsys.readouterr()
    flow_report = json.loads(captured_output.out)
    assert captured_output.err == ""
    assert flow_report["valid"] is False
    assert flow_report["compilation"] == "skipped"
    assert len(flow_report["diagnostics"]) > 1
    assert {diagnostic["path"] for diagnostic in flow_report["diagnostics"]}.issuperset(
        {"/flow", "/path", "/schema"}
    )


def test_validate_uses_aggregate_diagnostics_with_correlated_log_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid_flow_path = tmp_path / "invalid.flow.json"
    invalid_flow_path.write_text('{"schema":"flowspec/2"}', encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="flowspec2.cli"):
        assert main(["validate", str(invalid_flow_path)]) == 1

    captured_output = capsys.readouterr()
    rendered_log_ids = re.findall(r"\[log_id=(\d+)\]", captured_output.err)
    assert captured_output.out == ""
    assert len(rendered_log_ids) > 1
    assert all(
        any(getattr(record, "log_id", None) == log_id for record in caplog.records)
        for log_id in rendered_log_ids
    )


def test_check_reports_semantic_failure_before_compilation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    flow_document = json.loads(STREETLIGHT_FLOW.read_text(encoding="utf-8"))
    flow_document["path"][0] = {"use": "missing@1"}
    invalid_flow_path = tmp_path / "invalid.flow.json"
    invalid_flow_path.write_text(json.dumps(flow_document), encoding="utf-8")

    assert main(["check", str(invalid_flow_path), "--json"]) == 1

    flow_report = json.loads(capsys.readouterr().out)
    assert flow_report["compilation"] == "skipped"
    assert [diagnostic["code"] for diagnostic in flow_report["diagnostics"]] == [
        "FLOWSPEC_SEMANTIC_USE_DECLARATION_MISSING"
    ]
