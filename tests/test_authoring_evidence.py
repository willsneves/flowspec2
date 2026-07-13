"""Content-addressed authoring evidence and exact capture alignment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import jsonschema
import pytest

from flowspec2.authoring import (
    AUTHORING_EVIDENCE_FORMAT,
    AuthoredSource,
    AuthoringBenchmarkEvidence,
    AuthoringBenchmarkLimits,
    AuthoringProviderProvenance,
    AuthoringRequest,
    RecordingAuthor,
    authoring_evidence_schema,
    load_reference_authoring_corpus,
    run_authoring_benchmark,
    verify_authoring_evidence,
)
from flowspec2.profiles import reference_profile


def _provider_provenance() -> AuthoringProviderProvenance:
    return AuthoringProviderProvenance.from_configuration(
        identifier="fake_provider",
        model="fake-model",
        sdk="fake-sdk",
        sdk_version="1.0.0",
        prompt_format="flowspec2/fake-prompt@1",
        prompt_digest="1" * 64,
        generation_configuration={"seed": 0, "temperature": 0.0},
    )


def _evidence() -> AuthoringBenchmarkEvidence:
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
        "evidence_reference",
        corpus.cases,
        recording_author,
        profile=profile,
    )
    return AuthoringBenchmarkEvidence(
        package_version="0.1.0",
        repository_revision="revision-under-test",
        benchmark_limits=AuthoringBenchmarkLimits(),
        corpus=corpus,
        profile_identifier=profile.identifier,
        profile_digest=profile.digest,
        provider=_provider_provenance(),
        report=report,
        captures=recording_author.captures,
    )


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _resign(evidence_document: dict[str, object]) -> str:
    unsigned_document = dict(evidence_document)
    unsigned_document.pop("digest", None)
    evidence_document["digest"] = hashlib.sha256(
        _canonical_json(unsigned_document).encode("utf-8")
    ).hexdigest()
    return _canonical_json(evidence_document)


def test_evidence_is_canonical_deterministic_and_defensively_projected() -> None:
    first_evidence = _evidence()
    second_evidence = _evidence()

    assert first_evidence == second_evidence
    assert first_evidence.to_json() == second_evidence.to_json()
    evidence_document = json.loads(first_evidence.to_json())
    assert evidence_document["digest"] == first_evidence.digest
    assert evidence_document["format"] == AUTHORING_EVIDENCE_FORMAT
    assert evidence_document["corpus"] == first_evidence.corpus.metadata()
    assert evidence_document["provider"]["generation_configuration"] == {
        "seed": 0,
        "temperature": 0.0,
    }
    assert all(
        capture["effective_model_version"] is None for capture in evidence_document["captures"]
    )
    evidence_document["provider"]["model"] = "mutated"
    assert json.loads(first_evidence.to_json())["provider"]["model"] == "fake-model"


def test_evidence_digest_changes_with_reproducibility_inputs() -> None:
    evidence = _evidence()

    assert replace(evidence, repository_revision="another-revision").digest != evidence.digest
    assert (
        replace(
            evidence,
            provider=replace(evidence.provider, model="another-model"),
        ).digest
        != evidence.digest
    )
    assert (
        replace(
            evidence,
            provider=replace(evidence.provider, prompt_digest="2" * 64),
        ).digest
        != evidence.digest
    )
    assert (
        replace(evidence, benchmark_limits=AuthoringBenchmarkLimits(max_correction_rounds=3)).digest
        != evidence.digest
    )
    versioned_capture = replace(
        evidence.captures[0],
        effective_model_version="provider-model-001",
    )
    assert (
        replace(evidence, captures=(versioned_capture, *evidence.captures[1:])).digest
        != evidence.digest
    )


def test_evidence_rejects_missing_reordered_and_mismatched_captures() -> None:
    evidence = _evidence()

    with pytest.raises(ValueError, match="do not align"):
        replace(evidence, captures=evidence.captures[:-1])
    with pytest.raises(ValueError, match="do not align"):
        replace(evidence, captures=tuple(reversed(evidence.captures)))
    with pytest.raises(ValueError, match="source digest"):
        replace(
            evidence.captures[0],
            authored_source="different source",
        )


def test_evidence_rejects_report_exceeding_recorded_correction_limit() -> None:
    evidence = _evidence()
    recording_author = RecordingAuthor(lambda _request: AuthoredSource("{}"))
    failed_report = run_authoring_benchmark(
        "failed_evidence",
        evidence.corpus.cases,
        recording_author,
        profile=reference_profile(),
    )

    with pytest.raises(ValueError, match="correction-round limit"):
        replace(
            evidence,
            benchmark_limits=AuthoringBenchmarkLimits(max_correction_rounds=0),
            report=failed_report,
            captures=recording_author.captures,
        )


def test_recording_author_rejects_duplicate_attempt_identity() -> None:
    corpus = load_reference_authoring_corpus()
    profile = reference_profile()
    benchmark_case = corpus.cases[0]
    authoring_request = AuthoringRequest(
        task=benchmark_case.authoring_task(),
        format_identifier="flowspec/2",
        profile_identifier=profile.identifier,
        profile_contract_json=profile.canonical_json(),
        correction_round=0,
        previous_source=None,
        previous_diagnostics=(),
    )
    recording_author = RecordingAuthor(
        lambda _request: AuthoredSource(benchmark_case.expected_flow_json)
    )

    recording_author(authoring_request)
    with pytest.raises(ValueError, match="duplicate authoring capture"):
        recording_author(authoring_request)


def test_gemini_evidence_requires_effective_model_versions() -> None:
    evidence = _evidence()
    gemini_provider = replace(evidence.provider, identifier="google_gemini")

    with pytest.raises(ValueError, match="effective model versions"):
        replace(evidence, provider=gemini_provider)


def test_provider_provenance_rejects_secret_configuration_fields() -> None:
    with pytest.raises(ValueError, match="secret field"):
        AuthoringProviderProvenance.from_configuration(
            identifier="unsafe_provider",
            model="model",
            sdk="sdk",
            sdk_version="1",
            prompt_format="prompt/1",
            prompt_digest="4" * 64,
            generation_configuration={"api_key": "must-not-be-recorded"},
        )


def test_evidence_verifier_replays_every_capture_and_returns_safe_summary() -> None:
    evidence = _evidence()

    verification = verify_authoring_evidence(
        evidence.to_json(),
        expected_repository_revision="revision-under-test",
    )

    assert verification.digest == evidence.digest
    assert verification.benchmark_identifier == "evidence_reference"
    assert verification.successful_cases == verification.total_cases
    assert verification.total_attempts == len(evidence.captures)
    assert "authored_source" not in _canonical_json(verification.to_dict())


def test_evidence_schema_is_closed_and_defensively_projected() -> None:
    first_schema = authoring_evidence_schema()
    first_schema["properties"]["unexpected"] = {"type": "string"}

    assert "unexpected" not in authoring_evidence_schema()["properties"]

    evidence_document = json.loads(_evidence().to_json())
    evidence_document["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError, match="Additional properties are not allowed"):
        verify_authoring_evidence(_canonical_json(evidence_document))


def test_evidence_verifier_rejects_ambiguous_or_noncanonical_json() -> None:
    evidence = _evidence()

    with pytest.raises(ValueError, match="duplicate JSON object member"):
        verify_authoring_evidence(evidence.to_json().replace("{", '{"format":"duplicate",', 1))
    with pytest.raises(ValueError, match="canonical compact JSON"):
        verify_authoring_evidence(json.dumps(json.loads(evidence.to_json()), indent=2))


def test_evidence_verifier_rejects_digest_and_installed_contract_drift() -> None:
    evidence_document = json.loads(_evidence().to_json())
    evidence_document["repository_revision"] = "tampered"
    with pytest.raises(ValueError, match="digest does not match"):
        verify_authoring_evidence(_canonical_json(evidence_document))

    evidence_document = json.loads(_evidence().to_json())
    evidence_document["package_version"] = "999.0.0"
    with pytest.raises(ValueError, match="package version"):
        verify_authoring_evidence(_resign(evidence_document))

    evidence_document = json.loads(_evidence().to_json())
    evidence_document["corpus"]["digest"] = "0" * 64
    with pytest.raises(ValueError, match="installed corpus"):
        verify_authoring_evidence(_resign(evidence_document))

    evidence_document = json.loads(_evidence().to_json())
    evidence_document["profile"]["digest"] = "0" * 64
    with pytest.raises(ValueError, match="installed profile"):
        verify_authoring_evidence(_resign(evidence_document))


def test_evidence_verifier_rejects_capture_and_report_tampering_after_resigning() -> None:
    evidence_document = json.loads(_evidence().to_json())
    evidence_document["captures"][0]["authored_source"] = "{}"
    with pytest.raises(ValueError, match="capture source digest"):
        verify_authoring_evidence(_resign(evidence_document))

    evidence_document = json.loads(_evidence().to_json())
    evidence_document["report"]["successful_cases"] = 0
    with pytest.raises(ValueError, match="deterministic replay"):
        verify_authoring_evidence(_resign(evidence_document))

    with pytest.raises(ValueError, match="repository revision"):
        verify_authoring_evidence(
            _evidence().to_json(),
            expected_repository_revision="different-revision",
        )
