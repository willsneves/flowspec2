"""Immutable machine-readable diagnostics for flowspec2 validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DiagnosticSeverity = Literal["warning", "error"]
CompilationStatus = Literal["not_requested", "skipped", "succeeded", "failed"]


def _require_json_pointer(path: str) -> None:
    if path and not path.startswith("/"):
        raise ValueError(f"diagnostic path must be a JSON Pointer, got {path!r}")


@dataclass(frozen=True)
class DiagnosticLocation:
    """A related source location expressed as an RFC 6901 JSON Pointer."""

    path: str
    message: str | None = None

    def __post_init__(self) -> None:
        _require_json_pointer(self.path)

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic JSON-compatible representation."""

        location: dict[str, object] = {"path": self.path}
        if self.message is not None:
            location["message"] = self.message
        return location


@dataclass(frozen=True)
class FlowDiagnostic:
    """One stable validation or compilation finding."""

    code: str
    severity: DiagnosticSeverity
    path: str
    message: str
    related_locations: tuple[DiagnosticLocation, ...] = ()
    suggested_fix: str | None = None

    def __post_init__(self) -> None:
        _require_json_pointer(self.path)

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic JSON-compatible representation."""

        diagnostic: dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }
        if self.related_locations:
            diagnostic["related_locations"] = [
                location.to_dict() for location in self.related_locations
            ]
        if self.suggested_fix is not None:
            diagnostic["suggested_fix"] = self.suggested_fix
        return diagnostic


@dataclass(frozen=True)
class FlowCheckReport:
    """The complete deterministic result of checking one flow document."""

    diagnostics: tuple[FlowDiagnostic, ...]
    compilation: CompilationStatus

    @property
    def has_errors(self) -> bool:
        return any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    @property
    def has_warnings(self) -> bool:
        return any(diagnostic.severity == "warning" for diagnostic in self.diagnostics)

    @property
    def is_valid(self) -> bool:
        return not self.has_errors

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic JSON-compatible representation."""

        return {
            "valid": self.is_valid,
            "compilation": self.compilation,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }
