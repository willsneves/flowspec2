"""Build and smoke-test the wheel using only locked runtime dependencies."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
EXPECTED_WHEEL_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "flowspec2/flowspec-2.schema.json",
        "flowspec2/experimental/flowspec-3-draft.schema.json",
        "flowspec2/compat/schemas/open-workflow-conversation-1.schema.json",
        "flowspec2/compat/schemas/vendor/open-workflow-1.0.3.LICENSE",
        "flowspec2/compat/schemas/vendor/open-workflow-1.0.3.provenance.json",
        "flowspec2/compat/schemas/vendor/open-workflow-1.0.3.workflow.yaml",
        "flowspec2/authoring/corpus/await_correction.case.json",
        "flowspec2/authoring/corpus/flowspec2.ctk.json",
        "flowspec2/authoring/corpus/gated_derive.case.json",
        "flowspec2/authoring/corpus/linear.case.json",
        "flowspec2/authoring/corpus/manifest.json",
        "flowspec2/authoring/corpus/subflow.case.json",
        "flowspec2/authoring/corpus/terminal.case.json",
    }
)
INSTALLED_PACKAGE_SMOKE: Final[str] = """
from flowspec2.authoring import load_reference_authoring_corpus
from flowspec2.experimental import V2_SCHEMA_IDENTIFIER, V3_PREVIEW_SCHEMA_IDENTIFIER, preview_schema
from flowspec2.schema import schema

reference_corpus = load_reference_authoring_corpus()
assert reference_corpus.cases
assert V2_SCHEMA_IDENTIFIER == "flowspec/2"
assert V3_PREVIEW_SCHEMA_IDENTIFIER == "flowspec/3-draft"
assert schema()["$id"] == "https://prefeitura.rio/flowspec/2.json"
assert preview_schema()["$id"] == "https://prefeitura.rio/flowspec/3-draft.json"
"""


def _run(command_arguments: Sequence[str]) -> None:
    subprocess.run(
        tuple(command_arguments),
        cwd=PROJECT_ROOT,
        check=True,
    )


def _venv_python(virtual_environment: Path) -> Path:
    unix_python = virtual_environment / "bin" / "python"
    return unix_python if unix_python.exists() else virtual_environment / "Scripts" / "python.exe"


def _built_wheel(artifact_directory: Path) -> Path:
    wheel_paths = tuple(artifact_directory.glob("flowspec2-*.whl"))
    if len(wheel_paths) != 1:
        raise RuntimeError(f"package check expected one wheel, found {len(wheel_paths)}")
    return wheel_paths[0]


def _check_wheel_members(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel_archive:
        wheel_members = tuple(wheel_archive.namelist())
    duplicate_members = sorted(
        member_name for member_name in set(wheel_members) if wheel_members.count(member_name) > 1
    )
    if duplicate_members:
        raise RuntimeError(f"wheel contains duplicate members: {duplicate_members}")
    missing_members = sorted(EXPECTED_WHEEL_MEMBERS - set(wheel_members))
    if missing_members:
        raise RuntimeError(f"wheel is missing packaged contracts: {missing_members}")


def check_package(python_version: str) -> None:
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        raise RuntimeError("package check requires uv on PATH")

    with tempfile.TemporaryDirectory(prefix="flowspec2-package-check-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        artifact_directory = temporary_path / "artifacts"
        requirements_path = temporary_path / "runtime-requirements.txt"
        virtual_environment = temporary_path / "venv"

        _run((uv_executable, "build", "--wheel", "--out-dir", str(artifact_directory)))
        wheel_path = _built_wheel(artifact_directory)
        _check_wheel_members(wheel_path)
        _run(
            (
                uv_executable,
                "export",
                "--quiet",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                str(requirements_path),
            )
        )
        _run((uv_executable, "venv", "--python", python_version, str(virtual_environment)))
        python_executable = _venv_python(virtual_environment)
        _run(
            (
                uv_executable,
                "pip",
                "install",
                "--python",
                str(python_executable),
                "--require-hashes",
                "--requirements",
                str(requirements_path),
            )
        )
        _run(
            (
                uv_executable,
                "pip",
                "install",
                "--python",
                str(python_executable),
                "--no-deps",
                str(wheel_path),
            )
        )
        _run((str(python_executable), "-I", "-c", INSTALLED_PACKAGE_SMOKE))


def main() -> None:
    argument_parser = argparse.ArgumentParser(
        description="Build and smoke-test the flowspec2 wheel from locked dependencies."
    )
    argument_parser.add_argument("--python-version", default="3.11")
    command_arguments = argument_parser.parse_args()
    check_package(command_arguments.python_version)


if __name__ == "__main__":
    main()
