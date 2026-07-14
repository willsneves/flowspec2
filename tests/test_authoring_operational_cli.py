"""CLI lifecycle for report-only operational evidence."""

from __future__ import annotations

import json
from pathlib import Path

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
    OperationalModelResponse,
    OperationalProbeRequest,
    OperationalProviderError,
    RecordingAuthor,
    load_reference_authoring_corpus,
    run_authoring_benchmark,
)
from flowspec2.cli import main
from flowspec2.profiles import reference_profile

_REPOSITORY_REVISION = "operational-cli-revision"


def _authoring_evidence_path(directory_path: Path) -> Path:
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
        "operational_cli",
        corpus.cases,
        recording_author,
        profile=profile,
    )
    evidence = AuthoringBenchmarkEvidence(
        package_version=__version__,
        repository_revision=_REPOSITORY_REVISION,
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
    evidence_path = directory_path / "authoring-evidence.json"
    evidence_path.write_text(evidence.to_json(), encoding="utf-8")
    return evidence_path


class FakeOperationalExecutor:
    def __init__(self, *, matches: bool = True, fails: bool = False) -> None:
        self.matches = matches
        self.fails = fails
        self.closed = False

    def provenance(self) -> AuthoringProviderProvenance:
        return AuthoringProviderProvenance.from_configuration(
            identifier="fake_operational",
            model="fake-operational-model",
            sdk="fake-operational-sdk",
            sdk_version="1.0.0",
            prompt_format=OPERATIONAL_PROMPT_FORMAT,
            prompt_digest=OPERATIONAL_PROMPT_DIGEST,
            generation_configuration={"temperature": 0.0},
        )

    def __call__(self, request: OperationalProbeRequest) -> OperationalModelResponse:
        if self.fails:
            raise OperationalProviderError("provider failed safely")
        matching_outputs = {
            "linear-extract-authored": '{"request_description":"playground swing is broken"}',
            "linear-extract-counterfactual": '{"request_description":"broken streetlight"}',
            "linear-route-authored": '{"service":"linear_maintenance_request"}',
            "linear-route-counterfactual": '{"service":null}',
        }
        mismatching_outputs = {
            "linear-extract-authored": '{"request_description":"broken streetlight"}',
            "linear-extract-counterfactual": (
                '{"request_description":"playground swing is broken"}'
            ),
            "linear-route-authored": '{"service":null}',
            "linear-route-counterfactual": '{"service":"linear_maintenance_request"}',
        }
        raw_output = (matching_outputs if self.matches else mismatching_outputs)[
            request.probe_identifier
        ]
        return OperationalModelResponse(raw_output, "effective-model-001")

    def __enter__(self) -> FakeOperationalExecutor:
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.closed = True


def _benchmark_arguments(authoring_path: Path, output_path: Path) -> list[str]:
    return [
        "operational-benchmark-gemini",
        str(authoring_path),
        "--allow-network",
        "--model",
        "fake-operational-model",
        "--repository-revision",
        _REPOSITORY_REVISION,
        "--output",
        str(output_path),
    ]


def test_operational_cli_writes_and_offline_verifies_matching_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    authoring_path = _authoring_evidence_path(tmp_path)
    output_path = tmp_path / "operational-evidence.json"
    fake_executor = FakeOperationalExecutor()
    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_operational_executor",
        lambda _model: fake_executor,
    )

    assert main(_benchmark_arguments(authoring_path, output_path)) == 0
    assert fake_executor.closed is True
    operational_document = json.loads(output_path.read_text(encoding="utf-8"))
    assert operational_document["classification"] == "report_only"
    assert operational_document["all_matched"] is True

    assert (
        main(
            [
                "operational-evidence-verify",
                str(output_path),
                "--authoring-evidence",
                str(authoring_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--json",
            ]
        )
        == 0
    )
    verification = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert verification["classification"] == "report_only"
    assert verification["all_matched"] is True


def test_operational_cli_preserves_completed_mismatch_as_report_only_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authoring_path = _authoring_evidence_path(tmp_path)
    output_path = tmp_path / "operational-mismatch.json"
    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_operational_executor",
        lambda _model: FakeOperationalExecutor(matches=False),
    )

    assert main(_benchmark_arguments(authoring_path, output_path)) == 0
    assert output_path.exists()
    assert json.loads(output_path.read_text(encoding="utf-8"))["all_matched"] is False


def test_operational_cli_provider_failure_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    authoring_path = _authoring_evidence_path(tmp_path)
    output_path = tmp_path / "operational-failed.json"
    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_operational_executor",
        lambda _model: FakeOperationalExecutor(fails=True),
    )

    assert main(_benchmark_arguments(authoring_path, output_path)) == 1
    assert not output_path.exists()
    assert "provider failed safely" in capsys.readouterr().err


def test_operational_cli_requires_explicit_network_consent_before_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_created = False

    def forbidden_factory(_model: str) -> FakeOperationalExecutor:
        nonlocal provider_created
        provider_created = True
        return FakeOperationalExecutor()

    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_operational_executor",
        forbidden_factory,
    )

    assert (
        main(
            [
                "operational-benchmark-gemini",
                str(tmp_path / "authoring.json"),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--output",
                str(tmp_path / "operational.json"),
            ]
        )
        == 2
    )
    assert provider_created is False
