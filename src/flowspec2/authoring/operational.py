"""Report-only evidence for model-mediated routing and extraction behavior."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import version as package_version
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, Literal, cast

import jsonschema

from flowspec2.domains import make_slot_model
from flowspec2.json_codec import StrictJsonError, strict_json_loads, validate_json_value
from flowspec2.llm import (
    StructuredOutputRequest,
    build_extraction_request,
    build_route_request,
)
from flowspec2.models import AgentResponse

from .evidence import AuthoringProviderProvenance
from .evidence_verification import AuthoringEvidenceVerification, verify_authoring_evidence

AUTHORING_OPERATIONAL_EVIDENCE_FORMAT: Final[str] = "flowspec2/authoring-operational-evidence@2"
OPERATIONAL_EVIDENCE_CLASSIFICATION: Final[str] = "report_only"
OPERATIONAL_CORPUS_FORMAT: Final[str] = "flowspec2/operational-corpus@2"
OPERATIONAL_PROBE_FORMAT: Final[str] = "flowspec2/operational-probe@2"
REFERENCE_OPERATIONAL_CORPUS_IDENTIFIER: Final[str] = "flowspec2_reference_operational"
OPERATIONAL_PROMPT_FORMAT: Final[str] = "flowspec2/operational-structured-output@1"

_CORPUS_PATH: Final[Path] = Path(__file__).with_name("operational-corpus.json")
_EVIDENCE_SCHEMA_PATH: Final[Path] = Path(__file__).with_name(
    "authoring-operational-evidence.schema.json"
)
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|secret|access[_-]?token)",
    re.IGNORECASE,
)

OperationalKind = Literal["route", "extract"]
OperationalSubjectMode = Literal["authored", "counterfactual"]
OperationalDiagnostic = Literal[
    "invalid_json",
    "schema_mismatch",
    "semantic_mismatch",
]


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


def _request_contract(structured_request: StructuredOutputRequest) -> dict[str, object]:
    return {
        "system_instruction": structured_request.system,
        "prompt": structured_request.prompt,
        "response_schema": structured_request.response_schema,
    }


def _operational_prompt_contract() -> dict[str, object]:
    route_request = build_route_request(
        "reference citizen route input",
        [
            {
                "flow": "reference_flow",
                "route": {
                    "description": "Reference route description.",
                    "trigger_phrases": ["reference trigger phrase"],
                },
            }
        ],
    )
    extraction_request = build_extraction_request(
        "reference citizen extraction input",
        AgentResponse(
            description="Reference extraction prompt.",
            payload_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reference_slot": {
                        "description": "Reference extraction hint.",
                        "enum": ["reference_token"],
                    }
                },
                "required": ["reference_slot"],
            },
        ),
    )
    return {
        "format": OPERATIONAL_PROMPT_FORMAT,
        "request_fields": ["system_instruction", "prompt", "response_schema"],
        "route_request": _request_contract(route_request),
        "extraction_request": _request_contract(extraction_request),
    }


OPERATIONAL_PROMPT_DIGEST: Final[str] = _sha256(_canonical_json(_operational_prompt_contract()))


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


def _decode_pointer_segment(pointer_segment: str) -> str:
    return pointer_segment.replace("~1", "/").replace("~0", "~")


def _resolve_pointer(json_document: object, json_pointer: str) -> object:
    if not json_pointer.startswith("/"):
        raise ValueError("operational subject pointer must be an absolute JSON Pointer")
    current_value = json_document
    for encoded_segment in json_pointer[1:].split("/"):
        pointer_segment = _decode_pointer_segment(encoded_segment)
        if isinstance(current_value, Mapping):
            if pointer_segment not in current_value:
                raise ValueError(f"operational subject pointer does not exist: {json_pointer}")
            current_value = current_value[pointer_segment]
        elif isinstance(current_value, list):
            if not pointer_segment.isdigit() or int(pointer_segment) >= len(current_value):
                raise ValueError(f"operational subject pointer does not exist: {json_pointer}")
            current_value = current_value[int(pointer_segment)]
        else:
            raise ValueError(f"operational subject pointer does not exist: {json_pointer}")
    return current_value


def _replace_pointer(json_document: object, json_pointer: str, replacement: object) -> None:
    if not json_pointer.startswith("/"):
        raise ValueError("operational subject pointer must be an absolute JSON Pointer")
    encoded_segments = json_pointer[1:].split("/")
    if not encoded_segments or encoded_segments == [""]:
        raise ValueError("operational subject pointer cannot replace the document root")
    parent_value = json_document
    for encoded_segment in encoded_segments[:-1]:
        pointer_segment = _decode_pointer_segment(encoded_segment)
        if isinstance(parent_value, dict) and pointer_segment in parent_value:
            parent_value = parent_value[pointer_segment]
        elif (
            isinstance(parent_value, list)
            and pointer_segment.isdigit()
            and int(pointer_segment) < len(parent_value)
        ):
            parent_value = parent_value[int(pointer_segment)]
        else:
            raise ValueError(f"operational subject pointer does not exist: {json_pointer}")
    final_segment = _decode_pointer_segment(encoded_segments[-1])
    if isinstance(parent_value, dict) and final_segment in parent_value:
        parent_value[final_segment] = replacement
        return
    if (
        isinstance(parent_value, list)
        and final_segment.isdigit()
        and int(final_segment) < len(parent_value)
    ):
        parent_value[int(final_segment)] = replacement
        return
    raise ValueError(f"operational subject pointer does not exist: {json_pointer}")


@dataclass(frozen=True)
class OperationalProbe:
    """One public model-mediated observation bound to an authoring case."""

    identifier: str
    pair_identifier: str
    subject_mode: OperationalSubjectMode
    operation: OperationalKind
    authoring_case_identifier: str
    subject_pointer: str
    citizen_input: str
    expected_json: str
    counterfactual_json: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.identifier):
            raise ValueError("operational probe identifier is invalid")
        if not _IDENTIFIER_PATTERN.fullmatch(self.pair_identifier):
            raise ValueError("operational probe pair identifier is invalid")
        if self.subject_mode not in {"authored", "counterfactual"}:
            raise ValueError("operational probe subject mode is unsupported")
        if self.operation not in {"route", "extract"}:
            raise ValueError("operational probe operation is unsupported")
        if not _IDENTIFIER_PATTERN.fullmatch(self.authoring_case_identifier):
            raise ValueError("operational probe authoring case identifier is invalid")
        if not self.subject_pointer.startswith("/"):
            raise ValueError("operational probe subject pointer must be a JSON Pointer")
        if not self.citizen_input.strip():
            raise ValueError("operational probe citizen input must be non-empty")
        expected_output = strict_json_loads(self.expected_json)
        validate_json_value(expected_output, boundary="operational expected output")
        if self.expected_json != _canonical_json(expected_output):
            raise ValueError("operational probe expected output must be canonical JSON")
        if self.subject_mode == "authored" and self.counterfactual_json is not None:
            raise ValueError("authored operational probes cannot replace their subject")
        if self.subject_mode == "counterfactual" and self.counterfactual_json is None:
            raise ValueError("counterfactual operational probes require a replacement subject")
        if self.counterfactual_json is not None:
            counterfactual_subject = strict_json_loads(self.counterfactual_json)
            validate_json_value(
                counterfactual_subject,
                boundary="operational counterfactual subject",
            )
            if self.counterfactual_json != _canonical_json(counterfactual_subject):
                raise ValueError("operational counterfactual subject must be canonical JSON")

    @property
    def expected_output(self) -> object:
        return strict_json_loads(self.expected_json)

    @property
    def counterfactual_subject(self) -> object | None:
        return (
            None
            if self.counterfactual_json is None
            else strict_json_loads(self.counterfactual_json)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "pair_identifier": self.pair_identifier,
            "subject_mode": self.subject_mode,
            "operation": self.operation,
            "authoring_case_identifier": self.authoring_case_identifier,
            "subject_pointer": self.subject_pointer,
            "citizen_input": self.citizen_input,
            "expected": self.expected_output,
            "counterfactual": self.counterfactual_subject,
        }


@dataclass(frozen=True)
class OperationalCorpus:
    """Closed packaged operational probes and their content identity."""

    identifier: str
    digest: str
    probes: tuple[OperationalProbe, ...]
    format_identifier: str = OPERATIONAL_CORPUS_FORMAT
    probe_format_identifier: str = OPERATIONAL_PROBE_FORMAT

    def __post_init__(self) -> None:
        if self.format_identifier != OPERATIONAL_CORPUS_FORMAT:
            raise ValueError("operational corpus format is unsupported")
        if self.probe_format_identifier != OPERATIONAL_PROBE_FORMAT:
            raise ValueError("operational probe format is unsupported")
        if self.identifier != REFERENCE_OPERATIONAL_CORPUS_IDENTIFIER:
            raise ValueError("operational corpus identifier is unsupported")
        if not _DIGEST_PATTERN.fullmatch(self.digest):
            raise ValueError("operational corpus digest must be lowercase SHA-256")
        identifiers = tuple(probe.identifier for probe in self.probes)
        if not identifiers or identifiers != tuple(sorted(identifiers)):
            raise ValueError("operational probes must be non-empty and sorted")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("operational probe identifiers must be unique")
        probes_by_pair: dict[str, list[OperationalProbe]] = {}
        for probe in self.probes:
            probes_by_pair.setdefault(probe.pair_identifier, []).append(probe)
        for pair_identifier, paired_probes in probes_by_pair.items():
            if len(paired_probes) != 2 or {probe.subject_mode for probe in paired_probes} != {
                "authored",
                "counterfactual",
            }:
                raise ValueError(
                    f"operational pair must contain authored and counterfactual probes: "
                    f"{pair_identifier}"
                )
            comparison_fields = {
                (
                    probe.operation,
                    probe.authoring_case_identifier,
                    probe.subject_pointer,
                    probe.citizen_input,
                )
                for probe in paired_probes
            }
            if len(comparison_fields) != 1:
                raise ValueError(f"operational pair inputs are inconsistent: {pair_identifier}")
            if len({probe.expected_json for probe in paired_probes}) != 2:
                raise ValueError(
                    f"operational pair must expect distinct outcomes: {pair_identifier}"
                )

    def metadata(self) -> dict[str, object]:
        return {
            "format": self.format_identifier,
            "probe_format": self.probe_format_identifier,
            "identifier": self.identifier,
            "digest": self.digest,
            "probe_identifiers": [probe.identifier for probe in self.probes],
        }


_operational_corpus_cache: OperationalCorpus | None = None


def load_reference_operational_corpus() -> OperationalCorpus:
    """Load the immutable public operational probe corpus."""

    global _operational_corpus_cache
    if _operational_corpus_cache is not None:
        return _operational_corpus_cache
    corpus_resource = files("flowspec2.authoring").joinpath("operational-corpus.json")
    serialized_corpus = corpus_resource.read_text(encoding="utf-8")
    decoded_corpus = strict_json_loads(serialized_corpus)
    if not isinstance(decoded_corpus, dict):
        raise ValueError("operational corpus must be a JSON object")
    if set(decoded_corpus) != {"format", "case_format", "identifier", "probes"}:
        raise ValueError("operational corpus keys do not match the closed contract")
    if decoded_corpus["format"] != OPERATIONAL_CORPUS_FORMAT:
        raise ValueError("operational corpus format is unsupported")
    if decoded_corpus["case_format"] != OPERATIONAL_PROBE_FORMAT:
        raise ValueError("operational probe format is unsupported")
    raw_probes = decoded_corpus["probes"]
    if not isinstance(raw_probes, list):
        raise ValueError("operational corpus probes must be an array")
    probes: list[OperationalProbe] = []
    for raw_probe in raw_probes:
        if not isinstance(raw_probe, dict) or set(raw_probe) != {
            "identifier",
            "pair_identifier",
            "subject_mode",
            "operation",
            "authoring_case_identifier",
            "subject_pointer",
            "citizen_input",
            "expected",
            "counterfactual",
        }:
            raise ValueError("operational probe keys do not match the closed contract")
        probes.append(
            OperationalProbe(
                identifier=cast(str, raw_probe["identifier"]),
                pair_identifier=cast(str, raw_probe["pair_identifier"]),
                subject_mode=cast(OperationalSubjectMode, raw_probe["subject_mode"]),
                operation=cast(OperationalKind, raw_probe["operation"]),
                authoring_case_identifier=cast(str, raw_probe["authoring_case_identifier"]),
                subject_pointer=cast(str, raw_probe["subject_pointer"]),
                citizen_input=cast(str, raw_probe["citizen_input"]),
                expected_json=_canonical_json(raw_probe["expected"]),
                counterfactual_json=(
                    None
                    if raw_probe["counterfactual"] is None
                    else _canonical_json(raw_probe["counterfactual"])
                ),
            )
        )
    canonical_corpus = _canonical_json(decoded_corpus)
    _operational_corpus_cache = OperationalCorpus(
        identifier=cast(str, decoded_corpus["identifier"]),
        digest=_sha256(canonical_corpus),
        probes=tuple(probes),
    )
    return _operational_corpus_cache


@dataclass(frozen=True)
class OperationalProbeRequest:
    """Exact provider-neutral inputs for one operational probe."""

    probe_identifier: str
    operation: OperationalKind
    system_instruction: str
    prompt: str
    response_schema_json: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.probe_identifier):
            raise ValueError("operational request probe identifier is invalid")
        if self.operation not in {"route", "extract"}:
            raise ValueError("operational request operation is unsupported")
        if not self.system_instruction or not self.prompt:
            raise ValueError("operational request instructions must be non-empty")
        response_schema = strict_json_loads(self.response_schema_json)
        if not isinstance(response_schema, dict):
            raise ValueError("operational response schema must be an object")
        if self.response_schema_json != _canonical_json(response_schema):
            raise ValueError("operational response schema must be canonical JSON")
        jsonschema.Draft202012Validator.check_schema(response_schema)

    @classmethod
    def from_structured_request(
        cls,
        probe: OperationalProbe,
        structured_request: StructuredOutputRequest,
    ) -> OperationalProbeRequest:
        return cls(
            probe_identifier=probe.identifier,
            operation=probe.operation,
            system_instruction=structured_request.system,
            prompt=structured_request.prompt,
            response_schema_json=_canonical_json(structured_request.response_schema),
        )

    def response_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], strict_json_loads(self.response_schema_json))

    def to_dict(self) -> dict[str, object]:
        return {
            "system_instruction": self.system_instruction,
            "prompt": self.prompt,
            "response_schema": self.response_schema(),
        }


@dataclass(frozen=True)
class OperationalModelResponse:
    """Raw completed provider turn and optional effective model identity."""

    raw_output: str
    effective_model_version: str | None = None

    def __post_init__(self) -> None:
        if self.effective_model_version is not None:
            normalized_model = self.effective_model_version.strip()
            if not normalized_model:
                raise ValueError("operational effective model version must be non-empty")
            object.__setattr__(self, "effective_model_version", normalized_model)


@dataclass(frozen=True)
class _PreparedProbe:
    probe: OperationalProbe
    correction_round: int
    source_sha256: str
    subject_sha256: str
    request: OperationalProbeRequest
    expected_output_json: str

    @property
    def expected_output(self) -> object:
        return strict_json_loads(self.expected_output_json)


@dataclass(frozen=True)
class OperationalCapture:
    """One exact request, raw model turn, and deterministic observation."""

    probe_identifier: str
    pair_identifier: str
    subject_mode: OperationalSubjectMode
    operation: OperationalKind
    authoring_case_identifier: str
    correction_round: int
    source_sha256: str
    subject_pointer: str
    subject_sha256: str
    request_json: str
    expected_output_json: str
    raw_output: str
    raw_output_sha256: str
    parsed_output_json: str | None
    effective_model_version: str | None
    matched: bool
    diagnostic: OperationalDiagnostic | None

    def to_dict(self) -> dict[str, object]:
        return {
            "probe_identifier": self.probe_identifier,
            "pair_identifier": self.pair_identifier,
            "subject_mode": self.subject_mode,
            "operation": self.operation,
            "authoring_case_identifier": self.authoring_case_identifier,
            "correction_round": self.correction_round,
            "source_sha256": self.source_sha256,
            "subject_pointer": self.subject_pointer,
            "subject_sha256": self.subject_sha256,
            "request": strict_json_loads(self.request_json),
            "expected_output": strict_json_loads(self.expected_output_json),
            "raw_output": self.raw_output,
            "raw_output_sha256": self.raw_output_sha256,
            "parsed_output": (
                strict_json_loads(self.parsed_output_json)
                if self.parsed_output_json is not None
                else None
            ),
            "effective_model_version": self.effective_model_version,
            "matched": self.matched,
            "diagnostic": self.diagnostic,
        }


def _completed_sources(
    serialized_authoring_evidence: str,
    verified_authoring_evidence: AuthoringEvidenceVerification,
) -> dict[str, dict[str, Any]]:
    recomputed_verification = verify_authoring_evidence(
        serialized_authoring_evidence,
        expected_repository_revision=verified_authoring_evidence.repository_revision,
    )
    if recomputed_verification != verified_authoring_evidence:
        raise ValueError("authoring evidence verification does not match its exact document")
    evidence_document = strict_json_loads(serialized_authoring_evidence)
    if not isinstance(evidence_document, dict):
        raise ValueError("authoring evidence must be a JSON object")
    if evidence_document.get("digest") != verified_authoring_evidence.digest:
        raise ValueError("verified authoring evidence digest does not match its document")
    captures = cast(list[dict[str, Any]], evidence_document["captures"])
    captures_by_key = {
        (capture["case_identifier"], capture["correction_round"]): capture for capture in captures
    }
    completed_sources: dict[str, dict[str, Any]] = {}
    report = cast(dict[str, Any], evidence_document["report"])
    for case_result in cast(list[dict[str, Any]], report["case_results"]):
        if case_result["succeeded"] is not True:
            continue
        attempts = cast(list[dict[str, Any]], case_result["attempts"])
        final_round = cast(int, attempts[-1]["correction_round"])
        case_identifier = cast(str, case_result["case_identifier"])
        capture = captures_by_key[(case_identifier, final_round)]
        source_document = strict_json_loads(cast(str, capture["authored_source"]))
        if not isinstance(source_document, dict):
            raise ValueError("successful authored source must be a JSON object")
        completed_sources[case_identifier] = {
            "correction_round": final_round,
            "source_sha256": capture["source_sha256"],
            "source": source_document,
        }
    return completed_sources


def _catalog_contract(completed_sources: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    source_closure = [
        {
            "case_identifier": case_identifier,
            "correction_round": completed_sources[case_identifier]["correction_round"],
            "source_sha256": completed_sources[case_identifier]["source_sha256"],
        }
        for case_identifier in sorted(completed_sources)
    ]
    return {
        "source_closure": source_closure,
        "digest": _sha256(_canonical_json(source_closure)),
    }


def _extraction_agent_response(source_document: dict[str, Any], path_index: int) -> AgentResponse:
    path = cast(list[dict[str, Any]], source_document["path"])
    path_step = path[path_index]
    slot_name = cast(str, path_step["slot"])
    slots = cast(dict[str, dict[str, Any]], source_document["slots"])
    slot_contract = slots[slot_name]
    prompt_contract = cast(dict[str, Any], path_step["prompt"])
    slot_model = make_slot_model(
        slot_name,
        cast(str, slot_contract["domain"]),
        cast(dict[str, Any], source_document["domains"]),
        nullable=cast(bool, slot_contract.get("nullable", False)),
        extract_hint=cast(str | None, prompt_contract.get("extract_hint")),
    )
    return AgentResponse(
        description=cast(str, prompt_contract["text"]),
        payload_schema=slot_model.model_json_schema(),
        interactive=cast(dict[str, Any] | None, path_step.get("interactive")),
    )


def _prepare_probes(
    serialized_authoring_evidence: str,
    verified_authoring_evidence: AuthoringEvidenceVerification,
) -> tuple[tuple[_PreparedProbe, ...], dict[str, object]]:
    completed_sources = _completed_sources(
        serialized_authoring_evidence,
        verified_authoring_evidence,
    )
    corpus = load_reference_operational_corpus()
    prepared_probes: list[_PreparedProbe] = []
    for probe in corpus.probes:
        source_contract = completed_sources.get(probe.authoring_case_identifier)
        if source_contract is None:
            raise ValueError(
                f"operational probe requires successful authoring case: "
                f"{probe.authoring_case_identifier}"
            )
        source_document = cast(dict[str, Any], source_contract["source"])
        effective_source_document = copy.deepcopy(source_document)
        if probe.subject_mode == "counterfactual":
            _replace_pointer(
                effective_source_document,
                probe.subject_pointer,
                probe.counterfactual_subject,
            )
        subject_value = _resolve_pointer(effective_source_document, probe.subject_pointer)
        expected_output: object
        if probe.operation == "route":
            if probe.subject_pointer != "/route/trigger_phrases":
                raise ValueError("route probe must bind route.trigger_phrases")
            catalog_flows = [
                (
                    effective_source_document
                    if case_identifier == probe.authoring_case_identifier
                    else cast(dict[str, Any], completed_sources[case_identifier]["source"])
                )
                for case_identifier in sorted(completed_sources)
            ]
            structured_request = build_route_request(probe.citizen_input, catalog_flows)
            expected_output = {
                "service": effective_source_document["flow"]
                if cast(dict[str, Any], probe.expected_output)["selected"] is True
                else None
            }
        else:
            pointer_segments = probe.subject_pointer.strip("/").split("/")
            if (
                len(pointer_segments) != 4
                or pointer_segments[0] != "path"
                or not pointer_segments[1].isdigit()
                or pointer_segments[2:] != ["prompt", "extract_hint"]
            ):
                raise ValueError("extract probe must bind a path prompt.extract_hint")
            agent_response = _extraction_agent_response(
                effective_source_document,
                int(pointer_segments[1]),
            )
            structured_request = build_extraction_request(probe.citizen_input, agent_response)
            expected_output = probe.expected_output
        prepared_probes.append(
            _PreparedProbe(
                probe=probe,
                correction_round=cast(int, source_contract["correction_round"]),
                source_sha256=cast(str, source_contract["source_sha256"]),
                subject_sha256=_sha256(_canonical_json(subject_value)),
                request=OperationalProbeRequest.from_structured_request(
                    probe,
                    structured_request,
                ),
                expected_output_json=_canonical_json(expected_output),
            )
        )
    return tuple(prepared_probes), _catalog_contract(completed_sources)


def _capture(
    prepared_probe: _PreparedProbe,
    model_response: OperationalModelResponse,
) -> OperationalCapture:
    parsed_output: object | None = None
    diagnostic: OperationalDiagnostic | None = None
    try:
        parsed_output = strict_json_loads(model_response.raw_output)
    except (json.JSONDecodeError, StrictJsonError):
        diagnostic = "invalid_json"
    if diagnostic is None and not jsonschema.Draft202012Validator(
        prepared_probe.request.response_schema()
    ).is_valid(cast(Any, parsed_output)):
        diagnostic = "schema_mismatch"
    if diagnostic is None and parsed_output != prepared_probe.expected_output:
        diagnostic = "semantic_mismatch"
    matched = diagnostic is None
    return OperationalCapture(
        probe_identifier=prepared_probe.probe.identifier,
        pair_identifier=prepared_probe.probe.pair_identifier,
        subject_mode=prepared_probe.probe.subject_mode,
        operation=prepared_probe.probe.operation,
        authoring_case_identifier=prepared_probe.probe.authoring_case_identifier,
        correction_round=prepared_probe.correction_round,
        source_sha256=prepared_probe.source_sha256,
        subject_pointer=prepared_probe.probe.subject_pointer,
        subject_sha256=prepared_probe.subject_sha256,
        request_json=_canonical_json(prepared_probe.request.to_dict()),
        expected_output_json=prepared_probe.expected_output_json,
        raw_output=model_response.raw_output,
        raw_output_sha256=_sha256(model_response.raw_output),
        parsed_output_json=(
            _canonical_json(parsed_output) if diagnostic != "invalid_json" else None
        ),
        effective_model_version=model_response.effective_model_version,
        matched=matched,
        diagnostic=diagnostic,
    )


@dataclass(frozen=True)
class AuthoringOperationalEvidence:
    """Canonical report-only operational evidence bound to authoring evidence."""

    package_version: str
    repository_revision: str
    benchmark_evidence_digest: str
    corpus: OperationalCorpus
    provider: AuthoringProviderProvenance
    catalog_json: str
    captures: tuple[OperationalCapture, ...]

    def __post_init__(self) -> None:
        if not self.package_version or not self.repository_revision:
            raise ValueError("operational evidence package and revision must be non-empty")
        if not _DIGEST_PATTERN.fullmatch(self.benchmark_evidence_digest):
            raise ValueError("operational evidence authoring digest is invalid")
        catalog = strict_json_loads(self.catalog_json)
        if not isinstance(catalog, dict) or self.catalog_json != _canonical_json(catalog):
            raise ValueError("operational evidence catalog must be canonical JSON")
        capture_identifiers = tuple(capture.probe_identifier for capture in self.captures)
        expected_identifiers = tuple(probe.identifier for probe in self.corpus.probes)
        if capture_identifiers != expected_identifiers:
            raise ValueError("operational evidence capture closure does not match its corpus")

    @property
    def matched_probes(self) -> int:
        return sum(capture.matched for capture in self.captures)

    @property
    def total_probes(self) -> int:
        return len(self.captures)

    @property
    def all_matched(self) -> bool:
        return self.matched_probes == self.total_probes

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "format": AUTHORING_OPERATIONAL_EVIDENCE_FORMAT,
            "classification": OPERATIONAL_EVIDENCE_CLASSIFICATION,
            "package_version": self.package_version,
            "repository_revision": self.repository_revision,
            "benchmark_evidence_digest": self.benchmark_evidence_digest,
            "corpus": self.corpus.metadata(),
            "provider": self.provider.to_dict(),
            "catalog": strict_json_loads(self.catalog_json),
            "captures": [capture.to_dict() for capture in self.captures],
            "matched_probes": self.matched_probes,
            "total_probes": self.total_probes,
            "all_matched": self.all_matched,
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


def run_authoring_operational_evidence(
    serialized_authoring_evidence: str,
    verified_authoring_evidence: AuthoringEvidenceVerification,
    *,
    provider: AuthoringProviderProvenance,
    executor: Callable[[OperationalProbeRequest], OperationalModelResponse],
) -> AuthoringOperationalEvidence:
    """Run every packaged probe and retain completed mismatches as evidence."""

    prepared_probes, catalog = _prepare_probes(
        serialized_authoring_evidence,
        verified_authoring_evidence,
    )
    captures = tuple(
        _capture(prepared_probe, executor(prepared_probe.request))
        for prepared_probe in prepared_probes
    )
    return AuthoringOperationalEvidence(
        package_version=verified_authoring_evidence.package_version,
        repository_revision=verified_authoring_evidence.repository_revision,
        benchmark_evidence_digest=verified_authoring_evidence.digest,
        corpus=load_reference_operational_corpus(),
        provider=provider,
        catalog_json=_canonical_json(catalog),
        captures=captures,
    )


@dataclass(frozen=True)
class AuthoringOperationalEvidenceVerification:
    """Offline integrity and replay summary for operational evidence."""

    digest: str
    benchmark_evidence_digest: str
    repository_revision: str
    provider_identifier: str
    requested_model: str
    effective_model_versions: tuple[str, ...]
    matched_probes: int
    total_probes: int
    all_matched: bool
    classification: str = OPERATIONAL_EVIDENCE_CLASSIFICATION

    def to_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "benchmark_evidence_digest": self.benchmark_evidence_digest,
            "repository_revision": self.repository_revision,
            "provider_identifier": self.provider_identifier,
            "requested_model": self.requested_model,
            "effective_model_versions": list(self.effective_model_versions),
            "matched_probes": self.matched_probes,
            "total_probes": self.total_probes,
            "all_matched": self.all_matched,
            "classification": self.classification,
        }


_operational_evidence_schema_cache: dict[str, Any] | None = None


def _operational_evidence_schema() -> dict[str, Any]:
    global _operational_evidence_schema_cache
    if _operational_evidence_schema_cache is None:
        decoded_schema = strict_json_loads(_EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
        if not isinstance(decoded_schema, dict):
            raise ValueError("operational evidence schema must be a JSON object")
        jsonschema.Draft202012Validator.check_schema(decoded_schema)
        _operational_evidence_schema_cache = cast(dict[str, Any], decoded_schema)
    return _operational_evidence_schema_cache


def authoring_operational_evidence_schema() -> dict[str, Any]:
    """Return an owned closed schema for operational evidence."""

    return copy.deepcopy(_operational_evidence_schema())


def _provider_from_document(provider_document: dict[str, Any]) -> AuthoringProviderProvenance:
    if _contains_secret_key(provider_document.get("generation_configuration")):
        raise ValueError("operational provider configuration contains a secret field")
    return AuthoringProviderProvenance.from_configuration(
        identifier=cast(str, provider_document["identifier"]),
        model=cast(str, provider_document["model"]),
        sdk=cast(str, provider_document["sdk"]),
        sdk_version=cast(str, provider_document["sdk_version"]),
        prompt_format=cast(str, provider_document["prompt_format"]),
        prompt_digest=cast(str, provider_document["prompt_digest"]),
        generation_configuration=cast(
            dict[str, object], provider_document["generation_configuration"]
        ),
    )


def verify_authoring_operational_evidence(
    serialized_operational_evidence: str,
    serialized_authoring_evidence: str,
    verified_authoring_evidence: AuthoringEvidenceVerification,
) -> AuthoringOperationalEvidenceVerification:
    """Verify exact closure and replay captured model outputs without network access."""

    decoded_evidence = strict_json_loads(serialized_operational_evidence)
    jsonschema.Draft202012Validator(_operational_evidence_schema()).validate(decoded_evidence)
    if not isinstance(decoded_evidence, dict):
        raise AssertionError("operational evidence schema accepted a non-object")
    evidence_document = cast(dict[str, Any], decoded_evidence)
    if serialized_operational_evidence.strip() != _canonical_json(evidence_document):
        raise ValueError("operational evidence must use canonical compact JSON")
    unsigned_document = dict(evidence_document)
    recorded_digest = cast(str, unsigned_document.pop("digest"))
    if recorded_digest != _sha256(_canonical_json(unsigned_document)):
        raise ValueError("operational evidence digest does not match its content")
    if evidence_document["package_version"] != package_version("flowspec2"):
        raise ValueError("operational evidence package version does not match verifier")
    if evidence_document["benchmark_evidence_digest"] != verified_authoring_evidence.digest:
        raise ValueError("operational evidence is bound to different authoring evidence")
    if evidence_document["repository_revision"] != verified_authoring_evidence.repository_revision:
        raise ValueError("operational evidence repository revision is inconsistent")
    corpus = load_reference_operational_corpus()
    if evidence_document["corpus"] != corpus.metadata():
        raise ValueError("operational evidence corpus does not match installed corpus")
    prepared_probes, expected_catalog = _prepare_probes(
        serialized_authoring_evidence,
        verified_authoring_evidence,
    )
    if evidence_document["catalog"] != expected_catalog:
        raise ValueError("operational evidence source catalog does not match authoring evidence")
    provider_document = cast(dict[str, Any], evidence_document["provider"])
    provider = _provider_from_document(provider_document)
    if (
        provider.prompt_format != OPERATIONAL_PROMPT_FORMAT
        or provider.prompt_digest != OPERATIONAL_PROMPT_DIGEST
    ):
        raise ValueError("operational provider prompt contract is unsupported")
    capture_documents = cast(list[dict[str, Any]], evidence_document["captures"])
    if len(capture_documents) != len(prepared_probes):
        raise ValueError("operational evidence capture count is inconsistent")
    replayed_captures: list[OperationalCapture] = []
    for capture_document, prepared_probe in zip(
        capture_documents,
        prepared_probes,
        strict=True,
    ):
        replayed_capture = _capture(
            prepared_probe,
            OperationalModelResponse(
                raw_output=cast(str, capture_document["raw_output"]),
                effective_model_version=cast(
                    str | None, capture_document["effective_model_version"]
                ),
            ),
        )
        if replayed_capture.to_dict() != capture_document:
            raise ValueError(
                f"operational capture does not match offline replay: "
                f"{prepared_probe.probe.identifier}"
            )
        replayed_captures.append(replayed_capture)
    if provider.identifier == "google_gemini" and any(
        capture.effective_model_version is None for capture in replayed_captures
    ):
        raise ValueError("Gemini operational evidence requires effective model versions")
    matched_probes = sum(capture.matched for capture in replayed_captures)
    total_probes = len(replayed_captures)
    if (
        evidence_document["matched_probes"] != matched_probes
        or evidence_document["total_probes"] != total_probes
        or evidence_document["all_matched"] != (matched_probes == total_probes)
    ):
        raise ValueError("operational evidence summary does not match replay")
    effective_models = tuple(
        sorted(
            {
                capture.effective_model_version
                for capture in replayed_captures
                if capture.effective_model_version is not None
            }
        )
    )
    return AuthoringOperationalEvidenceVerification(
        digest=recorded_digest,
        benchmark_evidence_digest=verified_authoring_evidence.digest,
        repository_revision=verified_authoring_evidence.repository_revision,
        provider_identifier=provider.identifier,
        requested_model=provider.model,
        effective_model_versions=effective_models,
        matched_probes=matched_probes,
        total_probes=total_probes,
        all_matched=matched_probes == total_probes,
    )
