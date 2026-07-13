"""Shared immutable contracts for compatibility conversions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

DiagnosticSeverity = Literal["warning", "error"]
Artifact = TypeVar("Artifact")


@dataclass(frozen=True)
class CompatibilityDiagnostic:
    """One stable, machine-readable compatibility finding."""

    severity: DiagnosticSeverity
    code: str
    source_path: str
    message: str


@dataclass(frozen=True)
class CompatibilityReport:
    """All findings produced by one source-to-target conversion."""

    source_format: str
    target_format: str
    diagnostics: tuple[CompatibilityDiagnostic, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    @property
    def has_warnings(self) -> bool:
        return any(diagnostic.severity == "warning" for diagnostic in self.diagnostics)

    def blocking_diagnostics(self, *, allow_lossy: bool) -> tuple[CompatibilityDiagnostic, ...]:
        """Return findings that block serialization under the requested policy."""

        return tuple(
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.severity == "error"
            or (diagnostic.severity == "warning" and not allow_lossy)
        )


@dataclass(frozen=True)
class ConversionOutcome(Generic[Artifact]):
    """A converted in-memory artifact and its complete compatibility report."""

    artifact: Artifact
    report: CompatibilityReport


class CompatibilityError(ValueError):
    """Raised when the selected policy blocks a compatibility conversion."""

    def __init__(
        self,
        report: CompatibilityReport,
        *,
        allow_lossy: bool,
    ) -> None:
        self.report = report
        self.allow_lossy = allow_lossy
        diagnostics = report.blocking_diagnostics(allow_lossy=allow_lossy)
        detail = "; ".join(
            f"{diagnostic.code} at {diagnostic.source_path}: {diagnostic.message}"
            for diagnostic in diagnostics
        )
        super().__init__(
            f"{report.source_format} -> {report.target_format} conversion blocked: {detail}"
        )


def enforce_compatibility_policy(report: CompatibilityReport, *, allow_lossy: bool) -> None:
    """Raise once with the complete report when the conversion is not permitted."""

    if report.blocking_diagnostics(allow_lossy=allow_lossy):
        raise CompatibilityError(report, allow_lossy=allow_lossy)
