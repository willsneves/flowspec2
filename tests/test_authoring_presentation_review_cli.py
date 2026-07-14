"""CLI lifecycle for source-bound authoring presentation review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flowspec2 import __version__
from flowspec2.authoring import (
    AuthoredSource,
    AuthoringBenchmarkEvidence,
    AuthoringBenchmarkLimits,
    AuthoringProviderProvenance,
    AuthoringRequest,
    RecordingAuthor,
    load_reference_authoring_corpus,
    run_authoring_benchmark,
)
from flowspec2.cli import main
from flowspec2.profiles import reference_profile

_REPOSITORY_REVISION = "presentation-review-cli-revision"


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
        "presentation_review_cli",
        corpus.cases,
        recording_author,
        profile=profile,
    )
    return AuthoringBenchmarkEvidence(
        package_version=__version__,
        repository_revision=_REPOSITORY_REVISION,
        benchmark_limits=AuthoringBenchmarkLimits(),
        corpus=corpus,
        profile_identifier=profile.identifier,
        profile_digest=profile.digest,
        provider=AuthoringProviderProvenance.from_configuration(
            identifier="presentation_review_cli_provider",
            model="presentation-review-cli-model",
            sdk="presentation-review-cli-sdk",
            sdk_version="1.0.0",
            prompt_format="flowspec2/presentation-review-cli-prompt@1",
            prompt_digest="1" * 64,
            generation_configuration={"temperature": 0.0},
        ),
        report=report,
        captures=recording_author.captures,
    )


def _write_keys(directory: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes([11]) * 32)
    private_key_path = directory / "review-private-key.pem"
    public_key_path = directory / "review-public-key.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key_path, public_key_path


def _complete_review_draft(draft_path: Path) -> None:
    draft_document = json.loads(draft_path.read_text(encoding="utf-8"))
    draft_document["reviewer_identifier"] = "reviewer_one"
    for assessment in draft_document["assessments"]:
        for criterion in assessment["criteria"]:
            criterion["decision"] = "pass"
            criterion["rationale"] = "Confirmed against the public task and interaction contract."
    draft_path.write_text(
        f"{json.dumps(draft_document, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )


def test_cli_runs_complete_presentation_review_and_promotion_lifecycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = tmp_path / "evidence.json"
    draft_path = tmp_path / "presentation-review.draft.json"
    review_path = tmp_path / "presentation-review.json"
    signature_path = tmp_path / "presentation-review.signature.json"
    private_key_path, public_key_path = _write_keys(tmp_path)
    evidence_path.write_text(f"{_evidence().to_json()}\n", encoding="utf-8")

    assert (
        main(
            [
                "authoring-presentation-review-init",
                str(evidence_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--output",
                str(draft_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    draft_document = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft_document["public_tasks"]
    assert draft_document["assessments"]
    serialized_draft = json.dumps(draft_document)
    assert "expected_flow_json" not in serialized_draft
    assert "authored_source" not in serialized_draft
    _complete_review_draft(draft_path)

    assert (
        main(
            [
                "authoring-presentation-review-finalize",
                str(evidence_path),
                "--draft",
                str(draft_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--output",
                str(review_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert json.loads(review_path.read_text(encoding="utf-8"))["passed"] is True

    assert (
        main(
            [
                "authoring-presentation-review-verify",
                str(evidence_path),
                "--review",
                str(review_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--json",
            ]
        )
        == 0
    )
    verification = json.loads(capsys.readouterr().out)
    assert verification["passed"] is True

    assert (
        main(
            [
                "authoring-presentation-review-sign",
                str(evidence_path),
                "--review",
                str(review_path),
                "--private-key",
                str(private_key_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--output",
                str(signature_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "authoring-presentation-review-signature-verify",
                str(evidence_path),
                "--review",
                str(review_path),
                "--signature",
                str(signature_path),
                "--public-key",
                str(public_key_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--json",
            ]
        )
        == 0
    )
    authentication = json.loads(capsys.readouterr().out)
    assert authentication["authenticated"] is True
    assert "candidate_text" not in json.dumps(authentication)

    assert (
        main(
            [
                "authoring-promotion-verify",
                str(evidence_path),
                "--review",
                str(review_path),
                "--signature",
                str(signature_path),
                "--public-key",
                str(public_key_path),
                "--repository-revision",
                _REPOSITORY_REVISION,
                "--json",
            ]
        )
        == 0
    )
    promotion = json.loads(capsys.readouterr().out)
    assert promotion["eligible"] is True
    assert promotion["signature_authenticated"] is True
