"""Aggregate structural validation and optional compilation checks."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .compiler import compile_flow
from .diagnostics import (
    CompilationStatus,
    DiagnosticLocation,
    FlowCheckReport,
    FlowDiagnostic,
)
from .ir import build_flow_ir
from .json_codec import (
    StrictJsonError,
    StrictJsonValueError,
    strict_json_loads,
    validate_json_value,
)
from .observability import SNOWFLAKE_EPOCH_MILLISECONDS, SnowflakeIdGenerator
from .profiles import FlowProfile, reference_profile
from .schema import schema
from .semantics import semantic_diagnostics

_SCHEMA_DIAGNOSTIC_PREFIX = "FLOWSPEC_SCHEMA_"


def _fixed_clock_milliseconds() -> int:
    return SNOWFLAKE_EPOCH_MILLISECONDS


def _json_pointer(segments: Iterable[str | int]) -> str:
    encoded_segments = (str(segment).replace("~", "~0").replace("/", "~1") for segment in segments)
    return "".join(f"/{segment}" for segment in encoded_segments)


def _required_property(validation_error: ValidationError) -> str | None:
    if validation_error.validator != "required":
        return None
    required_properties = validation_error.validator_value
    document_section = validation_error.instance
    if not isinstance(required_properties, list) or not isinstance(document_section, Mapping):
        return None
    missing_properties = [
        property_name
        for property_name in required_properties
        if isinstance(property_name, str) and property_name not in document_section
    ]
    for property_name in missing_properties:
        if validation_error.message == f"{property_name!r} is a required property":
            return property_name
    return missing_properties[0] if len(missing_properties) == 1 else None


def _validation_error_pointer(validation_error: ValidationError) -> str:
    segments = list(validation_error.absolute_path)
    if missing_property := _required_property(validation_error):
        segments.append(missing_property)
    return _json_pointer(segments)


def _schema_diagnostic_code(validation_error: ValidationError) -> str:
    validator_name = str(validation_error.validator or "violation")
    stable_suffix = re.sub(r"[^A-Za-z0-9]+", "_", validator_name).strip("_").upper()
    return f"{_SCHEMA_DIAGNOSTIC_PREFIX}{stable_suffix or 'VIOLATION'}"


def _context_locations(validation_error: ValidationError) -> tuple[DiagnosticLocation, ...]:
    unique_locations = {
        DiagnosticLocation(
            path=_validation_error_pointer(context_error),
            message=context_error.message,
        )
        for context_error in validation_error.context
    }
    return tuple(
        sorted(unique_locations, key=lambda location: (location.path, location.message or ""))
    )


def _suggested_fix(validation_error: ValidationError) -> str | None:
    if validation_error.validator != "const":
        return None
    expected_value = json.dumps(
        validation_error.validator_value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )
    return f"Replace this value with the schema-required constant {expected_value}."


def _schema_diagnostic(validation_error: ValidationError) -> FlowDiagnostic:
    return FlowDiagnostic(
        code=_schema_diagnostic_code(validation_error),
        severity="error",
        path=_validation_error_pointer(validation_error),
        message=validation_error.message,
        related_locations=_context_locations(validation_error),
        suggested_fix=_suggested_fix(validation_error),
    )


def structural_diagnostics(flow_document: object) -> tuple[FlowDiagnostic, ...]:
    """Return every Draft 2020-12 violation in a stable order."""

    validator = Draft202012Validator(schema())
    diagnostics = (
        _schema_diagnostic(validation_error)
        for validation_error in validator.iter_errors(cast(Any, flow_document))
    )
    return tuple(
        sorted(
            diagnostics,
            key=lambda diagnostic: (
                diagnostic.path,
                diagnostic.code,
                diagnostic.message,
            ),
        )
    )


def _compilation_diagnostic(compilation_error: Exception) -> FlowDiagnostic:
    error_detail = str(compilation_error) or compilation_error.__class__.__name__
    return FlowDiagnostic(
        code="FLOWSPEC_COMPILE_FAILED",
        severity="error",
        path="",
        message=(
            "flowspec2 compiler rejected the structurally valid document: "
            f"{compilation_error.__class__.__name__}: {error_detail}"
        ),
    )


def check_flow(
    flow_document: object,
    *,
    compile_document: bool = True,
    profile: FlowProfile | None = None,
) -> FlowCheckReport:
    """Check structural, semantic, and profile contracts, then compile."""

    try:
        validate_json_value(flow_document, boundary="flow document")
    except StrictJsonValueError as json_value_error:
        compilation: CompilationStatus = "skipped" if compile_document else "not_requested"
        return FlowCheckReport(
            diagnostics=(
                FlowDiagnostic(
                    code="FLOWSPEC_JSON_VALUE_INVALID",
                    severity="error",
                    path=json_value_error.path,
                    message=str(json_value_error),
                ),
            ),
            compilation=compilation,
        )
    diagnostics = structural_diagnostics(flow_document)
    if diagnostics:
        semantic_compilation: CompilationStatus = "skipped" if compile_document else "not_requested"
        return FlowCheckReport(diagnostics=diagnostics, compilation=semantic_compilation)
    effective_profile = profile or reference_profile()
    diagnostics = semantic_diagnostics(
        cast(dict[str, Any], flow_document),
        profile=effective_profile,
    )
    if diagnostics:
        compilation = "skipped" if compile_document else "not_requested"
        return FlowCheckReport(diagnostics=diagnostics, compilation=compilation)
    if not compile_document:
        return FlowCheckReport(diagnostics=(), compilation="not_requested")

    compiler_log_id_generator = SnowflakeIdGenerator(
        worker_id=0,
        clock_milliseconds=_fixed_clock_milliseconds,
    )
    try:
        flow_ir = build_flow_ir(
            cast(dict[str, Any], flow_document),
            profile=effective_profile,
        )
        compile_flow(
            flow_ir.to_document(),
            tools=effective_profile.tools,
            subflows=effective_profile.subflows,
            log_id_generator=compiler_log_id_generator,
        )
    except Exception as compilation_error:
        return FlowCheckReport(
            diagnostics=(_compilation_diagnostic(compilation_error),),
            compilation="failed",
        )
    return FlowCheckReport(diagnostics=(), compilation="succeeded")


def check_json(
    serialized_flow: str,
    *,
    compile_document: bool = True,
    profile: FlowProfile | None = None,
) -> FlowCheckReport:
    """Parse and check one serialized JSON flow without mutating external state."""

    try:
        flow_document: object = strict_json_loads(serialized_flow)
    except (json.JSONDecodeError, StrictJsonError) as decoding_error:
        compilation: CompilationStatus = "skipped" if compile_document else "not_requested"
        decoding_message = (
            f"Invalid JSON at line {decoding_error.lineno}, "
            f"column {decoding_error.colno}: {decoding_error.msg}"
            if isinstance(decoding_error, json.JSONDecodeError)
            else f"Invalid JSON: {decoding_error}"
        )
        return FlowCheckReport(
            diagnostics=(
                FlowDiagnostic(
                    code="FLOWSPEC_JSON_INVALID",
                    severity="error",
                    path="",
                    message=decoding_message,
                ),
            ),
            compilation=compilation,
        )
    return check_flow(
        flow_document,
        compile_document=compile_document,
        profile=profile,
    )
