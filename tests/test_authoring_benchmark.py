"""Provider-neutral AI-authoring benchmark contracts and deterministic baselines."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

import pytest

from flowspec2.authoring import (
    TOKEN_PROXY_BYTES_PER_UNIT,
    AcceptanceExpectation,
    AuthoredSource,
    AuthoringAcceptanceContract,
    AuthoringAttempt,
    AuthoringBenchmarkCase,
    AuthoringBenchmarkLimits,
    AuthoringBenchmarkReport,
    AuthoringBenchmarkResult,
    AuthoringRequest,
    RequiredFlowConstruct,
    SourceAdaptation,
    run_authoring_benchmark,
)
from flowspec2.profiles import reference_profile
from flowspec2.tools import ToolDefinition, ToolRegistry

_FIXTURE_DIRECTORY = Path(__file__).parents[1] / "src" / "flowspec2" / "authoring" / "corpus"


class RequiredConstructFixture(TypedDict):
    identifier: str
    pointer_pattern: str
    expected: NotRequired[object]


class AuthoringFixture(TypedDict):
    format: str
    identifier: str
    prompt: str
    feature_tags: list[str]
    required_constructs: list[RequiredConstructFixture]
    source: dict[str, Any]


def _authoring_fixtures() -> tuple[AuthoringFixture, ...]:
    return tuple(
        cast(AuthoringFixture, json.loads(fixture_path.read_text(encoding="utf-8")))
        for fixture_path in sorted(_FIXTURE_DIRECTORY.glob("*.case.json"))
    )


def _benchmark_case(authoring_fixture: AuthoringFixture) -> AuthoringBenchmarkCase:
    required_constructs = tuple(
        (
            RequiredFlowConstruct.expecting(
                required_construct["identifier"],
                required_construct["pointer_pattern"],
                required_construct["expected"],
            )
            if "expected" in required_construct
            else RequiredFlowConstruct(
                identifier=required_construct["identifier"],
                pointer_pattern=required_construct["pointer_pattern"],
            )
        )
        for required_construct in authoring_fixture["required_constructs"]
    )
    return AuthoringBenchmarkCase.expecting_flow(
        identifier=authoring_fixture["identifier"],
        prompt=authoring_fixture["prompt"],
        expected_flow=authoring_fixture["source"],
        feature_tags=tuple(authoring_fixture["feature_tags"]),
        required_constructs=required_constructs,
    )


def _pretty_source(flow_document: object) -> str:
    return json.dumps(flow_document, ensure_ascii=False, indent=2, sort_keys=True)


def _authored(source: str) -> AuthoredSource:
    return AuthoredSource(source=source)


def _fixture_author(
    source_by_case: dict[str, dict[str, Any]],
) -> Callable[[AuthoringRequest], AuthoredSource]:
    def author(authoring_request: AuthoringRequest) -> AuthoredSource:
        return _authored(_pretty_source(source_by_case[authoring_request.task.identifier]))

    return author


async def _alternate_ticket_tool(**_ticket_inputs: Any) -> dict[str, Any]:
    return {
        "status": "success",
        "protocol_id": "ALTERNATE-PROTOCOL",
    }


def test_fixture_corpus_compiles_and_serializes_deterministically() -> None:
    authoring_fixtures = _authoring_fixtures()
    benchmark_cases = tuple(
        _benchmark_case(authoring_fixture) for authoring_fixture in authoring_fixtures
    )
    source_by_case = {
        authoring_fixture["identifier"]: authoring_fixture["source"]
        for authoring_fixture in authoring_fixtures
    }

    first_report = run_authoring_benchmark(
        "reference_corpus",
        benchmark_cases,
        _fixture_author(source_by_case),
    )
    second_report = run_authoring_benchmark(
        "reference_corpus",
        tuple(reversed(benchmark_cases)),
        _fixture_author(source_by_case),
    )

    assert first_report == second_report
    assert first_report.successful_cases == first_report.total_cases == len(authoring_fixtures)
    assert first_report.total_attempts == len(authoring_fixtures)
    assert first_report.total_correction_rounds == 0
    assert first_report.profile_identifier == "flowspec2/reference@2"
    assert json.loads(first_report.to_json()) == first_report.to_dict()
    assert first_report.to_json() == second_report.to_json()
    assert {
        feature_tag
        for case_result in first_report.case_results
        for feature_tag in case_result.feature_tags
    }.issuperset(
        {
            "linear",
            "gate",
            "derive",
            "subflow",
            "terminal",
            "await_external",
            "correction",
        }
    )
    for case_result in first_report.case_results:
        authoring_attempt = case_result.attempts[0]
        compact_source = json.dumps(
            source_by_case[case_result.case_identifier],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        compact_source_bytes = len(compact_source.encode("utf-8"))
        assert authoring_attempt.compilation == "succeeded"
        assert authoring_attempt.diagnostics == ()
        assert authoring_attempt.canonical_source_bytes == compact_source_bytes
        assert (
            authoring_attempt.token_proxy_units
            == (compact_source_bytes + TOKEN_PROXY_BYTES_PER_UNIT - 1) // TOKEN_PROXY_BYTES_PER_UNIT
        )


def test_fixture_oracles_distinguish_exact_rails_from_operational_presence() -> None:
    required_constructs = tuple(
        required_construct
        for authoring_fixture in _authoring_fixtures()
        for required_construct in authoring_fixture["required_constructs"]
    )
    presence_only_identifiers = {
        "route-trigger-examples",
        "request-description-extraction-hint",
    }

    assert {
        required_construct["identifier"]
        for required_construct in required_constructs
        if "expected" not in required_construct
    } == presence_only_identifiers
    assert all(
        "expected" in required_construct
        for required_construct in required_constructs
        if required_construct["identifier"] not in presence_only_identifiers
    )


def test_required_construct_projection_distinguishes_presence_from_exact_null() -> None:
    presence_requirement = RequiredFlowConstruct(
        identifier="presence-only",
        pointer_pattern="/route/trigger_phrases",
    )
    exact_null_requirement = RequiredFlowConstruct.expecting(
        "exact-null",
        "/route/trigger_phrases",
        None,
    )

    assert "expected" not in presence_requirement.to_dict()
    assert exact_null_requirement.to_dict()["expected"] is None


def test_author_request_exposes_public_acceptance_without_reference_source() -> None:
    benchmark_case = _benchmark_case(
        next(
            authoring_fixture
            for authoring_fixture in _authoring_fixtures()
            if authoring_fixture["identifier"] == "linear_collection"
        )
    )
    observed_requests: list[AuthoringRequest] = []

    def inspecting_author(authoring_request: AuthoringRequest) -> AuthoredSource:
        observed_requests.append(authoring_request)
        return AuthoredSource(benchmark_case.expected_flow_json)

    run_authoring_benchmark("oracle_boundary", (benchmark_case,), inspecting_author)

    authoring_request = observed_requests[0]
    assert {contract_field.name for contract_field in fields(authoring_request)} == {
        "task",
        "format_identifier",
        "profile_identifier",
        "profile_contract_json",
        "correction_round",
        "previous_source",
        "previous_diagnostics",
    }
    assert {contract_field.name for contract_field in fields(authoring_request.task)} == {
        "acceptance",
        "identifier",
        "prompt",
    }
    acceptance_document = authoring_request.task.acceptance.to_dict()
    assert acceptance_document["format"] == "flowspec2/authoring-acceptance@2"
    assert acceptance_document["expectations"]
    assert acceptance_document["required_constructs"]
    assert acceptance_document["forbidden_constructs"]
    assert not hasattr(authoring_request, "benchmark_case")
    assert not hasattr(authoring_request.task, "expected_flow_json")
    acceptance_json = json.dumps(
        acceptance_document,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert benchmark_case.expected_flow_json not in acceptance_json
    expected_flow = cast(dict[str, Any], json.loads(benchmark_case.expected_flow_json))
    assert expected_flow["flow"] not in acceptance_json
    assert cast(dict[str, Any], expected_flow["route"])["description"] not in acceptance_json
    assert all(domain_name not in acceptance_json for domain_name in expected_flow["domains"])
    assert all(
        cast(str, path_step["step"]) not in acceptance_json
        for path_step in cast(list[dict[str, Any]], expected_flow["path"])
        if isinstance(path_step.get("step"), str)
    )
    assert all(
        cast(dict[str, Any], path_step["prompt"])["text"] not in acceptance_json
        for path_step in cast(list[dict[str, Any]], expected_flow["path"])
        if "prompt" in path_step
        and cast(dict[str, Any], path_step["prompt"]).get("verbatim") is not True
    )


def test_terminal_oracle_rejects_semantically_opposite_values() -> None:
    terminal_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "terminal_fulfillment"
    )
    disabled_idempotency_source = copy.deepcopy(terminal_fixture["source"])
    cast(dict[str, Any], disabled_idempotency_source["terminal"])["idempotent"] = False
    wrong_output_source = copy.deepcopy(terminal_fixture["source"])
    cast(dict[str, Any], cast(dict[str, Any], wrong_output_source["terminal"])["outputs"])[
        "protocol_id"
    ] = "result.message"

    disabled_idempotency_attempt = (
        run_authoring_benchmark(
            "terminal_opposite",
            (_benchmark_case(terminal_fixture),),
            _fixture_author({terminal_fixture["identifier"]: disabled_idempotency_source}),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )
    assert disabled_idempotency_attempt.compilation == "succeeded"
    assert not disabled_idempotency_attempt.succeeded
    assert [diagnostic.code for diagnostic in disabled_idempotency_attempt.diagnostics] == [
        "AUTHORING_REQUIRED_TERMINAL_IDEMPOTENCY_MISMATCH"
    ]

    wrong_output_attempt = (
        run_authoring_benchmark(
            "terminal_opposite",
            (_benchmark_case(terminal_fixture),),
            _fixture_author({terminal_fixture["identifier"]: wrong_output_source}),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )
    assert wrong_output_attempt.compilation == "failed"
    assert not wrong_output_attempt.succeeded
    assert [diagnostic.code for diagnostic in wrong_output_attempt.diagnostics] == [
        "FLOWSPEC_COMPILE_FAILED",
        "AUTHORING_REQUIRED_TERMINAL_OUTPUT_MISMATCH",
    ]

    mismatch_diagnostic = (
        run_authoring_benchmark(
            "terminal_diagnostic",
            (_benchmark_case(terminal_fixture),),
            lambda _authoring_request: _authored(_pretty_source(disabled_idempotency_source)),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
        .diagnostics[0]
    )
    assert "matched false" in mismatch_diagnostic.message
    assert mismatch_diagnostic.suggested_fix == (
        "Set a location matching '/terminal/idempotent' to true."
    )


def test_terminal_oracle_rejects_an_alternate_compatible_tool() -> None:
    terminal_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "terminal_fulfillment"
    )
    terminal_source = copy.deepcopy(terminal_fixture["source"])
    cast(dict[str, Any], terminal_source["terminal"])["tool"] = "alternate_ticket"
    ticketing_definition = reference_profile().tools.definition("open_service_request")
    alternate_registry = ToolRegistry()

    alternate_registry.register(
        "alternate_ticket",
        _alternate_ticket_tool,
        definition=ToolDefinition(
            name="alternate_ticket",
            version=ticketing_definition.version,
            description="Compatible alternate ticket tool used by the oracle regression test.",
            input_schema=ticketing_definition.input_schema,
            output_schema=ticketing_definition.output_schema,
            effects=ticketing_definition.effects,
        ),
    )
    alternate_profile = replace(reference_profile(), tools=alternate_registry)

    benchmark_report = run_authoring_benchmark(
        "terminal_tool_opposite",
        (_benchmark_case(terminal_fixture),),
        lambda _authoring_request: _authored(_pretty_source(terminal_source)),
        limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        profile=alternate_profile,
    )

    authoring_attempt = benchmark_report.case_results[0].attempts[0]
    assert authoring_attempt.compilation == "succeeded"
    assert not authoring_attempt.succeeded
    assert [diagnostic.code for diagnostic in authoring_attempt.diagnostics] == [
        "AUTHORING_REQUIRED_TERMINAL_TOOL_MISMATCH"
    ]


def test_gated_derive_oracle_rejects_opposite_gate_and_source_order() -> None:
    gated_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "gated_derivation"
    )
    opposite_gate_source = copy.deepcopy(gated_fixture["source"])
    opposite_gate_path = cast(list[dict[str, Any]], opposite_gate_source["path"])
    opposite_gate_path[1]["ask_when"] = {"eq": ["slots.issue_category", "Obstruction"]}
    reversed_sources_source = copy.deepcopy(gated_fixture["source"])
    reversed_derives = cast(list[dict[str, Any]], reversed_sources_source["derive"])
    reversed_derives[0]["from"] = ["issue_detail", "issue_category"]

    opposite_gate_attempt = (
        run_authoring_benchmark(
            "gated_opposite",
            (_benchmark_case(gated_fixture),),
            _fixture_author({gated_fixture["identifier"]: opposite_gate_source}),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )
    assert opposite_gate_attempt.compilation == "succeeded"
    assert not opposite_gate_attempt.succeeded
    assert {diagnostic.code for diagnostic in opposite_gate_attempt.diagnostics} == {
        "AUTHORING_ACCEPTANCE_MISMATCH",
        "AUTHORING_REQUIRED_CONDITIONAL_GATE_MISMATCH",
    }

    reversed_sources_attempt = (
        run_authoring_benchmark(
            "gated_opposite",
            (_benchmark_case(gated_fixture),),
            _fixture_author({gated_fixture["identifier"]: reversed_sources_source}),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )
    reversed_diagnostic_codes = {
        diagnostic.code for diagnostic in reversed_sources_attempt.diagnostics
    }
    assert reversed_sources_attempt.compilation == "skipped"
    assert not reversed_sources_attempt.succeeded
    assert "AUTHORING_REQUIRED_DERIVE_SOURCES_MISMATCH" in reversed_diagnostic_codes
    assert "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_VALUE_OUT_OF_DOMAIN" in reversed_diagnostic_codes


def test_address_oracle_rejects_disabled_confirmation() -> None:
    address_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "address_subflow"
    )
    address_source = copy.deepcopy(address_fixture["source"])
    address_uses = cast(list[dict[str, Any]], address_source["uses"])
    cast(dict[str, Any], address_uses[0]["with"])["needs_confirmation"] = False

    benchmark_report = run_authoring_benchmark(
        "address_opposite",
        (_benchmark_case(address_fixture),),
        lambda _authoring_request: _authored(_pretty_source(address_source)),
        limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
    )

    authoring_attempt = benchmark_report.case_results[0].attempts[0]
    assert authoring_attempt.compilation == "succeeded"
    assert not authoring_attempt.succeeded
    assert {diagnostic.code for diagnostic in authoring_attempt.diagnostics} == {
        "AUTHORING_ACCEPTANCE_MISMATCH",
        "AUTHORING_REQUIRED_SUBFLOW_CONFIGURATION_MISMATCH",
    }


def test_correction_protocol_supplies_previous_source_and_diagnostics() -> None:
    authoring_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    observed_requests: list[AuthoringRequest] = []

    def correcting_author(authoring_request: AuthoringRequest) -> AuthoredSource:
        observed_requests.append(authoring_request)
        if authoring_request.correction_round == 0:
            return _authored('{"schema":')
        return _authored(_pretty_source(authoring_fixture["source"]))

    benchmark_report = run_authoring_benchmark(
        "correction_protocol",
        (_benchmark_case(authoring_fixture),),
        correcting_author,
        limits=AuthoringBenchmarkLimits(max_correction_rounds=1),
    )

    case_result = benchmark_report.case_results[0]
    assert case_result.succeeded
    assert case_result.correction_rounds == 1
    assert case_result.attempts[0].compilation == "skipped"
    assert case_result.attempts[0].canonical_source_bytes is None
    assert case_result.attempts[0].diagnostics[0].code == "AUTHORING_SOURCE_JSON_INVALID"
    assert observed_requests[0].previous_source is None
    assert observed_requests[0].previous_diagnostics == ()
    assert observed_requests[1].previous_source == '{"schema":'
    assert observed_requests[1].previous_diagnostics == case_result.attempts[0].diagnostics


def test_valid_but_irrelevant_flow_fails_required_constructs() -> None:
    authoring_fixtures = _authoring_fixtures()
    terminal_fixture = next(
        authoring_fixture
        for authoring_fixture in authoring_fixtures
        if authoring_fixture["identifier"] == "terminal_fulfillment"
    )
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in authoring_fixtures
        if authoring_fixture["identifier"] == "linear_collection"
    )

    benchmark_report = run_authoring_benchmark(
        "intent_guard",
        (_benchmark_case(terminal_fixture),),
        lambda _authoring_request: _authored(_pretty_source(linear_fixture["source"])),
        limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
    )

    authoring_attempt = benchmark_report.case_results[0].attempts[0]
    assert authoring_attempt.compilation == "succeeded"
    assert not authoring_attempt.succeeded
    diagnostic_codes = {diagnostic.code for diagnostic in authoring_attempt.diagnostics}
    assert {
        "AUTHORING_REQUIRED_TERMINAL_IDEMPOTENCY_MISSING",
        "AUTHORING_REQUIRED_TERMINAL_INPUT_MISSING",
        "AUTHORING_REQUIRED_TERMINAL_MARKER_MISSING",
        "AUTHORING_REQUIRED_TERMINAL_OUTPUT_MISSING",
        "AUTHORING_REQUIRED_TERMINAL_TOOL_MISSING",
    }.issubset(diagnostic_codes)
    assert {
        "AUTHORING_ACCEPTANCE_MISSING",
        "AUTHORING_ACCEPTANCE_UNEXPECTED",
    }.issubset(diagnostic_codes)


def test_public_acceptance_rejects_every_unrelated_secret_observation() -> None:
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    unrelated_secret_source = copy.deepcopy(linear_fixture["source"])
    cast(dict[str, Any], unrelated_secret_source["domains"])["SecretValue"] = {"type": "free_text"}
    cast(dict[str, Any], unrelated_secret_source["slots"])["secret_value"] = {
        "domain": "SecretValue",
        "persist": "internal",
        "required": True,
        "fill_only_when_asked": True,
    }
    cast(list[dict[str, Any]], unrelated_secret_source["path"]).append(
        {
            "step": "collect_secret_value",
            "slot": "secret_value",
            "prompt": {"text": "Provide the secret value."},
        }
    )

    authoring_attempt = (
        run_authoring_benchmark(
            "closed_oracle",
            (_benchmark_case(linear_fixture),),
            lambda _authoring_request: _authored(_pretty_source(unrelated_secret_source)),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )

    assert authoring_attempt.compilation == "succeeded"
    assert not authoring_attempt.succeeded
    assert {diagnostic.code for diagnostic in authoring_attempt.diagnostics} == {
        "AUTHORING_ACCEPTANCE_UNEXPECTED"
    }
    diagnostic_paths = {diagnostic.path for diagnostic in authoring_attempt.diagnostics}
    assert {
        "/path/1/slot",
        "/slots/secret_value/domain_contract/type",
        "/slots/secret_value/required",
    }.issubset(diagnostic_paths)


def test_acceptance_allows_consistent_internal_identifier_renaming() -> None:
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    alternate_source = copy.deepcopy(linear_fixture["source"])
    alternate_source["domains"] = {"CitizenRequest": {"type": "free_text"}}
    cast(dict[str, Any], alternate_source["slots"])["request_description"]["domain"] = (
        "CitizenRequest"
    )
    cast(list[dict[str, Any]], alternate_source["path"])[0]["step"] = "ask_for_request"

    authoring_attempt = (
        run_authoring_benchmark(
            "aggregate_acceptance",
            (_benchmark_case(linear_fixture),),
            lambda _authoring_request: _authored(_pretty_source(alternate_source)),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )

    assert authoring_attempt.compilation == "succeeded"
    assert authoring_attempt.succeeded
    assert authoring_attempt.diagnostics == ()


def test_acceptance_aggregates_all_semantic_differences_in_one_attempt() -> None:
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    alternate_source = copy.deepcopy(linear_fixture["source"])
    request_slot = cast(dict[str, Any], alternate_source["slots"])["request_description"]
    request_slot["required"] = False
    request_slot["persist"] = "internal"
    cast(dict[str, Any], alternate_source["domains"])["UnusedDomain"] = {"type": "free_text"}

    authoring_attempt = (
        run_authoring_benchmark(
            "aggregate_acceptance",
            (_benchmark_case(linear_fixture),),
            lambda _authoring_request: _authored(_pretty_source(alternate_source)),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )

    assert authoring_attempt.compilation == "succeeded"
    assert not authoring_attempt.succeeded
    assert {
        "/slots/request_description/persist",
        "/slots/request_description/required",
        "/unused_domain_contracts/0/type",
    }.issubset({diagnostic.path for diagnostic in authoring_attempt.diagnostics})
    assert {diagnostic.code for diagnostic in authoring_attempt.diagnostics} == {
        "AUTHORING_ACCEPTANCE_MISMATCH",
        "AUTHORING_ACCEPTANCE_MISSING",
        "AUTHORING_ACCEPTANCE_UNEXPECTED",
    }


def test_acceptance_ignores_declared_presentation_choices() -> None:
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    alternate_source = copy.deepcopy(linear_fixture["source"])
    alternate_source["flow"] = "citizen_chosen_flow_name"
    alternate_source["version"] = "9.8.7"
    cast(dict[str, Any], alternate_source["route"])["description"] = (
        "Route any municipal maintenance description."
    )
    cast(dict[str, Any], alternate_source["route"])["trigger_phrases"] = [
        "a different trigger example"
    ]
    cast(list[dict[str, Any]], alternate_source["path"])[0]["prompt"] = {
        "text": "What should the city maintain?",
        "extract_hint": "Retain the maintenance details supplied by the citizen.",
    }

    authoring_attempt = (
        run_authoring_benchmark(
            "presentation_choices",
            (_benchmark_case(linear_fixture),),
            lambda _authoring_request: _authored(_pretty_source(alternate_source)),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )

    assert authoring_attempt.compilation == "succeeded"
    assert authoring_attempt.succeeded
    assert authoring_attempt.diagnostics == ()


@dataclass(frozen=True)
class ScriptRemovingAdapter:
    """Test adapter proving forbidden checks apply before source lowering."""

    format_identifier: str = "experimental/script-removing"

    def adapt(self, authored_source: str) -> SourceAdaptation:
        authored_document = cast(dict[str, Any], json.loads(authored_source))
        executable_flow = copy.deepcopy(authored_document)
        executable_path = cast(list[dict[str, Any]], executable_flow["path"])
        executable_path[0].pop("script")
        return SourceAdaptation.from_documents(
            format_identifier=self.format_identifier,
            authored_document=authored_document,
            executable_flow=executable_flow,
        )


def test_forbidden_construct_cannot_be_hidden_by_an_adapter() -> None:
    authoring_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    mutated_source = copy.deepcopy(authoring_fixture["source"])
    mutated_path = cast(list[dict[str, Any]], mutated_source["path"])
    mutated_path[0]["script"] = "return citizen_input"

    benchmark_report = run_authoring_benchmark(
        "forbidden_construct",
        (_benchmark_case(authoring_fixture),),
        lambda _authoring_request: _authored(_pretty_source(mutated_source)),
        source_adapter=ScriptRemovingAdapter(),
        limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
    )

    authoring_attempt = benchmark_report.case_results[0].attempts[0]
    assert authoring_attempt.compilation == "succeeded"
    assert not authoring_attempt.succeeded
    assert [diagnostic.code for diagnostic in authoring_attempt.diagnostics] == [
        "AUTHORING_FORBIDDEN_PATH_SCRIPT"
    ]
    assert authoring_attempt.diagnostics[0].path == "/path/0/script"


@dataclass(frozen=True)
class WrappedExperimentalAdapter:
    """Minimal alternate source shape used to exercise cross-format comparison."""

    format_identifier: str = "experimental/wrapped"

    def adapt(self, authored_source: str) -> SourceAdaptation:
        authored_document = cast(dict[str, Any], json.loads(authored_source))
        executable_flow = authored_document["flow_document"]
        return SourceAdaptation.from_documents(
            format_identifier=self.format_identifier,
            authored_document=authored_document,
            executable_flow=executable_flow,
        )


def test_same_case_can_run_through_an_experimental_source_adapter() -> None:
    authoring_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "terminal_fulfillment"
    )
    wrapped_source = {"flow_document": authoring_fixture["source"]}

    benchmark_report = run_authoring_benchmark(
        "adapter_comparison",
        (_benchmark_case(authoring_fixture),),
        lambda _authoring_request: _authored(_pretty_source(wrapped_source)),
        source_adapter=WrappedExperimentalAdapter(),
    )

    assert benchmark_report.format_identifier == "experimental/wrapped"
    assert benchmark_report.successful_cases == 1
    assert benchmark_report.case_results[0].attempts[0].compilation == "succeeded"


@dataclass(frozen=True)
class PrefixedJsonAdapter:
    """Non-JSON syntax with an adapter-owned canonical representation."""

    format_identifier: str = "experimental/prefixed-json"
    source_prefix: str = "flow "

    def adapt(self, authored_source: str) -> SourceAdaptation:
        if not authored_source.startswith(self.source_prefix):
            raise ValueError(f"source must start with {self.source_prefix!r}")
        authored_document = json.loads(authored_source.removeprefix(self.source_prefix))
        canonical_json = json.dumps(
            authored_document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return SourceAdaptation.from_documents(
            format_identifier=self.format_identifier,
            authored_document=authored_document,
            executable_flow=authored_document,
            canonical_authored_source=f"{self.source_prefix}{canonical_json}",
        )


def test_non_json_adapter_controls_parsing_and_canonical_source_measurement() -> None:
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )
    source_adapter = PrefixedJsonAdapter()
    authored_source = f"{source_adapter.source_prefix}{_pretty_source(linear_fixture['source'])}"
    compact_flow_json = json.dumps(
        linear_fixture["source"],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    canonical_authored_source = f"{source_adapter.source_prefix}{compact_flow_json}"

    authoring_attempt = (
        run_authoring_benchmark(
            "non_json_adapter",
            (_benchmark_case(linear_fixture),),
            lambda _authoring_request: _authored(authored_source),
            source_adapter=source_adapter,
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )

    canonical_source_bytes = len(canonical_authored_source.encode("utf-8"))
    assert authoring_attempt.succeeded
    assert authoring_attempt.raw_source_bytes == len(authored_source.encode("utf-8"))
    assert authoring_attempt.canonical_source_bytes == canonical_source_bytes
    assert canonical_source_bytes != len(compact_flow_json.encode("utf-8"))
    assert (
        authoring_attempt.token_proxy_units
        == (canonical_source_bytes + TOKEN_PROXY_BYTES_PER_UNIT - 1) // TOKEN_PROXY_BYTES_PER_UNIT
    )


def test_adapter_failure_becomes_a_repair_diagnostic() -> None:
    linear_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "linear_collection"
    )

    authoring_attempt = (
        run_authoring_benchmark(
            "adapter_failure",
            (_benchmark_case(linear_fixture),),
            lambda _authoring_request: _authored("not prefixed syntax"),
            source_adapter=PrefixedJsonAdapter(),
            limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        )
        .case_results[0]
        .attempts[0]
    )

    assert authoring_attempt.compilation == "skipped"
    assert not authoring_attempt.succeeded
    assert authoring_attempt.canonical_source_bytes is None
    assert [diagnostic.code for diagnostic in authoring_attempt.diagnostics] == [
        "AUTHORING_SOURCE_ADAPTATION_FAILED"
    ]


def test_benchmark_uses_and_records_the_selected_runtime_profile() -> None:
    authoring_fixture = next(
        authoring_fixture
        for authoring_fixture in _authoring_fixtures()
        if authoring_fixture["identifier"] == "await_and_correction"
    )
    restricted_profile = replace(
        reference_profile(),
        identifier="restricted/reference@2",
        capabilities=frozenset(),
    )
    observed_requests: list[AuthoringRequest] = []

    def fixture_author(authoring_request: AuthoringRequest) -> AuthoredSource:
        observed_requests.append(authoring_request)
        return _authored(_pretty_source(authoring_fixture["source"]))

    benchmark_report = run_authoring_benchmark(
        "profile_contract",
        (_benchmark_case(authoring_fixture),),
        fixture_author,
        limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
        profile=restricted_profile,
    )

    authoring_attempt = benchmark_report.case_results[0].attempts[0]
    assert benchmark_report.profile_identifier == "restricted/reference@2"
    assert observed_requests[0].profile_identifier == benchmark_report.profile_identifier
    assert not authoring_attempt.succeeded
    assert authoring_attempt.compilation == "skipped"
    assert {diagnostic.code for diagnostic in authoring_attempt.diagnostics} == {
        "FLOWSPEC_PROFILE_CAPABILITY_UNAVAILABLE"
    }


def test_case_attempt_result_and_report_contracts_are_immutable() -> None:
    authoring_fixture = _authoring_fixtures()[0]
    benchmark_report = run_authoring_benchmark(
        "immutable_contracts",
        (_benchmark_case(authoring_fixture),),
        lambda _authoring_request: _authored(_pretty_source(authoring_fixture["source"])),
    )
    benchmark_case = _benchmark_case(authoring_fixture)
    case_result = benchmark_report.case_results[0]
    authoring_attempt = case_result.attempts[0]

    with pytest.raises(FrozenInstanceError):
        benchmark_case.prompt = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        authoring_attempt.succeeded = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        case_result.attempts = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        benchmark_report.case_results = ()  # type: ignore[misc]


def test_contracts_reject_inconsistent_attempt_and_report_state() -> None:
    with pytest.raises(ValueError, match="restricted to versioned presentation"):
        AuthoringAcceptanceContract(
            expectations=(AcceptanceExpectation.expecting("/schema", "flowspec/2"),),
            variable_pointer_patterns=("/terminal/tool",),
        )

    with pytest.raises(ValueError, match="token proxy"):
        AuthoringAttempt(
            correction_round=0,
            source_sha256="0" * 64,
            raw_source_bytes=2,
            canonical_source_bytes=2,
            token_proxy_units=2,
            compilation="succeeded",
            diagnostics=(),
            succeeded=True,
        )

    valid_attempt = AuthoringAttempt(
        correction_round=0,
        source_sha256="0" * 64,
        raw_source_bytes=2,
        canonical_source_bytes=2,
        token_proxy_units=1,
        compilation="succeeded",
        diagnostics=(),
        succeeded=True,
    )
    case_result = AuthoringBenchmarkResult(
        case_identifier="case_a",
        feature_tags=(),
        format_identifier="flowspec/2",
        attempts=(valid_attempt,),
    )
    with pytest.raises(ValueError, match="sorted"):
        AuthoringBenchmarkReport(
            benchmark_identifier="invalid_order",
            format_identifier="flowspec/2",
            profile_identifier="flowspec2/reference@2",
            case_results=(
                AuthoringBenchmarkResult(
                    case_identifier="case_b",
                    feature_tags=(),
                    format_identifier="flowspec/2",
                    attempts=(valid_attempt,),
                ),
                case_result,
            ),
        )
