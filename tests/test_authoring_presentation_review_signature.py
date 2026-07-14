"""Detached authentication and promotion checks for presentation reviews."""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Any, cast

import jsonschema
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import flowspec2.authoring.presentation_review_signature as presentation_signature_module
from flowspec2 import __version__
from flowspec2.authoring import (
    AuthoredSource,
    AuthoringBenchmarkEvidence,
    AuthoringBenchmarkLimits,
    AuthoringEvidenceVerification,
    AuthoringProviderProvenance,
    AuthoringRequest,
    RecordingAuthor,
    load_reference_authoring_corpus,
    run_authoring_benchmark,
    verify_authoring_evidence,
)
from flowspec2.authoring.presentation_review import (
    PRESENTATION_RUBRIC_CRITERIA,
    AuthoringPresentationReview,
    PresentationAssessment,
    PresentationCriterionResult,
    PresentationReviewDraft,
    PresentationSubject,
    presentation_review_draft,
)
from flowspec2.authoring.presentation_review_signature import (
    AUTHORING_PRESENTATION_REVIEW_SIGNATURE_ALGORITHM,
    AUTHORING_PRESENTATION_REVIEW_SIGNATURE_FORMAT,
    AuthoringPresentationReviewSignature,
    authoring_presentation_review_signature_schema,
    sign_presentation_review,
    verify_authoring_promotion,
    verify_presentation_review_signature,
)
from flowspec2.profiles import reference_profile

_REPOSITORY_REVISION = "presentation-signature-revision"


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _provider_provenance() -> AuthoringProviderProvenance:
    return AuthoringProviderProvenance.from_configuration(
        identifier="presentation_signature_provider",
        model="presentation-signature-model",
        sdk="presentation-signature-sdk",
        sdk_version="1.0.0",
        prompt_format="flowspec2/presentation-signature-prompt@1",
        prompt_digest="1" * 64,
        generation_configuration={"temperature": 0.0},
    )


@lru_cache(maxsize=None)
def _evidence(*, failed_case_identifier: str | None = None) -> AuthoringBenchmarkEvidence:
    corpus = load_reference_authoring_corpus()
    expected_sources = {
        benchmark_case.identifier: benchmark_case.expected_flow_json
        for benchmark_case in corpus.cases
    }

    def fixture_author(authoring_request: AuthoringRequest) -> AuthoredSource:
        if authoring_request.task.identifier == failed_case_identifier:
            return AuthoredSource("{}")
        return AuthoredSource(expected_sources[authoring_request.task.identifier])

    recording_author = RecordingAuthor(fixture_author)
    profile = reference_profile()
    report = run_authoring_benchmark(
        "presentation_signature_reference",
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
        provider=_provider_provenance(),
        report=report,
        captures=recording_author.captures,
    )


def _assessment(
    subject: PresentationSubject,
    *,
    failing_criterion: str | None = None,
) -> PresentationAssessment:
    applicable_criteria = tuple(
        criterion
        for criterion in PRESENTATION_RUBRIC_CRITERIA
        if subject.kind in criterion.subject_kinds
    )
    return PresentationAssessment(
        subject=subject,
        criteria=tuple(
            PresentationCriterionResult(
                criterion_identifier=criterion.identifier,
                decision="fail" if criterion.identifier == failing_criterion else "pass",
                rationale=f"Reviewed {criterion.identifier} against the public contract.",
            )
            for criterion in applicable_criteria
        ),
    )


@lru_cache(maxsize=None)
def _verified_serialized_evidence(
    serialized_evidence: str,
) -> AuthoringEvidenceVerification:
    return verify_authoring_evidence(
        serialized_evidence,
        expected_repository_revision=_REPOSITORY_REVISION,
    )


@lru_cache(maxsize=None)
def _review(
    evidence: AuthoringBenchmarkEvidence,
    *,
    failing_criterion: str | None = None,
) -> AuthoringPresentationReview:
    verified_evidence = _verified_serialized_evidence(evidence.to_json())
    draft = presentation_review_draft(evidence.to_json(), verified_evidence)
    return _review_from_draft(draft, failing_criterion=failing_criterion)


