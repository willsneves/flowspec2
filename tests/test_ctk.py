"""Executable conformance-kit corpus and report contracts."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from flowspec2 import FLOW_IR_FORMAT
from flowspec2.authoring import CtkCorpus, load_ctk_corpus, run_ctk_corpus

_CORPUS_PATH = (
    Path(__file__).parents[1] / "src" / "flowspec2" / "authoring" / "corpus" / "flowspec2.ctk.json"
)


def test_corpus_loads_fixture_references_without_mutating_sources() -> None:
    fixture_path = _CORPUS_PATH.parent / "linear.case.json"
    fixture_before = fixture_path.read_text(encoding="utf-8")
    first_corpus = load_ctk_corpus(_CORPUS_PATH)
    mutated_source = first_corpus.cases[0].source_document()
    mutated_source["flow"] = "mutated"
    second_corpus = load_ctk_corpus(_CORPUS_PATH)

    assert first_corpus.digest == second_corpus.digest
    assert second_corpus.cases[0].source_document()["flow"] == "linear_maintenance_request"
    assert fixture_path.read_text(encoding="utf-8") == fixture_before


async def test_corpus_executes_validation_diagnostic_ir_and_trace_oracles() -> None:
    conformance_corpus = load_ctk_corpus(_CORPUS_PATH)

    first_report = await run_ctk_corpus(conformance_corpus)
    second_report = await run_ctk_corpus(conformance_corpus)

    assert first_report.passed
    assert first_report.canonical_json() == second_report.canonical_json()
    assert first_report.digest == second_report.digest
    assert json.loads(first_report.canonical_json()) == first_report.to_dict()
    assert [case_report.identifier for case_report in first_report.case_reports] == [
        "linear_valid_runtime",
        "structural_diagnostic_order",
        "terminal_success_runtime",
        "address_subflow_runtime",
        "typed_external_resume_policies",
        "gated_derivation_gate_true_runtime",
        "gated_derivation_gate_false_runtime",
        "gated_derivation_default_runtime",
    ]
    report_by_identifier = {
        case_report.identifier: case_report.to_dict() for case_report in first_report.case_reports
    }
    positive_contract = report_by_identifier["linear_valid_runtime"]
    assert positive_contract["runtime_trace"]
    positive_ir = cast(dict[str, Any], positive_contract["ir"])
    assert positive_ir["ir_format"] == FLOW_IR_FORMAT
    assert positive_ir["canonical_ir"]["nodes"][0]["identifier"] == "__init__"
    negative_check = cast(
        dict[str, Any], report_by_identifier["structural_diagnostic_order"]["check"]
    )
    assert [
        (diagnostic["code"], diagnostic["path"])
        for diagnostic in cast(list[dict[str, str]], negative_check["diagnostics"])
    ] == [
        ("FLOWSPEC_SCHEMA_ADDITIONALPROPERTIES", ""),
        ("FLOWSPEC_SCHEMA_PATTERN", "/flow"),
        ("FLOWSPEC_SCHEMA_PATTERN", "/version"),
    ]
    terminal_contract = report_by_identifier["terminal_success_runtime"]
    terminal_trace = cast(list[dict[str, Any]], terminal_contract["runtime_trace"])
    assert terminal_trace[-1]["data"] == {
        "_reset_on_next_call": True,
        "problem_description": "Broken signal cabinet",
        "protocol_id": "SGRC-72E4E60B8E",
    }
    subflow_contract = report_by_identifier["address_subflow_runtime"]
    subflow_trace = cast(list[dict[str, Any]], subflow_contract["runtime_trace"])
    assert subflow_trace[-1]["data"]["address_confirmed"] is True
    external_contract = report_by_identifier["typed_external_resume_policies"]
    external_trace = cast(list[dict[str, Any]], external_contract["runtime_trace"])
    resume_marker = external_trace[1]["agent_response"]["interactive"]["resume_contract"]
    assert resume_marker["duplicate"] == "ignore"
    assert resume_marker["late"] == "reject"
    assert external_trace[2] == external_trace[3]
    assert external_trace[-1]["status"] == "error"
    assert "rejected late resume delivery" in external_trace[-1]["agent_response"]["error_message"]

    gate_true_trace = cast(
        list[dict[str, Any]],
        report_by_identifier["gated_derivation_gate_true_runtime"]["runtime_trace"],
    )
    assert gate_true_trace[1]["agent_response"]["description"] == "How large is the pothole?"
    assert gate_true_trace[-1] == {
        "status": "completed",
        "data": {
            "backend_classification": "POTHOLE_SMALL",
            "issue_category": "Pothole",
            "issue_detail": "Small",
        },
        "agent_response": {"description": "Serviço concluído com sucesso."},
    }

    case_by_identifier = {
        conformance_case.identifier: conformance_case
        for conformance_case in conformance_corpus.cases
    }
    gate_false_case = case_by_identifier["gated_derivation_gate_false_runtime"]
    assert gate_false_case.turns[-1].payload() == {
        "issue_category": "Obstruction",
        "issue_detail": "Large",
    }
    gate_false_trace = cast(
        list[dict[str, Any]],
        report_by_identifier["gated_derivation_gate_false_runtime"]["runtime_trace"],
    )
    assert gate_false_trace[-1] == {
        "status": "completed",
        "data": {
            "backend_classification": "STREET_OBSTRUCTION",
            "issue_category": "Obstruction",
        },
        "agent_response": {"description": "Serviço concluído com sucesso."},
    }

    default_case = case_by_identifier["gated_derivation_default_runtime"]
    default_derive = default_case.source_document()["derive"][0]
    assert default_derive["lookup"] == {
        "Obstruction|null": "STREET_OBSTRUCTION",
        "Pothole|Small": "POTHOLE_SMALL",
    }
    assert default_derive["default"] == "$from[0]"
    default_trace = cast(
        list[dict[str, Any]],
        report_by_identifier["gated_derivation_default_runtime"]["runtime_trace"],
    )
    assert default_trace[-1] == {
        "status": "completed",
        "data": {
            "backend_classification": "Pothole",
            "issue_category": "Pothole",
            "issue_detail": "Large",
        },
        "agent_response": {"description": "Serviço concluído com sucesso."},
    }


async def test_report_exposes_exact_ir_oracle_mismatch() -> None:
    conformance_corpus = load_ctk_corpus(_CORPUS_PATH)
    valid_case = conformance_corpus.cases[0]
    assert valid_case.expected_ir is not None
    invalid_ir_oracle = replace(valid_case.expected_ir, digest="0" * 64)
    mismatched_case = replace(valid_case, expected_ir=invalid_ir_oracle)
    mismatched_corpus = CtkCorpus(cases=(mismatched_case, *conformance_corpus.cases[1:]))

    mismatch_report = await run_ctk_corpus(mismatched_corpus)

    assert not mismatch_report.passed
    assert mismatch_report.case_reports[1].passed
    assert mismatch_report.case_reports[0].failures[0].startswith("IR oracle mismatch:")


async def test_report_to_dict_returns_owned_nested_contracts() -> None:
    conformance_report = await run_ctk_corpus(load_ctk_corpus(_CORPUS_PATH))
    first_contract = conformance_report.to_dict()
    first_contract["cases"] = []

    assert conformance_report.to_dict()["cases"]


def test_loader_rejects_source_path_traversal(tmp_path: Path) -> None:
    external_source = tmp_path.parent / "outside.json"
    external_source.write_text("{}", encoding="utf-8")
    invalid_manifest = {
        "format": "flowspec2/ctk-corpus",
        "version": "1",
        "cases": [
            {
                "identifier": "unsafe_path",
                "source": {"path": "../outside.json", "pointer": ""},
                "expected": {"compilation": "skipped", "diagnostics": []},
            }
        ],
    }
    manifest_path = tmp_path / "unsafe.ctk.json"
    manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="leaves the corpus root"):
        load_ctk_corpus(manifest_path)


def test_corpus_contract_returns_owned_source_documents() -> None:
    conformance_corpus = load_ctk_corpus(_CORPUS_PATH)
    first_contract = conformance_corpus.to_dict()
    copied_contract = copy.deepcopy(first_contract)
    copied_cases = cast(list[dict[str, Any]], copied_contract["cases"])
    copied_cases[0]["source"]["flow"] = "mutated"
    fresh_cases = cast(list[dict[str, Any]], conformance_corpus.to_dict()["cases"])

    assert fresh_cases[0]["source"]["flow"] == "linear_maintenance_request"
