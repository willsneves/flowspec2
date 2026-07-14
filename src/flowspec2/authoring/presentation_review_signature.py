"""Detached authentication and promotion checks for presentation reviews."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Final, cast

import jsonschema

from flowspec2.json_codec import strict_json_loads

from .detached_signature import (
    DetachedEd25519KeyMismatchError,
    DetachedEd25519VerificationError,
    canonical_ed25519_signature_bytes,
    sign_detached_ed25519,
    verify_detached_ed25519,
)
from .evidence_verification import (
    AuthoringEvidenceVerification,
    verify_authoring_evidence,
)
from .presentation_review import (
    PresentationReviewVerification,
    verify_presentation_review,
)

AUTHORING_PRESENTATION_REVIEW_SIGNATURE_FORMAT: Final[str] = (
    "flowspec2/authoring-presentation-review-signature@1"
)
AUTHORING_PRESENTATION_REVIEW_SIGNATURE_ALGORITHM: Final[str] = "ed25519"
_SIGNATURE_SCHEMA_IDENTIFIER: Final[str] = (
    "https://prefeitura.rio/flowspec2/authoring-presentation-review-signature-1.json"
)
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE_MESSAGE_PREFIX: Final[bytes] = (
    b"flowspec2/authoring-presentation-review-signature@1\x00ed25519\x00"
)


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _signature_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": _SIGNATURE_SCHEMA_IDENTIFIER,
        "title": "FlowSpec2 authoring presentation review signature",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "format",
            "algorithm",
            "presentation_review_digest",
            "key_id",
            "signature",
        ],
        "properties": {
            "format": {"const": AUTHORING_PRESENTATION_REVIEW_SIGNATURE_FORMAT},
            "algorithm": {"const": AUTHORING_PRESENTATION_REVIEW_SIGNATURE_ALGORITHM},
            "presentation_review_digest": {
                "type": "string",
                "pattern": _DIGEST_PATTERN.pattern,
            },
            "key_id": {"type": "string", "pattern": _DIGEST_PATTERN.pattern},
            "signature": {"type": "string", "pattern": "^[A-Za-z0-9+/]{86}==$"},
        },
    }


def authoring_presentation_review_signature_schema() -> dict[str, Any]:
    """Return an owned closed schema for presentation-review signatures."""

    signature_schema = _signature_schema()
    jsonschema.Draft202012Validator.check_schema(signature_schema)
    return copy.deepcopy(signature_schema)


def _signature_message(presentation_review_digest: str) -> bytes:
    if not _DIGEST_PATTERN.fullmatch(presentation_review_digest):
        raise ValueError("signed presentation review digest must be lowercase SHA-256")
    return _SIGNATURE_MESSAGE_PREFIX + presentation_review_digest.encode("ascii")


def _verified_review(
    serialized_review: str,
    serialized_evidence: str,
    expected_repository_revision: str | None,
) -> tuple[AuthoringEvidenceVerification, PresentationReviewVerification]:
    verified_evidence = verify_authoring_evidence(
        serialized_evidence,
        expected_repository_revision=expected_repository_revision,
    )
    verified_review = verify_presentation_review(
        serialized_review,
        serialized_evidence,
        verified_evidence,
    )
    return verified_evidence, verified_review


@dataclass(frozen=True)
class AuthoringPresentationReviewSignature:
    """Canonical detached signature for one verified presentation review."""

    presentation_review_digest: str
    key_id: str
    signature_base64: str

    def __post_init__(self) -> None:
        if not _DIGEST_PATTERN.fullmatch(self.presentation_review_digest):
            raise ValueError("signature presentation review digest must be lowercase SHA-256")
        if not _DIGEST_PATTERN.fullmatch(self.key_id):
            raise ValueError("signature key identifier must be lowercase SHA-256")
        canonical_ed25519_signature_bytes(self.signature_base64)

    @property
    def signature_bytes(self) -> bytes:
        return canonical_ed25519_signature_bytes(self.signature_base64)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": AUTHORING_PRESENTATION_REVIEW_SIGNATURE_FORMAT,
            "algorithm": AUTHORING_PRESENTATION_REVIEW_SIGNATURE_ALGORITHM,
            "presentation_review_digest": self.presentation_review_digest,
            "key_id": self.key_id,
            "signature": self.signature_base64,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(
        cls,
        serialized_signature: str,
    ) -> AuthoringPresentationReviewSignature:
        """Parse one strict canonical presentation-review signature."""

        decoded_signature = strict_json_loads(serialized_signature)
        jsonschema.Draft202012Validator(_signature_schema()).validate(decoded_signature)
        if not isinstance(decoded_signature, dict):
            raise AssertionError("the presentation review signature schema accepted a non-object")
        if serialized_signature.strip() != _canonical_json(decoded_signature):
            raise ValueError("presentation review signature must use canonical compact JSON")
        return cls(
            presentation_review_digest=cast(
                str,
                decoded_signature["presentation_review_digest"],
            ),
            key_id=cast(str, decoded_signature["key_id"]),
            signature_base64=cast(str, decoded_signature["signature"]),
        )


@dataclass(frozen=True)
class AuthoringPresentationReviewAuthentication:
    """Safe summary of a review authenticated by one trusted public key."""

    evidence: AuthoringEvidenceVerification
    presentation_review: PresentationReviewVerification
    key_id: str

    def __post_init__(self) -> None:
        if not _DIGEST_PATTERN.fullmatch(self.key_id):
            raise ValueError("authentication key identifier must be lowercase SHA-256")
        if self.presentation_review.benchmark_evidence_digest != self.evidence.digest:
            raise ValueError("authenticated review and evidence digests must match")

    def to_dict(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "algorithm": AUTHORING_PRESENTATION_REVIEW_SIGNATURE_ALGORITHM,
            "key_id": self.key_id,
            "evidence": self.evidence.to_dict(),
            "presentation_review": self.presentation_review.to_dict(),
        }


@dataclass(frozen=True)
class AuthoringPromotionVerification:
    """Pure promotion decision over deterministic and authenticated review evidence."""

    authentication: AuthoringPresentationReviewAuthentication

    @property
    def deterministic_evidence_passed(self) -> bool:
        evidence = self.authentication.evidence
        return evidence.successful_cases == evidence.total_cases

    @property
    def presentation_review_passed(self) -> bool:
        return self.authentication.presentation_review.passed

    @property
    def signature_authenticated(self) -> bool:
        return True

    @property
    def eligible(self) -> bool:
        return (
            self.deterministic_evidence_passed
            and self.presentation_review_passed
            and self.signature_authenticated
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "deterministic_evidence_passed": self.deterministic_evidence_passed,
            "presentation_review_passed": self.presentation_review_passed,
            "signature_authenticated": self.signature_authenticated,
            "authentication": self.authentication.to_dict(),
        }


def sign_presentation_review(
    serialized_review: str,
    serialized_evidence: str,
    private_key_pem: bytes,
    *,
    expected_repository_revision: str | None = None,
) -> AuthoringPresentationReviewSignature:
    """Verify a review against evidence before signing its digest."""

    _verified_evidence, verified_review = _verified_review(
        serialized_review,
        serialized_evidence,
        expected_repository_revision,
    )
    detached_signature = sign_detached_ed25519(
        _signature_message(verified_review.digest),
        private_key_pem,
    )
    return AuthoringPresentationReviewSignature(
        presentation_review_digest=verified_review.digest,
        key_id=detached_signature.key_id,
        signature_base64=detached_signature.signature_base64,
    )


def verify_presentation_review_signature(
    serialized_review: str,
    serialized_evidence: str,
    serialized_signature: str,
    public_key_pem: bytes,
    *,
    expected_repository_revision: str | None = None,
) -> AuthoringPresentationReviewAuthentication:
    """Reverify review and evidence, then authenticate the review digest."""

    verified_evidence, verified_review = _verified_review(
        serialized_review,
        serialized_evidence,
        expected_repository_revision,
    )
    presentation_signature = AuthoringPresentationReviewSignature.from_json(serialized_signature)
    if presentation_signature.presentation_review_digest != verified_review.digest:
        raise ValueError("presentation review signature names a different review digest")
    try:
        public_key_id = verify_detached_ed25519(
            _signature_message(verified_review.digest),
            presentation_signature.signature_base64,
            public_key_pem,
            expected_key_id=presentation_signature.key_id,
        )
    except DetachedEd25519KeyMismatchError:
        raise ValueError("presentation review signature names a different public key") from None
    except DetachedEd25519VerificationError as signature_error:
        raise ValueError("presentation review signature is invalid") from signature_error
    return AuthoringPresentationReviewAuthentication(
        evidence=verified_evidence,
        presentation_review=verified_review,
        key_id=public_key_id,
    )


def verify_authoring_promotion(
    serialized_evidence: str,
    serialized_review: str,
    serialized_signature: str,
    public_key_pem: bytes,
    *,
    expected_repository_revision: str | None = None,
) -> AuthoringPromotionVerification:
    """Return offline eligibility only after all required artifacts authenticate."""

    authentication = verify_presentation_review_signature(
        serialized_review,
        serialized_evidence,
        serialized_signature,
        public_key_pem,
        expected_repository_revision=expected_repository_revision,
    )
    return AuthoringPromotionVerification(authentication=authentication)
