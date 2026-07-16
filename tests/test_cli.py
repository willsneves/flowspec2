"""CLI smoke and compatibility conversion tests."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from flowspec2.cli import main
from flowspec2.compat.yaml import load_yaml_mapping

LUM = str(Path(__file__).resolve().parents[1] / "examples" / "streetlight_repair.flow.json")
FIXTURE_DIRECTORY = Path(__file__).with_name("fixtures")
OPEN_WORKFLOW_PORTABLE_FLOW = FIXTURE_DIRECTORY / "open_workflow" / "portable.flow.json"
ARBITRARY_OPEN_WORKFLOW = FIXTURE_DIRECTORY / "open_workflow" / "arbitrary.workflow.json"
RASA_PORTABLE_FLOWS = FIXTURE_DIRECTORY / "rasa" / "portable_flows.yml"
RASA_PORTABLE_DOMAIN = FIXTURE_DIRECTORY / "rasa" / "portable_domain.yml"


def test_validate_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", LUM]) == 0
    assert "valid flowspec/2" in capsys.readouterr().out


def test_graph_lists_nodes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["graph", LUM]) == 0
    out = capsys.readouterr().out
    assert "open_ticket" in out and "collect_address" in out


def test_mermaid_export(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mermaid", LUM]) == 0
    out = capsys.readouterr().out
    assert "graph" in out.lower()  # mermaid header (graph TD / flowchart)
    assert "open_ticket" in out


def test_unknown_command_returns_usage_with_log_id(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="flowspec2.cli"):
        assert main(["frobnicate", LUM]) == 2

    standard_error = capsys.readouterr().err
    log_id_match = re.search(r"\[log_id=(\d+)\]", standard_error)
    assert log_id_match is not None
    assert any(
        getattr(record, "log_id", None) == log_id_match.group(1) for record in caplog.records
    )


def test_open_workflow_cli_round_trip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_path = tmp_path / "portable.workflow.json"
    round_trip_path = tmp_path / "round-trip.flow.json"

    assert (
        main(
            [
                "open-workflow-export",
                str(OPEN_WORKFLOW_PORTABLE_FLOW),
                "--output",
                str(workflow_path),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "open-workflow-import",
                str(workflow_path),
                "--output",
                str(round_trip_path),
            ]
        )
        == 0
    )

    original_flow = json.loads(OPEN_WORKFLOW_PORTABLE_FLOW.read_text(encoding="utf-8"))
    round_trip_flow = json.loads(round_trip_path.read_text(encoding="utf-8"))
    assert round_trip_flow == original_flow
    assert "Wrote Open Workflow profile" in capsys.readouterr().out


def test_open_workflow_cli_refuses_to_overwrite_existing_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_path = tmp_path / "portable.workflow.yaml"
    workflow_path.write_text("preserve me\n", encoding="utf-8")

    assert (
        main(
            [
                "open-workflow-export",
                str(OPEN_WORKFLOW_PORTABLE_FLOW),
                "--output",
                str(workflow_path),
            ]
        )
        == 1
    )

    assert workflow_path.read_text(encoding="utf-8") == "preserve me\n"
    standard_error = capsys.readouterr().err
    assert "already exists" in standard_error
    assert re.search(r"\[log_id=\d+\]", standard_error)


def test_open_workflow_cli_does_not_write_invalid_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "invalid.flow.json"

    assert (
        main(
            [
                "open-workflow-import",
                str(ARBITRARY_OPEN_WORKFLOW),
                "--output",
                str(output_path),
            ]
        )
        == 1
    )

    assert not output_path.exists()
    assert "open_workflow.invalid_profile" in capsys.readouterr().err


def test_rasa_cli_enforces_loss_policy_and_round_trips(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    imported_flow_path = tmp_path / "portable.flow.json"
    bundle_directory = tmp_path / "rasa-bundle"
    round_trip_path = tmp_path / "round-trip.flow.json"

    assert (
        main(
            [
                "rasa-import",
                str(RASA_PORTABLE_FLOWS),
                "--domain",
                str(RASA_PORTABLE_DOMAIN),
                "--version",
                "2.3.4",
                "--output",
                str(imported_flow_path),
            ]
        )
        == 1
    )
    assert not imported_flow_path.exists()
    assert "RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED" in capsys.readouterr().err

    assert (
        main(
            [
                "rasa-import",
                str(RASA_PORTABLE_FLOWS),
                "--domain",
                str(RASA_PORTABLE_DOMAIN),
                "--version",
                "2.3.4",
                "--output",
                str(imported_flow_path),
                "--allow-lossy",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "rasa-export",
                str(imported_flow_path),
                "--output-dir",
                str(bundle_directory),
            ]
        )
        == 1
    )
    assert not bundle_directory.exists()
    assert "RASA_FLOW_VERSION_UNSUPPORTED" in capsys.readouterr().err

    assert (
        main(
            [
                "rasa-export",
                str(imported_flow_path),
                "--output-dir",
                str(bundle_directory),
                "--allow-lossy",
            ]
        )
        == 0
    )
    assert set(path.name for path in bundle_directory.iterdir()) == {
        "domain.yml",
        "flows.yml",
    }
    exported_flows = load_yaml_mapping(bundle_directory / "flows.yml")
    assert exported_flows["flows"]["report_issue"]["persisted_slots"] == [
        "category",
        "details",
        "confirmed",
    ]

    assert (
        main(
            [
                "rasa-import",
                str(bundle_directory / "flows.yml"),
                "--domain",
                str(bundle_directory / "domain.yml"),
                "--version",
                "2.3.4",
                "--output",
                str(round_trip_path),
                "--allow-lossy",
            ]
        )
        == 0
    )
    round_trip_flow = json.loads(round_trip_path.read_text(encoding="utf-8"))
    assert round_trip_flow["version"] == "2.3.4"
    assert [step["slot"] for step in round_trip_flow["path"]] == [
        "category",
        "details",
        "confirmed",
    ]
