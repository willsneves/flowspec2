"""Closed, provider-neutral authoring projection for normative ``flowspec/2``.

Structured-output providers commonly require every generated object to expose a
fixed property set.  The normative FlowSpec schema intentionally contains
identifier-keyed maps for domains, slots, derivations, and bindings.  Rewriting
those maps as fixed properties would either reject valid FlowSpec documents or
weaken their semantics.

The projection therefore uses a closed envelope whose payload is canonical
FlowSpec JSON.  The envelope schema is generated from the normative schema
identity and digest, contains no references, and can be handed directly to a
provider.  Lowering performs strict JSON decoding and the normative structural
check; the string encoding is the explicit portability cost of preserving the
format exactly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from flowspec2.diagnostics import FlowDiagnostic
from flowspec2.json_codec import StrictJsonError, StrictJsonValueError, strict_json_loads
from flowspec2.json_codec import validate_json_value as validate_strict_json_value
from flowspec2.schema import schema as normative_schema

from ..checker import structural_diagnostics

AUTHORING_PROJECTION_FORMAT: Final[str] = "flowspec2/authoring-projection"
AUTHORING_PROJECTION_VERSION: Final[str] = "1"

_SCHEMA_DIAGNOSTIC_PREFIX: Final[str] = "FLOWSPEC_AUTHORING_PROJECTION_SCHEMA_"
_artifact_cache: AuthoringProjectionArtifact | None = None


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


def _json_pointer(path_segments: Iterable[str | int]) -> str:
    encoded_segments = (
        str(path_segment).replace("~", "~0").replace("/", "~1") for path_segment in path_segments
    )
    return "".join(f"/{encoded_segment}" for encoded_segment in encoded_segments)


def _required_property(validation_error: ValidationError) -> str | None:
    if validation_error.validator != "required":
        return None
    required_properties = validation_error.validator_value
    projected_section = validation_error.instance
    if not isinstance(required_properties, list) or not isinstance(projected_section, Mapping):
        return None
    missing_properties = [
        property_name
        for property_name in required_properties
        if isinstance(property_name, str) and property_name not in projected_section
    ]
    return missing_properties[0] if len(missing_properties) == 1 else None


def _validation_error_pointer(validation_error: ValidationError) -> str:
    path_segments = list(validation_error.absolute_path)
    if missing_property := _required_property(validation_error):
        path_segments.append(missing_property)
    return _json_pointer(path_segments)


def _schema_diagnostic(validation_error: ValidationError) -> FlowDiagnostic:
    validator_name = str(validation_error.validator or "violation")
    stable_suffix = re.sub(r"[^A-Za-z0-9]+", "_", validator_name).strip("_").upper()
    return FlowDiagnostic(
        code=f"{_SCHEMA_DIAGNOSTIC_PREFIX}{stable_suffix or 'VIOLATION'}",
        severity="error",
        path=_validation_error_pointer(validation_error),
        message=validation_error.message,
    )


def _diagnostic_sort_key(flow_diagnostic: FlowDiagnostic) -> tuple[str, str, str, str]:
    return (
        flow_diagnostic.path,
        flow_diagnostic.code,
        flow_diagnostic.severity,
        flow_diagnostic.message,
    )


@dataclass(frozen=True)
class AuthoringProjectionArtifact:
    """Immutable metadata and JSON Schema for the authoring envelope."""

    format_identifier: str
    version: str
    normative_schema_identifier: str
    normative_schema_digest: str
    schema_digest: str
    canonical_schema_json: str

    def schema(self) -> dict[str, Any]:
        """Return an owned projection-schema document."""

        return cast(dict[str, Any], strict_json_loads(self.canonical_schema_json))

    def to_dict(self) -> dict[str, Any]:
        """Return deterministic metadata plus an owned schema document."""

        return {
            "format": self.format_identifier,
            "version": self.version,
            "normative_schema": {
                "identifier": self.normative_schema_identifier,
                "digest": self.normative_schema_digest,
            },
            "schema_digest": self.schema_digest,
            "schema": self.schema(),
        }


class AuthoringProjectionError(ValueError):
    """A projection envelope cannot lower to normative FlowSpec."""

    def __init__(self, diagnostics: tuple[FlowDiagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("authoring projection error requires diagnostics")
        self.diagnostics = diagnostics
        super().__init__("; ".join(diagnostic.message for diagnostic in diagnostics))


def _build_artifact() -> AuthoringProjectionArtifact:
    source_schema = normative_schema()
    source_schema_json = _canonical_json(source_schema)
    source_schema_digest = _sha256(source_schema_json)
    source_schema_identifier = source_schema.get("$id")
    if not isinstance(source_schema_identifier, str) or not source_schema_identifier:
        raise RuntimeError("normative FlowSpec schema must declare a non-empty $id")

    projected_schema: dict[str, Any] = {
        "title": "FlowSpec provider-neutral authoring projection",
        "description": (
            "A closed structured-output envelope carrying canonical flowspec/2 JSON; "
            "lowering validates the decoded document against the normative schema."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "format",
            "version",
            "normative_schema_digest",
            "flow_document_json",
        ],
        "properties": {
            "format": {"type": "string", "enum": [AUTHORING_PROJECTION_FORMAT]},
            "version": {"type": "string", "enum": [AUTHORING_PROJECTION_VERSION]},
            "normative_schema_digest": {
                "type": "string",
                "enum": [source_schema_digest],
            },
            "flow_document_json": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Canonical compact flowspec/2 JSON with lexicographically sorted object keys."
                ),
            },
        },
    }
    Draft202012Validator.check_schema(projected_schema)
    projected_schema_json = _canonical_json(projected_schema)
    return AuthoringProjectionArtifact(
        format_identifier=AUTHORING_PROJECTION_FORMAT,
        version=AUTHORING_PROJECTION_VERSION,
        normative_schema_identifier=source_schema_identifier,
        normative_schema_digest=source_schema_digest,
        schema_digest=_sha256(projected_schema_json),
        canonical_schema_json=projected_schema_json,
    )


def authoring_projection() -> AuthoringProjectionArtifact:
    """Return the cached immutable projection artifact."""

    global _artifact_cache
    if _artifact_cache is None:
        _artifact_cache = _build_artifact()
    return _artifact_cache


def project_flow_document(flow_document: object) -> dict[str, str]:
    """Encode one structurally valid flow in the closed authoring envelope."""

    try:
        validate_strict_json_value(flow_document, boundary="authoring projection flow document")
    except StrictJsonValueError as json_contract_error:
        raise AuthoringProjectionError(
            (
                FlowDiagnostic(
                    code="FLOWSPEC_AUTHORING_PROJECTION_JSON_VALUE_INVALID",
                    severity="error",
                    path=json_contract_error.path,
                    message=str(json_contract_error),
                ),
            )
        ) from json_contract_error
    if flow_diagnostics := structural_diagnostics(flow_document):
        raise AuthoringProjectionError(flow_diagnostics)

    projection_artifact = authoring_projection()
    return {
        "format": projection_artifact.format_identifier,
        "version": projection_artifact.version,
        "normative_schema_digest": projection_artifact.normative_schema_digest,
        "flow_document_json": _canonical_json(flow_document),
    }


def authoring_projection_diagnostics(projected_document: object) -> tuple[FlowDiagnostic, ...]:
    """Return stable envelope, canonicalization, and normative diagnostics."""

    projection_artifact = authoring_projection()
    projection_validator = Draft202012Validator(projection_artifact.schema())
    envelope_diagnostics = tuple(
        sorted(
            (
                _schema_diagnostic(validation_error)
                for validation_error in projection_validator.iter_errors(
                    cast(Any, projected_document)
                )
            ),
            key=_diagnostic_sort_key,
        )
    )
    if envelope_diagnostics:
        return envelope_diagnostics

    projected_mapping = cast(Mapping[str, object], projected_document)
    serialized_flow = cast(str, projected_mapping["flow_document_json"])
    try:
        decoded_flow = strict_json_loads(serialized_flow)
    except (json.JSONDecodeError, StrictJsonError) as decoding_error:
        return (
            FlowDiagnostic(
                code="FLOWSPEC_AUTHORING_PROJECTION_JSON_INVALID",
                severity="error",
                path="/flow_document_json",
                message=f"The projected FlowSpec JSON is invalid: {decoding_error}",
            ),
        )
    if serialized_flow != _canonical_json(decoded_flow):
        return (
            FlowDiagnostic(
                code="FLOWSPEC_AUTHORING_PROJECTION_JSON_NOT_CANONICAL",
                severity="error",
                path="/flow_document_json",
                message="The projected FlowSpec JSON is not in canonical compact form.",
            ),
        )
    return structural_diagnostics(decoded_flow)


def lower_authoring_projection(projected_document: object) -> dict[str, Any]:
    """Lower a valid projection to a fresh normative FlowSpec document."""

    if projection_diagnostics := authoring_projection_diagnostics(projected_document):
        raise AuthoringProjectionError(projection_diagnostics)
    projected_mapping = cast(Mapping[str, object], projected_document)
    decoded_flow = strict_json_loads(cast(str, projected_mapping["flow_document_json"]))
    if not isinstance(decoded_flow, dict):
        raise AssertionError("normative structural validation accepted a non-object flow")
    return cast(dict[str, Any], copy.deepcopy(decoded_flow))


def canonical_projection_json(projected_document: object) -> str:
    """Return canonical JSON after validating the complete projection contract."""

    lower_authoring_projection(projected_document)
    return _canonical_json(_canonical_clone(projected_document))
