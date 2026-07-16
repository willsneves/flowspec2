"""Repository-wide contract for English-only maintained content."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENCODED_FORBIDDEN_MARKERS = (
    "cmVwYXJv",
    "bHVtaW7DoXJpYQ==",
    "bHVtaW5hcmlh",
    "YnVyYWNv",
    "ZGVmZWl0bw==",
    "cXVhbnRpZGFkZQ==",
    "ZW5kZXJlw6dv",
    "ZW5kZXJlY28=",
    "YmFpcnJv",
    "bXVuaWPDrXBpbw==",
    "bXVuaWNpcGlv",
    "Y29uZmlybWHDp8Ojbw==",
    "Y29uZmlybWFjYW8=",
    "Y29ycmXDp8Ojbw==",
    "Y29ycmVjYW8=",
    "cHJvdG9jb2xv",
    "cHJhem8=",
    "cmVzdW1v",
    "cHVsYXI=",
    "bmVuaHVt",
    "cGFzc2Fy",
    "cHJhw6dh",
    "cHJhY2E=",
    "cXVhZHJh",
    "cnVh",
    "YXZlbmlkYQ==",
    "c29saWNpdGFudGU=",
    "YW7DtG5pbW8=",
    "YW5vbmltbw==",
    "aWRlbnRpZmljYcOnw6Nv",
    "aWRlbnRpZmljYWNhbw==",
    "YXV0ZW50aWNhw6fDo28=",
    "YXV0ZW50aWNhY2Fv",
    "c2VydmnDp28=",
    "c2Vydmljbw==",
    "Y2lkYWTDo28=",
    "Y2lkYWRhbw==",
    "YXBhZ2FkYQ==",
    "cGlzY2FuZG8=",
    "cGVuZHVyYWRh",
    "ZGFuaWZpY2FkYQ==",
    "cnXDrWRv",
    "cnVpZG8=",
    "cGVxdWVubw==",
    "bcOpZGlv",
    "bWVkaW8=",
    "Z3JhbmRl",
    "Y3JhdGVyYQ==",
    "YXNmYWx0bw==",
    "Y2Fsw6dhZGE=",
    "Y2FsY2FkYQ==",
    "dGVycmVubw==",
    "bWF0bw==",
    "bGl4bw==",
    "ZGVuw7puY2lh",
    "ZGVudW5jaWE=",
    "cGFnYW1lbnRv",
    "cHJlZmlybw==",
    "YWluZGE=",
    "cHJlZW5jaGVuZG8=",
    "ZG9tw61uaW8=",
    "ZG9taW5pbw==",
    "dGFtYsOpbQ==",
    "dGFtYmVt",
    "b2JyaWdhdMOzcmlv",
    "b2JyaWdhdG9yaW8=",
    "aW52w6FsaWRv",
    "aW52YWxpZG8=",
    "YXRlbmRlbnRl",
    "ZW5jYW1pbmhhcg==",
    "Y2FuY2VsYWRv",
    "dm9jw6o=",
    "dm9jZQ==",
    "ZXNjb2xoYQ==",
    "aW5mb3JtZQ==",
    "c2VsZWNpb25l",
    "c2lt",
    "bsOjbw==",
    "bmFv",
    "Y3Bm",
    "c2dyYw==",
    "Y2xhc3NpZmljYV9kZWZlaXRv",
)
FORBIDDEN_MARKERS = tuple(
    base64.b64decode(encoded_marker).decode("utf-8") for encoded_marker in ENCODED_FORBIDDEN_MARKERS
)
FORBIDDEN_MARKER_PATTERN = re.compile(
    rf"\b(?:{'|'.join(re.escape(marker) for marker in FORBIDDEN_MARKERS)})\b",
    flags=re.IGNORECASE,
)
LATIN_DIACRITIC_PATTERN = re.compile(r"[\u00c0-\u00ff]")


def _repository_paths() -> tuple[Path, ...]:
    completed_process = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        REPOSITORY_ROOT / relative_path
        for relative_path in completed_process.stdout.decode("utf-8").split("\0")
        if relative_path and (REPOSITORY_ROOT / relative_path).is_file()
    )


def _utf8_text(tracked_path: Path) -> str | None:
    try:
        return tracked_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def test_tracked_paths_and_text_are_english_only() -> None:
    violations: list[str] = []
    for tracked_path in _repository_paths():
        relative_path = tracked_path.relative_to(REPOSITORY_ROOT).as_posix()
        if FORBIDDEN_MARKER_PATTERN.search(relative_path):
            violations.append(f"{relative_path}: forbidden language marker in path")
        if (tracked_text := _utf8_text(tracked_path)) is None:
            continue
        for line_number, line in enumerate(tracked_text.splitlines(), start=1):
            if FORBIDDEN_MARKER_PATTERN.search(line) or LATIN_DIACRITIC_PATTERN.search(line):
                violations.append(f"{relative_path}:{line_number}: non-English maintained text")

    assert not violations, "\n".join(violations)
