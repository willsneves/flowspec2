"""Deterministic, provider-neutral benchmarks for AI-authored flow sources.

The benchmark deliberately does not call a model or estimate model quality.  A
caller injects an author and a source adapter.  Every adapted document is judged
by the same :func:`flowspec2.checker.check_flow` structural, semantic, and
compilation contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from flowspec2.checker import check_flow
from flowspec2.diagnostics import CompilationStatus, FlowDiagnostic
from flowspec2.ir import normalize_flow
from flowspec2.json_codec import StrictJsonError, strict_json_loads
from flowspec2.profiles import FlowProfile, reference_profile

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TOKEN_PROXY_METHOD: Final[str] = "utf8_byte_quartets"
TOKEN_PROXY_BYTES_PER_UNIT: Final[int] = 4


def _require_identifier(identifier: str, contract_name: str) -> None:
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(
            f"{contract_name} must contain only lowercase letters, digits, hyphens, "
            f"and underscores: {identifier!r}"
        )


def _compact_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalized_flow_json(flow_document: object) -> str:
    if not isinstance(flow_document, dict):
        raise ValueError("a benchmark reference flow must be a JSON object")
    return _compact_json(normalize_flow(cast(dict[str, Any], flow_document)))


def _json_pointer(path_segments: Iterable[str | int]) -> str:
    encoded_segments = (
        str(path_segment).replace("~", "~0").replace("/", "~1") for path_segment in path_segments
    )
    return "".join(f"/{encoded_segment}" for encoded_segment in encoded_segments)


def _diagnostic_sort_key(flow_diagnostic: FlowDiagnostic) -> tuple[str, str, str, str, str]:
    return (
        flow_diagnostic.path,
        flow_diagnostic.code,
        flow_diagnostic.severity,
        flow_diagnostic.message,
        _compact_json(flow_diagnostic.to_dict()),
    )


def _deterministic_diagnostics(
    flow_diagnostics: Iterable[FlowDiagnostic],
) -> tuple[FlowDiagnostic, ...]:
    return tuple(sorted(set(flow_diagnostics), key=_diagnostic_sort_key))


@dataclass(frozen=True)
class SourceAdaptation:
    """Immutable source and executable documents emitted by one adapter."""

    format_identifier: str
    authored_document_json: str | None
    executable_flow_json: str | None
    canonical_authored_source: str | None = None
    diagnostics: tuple[FlowDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not self.format_identifier:
            raise ValueError("source adaptation format identifier must be non-empty")
        for field_name, serialized_document in (
            ("authored_document_json", self.authored_document_json),
            ("executable_flow_json", self.executable_flow_json),
        ):
            if serialized_document is None:
                continue
            parsed_document = strict_json_loads(serialized_document)
            if _compact_json(parsed_document) != serialized_document:
                raise ValueError(f"source adaptation {field_name} must be canonical compact JSON")
        if self.executable_flow_json is not None and self.authored_document_json is None:
            raise ValueError("an executable flow requires an authored source document")
        if (self.authored_document_json is None) != (self.canonical_authored_source is None):
            raise ValueError(
                "an authored document and its canonical source must be present together"
            )
        if self.canonical_authored_source is not None and not isinstance(
            self.canonical_authored_source, str
        ):
            raise TypeError("source adaptation canonical authored source must be a string")
        if self.canonical_authored_source == "":
            raise ValueError("source adaptation canonical authored source must be non-empty")
        object.__setattr__(
            self,
            "diagnostics",
            _deterministic_diagnostics(self.diagnostics),
        )

    @classmethod
    def from_documents(
        cls,
        *,
        format_identifier: str,
        authored_document: object,
        executable_flow: object,
        canonical_authored_source: str | None = None,
        diagnostics: tuple[FlowDiagnostic, ...] = (),
    ) -> SourceAdaptation:
        """Create an adaptation with canonical documents and authored syntax."""

        authored_document_json = _compact_json(authored_document)

        return cls(
            format_identifier=format_identifier,
            authored_document_json=authored_document_json,
            executable_flow_json=_compact_json(executable_flow),
            canonical_authored_source=(
                authored_document_json
                if canonical_authored_source is None
                else canonical_authored_source
            ),
            diagnostics=diagnostics,
        )


class AuthoringSourceAdapter(Protocol):
    """Convert an authored source string into the executable flowspec contract."""

    @property
    def format_identifier(self) -> str: ...

    def adapt(self, authored_source: str) -> SourceAdaptation: ...


@dataclass(frozen=True)
class FlowSpec2JsonAdapter:
    """Identity adapter for JSON-authored ``flowspec/2`` documents."""

    format_identifier: str = "flowspec/2"

    def adapt(self, authored_source: str) -> SourceAdaptation:
        try:
            authored_document: object = strict_json_loads(authored_source)
        except (json.JSONDecodeError, StrictJsonError) as decoding_error:
            decoding_message = (
                f"Invalid JSON at line {decoding_error.lineno}, "
                f"column {decoding_error.colno}: {decoding_error.msg}"
                if isinstance(decoding_error, json.JSONDecodeError)
                else f"Invalid JSON: {decoding_error}"
            )
            return SourceAdaptation(
                format_identifier=self.format_identifier,
                authored_document_json=None,
                executable_flow_json=None,
                diagnostics=(
                    FlowDiagnostic(
                        code="AUTHORING_SOURCE_JSON_INVALID",
                        severity="error",
                        path="",
                        message=decoding_message,
                    ),
                ),
            )
        return SourceAdaptation.from_documents(
            format_identifier=self.format_identifier,
            authored_document=authored_document,
            executable_flow=authored_document,
        )


@dataclass(frozen=True)
class ForbiddenConstruct:
    """A JSON Pointer pattern for one intentionally unsupported construct."""

    identifier: str
    pointer_pattern: str
    message: str

    def __post_init__(self) -> None:
        _require_identifier(self.identifier, "forbidden construct identifier")
        if not self.pointer_pattern.startswith("/"):
            raise ValueError("forbidden construct pointer pattern must start with '/'")
        if not self.message:
            raise ValueError("forbidden construct message must be non-empty")


@dataclass(frozen=True)
class RequiredFlowConstruct:
    """A required executable-flow location and optional exact JSON value."""

    identifier: str
    pointer_pattern: str
    expected_json: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.identifier, "required flow construct identifier")
        if not self.pointer_pattern.startswith("/"):
            raise ValueError("required flow construct pointer pattern must start with '/'")
        if self.expected_json is not None:
            expected_value = strict_json_loads(self.expected_json)
            if _compact_json(expected_value) != self.expected_json:
                raise ValueError("required flow construct expected value must be canonical JSON")

    @classmethod
    def expecting(
        cls,
        identifier: str,
        pointer_pattern: str,
        expected_value: object,
    ) -> RequiredFlowConstruct:
        """Create a required construct with an exact JSON-compatible value."""

        return cls(
            identifier=identifier,
            pointer_pattern=pointer_pattern,
            expected_json=_compact_json(expected_value),
        )


def _forbidden_construct(
    identifier: str,
    pointer_pattern: str,
    construct_name: str,
) -> ForbiddenConstruct:
    return ForbiddenConstruct(
        identifier=identifier,
        pointer_pattern=pointer_pattern,
        message=(
            f"The authored source uses unsupported {construct_name}; "
            "keep the flow on the closed ordered conversational rail."
        ),
    )


DEFAULT_FORBIDDEN_CONSTRUCTS: Final[tuple[ForbiddenConstruct, ...]] = (
    _forbidden_construct("top-code", "/code", "arbitrary code"),
    _forbidden_construct("top-cron", "/cron", "cron scheduling"),
    _forbidden_construct("top-expressions", "/expressions", "arbitrary expressions"),
    _forbidden_construct("top-loop", "/loop", "loops"),
    _forbidden_construct("top-loops", "/loops", "loops"),
    _forbidden_construct("top-parallel", "/parallel", "parallel execution"),
    _forbidden_construct("top-schedule", "/schedule", "scheduling"),
    _forbidden_construct("top-script", "/script", "arbitrary scripts"),
    _forbidden_construct("top-transitions", "/transitions", "manual transition tables"),
    _forbidden_construct("path-code", "/path/*/code", "arbitrary step code"),
    _forbidden_construct("path-expression", "/path/*/expression", "arbitrary expressions"),
    _forbidden_construct("path-for-each", "/path/*/for_each", "iteration"),
    _forbidden_construct("path-goto", "/path/*/goto", "manual path transitions"),
    _forbidden_construct("path-loop", "/path/*/loop", "loops"),
    _forbidden_construct("path-next", "/path/*/next", "manual path transitions"),
    _forbidden_construct("path-parallel", "/path/*/parallel", "parallel execution"),
    _forbidden_construct("path-script", "/path/*/script", "arbitrary scripts"),
    _forbidden_construct("path-while", "/path/*/while", "loops"),
)


@dataclass(frozen=True)
class AuthoringBenchmarkCase:
    """One provider-neutral authoring prompt and its evaluation constraints."""

    identifier: str
    prompt: str
    expected_flow_json: str
    feature_tags: tuple[str, ...] = ()
    required_constructs: tuple[RequiredFlowConstruct, ...] = ()
    forbidden_constructs: tuple[ForbiddenConstruct, ...] = DEFAULT_FORBIDDEN_CONSTRUCTS

    def __post_init__(self) -> None:
        _require_identifier(self.identifier, "benchmark case identifier")
        if not self.prompt.strip():
            raise ValueError(f"benchmark case {self.identifier!r} prompt must be non-empty")
        expected_flow = strict_json_loads(self.expected_flow_json)
        feature_tags = tuple(self.feature_tags)
        required_constructs = tuple(self.required_constructs)
        forbidden_constructs = tuple(self.forbidden_constructs)
        if len(set(feature_tags)) != len(feature_tags):
            raise ValueError(f"benchmark case {self.identifier!r} feature tags must be unique")
        if len({rule.identifier for rule in forbidden_constructs}) != len(forbidden_constructs):
            raise ValueError(
                f"benchmark case {self.identifier!r} forbidden construct identifiers must be unique"
            )
        if len({rule.identifier for rule in required_constructs}) != len(required_constructs):
            raise ValueError(
                f"benchmark case {self.identifier!r} required construct identifiers must be unique"
            )
        object.__setattr__(self, "prompt", self.prompt.strip())
        object.__setattr__(self, "expected_flow_json", _normalized_flow_json(expected_flow))
        object.__setattr__(self, "feature_tags", tuple(sorted(feature_tags)))
        object.__setattr__(
            self,
            "required_constructs",
            tuple(sorted(required_constructs, key=lambda rule: rule.identifier)),
        )
        object.__setattr__(
            self,
            "forbidden_constructs",
            tuple(sorted(forbidden_constructs, key=lambda rule: rule.identifier)),
        )

    @classmethod
    def expecting_flow(
        cls,
        *,
        identifier: str,
        prompt: str,
        expected_flow: object,
        feature_tags: tuple[str, ...] = (),
        required_constructs: tuple[RequiredFlowConstruct, ...] = (),
        forbidden_constructs: tuple[ForbiddenConstruct, ...] = DEFAULT_FORBIDDEN_CONSTRUCTS,
    ) -> AuthoringBenchmarkCase:
        """Create a closed case from its complete reference executable flow."""

        return cls(
            identifier=identifier,
            prompt=prompt,
            expected_flow_json=_compact_json(expected_flow),
            feature_tags=feature_tags,
            required_constructs=required_constructs,
            forbidden_constructs=forbidden_constructs,
        )


@dataclass(frozen=True)
class AuthoringBenchmarkLimits:
    """Bound the correction interaction for every benchmark case."""

    max_correction_rounds: int = 2

    def __post_init__(self) -> None:
        if self.max_correction_rounds < 0:
            raise ValueError("max_correction_rounds must not be negative")


@dataclass(frozen=True)
class AuthoringRequest:
    """One initial or diagnostic-guided request delivered to an injected author."""

    benchmark_case: AuthoringBenchmarkCase
    format_identifier: str
    profile_identifier: str
    profile_contract_json: str
    correction_round: int
    previous_source: str | None
    previous_diagnostics: tuple[FlowDiagnostic, ...]

    def __post_init__(self) -> None:
        if not self.format_identifier:
            raise ValueError("authoring request format identifier must be non-empty")
        if not self.profile_identifier:
            raise ValueError("authoring request profile identifier must be non-empty")
        profile_contract = strict_json_loads(self.profile_contract_json)
        if not isinstance(profile_contract, dict):
            raise ValueError("authoring request profile contract must be a JSON object")
        if _compact_json(profile_contract) != self.profile_contract_json:
            raise ValueError("authoring request profile contract must be canonical JSON")
        if profile_contract.get("identifier") != self.profile_identifier:
            raise ValueError("authoring request profile contract identifier does not match")
        if self.correction_round < 0:
            raise ValueError("authoring request correction round must not be negative")
        if self.correction_round == 0 and (
            self.previous_source is not None or self.previous_diagnostics
        ):
            raise ValueError("the initial authoring request cannot contain a previous attempt")
        if self.correction_round > 0 and self.previous_source is None:
            raise ValueError("a correction request requires the previous source")
        object.__setattr__(
            self,
            "previous_diagnostics",
            _deterministic_diagnostics(self.previous_diagnostics),
        )


AuthoringAuthor = Callable[[AuthoringRequest], str]


@dataclass(frozen=True)
class AuthoringAttempt:
    """Machine-readable outcome for one emitted source document."""

    correction_round: int
    source_sha256: str
    raw_source_bytes: int
    canonical_source_bytes: int | None
    token_proxy_units: int | None
    compilation: CompilationStatus
    diagnostics: tuple[FlowDiagnostic, ...]
    succeeded: bool

    def __post_init__(self) -> None:
        if self.correction_round < 0:
            raise ValueError("authoring attempt correction round must not be negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("authoring attempt source digest must be lowercase SHA-256")
        if self.raw_source_bytes < 0:
            raise ValueError("authoring attempt raw source size must not be negative")
        if (self.canonical_source_bytes is None) != (self.token_proxy_units is None):
            raise ValueError("canonical source size and token proxy must be present together")
        if self.canonical_source_bytes is not None:
            expected_proxy = (
                self.canonical_source_bytes + TOKEN_PROXY_BYTES_PER_UNIT - 1
            ) // TOKEN_PROXY_BYTES_PER_UNIT
            if self.canonical_source_bytes < 0 or self.token_proxy_units != expected_proxy:
                raise ValueError(
                    "authoring attempt token proxy does not match canonical source size"
                )
        deterministic_diagnostics = _deterministic_diagnostics(self.diagnostics)
        object.__setattr__(self, "diagnostics", deterministic_diagnostics)
        if self.succeeded and (
            self.compilation != "succeeded"
            or any(
                flow_diagnostic.severity == "error" for flow_diagnostic in deterministic_diagnostics
            )
        ):
            raise ValueError("a successful authoring attempt must compile without errors")

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""

        return {
            "correction_round": self.correction_round,
            "source_sha256": self.source_sha256,
            "raw_source_bytes": self.raw_source_bytes,
            "canonical_source_bytes": self.canonical_source_bytes,
            "token_proxy_units": self.token_proxy_units,
            "compilation": self.compilation,
            "succeeded": self.succeeded,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True)
class AuthoringBenchmarkResult:
    """All bounded attempts for one benchmark case."""

    case_identifier: str
    feature_tags: tuple[str, ...]
    format_identifier: str
    attempts: tuple[AuthoringAttempt, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.case_identifier, "benchmark result case identifier")
        if not self.format_identifier:
            raise ValueError("benchmark result format identifier must be non-empty")
        feature_tags = tuple(self.feature_tags)
        if len(set(feature_tags)) != len(feature_tags):
            raise ValueError(
                f"benchmark result {self.case_identifier!r} feature tags must be unique"
            )
        authoring_attempts = tuple(self.attempts)
        object.__setattr__(self, "feature_tags", tuple(sorted(feature_tags)))
        object.__setattr__(self, "attempts", authoring_attempts)
        if not authoring_attempts:
            raise ValueError(f"benchmark result {self.case_identifier!r} requires an attempt")
        expected_rounds = tuple(range(len(authoring_attempts)))
        actual_rounds = tuple(attempt.correction_round for attempt in authoring_attempts)
        if actual_rounds != expected_rounds:
            raise ValueError(
                f"benchmark result {self.case_identifier!r} attempts must use contiguous rounds"
            )
        if any(attempt.succeeded for attempt in authoring_attempts[:-1]):
            raise ValueError(
                f"benchmark result {self.case_identifier!r} cannot continue after success"
            )

    @property
    def succeeded(self) -> bool:
        return self.attempts[-1].succeeded

    @property
    def correction_rounds(self) -> int:
        return len(self.attempts) - 1

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""

        return {
            "case_identifier": self.case_identifier,
            "feature_tags": list(self.feature_tags),
            "format_identifier": self.format_identifier,
            "succeeded": self.succeeded,
            "correction_rounds": self.correction_rounds,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class AuthoringBenchmarkReport:
    """Deterministic aggregate suitable for cross-adapter comparison."""

    benchmark_identifier: str
    format_identifier: str
    profile_identifier: str
    case_results: tuple[AuthoringBenchmarkResult, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.benchmark_identifier, "benchmark identifier")
        if not self.format_identifier:
            raise ValueError("benchmark report format identifier must be non-empty")
        if not self.profile_identifier:
            raise ValueError("benchmark report profile identifier must be non-empty")
        case_results = tuple(self.case_results)
        object.__setattr__(self, "case_results", case_results)
        if not case_results:
            raise ValueError("benchmark report requires at least one case")
        case_identifiers = tuple(case_result.case_identifier for case_result in case_results)
        if case_identifiers != tuple(sorted(case_identifiers)):
            raise ValueError("benchmark report cases must be sorted by identifier")
        if len(set(case_identifiers)) != len(case_identifiers):
            raise ValueError("benchmark report case identifiers must be unique")
        if any(
            case_result.format_identifier != self.format_identifier for case_result in case_results
        ):
            raise ValueError("benchmark report case formats must match the report format")

    @property
    def successful_cases(self) -> int:
        return sum(case_result.succeeded for case_result in self.case_results)

    @property
    def total_cases(self) -> int:
        return len(self.case_results)

    @property
    def total_attempts(self) -> int:
        return sum(len(case_result.attempts) for case_result in self.case_results)

    @property
    def total_correction_rounds(self) -> int:
        return sum(case_result.correction_rounds for case_result in self.case_results)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""

        return {
            "benchmark_identifier": self.benchmark_identifier,
            "format_identifier": self.format_identifier,
            "profile_identifier": self.profile_identifier,
            "successful_cases": self.successful_cases,
            "total_cases": self.total_cases,
            "total_attempts": self.total_attempts,
            "total_correction_rounds": self.total_correction_rounds,
            "token_proxy": {
                "method": TOKEN_PROXY_METHOD,
                "bytes_per_unit": TOKEN_PROXY_BYTES_PER_UNIT,
            },
            "case_results": [case_result.to_dict() for case_result in self.case_results],
        }

    def to_json(self) -> str:
        """Serialize the report as canonical compact JSON."""

        return _compact_json(self.to_dict())


def _document_locations(
    json_document: object,
    path_segments: tuple[str | int, ...] = (),
) -> Iterable[tuple[tuple[str | int, ...], object]]:
    if isinstance(json_document, Mapping):
        for property_name in sorted(json_document, key=str):
            property_path = (*path_segments, str(property_name))
            property_value = json_document[property_name]
            yield property_path, property_value
            yield from _document_locations(property_value, property_path)
    elif isinstance(json_document, list):
        for element_index, element_value in enumerate(json_document):
            element_path = (*path_segments, element_index)
            yield element_path, element_value
            yield from _document_locations(element_value, element_path)


def _pattern_segments(pointer_pattern: str) -> tuple[str, ...]:
    return tuple(
        encoded_segment.replace("~1", "/").replace("~0", "~")
        for encoded_segment in pointer_pattern[1:].split("/")
    )


def _matches_pointer_pattern(
    path_segments: tuple[str | int, ...],
    pointer_pattern: str,
) -> bool:
    expected_segments = _pattern_segments(pointer_pattern)
    return len(path_segments) == len(expected_segments) and all(
        expected_segment == "*" or expected_segment == str(path_segment)
        for path_segment, expected_segment in zip(path_segments, expected_segments, strict=True)
    )


def _forbidden_diagnostics(
    authored_document: object,
    forbidden_constructs: tuple[ForbiddenConstruct, ...],
) -> tuple[FlowDiagnostic, ...]:
    flow_diagnostics = (
        FlowDiagnostic(
            code=f"AUTHORING_FORBIDDEN_{forbidden_construct.identifier.upper().replace('-', '_')}",
            severity="error",
            path=_json_pointer(path_segments),
            message=forbidden_construct.message,
        )
        for path_segments, _property_value in _document_locations(authored_document)
        for forbidden_construct in forbidden_constructs
        if _matches_pointer_pattern(path_segments, forbidden_construct.pointer_pattern)
    )
    return _deterministic_diagnostics(flow_diagnostics)


def _matching_locations(
    json_document: object,
    pointer_pattern: str,
) -> tuple[tuple[tuple[str | int, ...], object], ...]:
    return tuple(
        (path_segments, property_value)
        for path_segments, property_value in _document_locations(json_document)
        if _matches_pointer_pattern(path_segments, pointer_pattern)
    )


def _missing_construct_path(pointer_pattern: str) -> str:
    fixed_segments = []
    for pattern_segment in _pattern_segments(pointer_pattern):
        if pattern_segment == "*":
            break
        fixed_segments.append(pattern_segment)
    return _json_pointer(fixed_segments)


def _required_construct_diagnostics(
    executable_flow: object,
    required_constructs: tuple[RequiredFlowConstruct, ...],
) -> tuple[FlowDiagnostic, ...]:
    flow_diagnostics: list[FlowDiagnostic] = []
    for required_construct in required_constructs:
        matching_locations = _matching_locations(
            executable_flow,
            required_construct.pointer_pattern,
        )
        diagnostic_code = required_construct.identifier.upper().replace("-", "_")
        if not matching_locations:
            expected_contract = (
                ""
                if required_construct.expected_json is None
                else f" with the exact JSON value {required_construct.expected_json}"
            )
            flow_diagnostics.append(
                FlowDiagnostic(
                    code=f"AUTHORING_REQUIRED_{diagnostic_code}_MISSING",
                    severity="error",
                    path=_missing_construct_path(required_construct.pointer_pattern),
                    message=(
                        "The executable flow is missing required construct "
                        f"{required_construct.pointer_pattern!r}{expected_contract}."
                    ),
                    suggested_fix=(
                        f"Add a location matching {required_construct.pointer_pattern!r}"
                        f"{expected_contract}."
                    ),
                )
            )
            continue
        if required_construct.expected_json is None or any(
            _compact_json(property_value) == required_construct.expected_json
            for _path_segments, property_value in matching_locations
        ):
            continue
        first_path, first_property_value = matching_locations[0]
        actual_json_value = _compact_json(first_property_value)
        flow_diagnostics.append(
            FlowDiagnostic(
                code=f"AUTHORING_REQUIRED_{diagnostic_code}_MISMATCH",
                severity="error",
                path=_json_pointer(first_path),
                message=(
                    f"Required construct {required_construct.pointer_pattern!r} matched "
                    f"{actual_json_value}, but must have the exact JSON value "
                    f"{required_construct.expected_json}."
                ),
                suggested_fix=(
                    f"Set a location matching {required_construct.pointer_pattern!r} to "
                    f"{required_construct.expected_json}."
                ),
            )
        )
    return _deterministic_diagnostics(flow_diagnostics)


def _first_json_difference(
    expected_document: object,
    observed_document: object,
    path_segments: tuple[str | int, ...] = (),
) -> tuple[tuple[str | int, ...], str] | None:
    if isinstance(expected_document, Mapping) and isinstance(observed_document, Mapping):
        expected_mapping = cast(Mapping[str, object], expected_document)
        observed_mapping = cast(Mapping[str, object], observed_document)
        for property_name in sorted(
            set(expected_mapping) | set(observed_mapping),
        ):
            property_path = (*path_segments, property_name)
            if property_name not in expected_mapping:
                return property_path, "the executable flow contains an unexpected property"
            if property_name not in observed_mapping:
                return property_path, "the executable flow omits a required property"
            if nested_difference := _first_json_difference(
                expected_mapping[property_name],
                observed_mapping[property_name],
                property_path,
            ):
                return nested_difference
        return None
    if isinstance(expected_document, list) and isinstance(observed_document, list):
        for element_index, (expected_element, observed_element) in enumerate(
            zip(expected_document, observed_document, strict=False)
        ):
            if nested_difference := _first_json_difference(
                expected_element,
                observed_element,
                (*path_segments, element_index),
            ):
                return nested_difference
        if len(expected_document) < len(observed_document):
            return (
                (*path_segments, len(expected_document)),
                "the executable flow contains an unexpected list element",
            )
        if len(expected_document) > len(observed_document):
            return (
                (*path_segments, len(observed_document)),
                "the executable flow omits a required list element",
            )
        return None
    if expected_document != observed_document:
        return (
            path_segments,
            (
                f"expected {_compact_json(expected_document)}, but observed "
                f"{_compact_json(observed_document)}"
            ),
        )
    return None


def _closed_flow_diagnostics(
    executable_flow: object,
    expected_flow_json: str,
) -> tuple[FlowDiagnostic, ...]:
    observed_flow_json = _normalized_flow_json(executable_flow)
    if observed_flow_json == expected_flow_json:
        return ()
    expected_flow = strict_json_loads(expected_flow_json)
    observed_flow = strict_json_loads(observed_flow_json)
    first_difference = _first_json_difference(expected_flow, observed_flow)
    if first_difference is None:
        raise AssertionError("different canonical flows must have a JSON difference")
    difference_path, difference_message = first_difference
    return (
        FlowDiagnostic(
            code="AUTHORING_CLOSED_FLOW_MISMATCH",
            severity="error",
            path=_json_pointer(difference_path),
            message=(
                "The executable flow does not match the benchmark's complete closed "
                f"semantic contract: {difference_message}."
            ),
            suggested_fix=(
                "Remove unrelated behavior and restore the complete reference flow semantics."
            ),
        ),
    )


def _evaluate_source(
    authored_source: str,
    source_adapter: AuthoringSourceAdapter,
    expected_flow_json: str,
    required_constructs: tuple[RequiredFlowConstruct, ...],
    forbidden_constructs: tuple[ForbiddenConstruct, ...],
    correction_round: int,
    flow_profile: FlowProfile,
) -> AuthoringAttempt:
    encoded_source = authored_source.encode("utf-8")
    try:
        source_adaptation = source_adapter.adapt(authored_source)
        if source_adaptation.format_identifier != source_adapter.format_identifier:
            raise ValueError(
                "source adapter returned a format identifier that differs from its declaration"
            )
        if source_adaptation.canonical_authored_source is not None:
            canonical_adaptation = source_adapter.adapt(source_adaptation.canonical_authored_source)
            source_projection = (
                source_adaptation.format_identifier,
                source_adaptation.authored_document_json,
                source_adaptation.executable_flow_json,
                source_adaptation.canonical_authored_source,
            )
            canonical_projection = (
                canonical_adaptation.format_identifier,
                canonical_adaptation.authored_document_json,
                canonical_adaptation.executable_flow_json,
                canonical_adaptation.canonical_authored_source,
            )
            if canonical_projection != source_projection:
                raise ValueError(
                    "source adapter canonical syntax must round-trip to the same authored "
                    "and executable projections"
                )
    except Exception as adaptation_error:
        adaptation_details = str(adaptation_error).strip() or "no details"
        return AuthoringAttempt(
            correction_round=correction_round,
            source_sha256=hashlib.sha256(encoded_source).hexdigest(),
            raw_source_bytes=len(encoded_source),
            canonical_source_bytes=None,
            token_proxy_units=None,
            compilation="skipped",
            diagnostics=(
                FlowDiagnostic(
                    code="AUTHORING_SOURCE_ADAPTATION_FAILED",
                    severity="error",
                    path="",
                    message=(
                        f"The {source_adapter.format_identifier!r} source adapter failed with "
                        f"{type(adaptation_error).__name__}: {adaptation_details}."
                    ),
                ),
            ),
            succeeded=False,
        )

    flow_diagnostics = list(source_adaptation.diagnostics)
    canonical_source_bytes: int | None = None
    token_proxy_units: int | None = None
    if source_adaptation.authored_document_json is not None:
        authored_document = strict_json_loads(source_adaptation.authored_document_json)
        canonical_source_bytes = len(
            cast(str, source_adaptation.canonical_authored_source).encode("utf-8")
        )
        token_proxy_units = (
            canonical_source_bytes + TOKEN_PROXY_BYTES_PER_UNIT - 1
        ) // TOKEN_PROXY_BYTES_PER_UNIT
        flow_diagnostics.extend(_forbidden_diagnostics(authored_document, forbidden_constructs))

    compilation: CompilationStatus = "skipped"
    executable_flow_available = source_adaptation.executable_flow_json is not None
    if executable_flow_available:
        executable_flow = strict_json_loads(cast(str, source_adaptation.executable_flow_json))
        required_diagnostics = _required_construct_diagnostics(executable_flow, required_constructs)
        flow_diagnostics.extend(required_diagnostics)
        flow_check_report = check_flow(executable_flow, profile=flow_profile)
        compilation = flow_check_report.compilation
        flow_diagnostics.extend(flow_check_report.diagnostics)
        if compilation == "succeeded" and not required_diagnostics:
            flow_diagnostics.extend(_closed_flow_diagnostics(executable_flow, expected_flow_json))

    deterministic_diagnostics = _deterministic_diagnostics(flow_diagnostics)
    succeeded = (
        executable_flow_available
        and compilation == "succeeded"
        and not any(
            flow_diagnostic.severity == "error" for flow_diagnostic in deterministic_diagnostics
        )
    )
    return AuthoringAttempt(
        correction_round=correction_round,
        source_sha256=hashlib.sha256(encoded_source).hexdigest(),
        raw_source_bytes=len(encoded_source),
        canonical_source_bytes=canonical_source_bytes,
        token_proxy_units=token_proxy_units,
        compilation=compilation,
        diagnostics=deterministic_diagnostics,
        succeeded=succeeded,
    )


def _run_benchmark_case(
    benchmark_case: AuthoringBenchmarkCase,
    author: AuthoringAuthor,
    source_adapter: AuthoringSourceAdapter,
    benchmark_limits: AuthoringBenchmarkLimits,
    flow_profile: FlowProfile,
) -> AuthoringBenchmarkResult:
    authoring_attempts: list[AuthoringAttempt] = []
    previous_source: str | None = None
    previous_diagnostics: tuple[FlowDiagnostic, ...] = ()
    for correction_round in range(benchmark_limits.max_correction_rounds + 1):
        authoring_request = AuthoringRequest(
            benchmark_case=benchmark_case,
            format_identifier=source_adapter.format_identifier,
            profile_identifier=flow_profile.identifier,
            profile_contract_json=flow_profile.canonical_json(),
            correction_round=correction_round,
            previous_source=previous_source,
            previous_diagnostics=previous_diagnostics,
        )
        authored_source = author(authoring_request)
        if not isinstance(authored_source, str):
            raise TypeError("the injected author must return a source string")
        authoring_attempt = _evaluate_source(
            authored_source,
            source_adapter,
            benchmark_case.expected_flow_json,
            benchmark_case.required_constructs,
            benchmark_case.forbidden_constructs,
            correction_round,
            flow_profile,
        )
        authoring_attempts.append(authoring_attempt)
        if authoring_attempt.succeeded:
            break
        previous_source = authored_source
        previous_diagnostics = authoring_attempt.diagnostics

    return AuthoringBenchmarkResult(
        case_identifier=benchmark_case.identifier,
        feature_tags=benchmark_case.feature_tags,
        format_identifier=source_adapter.format_identifier,
        attempts=tuple(authoring_attempts),
    )


def run_authoring_benchmark(
    benchmark_identifier: str,
    benchmark_cases: Sequence[AuthoringBenchmarkCase],
    author: AuthoringAuthor,
    *,
    source_adapter: AuthoringSourceAdapter | None = None,
    limits: AuthoringBenchmarkLimits | None = None,
    profile: FlowProfile | None = None,
) -> AuthoringBenchmarkReport:
    """Run all cases through the injected author, adapter, and exact checker."""

    _require_identifier(benchmark_identifier, "benchmark identifier")
    resolved_source_adapter = source_adapter or FlowSpec2JsonAdapter()
    resolved_limits = limits or AuthoringBenchmarkLimits()
    resolved_profile = profile or reference_profile()
    sorted_cases = tuple(
        sorted(benchmark_cases, key=lambda benchmark_case: benchmark_case.identifier)
    )
    case_identifiers = tuple(benchmark_case.identifier for benchmark_case in sorted_cases)
    if len(set(case_identifiers)) != len(case_identifiers):
        raise ValueError("benchmark case identifiers must be unique")

    return AuthoringBenchmarkReport(
        benchmark_identifier=benchmark_identifier,
        format_identifier=resolved_source_adapter.format_identifier,
        profile_identifier=resolved_profile.identifier,
        case_results=tuple(
            _run_benchmark_case(
                benchmark_case,
                author,
                resolved_source_adapter,
                resolved_limits,
                resolved_profile,
            )
            for benchmark_case in sorted_cases
        ),
    )
