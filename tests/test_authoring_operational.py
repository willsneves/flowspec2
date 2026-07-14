"""Report-only operational routing and extraction evidence."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import jsonschema
import pytest

from flowspec2 import __version__
from flowspec2.authoring import (
    OPERATIONAL_PROMPT_DIGEST,
    OPERATIONAL_PROMPT_FORMAT,
    AuthoredSource,
    AuthoringBenchmarkEvidence,
    AuthoringBenchmarkLimits,
    AuthoringProviderProvenance,
    AuthoringRequest,
    CodexOperationalExecutor,
    OperationalModelResponse,
    OperationalProbeRequest,
    RecordingAuthor,
    authoring_operational_evidence_schema,
    load_reference_authoring_corpus,
    load_reference_operational_corpus,
    run_authoring_benchmark,
    run_authoring_operational_evidence,
    verify_authoring_evidence,
    verify_authoring_operational_evidence,
)
from flowspec2.profiles import reference_profile


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _authoring_evidence() -> AuthoringBenchmarkEvidence:
    corpus = load_reference_authoring_corpus()
    expected_sources = {
        benchmark_case.identifier: benchmark_case.expected_flow_json
        for benchmark_case in corpus.cases
    }

    def fixture_author(authoring_request: AuthoringRequest) -> AuthoredSource:
        return AuthoredSource(expected_sources[authoring_request.task.identifier])

    recording_author = RecordingAuthor(fixture_author)
    profile = reference_profile()
    report = run_authoring_benchmark(
        "operational_reference",
        corpus.cases,
        recording_author,
        profile=profile,
    )
    return AuthoringBenchmarkEvidence(
        package_version=__version__,
        repository_revision="operational-revision",
        benchmark_limits=AuthoringBenchmarkLimits(),
        corpus=corpus,
        profile_identifier=profile.identifier,
        profile_digest=profile.digest,
        provider=AuthoringProviderProvenance.from_configuration(
            identifier="fake_author",
            model="fake-author-model",
            sdk="fake-author-sdk",
            sdk_version="1.0.0",
            prompt_format="flowspec2/fake-author-prompt@1",
            prompt_digest="1" * 64,
            generation_configuration={"temperature": 0.0},
        ),
        report=report,
        captures=recording_author.captures,
    )


def _operational_provider() -> AuthoringProviderProvenance:
    return AuthoringProviderProvenance.from_configuration(
        identifier="fake_operational",
        model="fake-operational-model",
        sdk="fake-operational-sdk",
        sdk_version="1.0.0",
        prompt_format=OPERATIONAL_PROMPT_FORMAT,
        prompt_digest=OPERATIONAL_PROMPT_DIGEST,
        generation_configuration={"temperature": 0.0},
    )


def _matching_executor(request: OperationalProbeRequest) -> OperationalModelResponse:
    raw_outputs = {
        "linear-extract-authored": '{"request_description":"playground swing is broken"}',
        "linear-extract-counterfactual": '{"request_description":"broken streetlight"}',
        "linear-route-authored": '{"service":"linear_maintenance_request"}',
        "linear-route-counterfactual": '{"service":null}',
    }
    raw_output = raw_outputs[request.probe_identifier]
    return OperationalModelResponse(raw_output, effective_model_version="effective-model-001")


def _operational_evidence_json() -> tuple[str, str]:
    authoring_evidence = _authoring_evidence()
    serialized_authoring_evidence = authoring_evidence.to_json()
    verified_authoring_evidence = verify_authoring_evidence(serialized_authoring_evidence)
    operational_evidence = run_authoring_operational_evidence(
        serialized_authoring_evidence,
        verified_authoring_evidence,
        provider=_operational_provider(),
        executor=_matching_executor,
    )
    return serialized_authoring_evidence, operational_evidence.to_json()


def _resign(document: dict[str, Any]) -> str:
    unsigned_document = dict(document)
    unsigned_document.pop("digest", None)
    document["digest"] = hashlib.sha256(
        _canonical_json(unsigned_document).encode("utf-8")
    ).hexdigest()
    return _canonical_json(document)


def test_operational_corpus_is_closed_sorted_and_content_addressed() -> None:
    first_corpus = load_reference_operational_corpus()
    second_corpus = load_reference_operational_corpus()

    assert first_corpus is second_corpus
    assert tuple(probe.identifier for probe in first_corpus.probes) == (
        "linear-extract-authored",
        "linear-extract-counterfactual",
        "linear-route-authored",
        "linear-route-counterfactual",
    )
    assert first_corpus.metadata()["digest"] == first_corpus.digest


def test_operational_evidence_captures_exact_trigger_and_hint_requests() -> None:
    _serialized_authoring_evidence, serialized_operational_evidence = _operational_evidence_json()
    evidence_document = json.loads(serialized_operational_evidence)

    assert evidence_document["classification"] == "report_only"
    assert evidence_document["all_matched"] is True
    route_authored_capture = next(
        capture
        for capture in evidence_document["captures"]
        if capture["probe_identifier"] == "linear-route-authored"
    )
    route_counterfactual_capture = next(
        capture
        for capture in evidence_document["captures"]
        if capture["probe_identifier"] == "linear-route-counterfactual"
    )
    extraction_authored_capture = next(
        capture
        for capture in evidence_document["captures"]
        if capture["probe_identifier"] == "linear-extract-authored"
    )
    extraction_counterfactual_capture = next(
        capture
        for capture in evidence_document["captures"]
        if capture["probe_identifier"] == "linear-extract-counterfactual"
    )
    assert '"cobalt lantern protocol"' in route_authored_capture["request"]["prompt"]
    assert '"amber compass protocol"' in route_counterfactual_capture["request"]["prompt"]
    assert (
        route_authored_capture["pair_identifier"] == route_counterfactual_capture["pair_identifier"]
    )
    assert extraction_authored_capture["request"]["response_schema"]["properties"][
        "request_description"
    ]["description"].endswith("ignore `ALPHA=[...]`.")
    assert extraction_counterfactual_capture["request"]["response_schema"]["properties"][
        "request_description"
    ]["description"].endswith("ignore `ZETA=[...]`.")
    assert (
        extraction_authored_capture["pair_identifier"]
        == extraction_counterfactual_capture["pair_identifier"]
    )
    assert {capture["subject_mode"] for capture in evidence_document["captures"]} == {
        "authored",
        "counterfactual",
    }


def test_operational_evidence_verifies_by_complete_offline_replay() -> None:
    serialized_authoring_evidence, serialized_operational_evidence = _operational_evidence_json()
    verified_authoring_evidence = verify_authoring_evidence(serialized_authoring_evidence)

    verification = verify_authoring_operational_evidence(
        serialized_operational_evidence,
        serialized_authoring_evidence,
        verified_authoring_evidence,
    )

    assert verification.classification == "report_only"
    assert verification.matched_probes == verification.total_probes == 4
    assert verification.all_matched is True
    assert verification.effective_model_versions == ("effective-model-001",)


def test_completed_semantic_mismatch_remains_verifiable_report_only_evidence() -> None:
    authoring_evidence = _authoring_evidence()
    serialized_authoring_evidence = authoring_evidence.to_json()
    verified_authoring_evidence = verify_authoring_evidence(serialized_authoring_evidence)

    def mismatching_executor(request: OperationalProbeRequest) -> OperationalModelResponse:
        mismatched_outputs = {
            "linear-extract-authored": '{"request_description":"broken streetlight"}',
            "linear-extract-counterfactual": (
                '{"request_description":"playground swing is broken"}'
            ),
            "linear-route-authored": '{"service":null}',
            "linear-route-counterfactual": '{"service":"linear_maintenance_request"}',
        }
        return OperationalModelResponse(mismatched_outputs[request.probe_identifier])

    operational_evidence = run_authoring_operational_evidence(
        serialized_authoring_evidence,
        verified_authoring_evidence,
        provider=_operational_provider(),
        executor=mismatching_executor,
    )
    verification = verify_authoring_operational_evidence(
        operational_evidence.to_json(),
        serialized_authoring_evidence,
        verified_authoring_evidence,
    )

    assert operational_evidence.all_matched is False
    assert verification.matched_probes == 0
    assert {capture.diagnostic for capture in operational_evidence.captures} == {
        "semantic_mismatch"
    }


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("subject_sha256", "0" * 64),
        ("source_sha256", "0" * 64),
        ("raw_output_sha256", "0" * 64),
    ],
)
def test_operational_verifier_rejects_resigned_capture_tampering(
    field_name: str,
    replacement: object,
) -> None:
    serialized_authoring_evidence, serialized_operational_evidence = _operational_evidence_json()
    verified_authoring_evidence = verify_authoring_evidence(serialized_authoring_evidence)
    operational_document = json.loads(serialized_operational_evidence)
    operational_document["captures"][0][field_name] = replacement

    with pytest.raises(ValueError, match="offline replay"):
        verify_authoring_operational_evidence(
            _resign(operational_document),
            serialized_authoring_evidence,
            verified_authoring_evidence,
        )


def test_runner_and_verifier_reject_stale_verification_for_tampered_authoring_source() -> None:
    serialized_authoring_evidence, serialized_operational_evidence = _operational_evidence_json()
    verified_authoring_evidence = verify_authoring_evidence(serialized_authoring_evidence)
    authoring_document = json.loads(serialized_authoring_evidence)
    linear_capture = next(
        capture
        for capture in authoring_document["captures"]
        if capture["case_identifier"] == "linear_collection"
    )
    source_document = json.loads(linear_capture["authored_source"])
    source_document["path"][0]["prompt"]["extract_hint"] = "Tampered guidance."
    linear_capture["authored_source"] = _canonical_json(source_document)
    tampered_authoring_evidence = _canonical_json(authoring_document)

    with pytest.raises(ValueError, match="authoring evidence digest"):
        run_authoring_operational_evidence(
            tampered_authoring_evidence,
            verified_authoring_evidence,
            provider=_operational_provider(),
            executor=_matching_executor,
        )
    with pytest.raises(ValueError, match="authoring evidence digest"):
        verify_authoring_operational_evidence(
            serialized_operational_evidence,
            tampered_authoring_evidence,
            verified_authoring_evidence,
        )


def test_operational_schema_is_valid_and_returns_owned_copies() -> None:
    first_schema = authoring_operational_evidence_schema()
    jsonschema.Draft202012Validator.check_schema(first_schema)
    first_schema["title"] = "changed"

    assert authoring_operational_evidence_schema()["title"] != "changed"


@pytest.mark.skipif(
    os.environ.get("FLOWSPEC2_RUN_CODEX_TESTS") != "1",
    reason="set FLOWSPEC2_RUN_CODEX_TESTS=1 and provide the local llmgate project",
)
def test_codex_operational_evidence_runs_and_replays_live() -> None:
    authoring_evidence = _authoring_evidence()
    serialized_authoring_evidence = authoring_evidence.to_json()
    verified_authoring_evidence = verify_authoring_evidence(serialized_authoring_evidence)

    with CodexOperationalExecutor() as executor:
        operational_evidence = run_authoring_operational_evidence(
            serialized_authoring_evidence,
            verified_authoring_evidence,
            provider=executor.provenance(),
            executor=executor,
        )
    verification = verify_authoring_operational_evidence(
        operational_evidence.to_json(),
        serialized_authoring_evidence,
        verified_authoring_evidence,
    )

    assert operational_evidence.all_matched is True
    assert verification.all_matched is True
