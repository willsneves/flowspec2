"""Read-only canonical document and IR CLI output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowspec2.cli import main
from flowspec2.schema import validate_flow

POTHOLE_FLOW = Path(__file__).resolve().parents[1] / "examples" / "pothole_repair.flow.json"


def test_normalize_command_emits_valid_canonical_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["normalize", str(POTHOLE_FLOW)]) == 0

    normalized_document = json.loads(capsys.readouterr().out)
    validate_flow(normalized_document)
    assert normalized_document["config"]["max_attempts"] == 3


def test_ir_command_emits_machine_readable_contracts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ir", str(POTHOLE_FLOW)]) == 0

    flow_ir = json.loads(capsys.readouterr().out)
    assert flow_ir["format"] == "flowspec/2"
    assert flow_ir["document"]["flow"] == "pothole_repair"
    assert flow_ir["nodes"][0]["identifier"] == "__init__"
    assert len(flow_ir["digest"]) == 64
