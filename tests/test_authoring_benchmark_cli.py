"""Explicit-network CLI for content-addressed Gemini benchmark evidence."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from flowspec2.authoring import (
    AuthoredSource,
    AuthoringProviderProvenance,
    AuthoringRequest,
    GeminiAuthorError,
    load_reference_authoring_corpus,
)
from flowspec2.cli import main


class FakeCliAuthor:
    def __init__(self, *, valid_source: bool = True) -> None:
        self.valid_source = valid_source
        self.closed = False
        self.expected_sources = {
            benchmark_case.identifier: benchmark_case.expected_flow_json
            for benchmark_case in load_reference_authoring_corpus().cases
        }

    def __call__(self, authoring_request: AuthoringRequest) -> AuthoredSource:
        source = (
            self.expected_sources[authoring_request.task.identifier] if self.valid_source else "{}"
        )
        return AuthoredSource(source, effective_model_version="fake-model-001")

    def provenance(self) -> AuthoringProviderProvenance:
        return AuthoringProviderProvenance.from_configuration(
            identifier="fake_gemini",
            model="fake-model",
            sdk="fake-sdk",
            sdk_version="1.0.0",
            prompt_format="flowspec2/fake-cli-prompt@1",
            prompt_digest="3" * 64,
            generation_configuration={"seed": 0, "temperature": 0.0},
        )

    def __enter__(self) -> FakeCliAuthor:
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.closed = True


def _arguments(output_path: Path) -> list[str]:
    return [
        "authoring-benchmark-gemini",
        "--allow-network",
        "--model",
        "fake-model",
        "--repository-revision",
        "revision-under-test",
        "--output",
        str(output_path),
    ]


def test_cli_requires_explicit_network_opt_in_before_provider_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_created = False

    def forbidden_factory(_model: str) -> FakeCliAuthor:
        nonlocal provider_created
        provider_created = True
        raise AssertionError("provider must not be created")

    monkeypatch.setattr("flowspec2.cli._create_gemini_author", forbidden_factory)
    arguments = _arguments(tmp_path / "evidence.json")
    arguments.remove("--allow-network")

    assert main(arguments) == 2
    assert provider_created is False
    assert "--allow-network" in capsys.readouterr().err


def test_cli_writes_canonical_evidence_with_exact_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_author = FakeCliAuthor()
    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_author",
        lambda _model: fake_author,
    )
    output_path = tmp_path / "evidence.json"

    assert main(_arguments(output_path)) == 0

    serialized_evidence = output_path.read_text(encoding="utf-8").strip()
    evidence_document = json.loads(serialized_evidence)
    assert serialized_evidence == json.dumps(
        evidence_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert evidence_document["repository_revision"] == "revision-under-test"
    assert evidence_document["provider"]["model"] == "fake-model"
    assert evidence_document["captures"]
    assert all(capture["authored_source"] for capture in evidence_document["captures"])
    assert {capture["effective_model_version"] for capture in evidence_document["captures"]} == {
        "fake-model-001"
    }
    assert "GEMINI_API_KEY" not in serialized_evidence
    assert fake_author.closed is True
    standard_output = capsys.readouterr().out
    assert "digest=" in standard_output
    assert "authored_source" not in standard_output

    assert (
        main(
            [
                "authoring-evidence-verify",
                str(output_path),
                "--repository-revision",
                "revision-under-test",
                "--json",
            ]
        )
        == 0
    )
    verification_output = json.loads(capsys.readouterr().out)
    assert verification_output["digest"] == evidence_document["digest"]
    assert "authored_source" not in verification_output


def test_cli_writes_completed_semantic_failure_and_returns_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_author",
        lambda _model: FakeCliAuthor(valid_source=False),
    )
    output_path = tmp_path / "failed-evidence.json"

    assert main(_arguments(output_path)) == 1

    evidence_document = json.loads(output_path.read_text(encoding="utf-8"))
    assert evidence_document["report"]["successful_cases"] == 0
    assert all(
        not case_result["succeeded"] for case_result in evidence_document["report"]["case_results"]
    )


def test_cli_provider_failure_does_not_leak_cause_or_write_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_secret = "sentinel-provider-secret"

    class FailingAuthor(FakeCliAuthor):
        def __call__(self, _authoring_request: AuthoringRequest) -> AuthoredSource:
            try:
                raise RuntimeError(sentinel_secret)
            except RuntimeError as provider_error:
                raise GeminiAuthorError("Gemini provider failed safely") from provider_error

    monkeypatch.setattr(
        "flowspec2.cli._create_gemini_author",
        lambda _model: FailingAuthor(),
    )
    output_path = tmp_path / "partial-evidence.json"

    with caplog.at_level(logging.ERROR):
        assert main(_arguments(output_path)) == 1

    captured_output = capsys.readouterr()
    assert output_path.exists() is False
    assert sentinel_secret not in captured_output.out
    assert sentinel_secret not in captured_output.err
    assert sentinel_secret not in caplog.text


def test_cli_preserves_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_created = False

    def provider_factory(_model: str) -> FakeCliAuthor:
        nonlocal provider_created
        provider_created = True
        return FakeCliAuthor()

    monkeypatch.setattr("flowspec2.cli._create_gemini_author", provider_factory)
    output_path = tmp_path / "existing-evidence.json"
    output_path.write_text("existing", encoding="utf-8")

    assert main(_arguments(output_path)) == 1
    assert output_path.read_text(encoding="utf-8") == "existing"
    assert provider_created is False
