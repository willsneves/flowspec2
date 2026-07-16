"""Documentation section-marker and generated-TOC contracts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_PATHS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CODE_OF_CONDUCT.md",
    PROJECT_ROOT / "CONTRIBUTING.md",
    *sorted((PROJECT_ROOT / "docs").rglob("*.md")),
)
MAXIMUM_MARKER_DISTANCE_LINES = 10
SECTION_IDENTIFIER_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SECTION_OPEN_PATTERN = re.compile(rf"<!-- section:(?P<identifier>{SECTION_IDENTIFIER_PATTERN}) -->")
SECTION_CLOSE_PATTERN = re.compile(
    rf"<!-- /section:(?P<identifier>{SECTION_IDENTIFIER_PATTERN}) -->"
)
HEADING_PATTERN = re.compile(r"^(?P<markers>#{1,6}) (?P<title>.+)$")
TOC_ENTRY_PATTERN = re.compile(
    rf"^\s*- .+: (?P<line_number>\d+) "
    rf"<!-- section:(?P<identifier>{SECTION_IDENTIFIER_PATTERN}) -->$"
)


def _relative_documentation_path(documentation_path: Path) -> str:
    return str(documentation_path.relative_to(PROJECT_ROOT))


@pytest.mark.parametrize(
    "documentation_path",
    DOCUMENTATION_PATHS,
    ids=_relative_documentation_path,
)
def test_documentation_has_a_complete_generated_toc(documentation_path: Path) -> None:
    documentation_lines = documentation_path.read_text(encoding="utf-8").splitlines()
    assert documentation_lines[0] == "<!-- section:toc -->"
    toc_close_index = documentation_lines.index("<!-- /section:toc -->")
    assert documentation_lines[2] == "Table of Contents:"

    title_index = next(
        line_index
        for line_index in range(toc_close_index + 1, len(documentation_lines))
        if documentation_lines[line_index]
    )
    assert documentation_lines[title_index].startswith("# ")
    assert (
        len([line for line in documentation_lines[toc_close_index + 1 :] if line.startswith("# ")])
        == 1
    )

    section_stack: list[str] = []
    section_open_line_numbers: dict[str, int] = {}
    heading_line_numbers: dict[str, int] = {}
    for line_number, documentation_line in enumerate(
        documentation_lines[toc_close_index + 1 :],
        start=toc_close_index + 2,
    ):
        if section_open_match := SECTION_OPEN_PATTERN.fullmatch(documentation_line):
            section_identifier = section_open_match.group("identifier")
            assert section_identifier not in section_open_line_numbers
            if section_stack:
                assert section_identifier.startswith(f"{section_stack[-1]}-")
            section_stack.append(section_identifier)
            section_open_line_numbers[section_identifier] = line_number
            continue
        if section_close_match := SECTION_CLOSE_PATTERN.fullmatch(documentation_line):
            section_identifier = section_close_match.group("identifier")
            assert section_stack and section_stack[-1] == section_identifier
            section_stack.pop()
            continue
        if heading_match := HEADING_PATTERN.fullmatch(documentation_line):
            if len(heading_match.group("markers")) == 1:
                continue
            assert section_stack
            section_identifier = section_stack[-1]
            assert line_number - section_open_line_numbers[section_identifier] <= (
                MAXIMUM_MARKER_DISTANCE_LINES
            )
            assert section_identifier not in heading_line_numbers
            heading_line_numbers[section_identifier] = line_number

    assert not section_stack

    toc_entries = [
        (
            toc_entry_match.group("identifier"),
            int(toc_entry_match.group("line_number")),
        )
        for documentation_line in documentation_lines[:toc_close_index]
        if (toc_entry_match := TOC_ENTRY_PATTERN.fullmatch(documentation_line))
    ]
    toc_line_numbers = dict(toc_entries)
    assert len(toc_entries) == len(toc_line_numbers)
    assert toc_line_numbers == heading_line_numbers
