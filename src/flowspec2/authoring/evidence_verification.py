"""Strict offline verification and deterministic replay of authoring evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Final, cast

import jsonschema

from flowspec2.json_codec import strict_json_loads
from flowspec2.profiles import reference_profile

from .benchmark import replay_authoring_benchmark
from .corpus import load_reference_authoring_corpus
from .evidence import AUTHORING_EVIDENCE_FORMAT, SOURCE_ADAPTER_CONTRACT

_EVIDENCE_SCHEMA_PATH: Final[Path] = Path(__file__).with_name("authoring-evidence.schema.json")
_SECRET_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|secret|access[_-]?token)",
    re.IGNORECASE,
)
_evidence_schema_cache: dict[str, Any] | None = None


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


def _evidence_schema() -> dict[str, Any]:
    global _evidence_schema_cache
    cached_schema = _evidence_schema_cache
    if cached_schema is None:
        decoded_schema = strict_json_loads(_EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
        if not isinstance(decoded_schema, dict):
            raise ValueError("authoring evidence schema must be a JSON object")
        jsonschema.Draft202012Validator.check_schema(decoded_schema)
        cached_schema = cast(dict[str, Any], decoded_schema)
        _evidence_schema_cache = cached_schema
    return cached_schema


def authoring_evidence_schema() -> dict[str, Any]:
    """Return an owned copy of the closed evidence schema."""

    return copy.deepcopy(_evidence_schema())


@dataclass(frozen=True)
class AuthoringEvidenceVerification:
    """Verified deterministic identity and replay summary for one artifact."""

    digest: str
    benchmark_identifier: str
    repository_revision: str
    package_version: str
    provider_identifier: str
    requested_model: str
    effective_model_versions: tuple[str, ...]
    successful_cases: int
    total_cases: int
    total_attempts: int

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable verification summary."""

        return {
            "digest": self.digest,
            "benchmark_identifier": self.benchmark_identifier,
            "repository_revision": self.repository_revision,
            "package_version": self.package_version,
            "provider_identifier": self.provider_identifier,
            "requested_model": self.requested_model,
            "effective_model_versions": list(self.effective_model_versions),
            "successful_cases": self.successful_cases,
            "total_cases": self.total_cases,
            "total_attempts": self.total_attempts,
        }


def verify_authoring_evidence(
    serialized_evidence: str,
    *,
    expected_repository_revision: str | None = None,
) -> AuthoringEvidenceVerification:
    """Validate provenance and replay every exact capture against packaged contracts."""

    decoded_evidence = strict_json_loads(serialized_evidence)
    jsonschema.Draft202012Validator(_evidence_schema()).validate(decoded_evidence)
    if not isinstance(decoded_evidence, dict):
        raise AssertionError("the evidence schema accepted a non-object document")
    evidence_document = cast(dict[str, Any], decoded_evidence)
    if serialized_evidence.strip() != _canonical_json(evidence_document):
        raise ValueError("authoring evidence must use canonical compact JSON")
    if evidence_document["format"] != AUTHORING_EVIDENCE_FORMAT:
        raise ValueError("authoring evidence format is unsupported")

    unsigned_document = dict(evidence_document)
    recorded_digest = cast(str, unsigned_document.pop("digest"))
    computed_digest = _sha256(_canonical_json(unsigned_document))
    if recorded_digest != computed_digest:
        raise ValueError("authoring evidence digest does not match its content")

    installed_package_version = package_version("flowspec2")
    recorded_package_version = cast(str, evidence_document["package_version"])
    if recorded_package_version != installed_package_version:
        raise ValueError("authoring evidence package version does not match the installed verifier")
    repository_revision = cast(str, evidence_document["repository_revision"])
    if (
        expected_repository_revision is not None
        and repository_revision != expected_repository_revision
    ):
        raise ValueError("authoring evidence repository revision does not match the expected value")

    reference_corpus = load_reference_authoring_corpus()
    if evidence_document["corpus"] != reference_corpus.metadata():
        raise ValueError("authoring evidence corpus does not match the installed corpus")
    flow_profile = reference_profile()
    expected_profile = {
        "identifier": flow_profile.identifier,
        "digest": flow_profile.digest,
    }
    if evidence_document["profile"] != expected_profile:
        raise ValueError("authoring evidence profile does not match the installed profile")
    if evidence_document["source_adapter"] != SOURCE_ADAPTER_CONTRACT:
        raise ValueError("authoring evidence source adapter contract is unsupported")

    provider_document = cast(dict[str, Any], evidence_document["provider"])
    if _contains_secret_key(provider_document["generation_configuration"]):
        raise ValueError("authoring evidence provider configuration contains a secret field")
    capture_documents = cast(list[dict[str, Any]], evidence_document["captures"])
    if provider_document["identifier"] == "google_gemini" and any(
        capture_document["effective_model_version"] is None
        for capture_document in capture_documents
    ):
        raise ValueError("Gemini authoring evidence requires effective model versions")

    report_document = cast(dict[str, Any], evidence_document["report"])
    benchmark_document = cast(dict[str, int], evidence_document["benchmark"])
    case_result_documents = cast(list[dict[str, Any]], report_document["case_results"])
    if any(
        len(cast(list[dict[str, Any]], case_result_document["attempts"]))
        > benchmark_document["max_correction_rounds"] + 1
        for case_result_document in case_result_documents
    ):
        raise ValueError("authoring evidence report exceeds its correction-round limit")
    expected_capture_keys = tuple(
        (case_result_document["case_identifier"], attempt_document["correction_round"])
        for case_result_document in case_result_documents
        for attempt_document in cast(list[dict[str, Any]], case_result_document["attempts"])
    )
    actual_capture_keys = tuple(
        (capture_document["case_identifier"], capture_document["correction_round"])
        for capture_document in capture_documents
    )
    if actual_capture_keys != expected_capture_keys:
        raise ValueError("authoring evidence captures do not align with report attempts")
    for capture_document in capture_documents:
        authored_source = cast(str, capture_document["authored_source"])
        if capture_document["source_sha256"] != _sha256(authored_source):
            raise ValueError("authoring evidence capture source digest does not match its source")

    captured_sources: dict[str, list[str]] = {
        benchmark_case.identifier: [] for benchmark_case in reference_corpus.cases
    }
    for capture_document in capture_documents:
        case_identifier = cast(str, capture_document["case_identifier"])
        if case_identifier not in captured_sources:
            raise ValueError("authoring evidence capture case is not in the installed corpus")
        captured_sources[case_identifier].append(cast(str, capture_document["authored_source"]))

    replayed_report = replay_authoring_benchmark(
        cast(str, report_document["benchmark_identifier"]),
        reference_corpus.cases,
        captured_sources,
        profile=flow_profile,
    )
    if replayed_report.to_dict() != report_document:
        raise ValueError("authoring evidence report does not match deterministic replay")

    effective_model_versions = tuple(
        sorted(
            {
                cast(str, capture_document["effective_model_version"])
                for capture_document in capture_documents
                if capture_document["effective_model_version"] is not None
            }
        )
    )
    return AuthoringEvidenceVerification(
        digest=recorded_digest,
        benchmark_identifier=replayed_report.benchmark_identifier,
        repository_revision=repository_revision,
        package_version=recorded_package_version,
        provider_identifier=cast(str, provider_document["identifier"]),
        requested_model=cast(str, provider_document["model"]),
        effective_model_versions=effective_model_versions,
        successful_cases=replayed_report.successful_cases,
        total_cases=replayed_report.total_cases,
        total_attempts=replayed_report.total_attempts,
    )
