"""Executable conformance-kit contracts for FlowSpec sources and traces.

The kit consumes a closed machine-readable corpus.  Corpus cases may reference
an existing JSON fixture by a safe local path and JSON Pointer, then apply
deterministic set mutations.  The runner records the complete checker report,
canonical IR digests, and caller-visible runtime traces without timestamps or
private implementation state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, cast

from jsonschema import Draft202012Validator

from flowspec2.checker import check_flow
from flowspec2.diagnostics import CompilationStatus, DiagnosticSeverity, FlowCheckReport
from flowspec2.ir import FlowIR, build_flow_ir
from flowspec2.json_codec import strict_json_loads, validate_json_value
from flowspec2.models import ServiceState
from flowspec2.observability import SNOWFLAKE_EPOCH_MILLISECONDS, SnowflakeIdGenerator
from flowspec2.profiles import FlowProfile, reference_profile
from flowspec2.runtime import FlowRuntime

CTK_CORPUS_FORMAT: Final[str] = "flowspec2/ctk-corpus"
CTK_REPORT_FORMAT: Final[str] = "flowspec2/ctk-report"
CTK_VERSION: Final[str] = "1"

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

_CORPUS_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["format", "version", "cases"],
    "properties": {
        "format": {"const": CTK_CORPUS_FORMAT},
        "version": {"const": CTK_VERSION},
        "cases": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["identifier", "source", "expected"],
                "properties": {
                    "identifier": {"type": "string", "pattern": _IDENTIFIER_PATTERN.pattern},
                    "source": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "pointer"],
                        "properties": {
                            "path": {"type": "string", "minLength": 1},
                            "pointer": {"type": "string", "pattern": r"^(?:|/.*)$"},
                            "mutations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["path", "replacement"],
                                    "properties": {
                                        "path": {"type": "string", "pattern": r"^/.+"},
                                        "replacement": {},
                                    },
                                },
                            },
                        },
                    },
                    "expected": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["compilation", "diagnostics"],
                        "properties": {
                            "compilation": {
                                "enum": ["not_requested", "skipped", "succeeded", "failed"]
                            },
                            "diagnostics": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["code", "severity", "path"],
                                    "properties": {
                                        "code": {"type": "string", "minLength": 1},
                                        "severity": {"enum": ["warning", "error"]},
                                        "path": {"type": "string", "pattern": r"^(?:|/.*)$"},
                                    },
                                },
                            },
                            "ir": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "ir_format",
                                    "flow",
                                    "version",
                                    "source_digest",
                                    "dependency_digest",
                                    "digest",
                                    "canonical_document_digest",
                                    "canonical_execution_ir_digest",
                                ],
                                "properties": {
                                    "ir_format": {"type": "string", "minLength": 1},
                                    "flow": {"type": "string", "minLength": 1},
                                    "version": {"type": "string", "minLength": 1},
                                    "source_digest": {
                                        "type": "string",
                                        "pattern": r"^[0-9a-f]{64}$",
                                    },
                                    "profile_digest": {
                                        "type": "string",
                                        "pattern": r"^[0-9a-f]{64}$",
                                    },
                                    "dependency_digest": {
                                        "type": "string",
                                        "pattern": r"^[0-9a-f]{64}$",
                                    },
                                    "digest": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                                    "canonical_document_digest": {
                                        "type": "string",
                                        "pattern": r"^[0-9a-f]{64}$",
                                    },
                                    "canonical_ir_digest": {
                                        "type": "string",
                                        "pattern": r"^[0-9a-f]{64}$",
                                    },
                                    "canonical_execution_ir_digest": {
                                        "type": "string",
                                        "pattern": r"^[0-9a-f]{64}$",
                                    },
                                },
                            },
                        },
                    },
                    "turns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["payload", "expected_trace"],
                            "properties": {
                                "payload": {"type": "object"},
                                "expected_trace": {"type": "object"},
                            },
                        },
                    },
                },
            },
        },
    },
}


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_clone(json_document: object) -> object:
    return strict_json_loads(_canonical_json(json_document))


def _sha256(serialized_contract: str) -> str:
    return hashlib.sha256(serialized_contract.encode("utf-8")).hexdigest()


def _fixed_clock() -> datetime:
    return datetime(2025, 1, 1, tzinfo=timezone.utc)


def _fixed_clock_milliseconds() -> int:
    return SNOWFLAKE_EPOCH_MILLISECONDS


def _require_identifier(identifier: str, contract_name: str) -> None:
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"{contract_name} has an invalid identifier: {identifier!r}")


def _require_digest(contract_digest: str, contract_name: str) -> None:
    if not _DIGEST_PATTERN.fullmatch(contract_digest):
        raise ValueError(f"{contract_name} must be a lowercase SHA-256 digest")


def _canonical_object(serialized_contract: str, contract_name: str) -> dict[str, Any]:
    decoded_contract = strict_json_loads(serialized_contract)
    if not isinstance(decoded_contract, dict):
        raise ValueError(f"{contract_name} must encode a JSON object")
    if _canonical_json(decoded_contract) != serialized_contract:
        raise ValueError(f"{contract_name} must be canonical JSON")
    return cast(dict[str, Any], decoded_contract)


def _decode_pointer_segment(encoded_segment: str) -> str:
    decoded_characters: list[str] = []
    character_index = 0
    while character_index < len(encoded_segment):
        character = encoded_segment[character_index]
        if character != "~":
            decoded_characters.append(character)
            character_index += 1
            continue
        if character_index + 1 >= len(encoded_segment):
            raise ValueError(f"invalid JSON Pointer escape in segment {encoded_segment!r}")
        escaped_character = encoded_segment[character_index + 1]
        if escaped_character == "0":
            decoded_characters.append("~")
        elif escaped_character == "1":
            decoded_characters.append("/")
        else:
            raise ValueError(f"invalid JSON Pointer escape in segment {encoded_segment!r}")
        character_index += 2
    return "".join(decoded_characters)


def _pointer_segments(json_pointer: str) -> tuple[str, ...]:
    if json_pointer == "":
        return ()
    if not json_pointer.startswith("/"):
        raise ValueError(f"JSON Pointer must be empty or start with '/': {json_pointer!r}")
    return tuple(_decode_pointer_segment(segment) for segment in json_pointer[1:].split("/"))


def _array_index(pointer_segment: str, *, array_length: int, allow_append: bool) -> int:
    if pointer_segment == "-" and allow_append:
        return array_length
    if not pointer_segment.isdigit() or (
        pointer_segment.startswith("0") and pointer_segment != "0"
    ):
        raise ValueError(f"invalid JSON Pointer array index {pointer_segment!r}")
    array_index = int(pointer_segment)
    maximum_index = array_length if allow_append else array_length - 1
    if array_index > maximum_index:
        raise ValueError(f"JSON Pointer array index is out of range: {array_index}")
    return array_index


def _resolve_pointer(json_document: object, json_pointer: str) -> object:
    selected_node = json_document
    for pointer_segment in _pointer_segments(json_pointer):
        if isinstance(selected_node, Mapping):
            if pointer_segment not in selected_node:
                raise ValueError(f"JSON Pointer does not exist: {json_pointer!r}")
            selected_node = selected_node[pointer_segment]
        elif isinstance(selected_node, list):
            selected_node = selected_node[
                _array_index(pointer_segment, array_length=len(selected_node), allow_append=False)
            ]
        else:
            raise ValueError(f"JSON Pointer traverses a scalar: {json_pointer!r}")
    return selected_node


def _set_pointer(json_document: object, json_pointer: str, replacement: object) -> None:
    pointer_segments = _pointer_segments(json_pointer)
    if not pointer_segments:
        raise ValueError("a conformance mutation cannot replace the source root")
    parent_pointer = "".join(
        f"/{segment.replace('~', '~0').replace('/', '~1')}" for segment in pointer_segments[:-1]
    )
    parent_node = _resolve_pointer(json_document, parent_pointer)
    final_segment = pointer_segments[-1]
    replacement_copy = copy.deepcopy(replacement)
    if isinstance(parent_node, dict):
        parent_node[final_segment] = replacement_copy
        return
    if isinstance(parent_node, list):
        array_index = _array_index(
            final_segment,
            array_length=len(parent_node),
            allow_append=True,
        )
        if array_index == len(parent_node):
            parent_node.append(replacement_copy)
        else:
            parent_node[array_index] = replacement_copy
        return
    raise ValueError(f"conformance mutation parent is not a container: {json_pointer!r}")


@dataclass(frozen=True)
class CtkDiagnosticOracle:
    """Stable diagnostic identity used to verify ordering without prose coupling."""

    code: str
    severity: DiagnosticSeverity
    path: str

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("conformance diagnostic code must be non-empty")
        if self.severity not in {"warning", "error"}:
            raise ValueError("conformance diagnostic severity is invalid")
        if self.path and not self.path.startswith("/"):
            raise ValueError("conformance diagnostic path must be a JSON Pointer")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "path": self.path}


@dataclass(frozen=True)
class CtkIrOracle:
    """Canonical IR identities expected from a valid source."""

    ir_format: str
    flow: str
    version: str
    source_digest: str
    dependency_digest: str
    digest: str
    canonical_document_digest: str
    canonical_execution_ir_digest: str
    profile_digest: str | None = None
    canonical_ir_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.ir_format or not self.flow or not self.version:
            raise ValueError("conformance IR format, flow, and version must be non-empty")
        for digest_name, contract_digest in (
            ("source digest", self.source_digest),
            ("dependency digest", self.dependency_digest),
            ("execution digest", self.digest),
            ("canonical document digest", self.canonical_document_digest),
            ("canonical execution IR digest", self.canonical_execution_ir_digest),
        ):
            _require_digest(contract_digest, f"conformance IR {digest_name}")
        if self.profile_digest is not None:
            _require_digest(self.profile_digest, "conformance IR profile digest")
        if self.canonical_ir_digest is not None:
            _require_digest(self.canonical_ir_digest, "conformance canonical IR digest")

    def to_dict(self) -> dict[str, str]:
        ir_contract = {
            "ir_format": self.ir_format,
            "flow": self.flow,
            "version": self.version,
            "source_digest": self.source_digest,
            "dependency_digest": self.dependency_digest,
            "digest": self.digest,
            "canonical_document_digest": self.canonical_document_digest,
            "canonical_execution_ir_digest": self.canonical_execution_ir_digest,
        }
        if self.profile_digest is not None:
            ir_contract["profile_digest"] = self.profile_digest
        if self.canonical_ir_digest is not None:
            ir_contract["canonical_ir_digest"] = self.canonical_ir_digest
        return ir_contract


@dataclass(frozen=True)
class CtkTurn:
    """One external payload and its exact caller-visible trace oracle."""

    payload_json: str
    expected_trace_json: str

    def __post_init__(self) -> None:
        _canonical_object(self.payload_json, "conformance turn payload")
        _canonical_object(self.expected_trace_json, "conformance turn trace oracle")

    def payload(self) -> dict[str, Any]:
        return _canonical_object(self.payload_json, "conformance turn payload")

    def expected_trace(self) -> dict[str, Any]:
        return _canonical_object(self.expected_trace_json, "conformance turn trace oracle")

    def to_dict(self) -> dict[str, object]:
        return {"payload": self.payload(), "expected_trace": self.expected_trace()}


@dataclass(frozen=True)
class CtkCase:
    """One immutable source, validation oracle, IR oracle, and trace sequence."""

    identifier: str
    source_json: str
    expected_compilation: CompilationStatus
    expected_diagnostics: tuple[CtkDiagnosticOracle, ...]
    expected_ir: CtkIrOracle | None = None
    turns: tuple[CtkTurn, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.identifier, "conformance case")
        _canonical_object(self.source_json, "conformance case source")
        if self.expected_compilation not in {
            "not_requested",
            "skipped",
            "succeeded",
            "failed",
        }:
            raise ValueError("conformance case compilation oracle is invalid")
        if self.turns and self.expected_compilation != "succeeded":
            raise ValueError("runtime trace oracles require successful compilation")
        if self.expected_ir is not None and self.expected_compilation != "succeeded":
            raise ValueError("an IR oracle requires successful compilation")

    def source_document(self) -> dict[str, Any]:
        return _canonical_object(self.source_json, "conformance case source")

    def to_dict(self) -> dict[str, object]:
        expected_contract: dict[str, object] = {
            "compilation": self.expected_compilation,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.expected_diagnostics],
        }
        if self.expected_ir is not None:
            expected_contract["ir"] = self.expected_ir.to_dict()
        case_contract: dict[str, object] = {
            "identifier": self.identifier,
            "source": self.source_document(),
            "expected": expected_contract,
        }
        if self.turns:
            case_contract["turns"] = [turn.to_dict() for turn in self.turns]
        return case_contract


@dataclass(frozen=True)
class CtkCorpus:
    """An immutable, content-addressed conformance corpus."""

    cases: tuple[CtkCase, ...]
    format_identifier: str = CTK_CORPUS_FORMAT
    version: str = CTK_VERSION

    def __post_init__(self) -> None:
        if self.format_identifier != CTK_CORPUS_FORMAT or self.version != CTK_VERSION:
            raise ValueError("conformance corpus format or version is unsupported")
        if not self.cases:
            raise ValueError("conformance corpus must contain cases")
        case_identifiers = [conformance_case.identifier for conformance_case in self.cases]
        if len(case_identifiers) != len(set(case_identifiers)):
            raise ValueError("conformance case identifiers must be unique")

    @property
    def digest(self) -> str:
        return _sha256(_canonical_json(self.to_dict()))

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format_identifier,
            "version": self.version,
            "cases": [conformance_case.to_dict() for conformance_case in self.cases],
        }


@dataclass(frozen=True)
class CtkCaseReport:
    """Observed contracts and oracle mismatches for one corpus case."""

    identifier: str
    check_report_json: str
    ir_observation_json: str | None
    runtime_trace_json: str
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.identifier, "conformance case report")
        _canonical_object(self.check_report_json, "conformance check report")
        if self.ir_observation_json is not None:
            _canonical_object(self.ir_observation_json, "conformance IR observation")
        runtime_trace = strict_json_loads(self.runtime_trace_json)
        if not isinstance(runtime_trace, list):
            raise ValueError("conformance runtime trace must encode a JSON array")
        if _canonical_json(runtime_trace) != self.runtime_trace_json:
            raise ValueError("conformance runtime trace must be canonical JSON")

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        case_report: dict[str, object] = {
            "identifier": self.identifier,
            "passed": self.passed,
            "check": strict_json_loads(self.check_report_json),
            "runtime_trace": strict_json_loads(self.runtime_trace_json),
            "failures": list(self.failures),
        }
        if self.ir_observation_json is not None:
            case_report["ir"] = strict_json_loads(self.ir_observation_json)
        return case_report


@dataclass(frozen=True)
class CtkReport:
    """Deterministic machine-readable result for a complete corpus run."""

    corpus_digest: str
    profile_identifier: str
    case_reports: tuple[CtkCaseReport, ...]
    format_identifier: str = CTK_REPORT_FORMAT
    version: str = CTK_VERSION

    def __post_init__(self) -> None:
        if self.format_identifier != CTK_REPORT_FORMAT or self.version != CTK_VERSION:
            raise ValueError("conformance report format or version is unsupported")
        _require_digest(self.corpus_digest, "conformance corpus digest")
        if not self.profile_identifier:
            raise ValueError("conformance report profile identifier must be non-empty")
        if not self.case_reports:
            raise ValueError("conformance report must contain case reports")

    @property
    def passed(self) -> bool:
        return all(case_report.passed for case_report in self.case_reports)

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "format": self.format_identifier,
            "version": self.version,
            "corpus_digest": self.corpus_digest,
            "profile": self.profile_identifier,
            "passed": self.passed,
            "cases": [case_report.to_dict() for case_report in self.case_reports],
        }

    @property
    def digest(self) -> str:
        return _sha256(_canonical_json(self._unsigned_dict()))

    def to_dict(self) -> dict[str, object]:
        report_contract = self._unsigned_dict()
        report_contract["digest"] = self.digest
        return report_contract

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())


def _load_json_document(json_path: Path) -> object:
    return strict_json_loads(json_path.read_text(encoding="utf-8"))


def _safe_corpus_path(corpus_root: Path, relative_path: str) -> Path:
    requested_path = Path(relative_path)
    if requested_path.is_absolute():
        raise ValueError("conformance source paths must be relative")
    resolved_root = corpus_root.resolve()
    resolved_path = (resolved_root / requested_path).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as traversal_error:
        raise ValueError("conformance source path leaves the corpus root") from traversal_error
    if any(path_component.startswith(".env") for path_component in requested_path.parts):
        raise ValueError("conformance source path cannot reference environment files")
    if resolved_path.suffix != ".json":
        raise ValueError("conformance source path must reference a JSON document")
    return resolved_path


def _manifest_schema_errors(manifest_document: object) -> tuple[str, ...]:
    validation_errors = sorted(
        Draft202012Validator(_CORPUS_SCHEMA).iter_errors(cast(Any, manifest_document)),
        key=lambda schema_error: (
            tuple(str(path_segment) for path_segment in schema_error.absolute_path),
            tuple(str(path_segment) for path_segment in schema_error.absolute_schema_path),
            schema_error.message,
        ),
    )
    return tuple(
        f"{'.'.join(str(path_segment) for path_segment in schema_error.absolute_path) or '<root>'}: "
        f"{schema_error.message}"
        for schema_error in validation_errors
    )


def _diagnostic_oracles(
    diagnostic_documents: Sequence[Mapping[str, object]],
) -> tuple[CtkDiagnosticOracle, ...]:
    return tuple(
        CtkDiagnosticOracle(
            code=cast(str, diagnostic_document["code"]),
            severity=cast(DiagnosticSeverity, diagnostic_document["severity"]),
            path=cast(str, diagnostic_document["path"]),
        )
        for diagnostic_document in diagnostic_documents
    )


def _ir_oracle(ir_document: Mapping[str, object] | None) -> CtkIrOracle | None:
    if ir_document is None:
        return None
    return CtkIrOracle(
        ir_format=cast(str, ir_document["ir_format"]),
        flow=cast(str, ir_document["flow"]),
        version=cast(str, ir_document["version"]),
        source_digest=cast(str, ir_document["source_digest"]),
        profile_digest=cast(str | None, ir_document.get("profile_digest")),
        dependency_digest=cast(str, ir_document["dependency_digest"]),
        digest=cast(str, ir_document["digest"]),
        canonical_document_digest=cast(str, ir_document["canonical_document_digest"]),
        canonical_execution_ir_digest=cast(
            str,
            ir_document["canonical_execution_ir_digest"],
        ),
        canonical_ir_digest=cast(str | None, ir_document.get("canonical_ir_digest")),
    )


def load_ctk_corpus(manifest_path: str | Path) -> CtkCorpus:
    """Load a closed corpus and resolve fixture references within its directory."""

    resolved_manifest_path = Path(manifest_path).resolve()
    manifest_document = _load_json_document(resolved_manifest_path)
    if manifest_errors := _manifest_schema_errors(manifest_document):
        raise ValueError("invalid conformance corpus: " + "; ".join(manifest_errors))
    manifest_mapping = cast(Mapping[str, object], manifest_document)
    case_documents = cast(Sequence[Mapping[str, object]], manifest_mapping["cases"])
    conformance_cases: list[CtkCase] = []
    for case_document in case_documents:
        source_contract = cast(Mapping[str, object], case_document["source"])
        source_path = _safe_corpus_path(
            resolved_manifest_path.parent,
            cast(str, source_contract["path"]),
        )
        fixture_document = _load_json_document(source_path)
        source_document = _canonical_clone(
            _resolve_pointer(fixture_document, cast(str, source_contract["pointer"]))
        )
        for mutation_contract in cast(
            Sequence[Mapping[str, object]], source_contract.get("mutations", [])
        ):
            _set_pointer(
                source_document,
                cast(str, mutation_contract["path"]),
                mutation_contract["replacement"],
            )
        validate_json_value(source_document, boundary="conformance source document")
        if not isinstance(source_document, dict):
            raise ValueError("conformance source pointer must select a JSON object")

        expected_contract = cast(Mapping[str, object], case_document["expected"])
        turn_contracts = cast(Sequence[Mapping[str, object]], case_document.get("turns", []))
        conformance_cases.append(
            CtkCase(
                identifier=cast(str, case_document["identifier"]),
                source_json=_canonical_json(source_document),
                expected_compilation=cast(CompilationStatus, expected_contract["compilation"]),
                expected_diagnostics=_diagnostic_oracles(
                    cast(Sequence[Mapping[str, object]], expected_contract["diagnostics"])
                ),
                expected_ir=_ir_oracle(
                    cast(Mapping[str, object] | None, expected_contract.get("ir"))
                ),
                turns=tuple(
                    CtkTurn(
                        payload_json=_canonical_json(turn_contract["payload"]),
                        expected_trace_json=_canonical_json(turn_contract["expected_trace"]),
                    )
                    for turn_contract in turn_contracts
                ),
            )
        )
    return CtkCorpus(cases=tuple(conformance_cases))


def _observed_diagnostics(flow_check_report: FlowCheckReport) -> tuple[CtkDiagnosticOracle, ...]:
    return tuple(
        CtkDiagnosticOracle(
            code=flow_diagnostic.code,
            severity=flow_diagnostic.severity,
            path=flow_diagnostic.path,
        )
        for flow_diagnostic in flow_check_report.diagnostics
    )


def _ir_observation(flow_ir: FlowIR) -> dict[str, object]:
    canonical_ir = flow_ir.to_dict()
    canonical_execution_ir = copy.deepcopy(canonical_ir)
    canonical_execution_ir.pop("profile_digest")
    return {
        "ir_format": flow_ir.ir_format,
        "flow": flow_ir.flow,
        "version": flow_ir.version,
        "source_digest": flow_ir.source_digest,
        "profile_digest": flow_ir.profile_digest,
        "dependency_digest": flow_ir.dependency_digest,
        "digest": flow_ir.digest,
        "canonical_document_digest": _sha256(flow_ir.canonical_json),
        "canonical_ir_digest": _sha256(_canonical_json(canonical_ir)),
        "canonical_execution_ir_digest": _sha256(_canonical_json(canonical_execution_ir)),
        "canonical_ir": canonical_ir,
    }


def _runtime_trace(state: ServiceState) -> dict[str, object]:
    trace_contract: dict[str, object] = {
        "status": state.status,
        "data": copy.deepcopy(state.data),
    }
    agent_response = state.agent_response
    if agent_response is not None:
        agent_contract: dict[str, object] = {"description": agent_response.description}
        for response_field in ("payload_schema", "interactive", "error_message"):
            response_contract = getattr(agent_response, response_field)
            if response_contract is not None:
                agent_contract[response_field] = copy.deepcopy(response_contract)
        trace_contract["agent_response"] = agent_contract
    return cast(dict[str, object], _canonical_clone(trace_contract))


def _oracle_failures(
    conformance_case: CtkCase,
    flow_check_report: FlowCheckReport,
    ir_observation: Mapping[str, object] | None,
    runtime_trace: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    oracle_failures: list[str] = []
    if flow_check_report.compilation != conformance_case.expected_compilation:
        oracle_failures.append(
            "compilation mismatch: expected "
            f"{conformance_case.expected_compilation!r}, observed "
            f"{flow_check_report.compilation!r}"
        )
    observed_diagnostics = _observed_diagnostics(flow_check_report)
    if observed_diagnostics != conformance_case.expected_diagnostics:
        oracle_failures.append(
            "diagnostic order mismatch: expected "
            f"{_canonical_json([oracle.to_dict() for oracle in conformance_case.expected_diagnostics])}, "
            f"observed {_canonical_json([oracle.to_dict() for oracle in observed_diagnostics])}"
        )
    if conformance_case.expected_ir is not None:
        if ir_observation is None:
            oracle_failures.append("IR oracle mismatch: no IR was produced")
        else:
            expected_ir_contract = conformance_case.expected_ir.to_dict()
            selected_ir_observation = {
                ir_field: ir_observation.get(ir_field) for ir_field in expected_ir_contract
            }
            if selected_ir_observation != expected_ir_contract:
                oracle_failures.append(
                    "IR oracle mismatch: expected "
                    f"{_canonical_json(expected_ir_contract)}, observed "
                    f"{_canonical_json(selected_ir_observation)}"
                )
    expected_trace = tuple(turn.expected_trace() for turn in conformance_case.turns)
    if tuple(runtime_trace) != expected_trace:
        oracle_failures.append(
            "runtime trace mismatch: expected "
            f"{_canonical_json(expected_trace)}, observed {_canonical_json(runtime_trace)}"
        )
    return tuple(oracle_failures)


async def _execute_runtime_trace(
    conformance_case: CtkCase,
    runtime_profile: FlowProfile,
) -> tuple[dict[str, object], ...]:
    if not conformance_case.turns:
        return ()
    runtime = FlowRuntime(
        conformance_case.source_document(),
        tools=runtime_profile.tools,
        subflows=runtime_profile.subflows,
        clock=_fixed_clock,
        log_id_generator=SnowflakeIdGenerator(
            worker_id=0,
            clock_milliseconds=_fixed_clock_milliseconds,
        ),
    )
    execution_state = runtime.new_state(f"ctk-{conformance_case.identifier}")
    trace_contracts: list[dict[str, object]] = []
    for conformance_turn in conformance_case.turns:
        execution_state = await runtime.execute(execution_state, conformance_turn.payload())
        trace_contracts.append(_runtime_trace(execution_state))
    return tuple(trace_contracts)


async def run_ctk_corpus(
    conformance_corpus: CtkCorpus,
    *,
    profile: FlowProfile | None = None,
) -> CtkReport:
    """Execute every source, IR, diagnostic-order, and runtime-trace oracle."""

    runtime_profile = profile or reference_profile()
    case_reports: list[CtkCaseReport] = []
    for conformance_case in conformance_corpus.cases:
        source_document = conformance_case.source_document()
        flow_check_report = check_flow(source_document, profile=runtime_profile)
        ir_observation: dict[str, object] | None = None
        runtime_trace: tuple[dict[str, object], ...] = ()
        execution_failure: str | None = None
        if flow_check_report.compilation == "succeeded":
            try:
                ir_observation = _ir_observation(
                    build_flow_ir(source_document, profile=runtime_profile)
                )
                runtime_trace = await _execute_runtime_trace(conformance_case, runtime_profile)
            except Exception as runtime_error:  # noqa: BLE001
                execution_failure = (
                    "conformance execution failed: "
                    f"{runtime_error.__class__.__name__}: {runtime_error}"
                )
        oracle_failures = list(
            _oracle_failures(
                conformance_case,
                flow_check_report,
                ir_observation,
                runtime_trace,
            )
        )
        if execution_failure is not None:
            oracle_failures.append(execution_failure)
        case_reports.append(
            CtkCaseReport(
                identifier=conformance_case.identifier,
                check_report_json=_canonical_json(flow_check_report.to_dict()),
                ir_observation_json=(
                    _canonical_json(ir_observation) if ir_observation is not None else None
                ),
                runtime_trace_json=_canonical_json(runtime_trace),
                failures=tuple(oracle_failures),
            )
        )
    return CtkReport(
        corpus_digest=conformance_corpus.digest,
        profile_identifier=runtime_profile.identifier,
        case_reports=tuple(case_reports),
    )