def _review_from_draft(
    draft: PresentationReviewDraft,
    *,
    failing_criterion: str | None,
) -> AuthoringPresentationReview:
    return AuthoringPresentationReview(
        benchmark_evidence_digest=draft.benchmark_evidence_digest,
        reviewer_identifier="presentation_reviewer",
        assessments=tuple(
            _assessment(
                draft_subject.subject,
                failing_criterion=failing_criterion,
            )
            for draft_subject in draft.subjects
        ),
    )


def _ed25519_key_material(
    seed_byte: int = 7,
) -> tuple[Ed25519PrivateKey, bytes, bytes]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, private_key_pem, public_key_pem


@pytest.fixture(scope="module", autouse=True)
def _cache_verified_review_replays() -> Any:
    original_verifier = presentation_signature_module._verified_review
    cached_verifier = lru_cache(maxsize=None)(original_verifier)
    presentation_signature_module._verified_review = cached_verifier
    yield
    presentation_signature_module._verified_review = original_verifier


def test_review_signature_is_deterministic_and_returns_safe_authentication() -> None:
    evidence = _evidence()
    review = _review(evidence)
    _private_key, private_key_pem, public_key_pem = _ed25519_key_material()

    first_signature = sign_presentation_review(
        review.to_json(),
        evidence.to_json(),
        private_key_pem,
        expected_repository_revision=_REPOSITORY_REVISION,
    )
    second_signature = sign_presentation_review(
        review.to_json(),
        evidence.to_json(),
        private_key_pem,
    )
    authentication = verify_presentation_review_signature(
        review.to_json(),
        evidence.to_json(),
        first_signature.to_json(),
        public_key_pem,
        expected_repository_revision=_REPOSITORY_REVISION,
    )

    assert first_signature == second_signature
    assert first_signature.presentation_review_digest == review.digest
    assert authentication.key_id == first_signature.key_id
    assert authentication.evidence.digest == evidence.digest
    assert authentication.presentation_review.digest == review.digest
    assert authentication.presentation_review.passed
    safe_summary = _canonical_json(authentication.to_dict())
    assert "authored_source" not in safe_summary
    assert "candidate_text" not in safe_summary
    assert "rationale" not in safe_summary
    assert "PRIVATE KEY" not in first_signature.to_json()


