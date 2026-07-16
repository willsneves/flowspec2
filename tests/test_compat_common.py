"""Shared compatibility diagnostics and safe YAML contracts."""

from __future__ import annotations

import pytest

from flowspec2.compat.models import (
    CompatibilityDiagnostic,
    CompatibilityError,
    CompatibilityReport,
    enforce_compatibility_policy,
)
from flowspec2.compat.yaml import (
    DuplicateYamlKeyError,
    YamlDocumentError,
    dumps_yaml_mapping,
    loads_yaml_mapping,
)


def test_strict_policy_reports_every_blocking_diagnostic() -> None:
    report = CompatibilityReport(
        source_format="source",
        target_format="target",
        diagnostics=(
            CompatibilityDiagnostic("warning", "LOSS", "/route", "route changed"),
            CompatibilityDiagnostic("error", "BROKEN", "/path/0", "step unsupported"),
        ),
    )

    with pytest.raises(CompatibilityError) as raised:
        enforce_compatibility_policy(report, allow_lossy=False)

    assert raised.value.report is report
    assert "LOSS at /route" in str(raised.value)
    assert "BROKEN at /path/0" in str(raised.value)


def test_lossy_policy_permits_warnings_but_never_errors() -> None:
    warning_report = CompatibilityReport(
        source_format="source",
        target_format="target",
        diagnostics=(CompatibilityDiagnostic("warning", "LOSS", "/", "changed"),),
    )
    enforce_compatibility_policy(warning_report, allow_lossy=True)

    error_report = CompatibilityReport(
        source_format="source",
        target_format="target",
        diagnostics=(CompatibilityDiagnostic("error", "BROKEN", "/", "invalid"),),
    )
    with pytest.raises(CompatibilityError):
        enforce_compatibility_policy(error_report, allow_lossy=True)


def test_yaml_loader_rejects_duplicate_keys() -> None:
    with pytest.raises(DuplicateYamlKeyError, match="duplicate YAML key 'flow'"):
        loads_yaml_mapping("flow: first\nflow: second\n")


@pytest.mark.parametrize("document", ["", "null\n", "- flow\n"])
def test_yaml_loader_requires_non_empty_mapping(document: str) -> None:
    with pytest.raises(YamlDocumentError, match="non-empty mapping"):
        loads_yaml_mapping(document)


def test_yaml_dump_is_unicode_preserving_and_stable() -> None:
    dumped = dumps_yaml_mapping({"flows": {"lighting": {"description": "Streetlight"}}})

    assert dumped.startswith("flows:\n")
    assert "Streetlight" in dumped
    assert loads_yaml_mapping(dumped)["flows"]["lighting"]["description"] == "Streetlight"


def test_yaml_loader_uses_yaml_1_2_boolean_tokens() -> None:
    document = loads_yaml_mapping("values: [yes, no, on, off, true, false]\n")

    assert document["values"] == ["yes", "no", "on", "off", True, False]


def test_yaml_loader_uses_yaml_1_2_numeric_and_string_tokens() -> None:
    document = loads_yaml_mapping("values: [12:34, 012, 0o12, 0x12, 1e3, 2026-07-13]\n")

    assert document["values"] == ["12:34", 12, 10, 18, 1000.0, "2026-07-13"]
