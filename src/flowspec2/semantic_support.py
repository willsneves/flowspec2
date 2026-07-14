"""Shared diagnostic and JSON projection helpers for semantic linking."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .diagnostics import DiagnosticLocation, FlowDiagnostic


def json_pointer(*segments: str | int) -> str:
    return "".join(f"/{str(segment).replace('~', '~0').replace('/', '~1')}" for segment in segments)


def semantic_diagnostic(
    code: str,
    path: str,
    message: str,
    *,
    related: tuple[DiagnosticLocation, ...] = (),
    suggested_fix: str | None = None,
) -> FlowDiagnostic:
    return FlowDiagnostic(
        code=code,
        severity="error",
        path=path,
        message=message,
        related_locations=related,
        suggested_fix=suggested_fix,
    )


def mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: mutable_json(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [mutable_json(nested_value) for nested_value in value]
    if isinstance(value, list):
        return [mutable_json(nested_value) for nested_value in value]
    return value