def test_review_signature_schema_is_closed_stable_and_owned() -> None:
    first_schema = authoring_presentation_review_signature_schema()
    assert first_schema["$id"] == (
        "https://prefeitura.rio/flowspec2/authoring-presentation-review-signature-1.json"
    )
    jsonschema.Draft202012Validator.check_schema(first_schema)
    first_schema["properties"]["unexpected"] = {"type": "string"}
    assert "unexpected" not in authoring_presentation_review_signature_schema()["properties"]

    evidence = _evidence()
    review = _review(evidence)
    _private_key, private_key_pem, _public_key_pem = _ed25519_key_material()
    signature_document = json.loads(
        sign_presentation_review(
            review.to_json(),
            evidence.to_json(),
            private_key_pem,
        ).to_json()
    )
    assert signature_document["format"] == AUTHORING_PRESENTATION_REVIEW_SIGNATURE_FORMAT
    assert signature_document["algorithm"] == AUTHORING_PRESENTATION_REVIEW_SIGNATURE_ALGORITHM
    signature_document["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
        AuthoringPresentationReviewSignature.from_json(_canonical_json(signature_document))


def test_review_signature_rejects_wrong_key_tampering_and_digest_mismatch() -> None:
    evidence = _evidence()
    review = _review(evidence)
    _private_key, private_key_pem, public_key_pem = _ed25519_key_material()
    _other_key, _other_private_key_pem, other_public_key_pem = _ed25519_key_material(8)
    signature = sign_presentation_review(
        review.to_json(),
        evidence.to_json(),
        private_key_pem,
    )

    with pytest.raises(ValueError, match="different public key"):
        verify_presentation_review_signature(
            review.to_json(),
            evidence.to_json(),
            signature.to_json(),
            other_public_key_pem,
        )

    tampered_signature = json.loads(signature.to_json())
    tampered_signature["signature"] = (
        "A" if tampered_signature["signature"][0] != "A" else "B"
    ) + tampered_signature["signature"][1:]
    with pytest.raises(ValueError, match="signature is invalid"):
        verify_presentation_review_signature(
            review.to_json(),
            evidence.to_json(),
            _canonical_json(tampered_signature),
            public_key_pem,
        )

    mismatched_digest = json.loads(signature.to_json())
    mismatched_digest["presentation_review_digest"] = "0" * 64
    with pytest.raises(ValueError, match="different review digest"):
        verify_presentation_review_signature(
            review.to_json(),
            evidence.to_json(),
            _canonical_json(mismatched_digest),
            public_key_pem,
        )


def test_review_signature_uses_its_domain_separated_message() -> None:
    evidence = _evidence()
    review = _review(evidence)
    private_key, _private_key_pem, public_key_pem = _ed25519_key_material()
    valid_signature = sign_presentation_review(
        review.to_json(),
        evidence.to_json(),
        _ed25519_key_material()[1],
    )
    wrong_domain_signature = private_key.sign(review.digest.encode("ascii"))
    wrong_domain_document = valid_signature.to_dict()
    wrong_domain_document["signature"] = base64.b64encode(wrong_domain_signature).decode("ascii")

    with pytest.raises(ValueError, match="signature is invalid"):
        verify_presentation_review_signature(
            review.to_json(),
            evidence.to_json(),
            _canonical_json(wrong_domain_document),
            public_key_pem,
        )


def test_sign_and_verify_revalidate_review_and_evidence_before_cryptography() -> None:
    evidence = _evidence()
    review = _review(evidence)
    _private_key, private_key_pem, public_key_pem = _ed25519_key_material()
    signature = sign_presentation_review(
        review.to_json(),
        evidence.to_json(),
        private_key_pem,
    )

    with pytest.raises(jsonschema.ValidationError):
        sign_presentation_review("{}", evidence.to_json(), b"not a private key")

    tampered_evidence = json.loads(evidence.to_json())
    tampered_evidence["repository_revision"] = "tampered"
    with pytest.raises(ValueError, match="evidence digest"):
        verify_presentation_review_signature(
            review.to_json(),
            _canonical_json(tampered_evidence),
            "not JSON",
            public_key_pem,
        )

    with pytest.raises(ValueError, match="canonical compact JSON"):
        verify_presentation_review_signature(
            review.to_json(),
            evidence.to_json(),
            json.dumps(json.loads(signature.to_json()), indent=2),
            public_key_pem,
        )


def test_authoring_promotion_requires_all_authenticated_artifacts() -> None:
    private_key, private_key_pem, public_key_pem = _ed25519_key_material()
    del private_key

    successful_evidence = _evidence()
    passing_review = _review(successful_evidence)
    passing_signature = sign_presentation_review(
        passing_review.to_json(),
        successful_evidence.to_json(),
        private_key_pem,
    )
    eligible_promotion = verify_authoring_promotion(
        successful_evidence.to_json(),
        passing_review.to_json(),
        passing_signature.to_json(),
        public_key_pem,
        expected_repository_revision=_REPOSITORY_REVISION,
    )

    assert eligible_promotion.eligible
    assert eligible_promotion.deterministic_evidence_passed
    assert eligible_promotion.presentation_review_passed
    assert eligible_promotion.signature_authenticated
    assert eligible_promotion.to_dict()["eligible"] is True

    failing_review = _review(
        successful_evidence,
        failing_criterion="route_scope_exclusion",
    )
    failing_review_signature = sign_presentation_review(
        failing_review.to_json(),
        successful_evidence.to_json(),
        private_key_pem,
    )
    failed_review_promotion = verify_authoring_promotion(
        successful_evidence.to_json(),
        failing_review.to_json(),
        failing_review_signature.to_json(),
        public_key_pem,
    )
    assert not failed_review_promotion.eligible
    assert failed_review_promotion.deterministic_evidence_passed
    assert not failed_review_promotion.presentation_review_passed

    incomplete_evidence = _evidence(failed_case_identifier="linear_collection")
    incomplete_review = _review(incomplete_evidence)
    incomplete_signature = sign_presentation_review(
        incomplete_review.to_json(),
        incomplete_evidence.to_json(),
        private_key_pem,
    )
    incomplete_promotion = verify_authoring_promotion(
        incomplete_evidence.to_json(),
        incomplete_review.to_json(),
        incomplete_signature.to_json(),
        public_key_pem,
    )
    assert not incomplete_promotion.eligible
    assert not incomplete_promotion.deterministic_evidence_passed
    assert incomplete_promotion.presentation_review_passed

    with pytest.raises(TypeError):
        cast(Any, verify_authoring_promotion)(successful_evidence.to_json())
