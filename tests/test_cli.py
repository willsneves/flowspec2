"""CLI smoke: validate / graph / mermaid."""

from __future__ import annotations

from pathlib import Path

from flowspec2.cli import main

LUM = str(Path(__file__).resolve().parents[1] / "examples" / "reparo_luminaria.flow.json")


def test_validate_ok(capsys):
    assert main(["validate", LUM]) == 0
    assert "valid flowspec/2" in capsys.readouterr().out


def test_graph_lists_nodes(capsys):
    assert main(["graph", LUM]) == 0
    out = capsys.readouterr().out
    assert "open_ticket" in out and "collect_address" in out


def test_mermaid_export(capsys):
    assert main(["mermaid", LUM]) == 0
    out = capsys.readouterr().out
    assert "graph" in out.lower()  # mermaid header (graph TD / flowchart)
    assert "open_ticket" in out


def test_unknown_command_returns_usage():
    assert main(["frobnicate", LUM]) == 2
