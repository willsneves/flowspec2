"""Versioned, packaged benchmark cases for AI flow authoring."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, Final, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from flowspec2.json_codec import strict_json_loads

from .benchmark import AuthoringBenchmarkCase, RequiredFlowConstruct

AUTHORING_CASE_FORMAT: Final[str] = "flowspec2/authoring-case@1"
AUTHORING_CORPUS_FORMAT: Final[str] = "flowspec2/authoring-corpus@1"
REFERENCE_AUTHORING_CORPUS_ID: Final[str] = "flowspec2_reference"

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CASE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*\.case\.json$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["format", "identifier", "cases"],
    "properties": {
        "format": {"const": AUTHORING_CORPUS_FORMAT},
        "identifier": {"type": "string", "pattern": _IDENTIFIER_PATTERN.pattern},
        "cases": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["identifier", "path", "sha256"],
                "properties": {
                    "identifier": {
                        "type": "string",
                        "pattern": _IDENTIFIER_PATTERN.pattern,
                    },
                    "path": {"type": "string", "pattern": _CASE_PATH_PATTERN.pattern},
                    "sha256": {"type": "string", "pattern": _DIGEST_PATTERN.pattern},
                },
            },
        },
    },
}
_CASE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "format",
        "identifier",
        "prompt",
        "feature_tags",
        "required_constructs",
        "source",
    ],
    "properties": {
        "format": {"const": AUTHORING_CASE_FORMAT},
        "identifier": {"type": "string", "pattern": _IDENTIFIER_PATTERN.pattern},
        "prompt": {"type": "string", "minLength": 1},
        "feature_tags": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _IDENTIFIER_PATTERN.pattern},
        },
        "required_constructs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["identifier", "pointer_pattern", "expected"],
                "properties": {
                    "identifier": {
                        "type": "string",
                        "pattern": _IDENTIFIER_PATTERN.pattern,
                    },
                    "pointer_pattern": {"type": "string", "pattern": r"^/.+"},
                    "expected": {},
                },
            },
        },
        "source": {"type": "object"},
    },
}
_manifest_validator: Final[Draft202012Validator] = Draft202012Validator(_MANIFEST_SCHEMA)
_case_validator: Final[Draft202012Validator] = Draft202012Validator(_CASE_SCHEMA)
_reference_corpus_cache: AuthoringCorpus | None = None


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validation_errors(
    json_document: object,
    validator: Draft202012Validator,
) -> tuple[str, ...]:
    validation_errors = sorted(
        validator.iter_errors(cast(Any, json_document)),
        key=lambda validation_error: (
            tuple(str(path_segment) for path_segment in validation_error.absolute_path),
            validation_error.message,
        ),
    )
    return tuple(
        f"{'.'.join(str(path_segment) for path_segment in validation_error.absolute_path) or '<root>'}: "
        f"{validation_error.message}"
        for validation_error in validation_errors
    )


def _load_case_document(case_resource: Traversable) -> dict[str, Any]:
    decoded_case = strict_json_loads(case_resource.read_text(encoding="utf-8"))
    if validation_errors := _validation_errors(decoded_case, _case_validator):
        raise ValueError(
            f"invalid packaged authoring case {case_resource.name}: " + "; ".join(validation_errors)
        )
    return cast(dict[str, Any], decoded_case)


def _load_manifest(corpus_directory: Traversable) -> dict[str, Any]:
    manifest_resource = corpus_directory.joinpath("manifest.json")
    decoded_manifest = strict_json_loads(manifest_resource.read_text(encoding="utf-8"))
    if validation_errors := _validation_errors(decoded_manifest, _manifest_validator):
        raise ValueError(
            "invalid packaged authoring corpus manifest: " + "; ".join(validation_errors)
        )
    return cast(dict[str, Any], decoded_manifest)


def _benchmark_case(case_document: dict[str, Any]) -> AuthoringBenchmarkCase:
    required_constructs = tuple(
        RequiredFlowConstruct.expecting(
            cast(str, required_construct["identifier"]),
            cast(str, required_construct["pointer_pattern"]),
            required_construct["expected"],
        )
        for required_construct in cast(
            list[dict[str, Any]],
            case_document["required_constructs"],
        )
    )
    return AuthoringBenchmarkCase.expecting_flow(
        identifier=cast(str, case_document["identifier"]),
        prompt=cast(str, case_document["prompt"]),
        expected_flow=case_document["source"],
        feature_tags=tuple(cast(list[str], case_document["feature_tags"])),
        required_constructs=required_constructs,
    )


@dataclass(frozen=True)
class AuthoringCorpus:
    """Immutable cases and content identity for one benchmark corpus."""

    format_identifier: str
    case_format_identifier: str
    identifier: str
    digest: str
    cases: tuple[AuthoringBenchmarkCase, ...]

    def __post_init__(self) -> None:
        if self.format_identifier != AUTHORING_CORPUS_FORMAT:
            raise ValueError("unknown authoring corpus format")
        if self.case_format_identifier != AUTHORING_CASE_FORMAT:
            raise ValueError("unknown authoring case format")
        if not _IDENTIFIER_PATTERN.fullmatch(self.identifier):
            raise ValueError("authoring corpus identifier is invalid")
        if not _DIGEST_PATTERN.fullmatch(self.digest):
            raise ValueError("authoring corpus digest must be lowercase SHA-256")
        case_identifiers = tuple(benchmark_case.identifier for benchmark_case in self.cases)
        if not case_identifiers:
            raise ValueError("authoring corpus requires at least one case")
        if case_identifiers != tuple(sorted(case_identifiers)):
            raise ValueError("authoring corpus cases must be sorted by identifier")
        if len(set(case_identifiers)) != len(case_identifiers):
            raise ValueError("authoring corpus case identifiers must be unique")

    def metadata(self) -> dict[str, object]:
        """Return deterministic corpus provenance without reference answers."""

        return {
            "format": self.format_identifier,
            "case_format": self.case_format_identifier,
            "identifier": self.identifier,
            "digest": self.digest,
            "case_identifiers": [benchmark_case.identifier for benchmark_case in self.cases],
        }


def _load_authoring_corpus(
    corpus_directory: Traversable,
    *,
    expected_identifier: str,
) -> AuthoringCorpus:
    manifest_document = _load_manifest(corpus_directory)
    if manifest_document["identifier"] != expected_identifier:
        raise ValueError("authoring corpus manifest identifier is not the expected contract")
    manifest_cases = cast(list[dict[str, Any]], manifest_document["cases"])
    manifest_identifiers = tuple(
        cast(str, case_contract["identifier"]) for case_contract in manifest_cases
    )
    manifest_paths = tuple(cast(str, case_contract["path"]) for case_contract in manifest_cases)
    if manifest_identifiers != tuple(sorted(manifest_identifiers)):
        raise ValueError("authoring corpus manifest cases must be sorted by identifier")
    if len(set(manifest_identifiers)) != len(manifest_identifiers):
        raise ValueError("authoring corpus manifest case identifiers must be unique")
    if len(set(manifest_paths)) != len(manifest_paths):
        raise ValueError("authoring corpus manifest case paths must be unique")
    packaged_case_paths = {
        child_resource.name
        for child_resource in corpus_directory.iterdir()
        if child_resource.is_file() and child_resource.name.endswith(".case.json")
    }
    if packaged_case_paths != set(manifest_paths):
        missing_paths = sorted(set(manifest_paths) - packaged_case_paths)
        unlisted_paths = sorted(packaged_case_paths - set(manifest_paths))
        raise ValueError(
            "authoring corpus manifest membership mismatch: "
            f"missing={missing_paths}, unlisted={unlisted_paths}"
        )

    case_documents: list[dict[str, Any]] = []
    for case_contract in manifest_cases:
        case_path = cast(str, case_contract["path"])
        case_resource = corpus_directory.joinpath(case_path)
        serialized_case = case_resource.read_bytes()
        observed_digest = hashlib.sha256(serialized_case).hexdigest()
        expected_digest = cast(str, case_contract["sha256"])
        if observed_digest != expected_digest:
            raise ValueError(f"packaged authoring case failed integrity verification: {case_path}")
        case_document = _load_case_document(case_resource)
        if case_document["identifier"] != case_contract["identifier"]:
            raise ValueError(
                f"packaged authoring case identifier disagrees with manifest: {case_path}"
            )
        case_documents.append(case_document)

    contract_document = {
        "format": AUTHORING_CORPUS_FORMAT,
        "case_format": AUTHORING_CASE_FORMAT,
        "identifier": manifest_document["identifier"],
        "cases": case_documents,
    }
    corpus_digest = hashlib.sha256(_canonical_json(contract_document).encode("utf-8")).hexdigest()
    try:
        benchmark_cases = tuple(
            sorted(
                (_benchmark_case(case_document) for case_document in case_documents),
                key=lambda benchmark_case: benchmark_case.identifier,
            )
        )
    except (ValidationError, ValueError) as case_contract_error:
        raise ValueError(
            "authoring corpus contains an invalid reference flow"
        ) from case_contract_error
    return AuthoringCorpus(
        format_identifier=AUTHORING_CORPUS_FORMAT,
        case_format_identifier=AUTHORING_CASE_FORMAT,
        identifier=cast(str, manifest_document["identifier"]),
        digest=corpus_digest,
        cases=benchmark_cases,
    )


def load_reference_authoring_corpus() -> AuthoringCorpus:
    """Load and validate the immutable corpus distributed with flowspec2."""

    global _reference_corpus_cache
    if _reference_corpus_cache is not None:
        return _reference_corpus_cache

    corpus_directory = files("flowspec2.authoring").joinpath("corpus")
    _reference_corpus_cache = _load_authoring_corpus(
        corpus_directory,
        expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
    )
    return _reference_corpus_cache
