"""Closed offline review contracts for candidate-authored presentation prose."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from functools import lru_cache
from typing import Any, cast

import jsonschema
import pytest

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
    verify_authoring_evidence,
)
from flowspec2.authoring.evidence_verification import AuthoringEvidenceVerification
from flowspec2.authoring.presentation_review import (
    PRESENTATION_REVIEW_FORMAT,
    PRESENTATION_RUBRIC_CRITERIA,
    PRESENTATION_RUBRIC_DIGEST,
    AuthoringPresentationReview,
    PresentationAssessment,
    PresentationCriterionResult,
    PresentationReviewDraft,
    PresentationSubject,
    _draft_subjects_for_source,
    finalize_presentation_review_draft,
    presentation_review_draft,
    presentation_review_schema,
    verify_presentation_review,
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


def _sha256(serialized_contract: str) -> str:
    return hashlib.sha256(serialized_contract.encode("utf-8")).hexdigest()


def _provider_provenance() -> AuthoringProviderProvenance:
    return AuthoringProviderProvenance.from_configuration(
        identifier="presentation_test_provider",
        model="presentation-test-model",
        sdk="presentation-test-sdk",
        sdk_version="1.0.0",
        prompt_format="flowspec2/presentation-test-prompt@1",
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
        "presentation_reference",
        corpus.cases,
        recording_author,
        profile=profile,
    )
    return AuthoringBenchmarkEvidence(
        package_version=__version__,
        repository_revision="presentation-review-revision",
        benchmark_limits=AuthoringBenchmarkLimits(),
        corpus=corpus,
        profile_identifier=profile.identifier,
        profile_digest=profile.digest,
        provider=_provider_provenance(),
        report=report,
        captures=recording_author.captures,
    )


@lru_cache(maxsize=None)
def _verified_serialized_evidence(serialized_evidence: str) -> AuthoringEvidenceVerification:
    return verify_authoring_evidence(
        serialized_evidence,
        expected_repository_revision="presentation-review-revision",
    )


def _verified_evidence(
    evidence: AuthoringBenchmarkEvidence,
) -> AuthoringEvidenceVerification:
    return _verified_serialized_evidence(evidence.to_json())


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
def _review(
    draft: PresentationReviewDraft,
    *,
    failing_criterion: str | None = None,
) -> AuthoringPresentationReview:
    return AuthoringPresentationReview(
        benchmark_evidence_digest=draft.benchmark_evidence_digest,
        reviewer_identifier="reviewer_one",
        assessments=tuple(
            _assessment(
                draft_subject.subject,
                failing_criterion=failing_criterion,
            )
            for draft_subject in draft.subjects
        ),
    )


def _completed_draft_document(draft: PresentationReviewDraft) -> dict[str, Any]:
    draft_document = cast(dict[str, Any], draft.to_dict())
    draft_document["reviewer_identifier"] = "reviewer_one"
    for assessment in draft_document["assessments"]:
        for criterion in assessment["criteria"]:
            criterion["decision"] = "pass"
            criterion["rationale"] = "Reviewed against the complete public contract."
    return draft_document


def _all_property_names(json_document: object) -> set[str]:
    if isinstance(json_document, dict):
        return set(json_document) | {
            nested_property
            for property_value in json_document.values()
            for nested_property in _all_property_names(property_value)
        }
    if isinstance(json_document, list):
        return {
            nested_property
            for element_value in json_document
            for nested_property in _all_property_names(element_value)
        }
    return set()


def test_draft_extracts_final_success_route_and_non_verbatim_authored_prompts() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)

    first_draft = presentation_review_draft(evidence.to_json(), verification)
    second_draft = presentation_review_draft(evidence.to_json(), verification)

    assert first_draft == second_draft
    assert first_draft.to_json() == second_draft.to_json()
    route_subjects = [
        draft_subject
        for draft_subject in first_draft.subjects
        if draft_subject.subject.kind == "route_description"
    ]
    assert len(route_subjects) == verification.successful_cases
    pointers = {draft_subject.subject.pointer for draft_subject in first_draft.subjects}
    assert "/path/0/prompt/text" in pointers
    assert "/path/2/prompt/text" in pointers
    assert "/capabilities/await_external/prompt/text" not in pointers
    assert all(
        _sha256(draft_subject.candidate_text) == draft_subject.subject.text_sha256
        for draft_subject in first_draft.subjects
    )
    draft_document = cast(dict[str, Any], first_draft.to_dict())
    assert {public_task["task_identifier"] for public_task in draft_document["public_tasks"]} == {
        draft_subject.subject.case_identifier for draft_subject in first_draft.subjects
    }
    assert all(public_task["prompt"] for public_task in draft_document["public_tasks"])
    assert all(public_task["acceptance"] for public_task in draft_document["public_tasks"])
    route_draft_subject = next(
        draft_subject
        for draft_subject in first_draft.subjects
        if draft_subject.subject.kind == "route_description"
    )
    assert json.loads(route_draft_subject.public_interaction_context_json) == {
        "kind": "route_description"
    }
    linear_prompt_subject = next(
        draft_subject
        for draft_subject in first_draft.subjects
        if draft_subject.subject.case_identifier == "linear_collection"
        and draft_subject.subject.kind == "path_prompt"
    )
    linear_context = json.loads(linear_prompt_subject.public_interaction_context_json)
    assert "prompt" not in linear_context["path_step"]
    assert "step" not in linear_context["path_step"]
    assert linear_context["referenced_slot"]["identifier"] == "request_description"
    assert linear_context["referenced_domain"]["contract"]["type"] == "free_text"
    forbidden_packet_properties = {
        "authored_source",
        "expected_flow_json",
        "feature_tags",
        "reference_source",
        "source",
    }
    assert not forbidden_packet_properties & _all_property_names(draft_document)


def test_draft_excludes_failed_cases_instead_of_reviewing_nonfinal_attempts() -> None:
    evidence = _evidence(failed_case_identifier="linear_collection")
    verification = _verified_evidence(evidence)

    draft = presentation_review_draft(evidence.to_json(), verification)

    assert all(
        draft_subject.subject.case_identifier != "linear_collection"
        for draft_subject in draft.subjects
    )
    assert {draft_subject.subject.case_identifier for draft_subject in draft.subjects} == {
        case_result.case_identifier
        for case_result in evidence.report.case_results
        if case_result.succeeded
    }


def test_extractor_handles_top_level_confirm_and_excludes_verbatim_or_other_prompts() -> None:
    source_document = {
        "route": {"description": "Route the supported request."},
        "domains": {"confirmation": {"type": "enum", "values": ["yes", "no"]}},
        "slots": {"confirmed": {"domain": "confirmation"}},
        "path": [
            {"prompt": {"text": "Exact legal wording.", "verbatim": True}},
            {"prompt": {"text": "Describe the request."}},
        ],
        "confirm": {
            "confirm": "confirmed",
            "prompt": {"text": "Do you confirm?"},
        },
        "capabilities": {"await_external": {"prompt": {"text": "Profile-external wording."}}},
    }
    authored_source = _canonical_json(source_document)
    source_digest = _sha256(authored_source)
    draft_subjects = _draft_subjects_for_source(
        case_identifier="synthetic_case",
        correction_round=0,
        source_sha256=source_digest,
        source_document=source_document,
    )

    assert {
        (draft_subject.subject.kind, draft_subject.subject.pointer)
        for draft_subject in draft_subjects
    } == {
        ("route_description", "/route/description"),
        ("path_prompt", "/path/1/prompt/text"),
        ("confirm_prompt", "/confirm/prompt/text"),
    }
    confirm_subject = next(
        draft_subject
        for draft_subject in draft_subjects
        if draft_subject.subject.kind == "confirm_prompt"
    )
    confirm_context = json.loads(confirm_subject.public_interaction_context_json)
    assert "prompt" not in confirm_context["confirm_contract"]
    assert confirm_context["confirm_contract"] == {"confirm": "confirmed"}
    assert confirm_context["referenced_slot"]["identifier"] == "confirmed"
    assert confirm_context["referenced_domain"]["identifier"] == "confirmation"


def test_finalizer_accepts_edited_noncanonical_draft_and_returns_canonical_review() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    draft = presentation_review_draft(evidence.to_json(), verification)
    completed_document = _completed_draft_document(draft)

    finalized_review = finalize_presentation_review_draft(
        json.dumps(completed_document, ensure_ascii=False, indent=2),
        evidence.to_json(),
        verification,
    )
    review_verification = verify_presentation_review(
        finalized_review.to_json(),
        evidence.to_json(),
        verification,
    )

    assert finalized_review.reviewer_identifier == "reviewer_one"
    assert finalized_review.passed
    assert finalized_review.to_json() == _canonical_json(finalized_review.to_dict())
    assert review_verification.digest == finalized_review.digest
    assert review_verification.subject_count == len(draft.subjects)


def test_finalizer_rejects_changed_packet_subject_question_and_context_data() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    draft = presentation_review_draft(evidence.to_json(), verification)

    changed_task_prompt = _completed_draft_document(draft)
    changed_task_prompt["public_tasks"][0]["prompt"] = "Changed public task prompt."
    with pytest.raises(ValueError, match="immutable 'public_tasks' data"):
        finalize_presentation_review_draft(
            _canonical_json(changed_task_prompt), evidence.to_json(), verification
        )

    changed_acceptance = _completed_draft_document(draft)
    changed_acceptance["public_tasks"][0]["acceptance"] = {}
    with pytest.raises(ValueError, match="immutable 'public_tasks' data"):
        finalize_presentation_review_draft(
            _canonical_json(changed_acceptance), evidence.to_json(), verification
        )

    changed_candidate_text = _completed_draft_document(draft)
    changed_candidate_text["assessments"][0]["candidate_text"] = "Changed candidate text."
    with pytest.raises(ValueError, match="immutable subject or context data"):
        finalize_presentation_review_draft(
            _canonical_json(changed_candidate_text), evidence.to_json(), verification
        )

    changed_public_context = _completed_draft_document(draft)
    changed_public_context["assessments"][0]["public_interaction_context"] = {"kind": "changed"}
    with pytest.raises(ValueError, match="immutable subject or context data"):
        finalize_presentation_review_draft(
            _canonical_json(changed_public_context), evidence.to_json(), verification
        )

    changed_subject = _completed_draft_document(draft)
    changed_subject["assessments"][0]["subject"]["pointer"] = "/changed"
    with pytest.raises(ValueError, match="immutable subject or context data"):
        finalize_presentation_review_draft(
            _canonical_json(changed_subject), evidence.to_json(), verification
        )

    changed_question = _completed_draft_document(draft)
    changed_question["assessments"][0]["criteria"][0]["question"] = "Changed question?"
    with pytest.raises(ValueError, match="immutable rubric question data"):
        finalize_presentation_review_draft(
            _canonical_json(changed_question), evidence.to_json(), verification
        )


def test_finalizer_rejects_unknown_missing_or_pending_review_data() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    draft = presentation_review_draft(evidence.to_json(), verification)

    unknown_top_level = _completed_draft_document(draft)
    unknown_top_level["unexpected"] = True
    with pytest.raises(ValueError, match="exact required fields"):
        finalize_presentation_review_draft(
            _canonical_json(unknown_top_level), evidence.to_json(), verification
        )

    unknown_assessment_field = _completed_draft_document(draft)
    unknown_assessment_field["assessments"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="exact required fields"):
        finalize_presentation_review_draft(
            _canonical_json(unknown_assessment_field), evidence.to_json(), verification
        )

    missing_assessment = _completed_draft_document(draft)
    missing_assessment["assessments"].pop()
    with pytest.raises(ValueError, match="exact subject set"):
        finalize_presentation_review_draft(
            _canonical_json(missing_assessment), evidence.to_json(), verification
        )

    pending_decision = _completed_draft_document(draft)
    pending_decision["assessments"][0]["criteria"][0]["decision"] = None
    with pytest.raises(ValueError, match="pending decision"):
        finalize_presentation_review_draft(
            _canonical_json(pending_decision), evidence.to_json(), verification
        )

    blank_rationale = _completed_draft_document(draft)
    blank_rationale["assessments"][0]["criteria"][0]["rationale"] = " "
    with pytest.raises(ValueError, match="rationale is required"):
        finalize_presentation_review_draft(
            _canonical_json(blank_rationale), evidence.to_json(), verification
        )

    invalid_reviewer = _completed_draft_document(draft)
    invalid_reviewer["reviewer_identifier"] = "Reviewer One"
    with pytest.raises(ValueError, match="reviewer identifier is invalid"):
        finalize_presentation_review_draft(
            _canonical_json(invalid_reviewer), evidence.to_json(), verification
        )


def test_review_is_canonical_content_addressed_and_derives_pass_state() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    draft = presentation_review_draft(evidence.to_json(), verification)
    passing_review = _review(draft)
    failing_review = _review(draft, failing_criterion="route_scope_exclusion")

    assert passing_review.passed
    assert not failing_review.passed
    assert passing_review.digest != failing_review.digest
    assert (
        passing_review.to_json()
        == AuthoringPresentationReview.from_json(passing_review.to_json()).to_json()
    )
    review_document = json.loads(passing_review.to_json())
    assert review_document["format"] == PRESENTATION_REVIEW_FORMAT
    assert review_document["rubric"]["digest"] == PRESENTATION_RUBRIC_DIGEST
    assert review_document["digest"] == passing_review.digest


def test_review_enforces_exact_criterion_and_subject_closure() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    draft = presentation_review_draft(evidence.to_json(), verification)
    first_assessment = _assessment(draft.subjects[0].subject)

    with pytest.raises(ValueError, match="fixed rubric"):
        replace(first_assessment, criteria=first_assessment.criteria[:-1])
    with pytest.raises(ValueError, match="fixed rubric"):
        replace(
            first_assessment,
            criteria=(
                *first_assessment.criteria,
                PresentationCriterionResult(
                    criterion_identifier="unknown_criterion",
                    decision="pass",
                    rationale="Should not be accepted.",
                ),
            ),
        )
    with pytest.raises(ValueError, match="subjects must be unique"):
        AuthoringPresentationReview(
            benchmark_evidence_digest=verification.digest,
            reviewer_identifier="reviewer_one",
            assessments=(first_assessment, first_assessment),
        )

    incomplete_review = AuthoringPresentationReview(
        benchmark_evidence_digest=verification.digest,
        reviewer_identifier="reviewer_one",
        assessments=tuple(
            _assessment(draft_subject.subject) for draft_subject in draft.subjects[:-1]
        ),
    )
    with pytest.raises(ValueError, match="do not exactly match"):
        verify_presentation_review(
            incomplete_review.to_json(),
            evidence.to_json(),
            verification,
        )


def test_offline_verifier_binds_review_to_exact_evidence_subjects() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    draft = presentation_review_draft(evidence.to_json(), verification)
    review = _review(draft)

    review_verification = verify_presentation_review(
        review.to_json(),
        evidence.to_json(),
        verification,
    )

    assert review_verification.digest == review.digest
    assert review_verification.benchmark_evidence_digest == evidence.digest
    assert review_verification.reviewer_identifier == "reviewer_one"
    assert review_verification.passed
    assert review_verification.subject_count == len(draft.subjects)
    assert "candidate_text" not in _canonical_json(review_verification.to_dict())

    stale_subject = replace(draft.subjects[0].subject, text_sha256="0" * 64)
    stale_assessments = (
        _assessment(stale_subject),
        *tuple(_assessment(subject.subject) for subject in draft.subjects[1:]),
    )
    stale_review = AuthoringPresentationReview(
        benchmark_evidence_digest=verification.digest,
        reviewer_identifier="reviewer_one",
        assessments=stale_assessments,
    )
    with pytest.raises(ValueError, match="do not exactly match"):
        verify_presentation_review(
            stale_review.to_json(),
            evidence.to_json(),
            verification,
        )


def test_review_parser_rejects_noncanonical_unknown_and_derived_field_tampering() -> None:
    evidence = _evidence()
    verification = _verified_evidence(evidence)
    review = _review(presentation_review_draft(evidence.to_json(), verification))
    review_document: dict[str, Any] = json.loads(review.to_json())

    with pytest.raises(ValueError, match="canonical compact JSON"):
        AuthoringPresentationReview.from_json(json.dumps(review_document, indent=2))

    review_document["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
        AuthoringPresentationReview.from_json(_canonical_json(review_document))

    review_document = json.loads(review.to_json())
    review_document["passed"] = False
    unsigned_review = dict(review_document)
    unsigned_review.pop("digest")
    review_document["digest"] = _sha256(_canonical_json(unsigned_review))
    with pytest.raises(ValueError, match="pass state"):
        AuthoringPresentationReview.from_json(_canonical_json(review_document))


def test_review_schema_is_closed_and_owned() -> None:
    first_schema = presentation_review_schema()
    first_schema["properties"]["unexpected"] = {"type": "string"}

    assert "unexpected" not in presentation_review_schema()["properties"]
    jsonschema.Draft202012Validator.check_schema(presentation_review_schema())
