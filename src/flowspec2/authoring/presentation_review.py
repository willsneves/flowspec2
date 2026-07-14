"""Closed human-review contracts for author-authored presentation prose."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import jsonschema

from flowspec2.json_codec import strict_json_loads

from .corpus import load_reference_authoring_corpus
from .evidence_verification import AuthoringEvidenceVerification

PRESENTATION_RUBRIC_FORMAT: Final[str] = "flowspec2/authoring-presentation-rubric@1"
PRESENTATION_RUBRIC_IDENTIFIER: Final[str] = "flowspec2_citizen_presentation"
PRESENTATION_REVIEW_FORMAT: Final[str] = "flowspec2/authoring-presentation-review@1"
PRESENTATION_REVIEW_DRAFT_FORMAT: Final[str] = "flowspec2/authoring-presentation-review-draft@1"
_PRESENTATION_REVIEW_SCHEMA_IDENTIFIER: Final[str] = (
    "https://prefeitura.rio/flowspec2/authoring-presentation-review-1.json"
)

PresentationSubjectKind = Literal["route_description", "path_prompt", "confirm_prompt"]
PresentationDecision = Literal["pass", "fail"]

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


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


def _json_pointer_segment(property_name: str) -> str:
    return property_name.replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class PresentationRubricCriterion:
    """One fixed public question applied to a presentation subject kind."""

    identifier: str
    subject_kinds: tuple[PresentationSubjectKind, ...]
    question: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.identifier):
            raise ValueError("presentation criterion identifier is invalid")
        if not self.subject_kinds or len(set(self.subject_kinds)) != len(self.subject_kinds):
            raise ValueError("presentation criterion subject kinds must be non-empty and unique")
        if not self.question.strip():
            raise ValueError("presentation criterion question must be non-empty")
        object.__setattr__(self, "subject_kinds", tuple(sorted(self.subject_kinds)))
        object.__setattr__(self, "question", self.question.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "subject_kinds": list(self.subject_kinds),
            "question": self.question,
        }


PRESENTATION_RUBRIC_CRITERIA: Final[tuple[PresentationRubricCriterion, ...]] = (
    PresentationRubricCriterion(
        identifier="prompt_claim_fidelity",
        subject_kinds=("path_prompt", "confirm_prompt"),
        question=(
            "Does the prompt avoid implying completion, authorization, privacy, or backend "
            "outcomes that the public runtime contract does not guarantee?"
        ),
    ),
    PresentationRubricCriterion(
        identifier="prompt_input_contract_fidelity",
        subject_kinds=("path_prompt", "confirm_prompt"),
        question=(
            "Does the prompt request only information or action supported by its public input "
            "and interaction contract?"
        ),
    ),
    PresentationRubricCriterion(
        identifier="prompt_interaction_context",
        subject_kinds=("path_prompt", "confirm_prompt"),
        question="Does the prompt give the citizen enough context to answer at this point?",
    ),
    PresentationRubricCriterion(
        identifier="prompt_requested_action_clarity",
        subject_kinds=("path_prompt", "confirm_prompt"),
        question="Does the prompt clearly state what the citizen should provide, choose, or do?",
    ),
    PresentationRubricCriterion(
        identifier="route_claim_fidelity",
        subject_kinds=("route_description",),
        question=(
            "Does the route description avoid capabilities or guarantees absent from the public "
            "flow contract?"
        ),
    ),
    PresentationRubricCriterion(
        identifier="route_scope_coverage",
        subject_kinds=("route_description",),
        question="Does the route description cover the supported citizen intent?",
    ),
    PresentationRubricCriterion(
        identifier="route_scope_exclusion",
        subject_kinds=("route_description",),
        question="Does the route description avoid adjacent or unsupported service intents?",
    ),
    PresentationRubricCriterion(
        identifier="route_standalone_meaning",
        subject_kinds=("route_description",),
        question=(
            "Is the route description understandable without private identifiers or implementation "
            "context?"
        ),
    ),
)


def _rubric_contract() -> dict[str, object]:
    return {
        "format": PRESENTATION_RUBRIC_FORMAT,
        "identifier": PRESENTATION_RUBRIC_IDENTIFIER,
        "criteria": [criterion.to_dict() for criterion in PRESENTATION_RUBRIC_CRITERIA],
    }


PRESENTATION_RUBRIC_DIGEST: Final[str] = _sha256(_canonical_json(_rubric_contract()))


def presentation_rubric() -> dict[str, object]:
    """Return an owned representation of the fixed public rubric."""

    return cast(dict[str, object], json.loads(_canonical_json(_rubric_contract())))


def _criteria_for_kind(
    subject_kind: PresentationSubjectKind,
) -> tuple[PresentationRubricCriterion, ...]:
    return tuple(
        criterion
        for criterion in PRESENTATION_RUBRIC_CRITERIA
        if subject_kind in criterion.subject_kinds
    )


@dataclass(frozen=True)
class PresentationSubject:
    """Content identity of one candidate-authored presentation string."""

    case_identifier: str
    correction_round: int
    source_sha256: str
    kind: PresentationSubjectKind
    pointer: str
    text_sha256: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.case_identifier):
            raise ValueError("presentation subject case identifier is invalid")
        if self.correction_round < 0:
            raise ValueError("presentation subject correction round must not be negative")
        if not _DIGEST_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("presentation subject source digest must be lowercase SHA-256")
        if self.kind not in {"route_description", "path_prompt", "confirm_prompt"}:
            raise ValueError("presentation subject kind is unsupported")
        if not self.pointer.startswith("/"):
            raise ValueError("presentation subject pointer must be a JSON Pointer")
        if not _DIGEST_PATTERN.fullmatch(self.text_sha256):
            raise ValueError("presentation subject text digest must be lowercase SHA-256")

    @property
    def identity(self) -> tuple[str, int, str, str]:
        return (
            self.case_identifier,
            self.correction_round,
            self.pointer,
            self.kind,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_identifier": self.case_identifier,
            "correction_round": self.correction_round,
            "source_sha256": self.source_sha256,
            "kind": self.kind,
            "pointer": self.pointer,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True)
class PresentationCriterionResult:
    """One attributable human decision under the fixed rubric."""

    criterion_identifier: str
    decision: PresentationDecision
    rationale: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.criterion_identifier):
            raise ValueError("presentation result criterion identifier is invalid")
        if self.decision not in {"pass", "fail"}:
            raise ValueError("presentation result decision is unsupported")
        if not self.rationale.strip():
            raise ValueError("presentation result rationale must be non-empty")
        object.__setattr__(self, "rationale", self.rationale.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "criterion_identifier": self.criterion_identifier,
            "decision": self.decision,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class PresentationAssessment:
    """Exact criterion closure for one candidate presentation subject."""

    subject: PresentationSubject
    criteria: tuple[PresentationCriterionResult, ...]

    def __post_init__(self) -> None:
        criteria = tuple(sorted(self.criteria, key=lambda result: result.criterion_identifier))
        actual_identifiers = tuple(result.criterion_identifier for result in criteria)
        expected_identifiers = tuple(
            criterion.identifier for criterion in _criteria_for_kind(self.subject.kind)
        )
        if actual_identifiers != expected_identifiers:
            raise ValueError(
                "presentation assessment criteria do not match the fixed rubric for its subject"
            )
        object.__setattr__(self, "criteria", criteria)

    @property
    def passed(self) -> bool:
        return all(result.decision == "pass" for result in self.criteria)

    def to_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject.to_dict(),
            "criteria": [result.to_dict() for result in self.criteria],
            "passed": self.passed,
        }


@dataclass(frozen=True)
class AuthoringPresentationReview:
    """Canonical content-addressed human review bound to benchmark evidence."""

    benchmark_evidence_digest: str
    reviewer_identifier: str
    assessments: tuple[PresentationAssessment, ...]
    format_identifier: str = PRESENTATION_REVIEW_FORMAT
    rubric_format: str = PRESENTATION_RUBRIC_FORMAT
    rubric_identifier: str = PRESENTATION_RUBRIC_IDENTIFIER
    rubric_digest: str = PRESENTATION_RUBRIC_DIGEST

    def __post_init__(self) -> None:
        if self.format_identifier != PRESENTATION_REVIEW_FORMAT:
            raise ValueError("presentation review format is unsupported")
        if not _DIGEST_PATTERN.fullmatch(self.benchmark_evidence_digest):
            raise ValueError("presentation review evidence digest must be lowercase SHA-256")
        if not _IDENTIFIER_PATTERN.fullmatch(self.reviewer_identifier):
            raise ValueError("presentation review reviewer identifier is invalid")
        if (
            self.rubric_format != PRESENTATION_RUBRIC_FORMAT
            or self.rubric_identifier != PRESENTATION_RUBRIC_IDENTIFIER
            or self.rubric_digest != PRESENTATION_RUBRIC_DIGEST
        ):
            raise ValueError("presentation review rubric does not match the fixed public rubric")
        assessments = tuple(sorted(self.assessments, key=lambda item: item.subject.identity))
        if not assessments:
            raise ValueError("presentation review requires assessments")
        subject_identities = tuple(assessment.subject.identity for assessment in assessments)
        if len(set(subject_identities)) != len(subject_identities):
            raise ValueError("presentation review subjects must be unique")
        object.__setattr__(self, "assessments", assessments)

    @property
    def passed(self) -> bool:
        return all(assessment.passed for assessment in self.assessments)

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "format": self.format_identifier,
            "benchmark_evidence_digest": self.benchmark_evidence_digest,
            "rubric": {
                "format": self.rubric_format,
                "identifier": self.rubric_identifier,
                "digest": self.rubric_digest,
            },
            "reviewer_identifier": self.reviewer_identifier,
            "assessments": [assessment.to_dict() for assessment in self.assessments],
            "passed": self.passed,
        }

    @property
    def digest(self) -> str:
        return _sha256(_canonical_json(self._unsigned_dict()))

    def to_dict(self) -> dict[str, object]:
        review_document = self._unsigned_dict()
        review_document["digest"] = self.digest
        return review_document

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, serialized_review: str) -> AuthoringPresentationReview:
        """Parse a canonical closed review and verify all derived fields."""

        decoded_review = strict_json_loads(serialized_review)
        jsonschema.Draft202012Validator(_review_schema()).validate(decoded_review)
        if not isinstance(decoded_review, dict):
            raise AssertionError("the presentation review schema accepted a non-object")
        review_document = cast(dict[str, Any], decoded_review)
        if serialized_review.strip() != _canonical_json(review_document):
            raise ValueError("presentation review must use canonical compact JSON")
        rubric_document = cast(dict[str, str], review_document["rubric"])
        assessments = tuple(
            PresentationAssessment(
                subject=PresentationSubject(**cast(dict[str, Any], assessment["subject"])),
                criteria=tuple(
                    PresentationCriterionResult(**criterion_result)
                    for criterion_result in cast(list[dict[str, Any]], assessment["criteria"])
                ),
            )
            for assessment in cast(list[dict[str, Any]], review_document["assessments"])
        )
        review = cls(
            benchmark_evidence_digest=cast(str, review_document["benchmark_evidence_digest"]),
            reviewer_identifier=cast(str, review_document["reviewer_identifier"]),
            assessments=assessments,
            format_identifier=cast(str, review_document["format"]),
            rubric_format=rubric_document["format"],
            rubric_identifier=rubric_document["identifier"],
            rubric_digest=rubric_document["digest"],
        )
        if review_document["passed"] is not review.passed:
            raise ValueError("presentation review pass state is not derived from its criteria")
        if review_document["digest"] != review.digest:
            raise ValueError("presentation review digest does not match its content")
        return review


@dataclass(frozen=True)
class PresentationReviewTaskContext:
    """Source-free public task context shown to a presentation reviewer."""

    task_identifier: str
    prompt: str
    acceptance_json: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.task_identifier):
            raise ValueError("presentation task context identifier is invalid")
        if not self.prompt.strip():
            raise ValueError("presentation task context prompt must be non-empty")
        acceptance_document = strict_json_loads(self.acceptance_json)
        if not isinstance(acceptance_document, dict):
            raise ValueError("presentation task acceptance must be a JSON object")
        if _canonical_json(acceptance_document) != self.acceptance_json:
            raise ValueError("presentation task acceptance must be canonical JSON")
        if any(
            forbidden_property in acceptance_document
            for forbidden_property in ("expected_flow_json", "feature_tags", "source")
        ):
            raise ValueError("presentation task context contains a private oracle field")
        object.__setattr__(self, "prompt", self.prompt.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "task_identifier": self.task_identifier,
            "prompt": self.prompt,
            "acceptance": strict_json_loads(self.acceptance_json),
        }


@dataclass(frozen=True)
class PresentationReviewDraftSubject:
    """Reviewer-facing candidate text and its content identity."""

    subject: PresentationSubject
    candidate_text: str
    public_interaction_context_json: str = "{}"

    def __post_init__(self) -> None:
        if not self.candidate_text.strip():
            raise ValueError("presentation draft candidate text must be non-empty")
        if _sha256(self.candidate_text) != self.subject.text_sha256:
            raise ValueError("presentation draft candidate text does not match its subject")
        public_interaction_context = strict_json_loads(self.public_interaction_context_json)
        if not isinstance(public_interaction_context, dict):
            raise ValueError("presentation public interaction context must be a JSON object")
        if _canonical_json(public_interaction_context) != self.public_interaction_context_json:
            raise ValueError("presentation public interaction context must be canonical JSON")

    def to_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject.to_dict(),
            "candidate_text": self.candidate_text,
            "public_interaction_context": strict_json_loads(self.public_interaction_context_json),
            "criteria": [
                {
                    "criterion_identifier": criterion.identifier,
                    "question": criterion.question,
                    "decision": None,
                    "rationale": "",
                }
                for criterion in _criteria_for_kind(self.subject.kind)
            ],
        }


@dataclass(frozen=True)
class PresentationReviewDraft:
    """Deterministic reviewer packet containing candidate prose but no private oracle."""

    benchmark_evidence_digest: str
    subjects: tuple[PresentationReviewDraftSubject, ...]
    format_identifier: str = PRESENTATION_REVIEW_DRAFT_FORMAT
    public_tasks: tuple[PresentationReviewTaskContext, ...] = ()

    def __post_init__(self) -> None:
        if self.format_identifier != PRESENTATION_REVIEW_DRAFT_FORMAT:
            raise ValueError("presentation review draft format is unsupported")
        if not _DIGEST_PATTERN.fullmatch(self.benchmark_evidence_digest):
            raise ValueError("presentation review draft evidence digest is invalid")
        subjects = tuple(sorted(self.subjects, key=lambda item: item.subject.identity))
        if not subjects:
            raise ValueError("presentation review draft requires subjects")
        identities = tuple(subject.subject.identity for subject in subjects)
        if len(set(identities)) != len(identities):
            raise ValueError("presentation review draft subjects must be unique")
        public_tasks = tuple(
            sorted(self.public_tasks, key=lambda public_task: public_task.task_identifier)
        )
        task_identifiers = tuple(public_task.task_identifier for public_task in public_tasks)
        if len(set(task_identifiers)) != len(task_identifiers):
            raise ValueError("presentation review draft task contexts must be unique")
        if public_tasks and set(task_identifiers) != {
            subject.subject.case_identifier for subject in subjects
        }:
            raise ValueError("presentation review draft tasks do not match its subjects")
        object.__setattr__(self, "subjects", subjects)
        object.__setattr__(self, "public_tasks", public_tasks)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format_identifier,
            "benchmark_evidence_digest": self.benchmark_evidence_digest,
            "rubric": {
                **presentation_rubric(),
                "digest": PRESENTATION_RUBRIC_DIGEST,
            },
            "public_tasks": [public_task.to_dict() for public_task in self.public_tasks],
            "reviewer_identifier": "",
            "assessments": [subject.to_dict() for subject in self.subjects],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


def _subject(
    *,
    case_identifier: str,
    correction_round: int,
    source_sha256: str,
    kind: PresentationSubjectKind,
    pointer: str,
    candidate_text: str,
    public_interaction_context: Mapping[str, object],
) -> PresentationReviewDraftSubject:
    return PresentationReviewDraftSubject(
        subject=PresentationSubject(
            case_identifier=case_identifier,
            correction_round=correction_round,
            source_sha256=source_sha256,
            kind=kind,
            pointer=pointer,
            text_sha256=_sha256(candidate_text),
        ),
        candidate_text=candidate_text,
        public_interaction_context_json=_canonical_json(public_interaction_context),
    )


def _prompt_subject(
    *,
    case_identifier: str,
    correction_round: int,
    source_sha256: str,
    prompt_contract: object,
    kind: Literal["path_prompt", "confirm_prompt"],
    pointer: str,
    public_interaction_context: Mapping[str, object],
) -> PresentationReviewDraftSubject | None:
    if not isinstance(prompt_contract, Mapping) or prompt_contract.get("verbatim") is True:
        return None
    candidate_text = prompt_contract.get("text")
    if not isinstance(candidate_text, str) or not candidate_text.strip():
        raise ValueError(f"candidate prompt at {pointer!r} must contain non-empty text")
    return _subject(
        case_identifier=case_identifier,
        correction_round=correction_round,
        source_sha256=source_sha256,
        kind=kind,
        pointer=pointer,
        candidate_text=candidate_text,
        public_interaction_context=public_interaction_context,
    )


def _referenced_input_context(
    source_document: Mapping[str, object],
    interaction_contract: Mapping[str, object],
) -> dict[str, object]:
    slot_identifier = interaction_contract.get("slot")
    if not isinstance(slot_identifier, str):
        confirmation_identifier = interaction_contract.get("confirm")
        slot_identifier = (
            confirmation_identifier if isinstance(confirmation_identifier, str) else None
        )
    if slot_identifier is None:
        return {}
    slots = source_document.get("slots")
    if not isinstance(slots, Mapping):
        return {}
    slot_contract = slots.get(slot_identifier)
    if not isinstance(slot_contract, Mapping):
        return {}
    referenced_context: dict[str, object] = {
        "referenced_slot": {
            "identifier": slot_identifier,
            "contract": strict_json_loads(_canonical_json(slot_contract)),
        }
    }
    domain_identifier = slot_contract.get("domain")
    domains = source_document.get("domains")
    if isinstance(domain_identifier, str) and isinstance(domains, Mapping):
        domain_contract = domains.get(domain_identifier)
        if isinstance(domain_contract, Mapping):
            referenced_context["referenced_domain"] = {
                "identifier": domain_identifier,
                "contract": strict_json_loads(_canonical_json(domain_contract)),
            }
    return referenced_context


def _path_interaction_context(
    source_document: Mapping[str, object],
    path_step: Mapping[str, object],
) -> dict[str, object]:
    public_path_step = {
        property_name: strict_json_loads(_canonical_json(property_value))
        for property_name, property_value in path_step.items()
        if property_name not in {"prompt", "step"}
    }
    return {
        "kind": "path_prompt",
        "path_step": public_path_step,
        **_referenced_input_context(source_document, path_step),
    }


def _confirm_interaction_context(
    source_document: Mapping[str, object],
    confirm_contract: Mapping[str, object],
) -> dict[str, object]:
    public_confirm_contract = {
        property_name: strict_json_loads(_canonical_json(property_value))
        for property_name, property_value in confirm_contract.items()
        if property_name != "prompt"
    }
    return {
        "kind": "confirm_prompt",
        "confirm_contract": public_confirm_contract,
        **_referenced_input_context(source_document, confirm_contract),
    }


def _draft_subjects_for_source(
    *,
    case_identifier: str,
    correction_round: int,
    source_sha256: str,
    source_document: Mapping[str, object],
) -> tuple[PresentationReviewDraftSubject, ...]:
    route_contract = source_document.get("route")
    route_description = (
        route_contract.get("description") if isinstance(route_contract, Mapping) else None
    )
    if not isinstance(route_description, str) or not route_description.strip():
        raise ValueError("successful presentation source requires a route description")
    draft_subjects = [
        _subject(
            case_identifier=case_identifier,
            correction_round=correction_round,
            source_sha256=source_sha256,
            kind="route_description",
            pointer="/route/description",
            candidate_text=route_description,
            public_interaction_context={"kind": "route_description"},
        )
    ]

    path_contract = source_document.get("path")
    if not isinstance(path_contract, list):
        raise ValueError("successful presentation source requires an ordered path")
    for path_index, path_step in enumerate(path_contract):
        if not isinstance(path_step, Mapping):
            continue
        if prompt_subject := _prompt_subject(
            case_identifier=case_identifier,
            correction_round=correction_round,
            source_sha256=source_sha256,
            prompt_contract=path_step.get("prompt"),
            kind="path_prompt",
            pointer=f"/path/{path_index}/prompt/text",
            public_interaction_context=_path_interaction_context(
                source_document,
                path_step,
            ),
        ):
            draft_subjects.append(prompt_subject)

    confirm_contract = source_document.get("confirm")
    if isinstance(confirm_contract, Mapping):
        if confirm_subject := _prompt_subject(
            case_identifier=case_identifier,
            correction_round=correction_round,
            source_sha256=source_sha256,
            prompt_contract=confirm_contract.get("prompt"),
            kind="confirm_prompt",
            pointer=f"/{_json_pointer_segment('confirm')}/prompt/text",
            public_interaction_context=_confirm_interaction_context(
                source_document,
                confirm_contract,
            ),
        ):
            draft_subjects.append(confirm_subject)
    return tuple(draft_subjects)


def _evidence_document(
    serialized_evidence: str,
    verified_evidence: AuthoringEvidenceVerification,
) -> dict[str, Any]:
    decoded_evidence = strict_json_loads(serialized_evidence)
    if not isinstance(decoded_evidence, dict):
        raise ValueError("verified authoring evidence must be a JSON object")
    evidence_document = cast(dict[str, Any], decoded_evidence)
    if serialized_evidence.strip() != _canonical_json(evidence_document):
        raise ValueError("verified authoring evidence must use canonical compact JSON")
    unsigned_document = dict(evidence_document)
    recorded_digest = unsigned_document.pop("digest", None)
    if recorded_digest != verified_evidence.digest:
        raise ValueError("serialized evidence does not match its verified digest")
    if _sha256(_canonical_json(unsigned_document)) != verified_evidence.digest:
        raise ValueError("serialized evidence content does not match its verified digest")
    return evidence_document


def presentation_review_draft(
    serialized_evidence: str,
    verified_evidence: AuthoringEvidenceVerification,
) -> PresentationReviewDraft:
    """Extract every reviewable final-success presentation from verified evidence."""

    evidence_document = _evidence_document(serialized_evidence, verified_evidence)
    report_document = cast(dict[str, Any], evidence_document["report"])
    case_result_documents = cast(list[dict[str, Any]], report_document["case_results"])
    capture_documents = cast(list[dict[str, Any]], evidence_document["captures"])
    captures_by_key = {
        (capture["case_identifier"], capture["correction_round"]): capture
        for capture in capture_documents
    }
    packaged_cases = {
        benchmark_case.identifier: benchmark_case
        for benchmark_case in load_reference_authoring_corpus().cases
    }
    draft_subjects: list[PresentationReviewDraftSubject] = []
    public_tasks: list[PresentationReviewTaskContext] = []
    successful_cases = 0
    for case_result in case_result_documents:
        attempts = cast(list[dict[str, Any]], case_result["attempts"])
        if not attempts or attempts[-1]["succeeded"] is not True:
            continue
        successful_cases += 1
        case_identifier = cast(str, case_result["case_identifier"])
        benchmark_case = packaged_cases.get(case_identifier)
        if benchmark_case is None:
            raise ValueError("successful presentation case is not in the packaged corpus")
        authoring_task = benchmark_case.authoring_task()
        public_tasks.append(
            PresentationReviewTaskContext(
                task_identifier=authoring_task.identifier,
                prompt=authoring_task.prompt,
                acceptance_json=_canonical_json(authoring_task.acceptance.to_dict()),
            )
        )
        correction_round = cast(int, attempts[-1]["correction_round"])
        capture = captures_by_key.get((case_identifier, correction_round))
        if capture is None:
            raise ValueError("successful presentation case has no aligned final capture")
        authored_source = cast(str, capture["authored_source"])
        source_sha256 = cast(str, capture["source_sha256"])
        if _sha256(authored_source) != source_sha256:
            raise ValueError("presentation source does not match its capture digest")
        source_document = strict_json_loads(authored_source)
        if not isinstance(source_document, dict):
            raise ValueError("successful presentation source must be a JSON object")
        draft_subjects.extend(
            _draft_subjects_for_source(
                case_identifier=case_identifier,
                correction_round=correction_round,
                source_sha256=source_sha256,
                source_document=source_document,
            )
        )

    if successful_cases != verified_evidence.successful_cases:
        raise ValueError("presentation subjects disagree with the verified successful-case count")
    return PresentationReviewDraft(
        benchmark_evidence_digest=verified_evidence.digest,
        subjects=tuple(draft_subjects),
        public_tasks=tuple(public_tasks),
    )


@dataclass(frozen=True)
class PresentationReviewVerification:
    """Safe offline summary of a closed review bound to verified evidence."""

    digest: str
    benchmark_evidence_digest: str
    reviewer_identifier: str
    passed: bool
    subject_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "benchmark_evidence_digest": self.benchmark_evidence_digest,
            "reviewer_identifier": self.reviewer_identifier,
            "passed": self.passed,
            "subject_count": self.subject_count,
        }


def _require_exact_keys(
    document: Mapping[str, object],
    expected_keys: frozenset[str],
    contract_name: str,
) -> None:
    if set(document) != expected_keys:
        raise ValueError(f"{contract_name} does not have the exact required fields")


def finalize_presentation_review_draft(
    serialized_draft: str,
    serialized_evidence: str,
    verified_evidence: AuthoringEvidenceVerification,
) -> AuthoringPresentationReview:
    """Finalize human decisions while proving the reviewer packet stayed immutable."""

    expected_draft = presentation_review_draft(serialized_evidence, verified_evidence)
    expected_document = expected_draft.to_dict()
    decoded_draft = strict_json_loads(serialized_draft)
    if not isinstance(decoded_draft, dict):
        raise ValueError("presentation review draft must be a JSON object")
    draft_document = cast(dict[str, object], decoded_draft)
    _require_exact_keys(
        draft_document,
        frozenset(expected_document),
        "presentation review draft",
    )
    for immutable_field in (
        "format",
        "benchmark_evidence_digest",
        "rubric",
        "public_tasks",
    ):
        if draft_document[immutable_field] != expected_document[immutable_field]:
            raise ValueError(
                f"presentation review draft changed immutable {immutable_field!r} data"
            )

    reviewer_identifier = draft_document["reviewer_identifier"]
    if not isinstance(reviewer_identifier, str) or not _IDENTIFIER_PATTERN.fullmatch(
        reviewer_identifier
    ):
        raise ValueError("presentation review draft reviewer identifier is invalid")
    edited_assessments = draft_document["assessments"]
    expected_assessments = expected_document["assessments"]
    if not isinstance(edited_assessments, list) or not isinstance(expected_assessments, list):
        raise ValueError("presentation review draft assessments must be arrays")
    if len(edited_assessments) != len(expected_assessments):
        raise ValueError("presentation review draft changed the exact subject set")

    finalized_assessments: list[PresentationAssessment] = []
    for assessment_index, (edited_assessment, expected_assessment) in enumerate(
        zip(edited_assessments, expected_assessments, strict=True)
    ):
        if not isinstance(edited_assessment, dict) or not isinstance(expected_assessment, dict):
            raise ValueError("presentation review draft assessment must be an object")
        _require_exact_keys(
            edited_assessment,
            frozenset(expected_assessment),
            f"presentation assessment {assessment_index}",
        )
        for immutable_field in (
            "subject",
            "candidate_text",
            "public_interaction_context",
        ):
            if edited_assessment[immutable_field] != expected_assessment[immutable_field]:
                raise ValueError(
                    "presentation review draft changed immutable subject or context data"
                )
        edited_criteria = edited_assessment["criteria"]
        expected_criteria = expected_assessment["criteria"]
        if not isinstance(edited_criteria, list) or not isinstance(expected_criteria, list):
            raise ValueError("presentation assessment criteria must be arrays")
        if len(edited_criteria) != len(expected_criteria):
            raise ValueError("presentation assessment changed the exact criterion set")

        criterion_results: list[PresentationCriterionResult] = []
        for criterion_index, (edited_criterion, expected_criterion) in enumerate(
            zip(edited_criteria, expected_criteria, strict=True)
        ):
            if not isinstance(edited_criterion, dict) or not isinstance(expected_criterion, dict):
                raise ValueError("presentation criterion must be an object")
            _require_exact_keys(
                edited_criterion,
                frozenset(expected_criterion),
                f"presentation criterion {assessment_index}:{criterion_index}",
            )
            for immutable_field in ("criterion_identifier", "question"):
                if edited_criterion[immutable_field] != expected_criterion[immutable_field]:
                    raise ValueError(
                        "presentation review draft changed immutable rubric question data"
                    )
            decision = edited_criterion["decision"]
            rationale = edited_criterion["rationale"]
            if not isinstance(decision, str) or decision not in {"pass", "fail"}:
                raise ValueError("presentation review draft contains a pending decision")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("presentation review draft criterion rationale is required")
            criterion_results.append(
                PresentationCriterionResult(
                    criterion_identifier=cast(str, edited_criterion["criterion_identifier"]),
                    decision=cast(PresentationDecision, decision),
                    rationale=rationale,
                )
            )

        expected_subject = cast(dict[str, Any], expected_assessment["subject"])
        finalized_assessments.append(
            PresentationAssessment(
                subject=PresentationSubject(**expected_subject),
                criteria=tuple(criterion_results),
            )
        )

    return AuthoringPresentationReview(
        benchmark_evidence_digest=verified_evidence.digest,
        reviewer_identifier=reviewer_identifier,
        assessments=tuple(finalized_assessments),
    )


def verify_presentation_review(
    serialized_review: str,
    serialized_evidence: str,
    verified_evidence: AuthoringEvidenceVerification,
) -> PresentationReviewVerification:
    """Verify exact subject and criterion closure without network access."""

    review = AuthoringPresentationReview.from_json(serialized_review)
    if review.benchmark_evidence_digest != verified_evidence.digest:
        raise ValueError("presentation review is bound to different authoring evidence")
    expected_draft = presentation_review_draft(serialized_evidence, verified_evidence)
    expected_subjects = tuple(subject.subject for subject in expected_draft.subjects)
    observed_subjects = tuple(assessment.subject for assessment in review.assessments)
    if observed_subjects != expected_subjects:
        raise ValueError("presentation review subjects do not exactly match verified evidence")
    return PresentationReviewVerification(
        digest=review.digest,
        benchmark_evidence_digest=review.benchmark_evidence_digest,
        reviewer_identifier=review.reviewer_identifier,
        passed=review.passed,
        subject_count=len(review.assessments),
    )


def _review_schema() -> dict[str, Any]:
    criterion_identifiers = [criterion.identifier for criterion in PRESENTATION_RUBRIC_CRITERIA]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": _PRESENTATION_REVIEW_SCHEMA_IDENTIFIER,
        "title": "FlowSpec2 authoring presentation review",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "format",
            "benchmark_evidence_digest",
            "rubric",
            "reviewer_identifier",
            "assessments",
            "passed",
            "digest",
        ],
        "properties": {
            "format": {"const": PRESENTATION_REVIEW_FORMAT},
            "benchmark_evidence_digest": {"$ref": "#/$defs/digest"},
            "rubric": {
                "type": "object",
                "additionalProperties": False,
                "required": ["format", "identifier", "digest"],
                "properties": {
                    "format": {"const": PRESENTATION_RUBRIC_FORMAT},
                    "identifier": {"const": PRESENTATION_RUBRIC_IDENTIFIER},
                    "digest": {"const": PRESENTATION_RUBRIC_DIGEST},
                },
            },
            "reviewer_identifier": {"$ref": "#/$defs/identifier"},
            "assessments": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/assessment"},
            },
            "passed": {"type": "boolean"},
            "digest": {"$ref": "#/$defs/digest"},
        },
        "$defs": {
            "identifier": {
                "type": "string",
                "pattern": _IDENTIFIER_PATTERN.pattern,
            },
            "digest": {"type": "string", "pattern": _DIGEST_PATTERN.pattern},
            "subject": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "case_identifier",
                    "correction_round",
                    "source_sha256",
                    "kind",
                    "pointer",
                    "text_sha256",
                ],
                "properties": {
                    "case_identifier": {"$ref": "#/$defs/identifier"},
                    "correction_round": {"type": "integer", "minimum": 0},
                    "source_sha256": {"$ref": "#/$defs/digest"},
                    "kind": {"enum": ["route_description", "path_prompt", "confirm_prompt"]},
                    "pointer": {"type": "string", "pattern": "^/"},
                    "text_sha256": {"$ref": "#/$defs/digest"},
                },
            },
            "criterion": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion_identifier", "decision", "rationale"],
                "properties": {
                    "criterion_identifier": {"enum": criterion_identifiers},
                    "decision": {"enum": ["pass", "fail"]},
                    "rationale": {"type": "string", "minLength": 1},
                },
            },
            "assessment": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "criteria", "passed"],
                "properties": {
                    "subject": {"$ref": "#/$defs/subject"},
                    "criteria": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"$ref": "#/$defs/criterion"},
                    },
                    "passed": {"type": "boolean"},
                },
            },
        },
    }


def presentation_review_schema() -> dict[str, Any]:
    """Return an owned closed schema for canonical presentation reviews."""

    schema_document = _review_schema()
    jsonschema.Draft202012Validator.check_schema(schema_document)
    return cast(dict[str, Any], json.loads(_canonical_json(schema_document)))
