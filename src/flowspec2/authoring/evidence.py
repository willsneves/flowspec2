"""Content-addressed evidence for externally authored benchmark runs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from flowspec2.json_codec import strict_json_loads, validate_json_value

from .benchmark import AuthoredSource, AuthoringBenchmarkReport, AuthoringRequest
from .corpus import AuthoringCorpus

AUTHORING_EVIDENCE_FORMAT: Final[str] = "flowspec2/authoring-benchmark-evidence@2"
SOURCE_ADAPTER_CONTRACT: Final[dict[str, str]] = {
    "format": "flowspec/2",
    "implementation": "FlowSpec2JsonAdapter",
    "version": "1",
}

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|secret|access[_-]?token)",
    re.IGNORECASE,
)


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


def _contains_secret_key(json_document: object) -> bool:
    if isinstance(json_document, Mapping):
        return any(
            _SECRET_KEY_PATTERN.search(str(property_name)) is not None
            or _contains_secret_key(property_value)
            for property_name, property_value in json_document.items()
        )
    if isinstance(json_document, list):
        return any(_contains_secret_key(element_value) for element_value in json_document)
    return False


@dataclass(frozen=True)
class AuthoringCapture:
    """The exact authored source returned for one benchmark attempt."""

    case_identifier: str
    correction_round: int
    authored_source: str
    source_sha256: str
    effective_model_version: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.case_identifier):
            raise ValueError("authoring capture case identifier is invalid")
        if self.correction_round < 0:
            raise ValueError("authoring capture correction round must not be negative")
        if self.source_sha256 != _sha256(self.authored_source):
            raise ValueError("authoring capture source digest does not match its source")
        if self.effective_model_version is not None:
            normalized_model_version = self.effective_model_version.strip()
            if not normalized_model_version:
                raise ValueError("authoring capture model version must be non-empty when present")
            object.__setattr__(self, "effective_model_version", normalized_model_version)

    @classmethod
    def from_response(
        cls,
        case_identifier: str,
        correction_round: int,
        authored_response: AuthoredSource,
    ) -> AuthoringCapture:
        """Capture one response with its deterministic content identity."""

        return cls(
            case_identifier=case_identifier,
            correction_round=correction_round,
            authored_source=authored_response.source,
            source_sha256=_sha256(authored_response.source),
            effective_model_version=authored_response.effective_model_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_identifier": self.case_identifier,
            "correction_round": self.correction_round,
            "authored_source": self.authored_source,
            "source_sha256": self.source_sha256,
            "effective_model_version": self.effective_model_version,
        }


class RecordingAuthor:
    """Record exact model sources while preserving the benchmark author protocol."""

    def __init__(self, author: Callable[[AuthoringRequest], AuthoredSource]) -> None:
        self._author = author
        self._captures: list[AuthoringCapture] = []
        self._capture_keys: set[tuple[str, int]] = set()

    def __call__(self, authoring_request: AuthoringRequest) -> AuthoredSource:
        authored_response = self._author(authoring_request)
        if not isinstance(authored_response, AuthoredSource):
            raise TypeError("the recorded author must return AuthoredSource")
        capture_key = (
            authoring_request.task.identifier,
            authoring_request.correction_round,
        )
        if capture_key in self._capture_keys:
            raise ValueError(f"duplicate authoring capture: {capture_key!r}")
        self._capture_keys.add(capture_key)
        self._captures.append(
            AuthoringCapture.from_response(
                capture_key[0],
                capture_key[1],
                authored_response,
            )
        )
        return authored_response

    @property
    def captures(self) -> tuple[AuthoringCapture, ...]:
        return tuple(self._captures)


@dataclass(frozen=True)
class AuthoringProviderProvenance:
    """Closed non-secret configuration for the model author boundary."""

    identifier: str
    model: str
    sdk: str
    sdk_version: str
    prompt_format: str
    prompt_digest: str
    generation_configuration_json: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.identifier):
            raise ValueError("authoring provider identifier is invalid")
        for field_name, field_value in (
            ("model", self.model),
            ("sdk", self.sdk),
            ("sdk_version", self.sdk_version),
            ("prompt_format", self.prompt_format),
        ):
            if not field_value.strip():
                raise ValueError(f"authoring provider {field_name} must be non-empty")
        if not _DIGEST_PATTERN.fullmatch(self.prompt_digest):
            raise ValueError("authoring provider prompt digest must be lowercase SHA-256")
        generation_configuration = strict_json_loads(self.generation_configuration_json)
        if not isinstance(generation_configuration, dict):
            raise ValueError("authoring provider generation configuration must be an object")
        validate_json_value(
            generation_configuration,
            boundary="authoring provider generation configuration",
        )
        if _canonical_json(generation_configuration) != self.generation_configuration_json:
            raise ValueError("authoring provider generation configuration must be canonical JSON")
        if _contains_secret_key(generation_configuration):
            raise ValueError("authoring provider generation configuration contains a secret field")

    @classmethod
    def from_configuration(
        cls,
        *,
        identifier: str,
        model: str,
        sdk: str,
        sdk_version: str,
        prompt_format: str,
        prompt_digest: str,
        generation_configuration: Mapping[str, object],
    ) -> AuthoringProviderProvenance:
        return cls(
            identifier=identifier,
            model=model,
            sdk=sdk,
            sdk_version=sdk_version,
            prompt_format=prompt_format,
            prompt_digest=prompt_digest,
            generation_configuration_json=_canonical_json(dict(generation_configuration)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "model": self.model,
            "sdk": self.sdk,
            "sdk_version": self.sdk_version,
            "prompt_format": self.prompt_format,
            "prompt_digest": self.prompt_digest,
            "generation_configuration": json.loads(self.generation_configuration_json),
        }


@dataclass(frozen=True)
class AuthoringBenchmarkEvidence:
    """Canonical report, captures, and provenance for one completed run."""

    package_version: str
    repository_revision: str
    corpus: AuthoringCorpus
    profile_identifier: str
    profile_digest: str
    provider: AuthoringProviderProvenance
    report: AuthoringBenchmarkReport
    captures: tuple[AuthoringCapture, ...]

    def __post_init__(self) -> None:
        if not self.package_version.strip():
            raise ValueError("authoring evidence package version must be non-empty")
        if not self.repository_revision.strip():
            raise ValueError("authoring evidence repository revision must be non-empty")
        if self.profile_identifier != self.report.profile_identifier:
            raise ValueError("authoring evidence profile identifier disagrees with report")
        if not _DIGEST_PATTERN.fullmatch(self.profile_digest):
            raise ValueError("authoring evidence profile digest must be lowercase SHA-256")
        report_case_identifiers = tuple(
            case_result.case_identifier for case_result in self.report.case_results
        )
        corpus_case_identifiers = tuple(
            benchmark_case.identifier for benchmark_case in self.corpus.cases
        )
        if report_case_identifiers != corpus_case_identifiers:
            raise ValueError("authoring evidence report cases disagree with corpus")

        expected_attempts = tuple(
            (case_result.case_identifier, authoring_attempt)
            for case_result in self.report.case_results
            for authoring_attempt in case_result.attempts
        )
        actual_capture_keys = tuple(
            (capture.case_identifier, capture.correction_round) for capture in self.captures
        )
        expected_capture_keys = tuple(
            (case_identifier, authoring_attempt.correction_round)
            for case_identifier, authoring_attempt in expected_attempts
        )
        if actual_capture_keys != expected_capture_keys:
            raise ValueError("authoring evidence captures do not align with report attempts")
        if any(
            capture.source_sha256 != authoring_attempt.source_sha256
            for capture, (_case_identifier, authoring_attempt) in zip(
                self.captures,
                expected_attempts,
                strict=True,
            )
        ):
            raise ValueError("authoring evidence capture digest disagrees with report")
        if self.provider.identifier == "google_gemini" and any(
            capture.effective_model_version is None for capture in self.captures
        ):
            raise ValueError("Gemini authoring evidence requires effective model versions")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "format": AUTHORING_EVIDENCE_FORMAT,
            "package_version": self.package_version,
            "repository_revision": self.repository_revision,
            "corpus": self.corpus.metadata(),
            "profile": {
                "identifier": self.profile_identifier,
                "digest": self.profile_digest,
            },
            "source_adapter": dict(SOURCE_ADAPTER_CONTRACT),
            "provider": self.provider.to_dict(),
            "captures": [capture.to_dict() for capture in self.captures],
            "report": self.report.to_dict(),
        }

    @property
    def digest(self) -> str:
        return _sha256(_canonical_json(self._unsigned_dict()))

    def to_dict(self) -> dict[str, object]:
        evidence_document = self._unsigned_dict()
        evidence_document["digest"] = self.digest
        return evidence_document

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())
