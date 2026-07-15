"""Build and verify package artifacts using locked runtime dependencies."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CHECKSUM_MANIFEST_NAME: Final[str] = "SHA256SUMS"
PACKAGE_NAME: Final[str] = "flowspec2"
AT_FDCWD: Final[int] = -100
RENAME_EXCL: Final[int] = 0x00000004
RENAME_NOREPLACE: Final[int] = 0x00000001
VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXPECTED_WHEEL_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "flowspec2/flowspec-2.schema.json",
        "flowspec2/py.typed",
        "flowspec2/authoring/authoring-evidence.schema.json",
        "flowspec2/authoring/authoring-evidence-signature.schema.json",
        "flowspec2/authoring/authoring-operational-evidence.schema.json",
        "flowspec2/authoring/operational-corpus.json",
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
EXPECTED_SDIST_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        *(f"src/{member_name}" for member_name in EXPECTED_WHEEL_MEMBERS),
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "docs/VERSIONING.md",
        "pyproject.toml",
        "uv.lock",
    }
)
INSTALLED_PACKAGE_SMOKE: Final[str] = """
import sys
from importlib.metadata import version as installed_package_version

from flowspec2 import CodexAgent, __version__
from flowspec2.authoring import (
    CodexAuthor,
    OPERATIONAL_EVIDENCE_CLASSIFICATION,
    PRESENTATION_REVIEW_FORMAT,
    authoring_evidence_schema,
    authoring_evidence_signature_schema,
    authoring_operational_evidence_schema,
    authoring_presentation_review_signature_schema,
    load_reference_authoring_corpus,
    load_reference_operational_corpus,
    presentation_review_schema,
)
from flowspec2.experimental import V2_SCHEMA_IDENTIFIER, V3_PREVIEW_SCHEMA_IDENTIFIER, preview_schema
from flowspec2.schema import schema

expected_version = sys.argv[1]
reference_corpus = load_reference_authoring_corpus()
operational_corpus = load_reference_operational_corpus()
assert __version__ == expected_version
assert installed_package_version("flowspec2") == expected_version
assert CodexAgent.__name__ == "CodexAgent"
assert CodexAuthor.__name__ == "CodexAuthor"
assert reference_corpus.cases
assert operational_corpus.probes
assert authoring_evidence_schema()["$id"] == "https://prefeitura.rio/flowspec2/authoring-benchmark-evidence-2.json"
assert authoring_evidence_signature_schema()["$id"] == "https://prefeitura.rio/flowspec2/authoring-evidence-signature-1.json"
assert OPERATIONAL_EVIDENCE_CLASSIFICATION == "report_only"
assert authoring_operational_evidence_schema()["$id"] == "https://prefeitura.rio/flowspec2/authoring-operational-evidence-2.json"
assert PRESENTATION_REVIEW_FORMAT == "flowspec2/authoring-presentation-review@1"
assert presentation_review_schema()["$id"] == "https://prefeitura.rio/flowspec2/authoring-presentation-review-1.json"
assert authoring_presentation_review_signature_schema()["$id"] == "https://prefeitura.rio/flowspec2/authoring-presentation-review-signature-1.json"
assert V2_SCHEMA_IDENTIFIER == "flowspec/2"
assert V3_PREVIEW_SCHEMA_IDENTIFIER == "flowspec/3-draft"
assert schema()["$id"] == "https://prefeitura.rio/flowspec/2.json"
assert preview_schema()["$id"] == "https://prefeitura.rio/flowspec/3-draft.json"
"""


def _run(command_arguments: Sequence[str], *, working_directory: Path = PROJECT_ROOT) -> None:
    subprocess.run(
        tuple(command_arguments),
        cwd=working_directory,
        check=True,
    )


def _run_output(command_arguments: Sequence[str]) -> str:
    completed_process = subprocess.run(
        tuple(command_arguments),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed_process.stdout.strip()


def _uv_executable() -> str:
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        raise RuntimeError("package check requires uv on PATH")
    return uv_executable


def _venv_python(virtual_environment: Path) -> Path:
    unix_python = virtual_environment / "bin" / "python"
    return unix_python if unix_python.exists() else virtual_environment / "Scripts" / "python.exe"


def _project_version(project_directory: Path) -> str:
    with (project_directory / "pyproject.toml").open("rb") as project_file:
        project_document = tomllib.load(project_file)
    package_version = project_document.get("project", {}).get("version")
    if not isinstance(package_version, str) or VERSION_PATTERN.fullmatch(package_version) is None:
        raise RuntimeError("pyproject.toml must declare a valid Semantic Version")
    return package_version


def _archive_names(expected_version: str) -> tuple[str, str]:
    return (
        f"{PACKAGE_NAME}-{expected_version}-py3-none-any.whl",
        f"{PACKAGE_NAME}-{expected_version}.tar.gz",
    )


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _require_regular_file(file_path: Path) -> None:
    try:
        file_mode = file_path.lstat().st_mode
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required release artifact does not exist: {file_path}") from error
    if not stat.S_ISREG(file_mode):
        raise RuntimeError(f"release artifact must be a regular file: {file_path}")


def _artifact_paths(
    artifact_directory: Path,
    expected_version: str,
    *,
    include_manifest: bool,
) -> tuple[Path, Path]:
    if artifact_directory.is_symlink() or not artifact_directory.is_dir():
        raise FileNotFoundError(
            f"artifact directory must be an existing regular directory: {artifact_directory}"
        )
    wheel_name, source_distribution_name = _archive_names(expected_version)
    expected_names = {wheel_name, source_distribution_name}
    if include_manifest:
        expected_names.add(CHECKSUM_MANIFEST_NAME)
    actual_names = {directory_entry.name for directory_entry in artifact_directory.iterdir()}
    if actual_names != expected_names:
        missing_names = sorted(expected_names - actual_names)
        extra_names = sorted(actual_names - expected_names)
        raise RuntimeError(
            "release artifact directory is not a closed set: "
            f"missing={missing_names}, extra={extra_names}"
        )
    wheel_path = artifact_directory / wheel_name
    source_distribution_path = artifact_directory / source_distribution_name
    for artifact_path in (wheel_path, source_distribution_path):
        _require_regular_file(artifact_path)
    if include_manifest:
        _require_regular_file(artifact_directory / CHECKSUM_MANIFEST_NAME)
    return wheel_path, source_distribution_path


def _checksum_manifest_content(wheel_path: Path, source_distribution_path: Path) -> str:
    return "".join(
        f"{hashlib.sha256(artifact_path.read_bytes()).hexdigest()}  {artifact_path.name}\n"
        for artifact_path in sorted(
            (wheel_path, source_distribution_path),
            key=lambda candidate_path: candidate_path.name,
        )
    )


def _write_checksum_manifest(artifact_directory: Path, expected_version: str) -> None:
    wheel_path, source_distribution_path = _artifact_paths(
        artifact_directory,
        expected_version,
        include_manifest=False,
    )
    checksum_path = artifact_directory / CHECKSUM_MANIFEST_NAME
    checksum_path.write_text(
        _checksum_manifest_content(wheel_path, source_distribution_path),
        encoding="utf-8",
    )


def _verify_checksum_manifest(
    artifact_directory: Path,
    expected_version: str,
) -> tuple[Path, Path]:
    wheel_path, source_distribution_path = _artifact_paths(
        artifact_directory,
        expected_version,
        include_manifest=True,
    )
    checksum_path = artifact_directory / CHECKSUM_MANIFEST_NAME
    try:
        recorded_manifest = checksum_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("checksum manifest must be canonical UTF-8") from error
    expected_manifest = _checksum_manifest_content(wheel_path, source_distribution_path)
    if recorded_manifest != expected_manifest:
        raise RuntimeError("package artifact checksum manifest is not canonical or does not match")
    return wheel_path, source_distribution_path


def _metadata_version(metadata_text: str, metadata_source: str) -> str:
    metadata_document = Parser().parsestr(metadata_text)
    package_name = metadata_document.get("Name")
    package_version = metadata_document.get("Version")
    if package_name != PACKAGE_NAME or not isinstance(package_version, str):
        raise RuntimeError(f"{metadata_source} has invalid package identity metadata")
    return package_version


def _check_wheel(wheel_path: Path, expected_version: str) -> None:
    metadata_member = f"{PACKAGE_NAME}-{expected_version}.dist-info/METADATA"
    with zipfile.ZipFile(wheel_path) as wheel_archive:
        wheel_entries = tuple(wheel_archive.infolist())
        wheel_members = tuple(wheel_entry.filename for wheel_entry in wheel_entries)
        duplicate_members = sorted(
            member_name
            for member_name in set(wheel_members)
            if wheel_members.count(member_name) > 1
        )
        if duplicate_members:
            raise RuntimeError(f"wheel contains duplicate members: {duplicate_members}")
        allowed_roots = {PACKAGE_NAME, f"{PACKAGE_NAME}-{expected_version}.dist-info"}
        for wheel_entry in wheel_entries:
            member_name = wheel_entry.filename
            normalized_name = member_name[:-1] if wheel_entry.is_dir() else member_name
            path_parts = normalized_name.split("/")
            file_type = stat.S_IFMT(wheel_entry.external_attr >> 16)
            if (
                not normalized_name
                or "\\" in member_name
                or any(path_part in {"", ".", ".."} for path_part in path_parts)
                or path_parts[0] not in allowed_roots
                or (wheel_entry.is_dir() and file_type not in {0, stat.S_IFDIR})
                or (not wheel_entry.is_dir() and file_type not in {0, stat.S_IFREG})
            ):
                raise RuntimeError(f"wheel contains an unsafe member: {member_name}")
        missing_members = sorted(EXPECTED_WHEEL_MEMBERS - set(wheel_members))
        if missing_members:
            raise RuntimeError(f"wheel is missing packaged contracts: {missing_members}")
        try:
            metadata_text = wheel_archive.read(metadata_member).decode("utf-8")
        except KeyError as error:
            raise RuntimeError(f"wheel is missing metadata: {metadata_member}") from error
    if _metadata_version(metadata_text, "wheel") != expected_version:
        raise RuntimeError("wheel metadata version does not match the expected release version")


def _validated_sdist_members(
    source_archive: tarfile.TarFile,
    expected_version: str,
) -> tuple[tarfile.TarInfo, ...]:
    archive_members = tuple(source_archive.getmembers())
    expected_root = f"{PACKAGE_NAME}-{expected_version}"
    member_names: list[str] = []
    for archive_member in archive_members:
        member_path = PurePosixPath(archive_member.name)
        if (
            member_path.is_absolute()
            or "\\" in archive_member.name
            or any(path_part in {"", ".", ".."} for path_part in member_path.parts)
            or not member_path.parts
            or member_path.parts[0] != expected_root
        ):
            raise RuntimeError(
                f"source distribution contains an unsafe member: {archive_member.name}"
            )
        is_documentation_alias = (
            archive_member.issym()
            and archive_member.name == f"{expected_root}/CLAUDE.md"
            and archive_member.linkname == "AGENTS.md"
        )
        if not (archive_member.isdir() or archive_member.isfile() or is_documentation_alias):
            raise RuntimeError(
                f"source distribution contains a non-regular member: {archive_member.name}"
            )
        member_names.append(archive_member.name)
    duplicate_members = sorted(
        member_name for member_name in set(member_names) if member_names.count(member_name) > 1
    )
    if duplicate_members:
        raise RuntimeError(f"source distribution contains duplicate members: {duplicate_members}")
    project_members = {
        member_name.split("/", maxsplit=1)[1] for member_name in member_names if "/" in member_name
    }
    missing_members = sorted(EXPECTED_SDIST_MEMBERS - project_members)
    if missing_members:
        raise RuntimeError(f"source distribution is missing packaged contracts: {missing_members}")
    return archive_members


def _read_sdist_member(
    source_archive: tarfile.TarFile,
    member_name: str,
) -> bytes:
    extracted_file = source_archive.extractfile(member_name)
    if extracted_file is None:
        raise RuntimeError(f"source distribution member is not a regular file: {member_name}")
    return extracted_file.read()


def _check_sdist(source_distribution_path: Path, expected_version: str) -> None:
    expected_root = f"{PACKAGE_NAME}-{expected_version}"
    with tarfile.open(source_distribution_path, mode="r:gz") as source_archive:
        _validated_sdist_members(source_archive, expected_version)
        package_metadata = _read_sdist_member(source_archive, f"{expected_root}/PKG-INFO").decode(
            "utf-8"
        )
        project_document = tomllib.loads(
            _read_sdist_member(source_archive, f"{expected_root}/pyproject.toml").decode("utf-8")
        )
    if _metadata_version(package_metadata, "source distribution") != expected_version:
        raise RuntimeError(
            "source distribution metadata version does not match the expected release version"
        )
    if project_document.get("project", {}).get("version") != expected_version:
        raise RuntimeError(
            "source distribution project version does not match the expected release version"
        )


def _extract_sdist(
    source_distribution_path: Path,
    extraction_directory: Path,
    expected_version: str,
) -> Path:
    expected_root = f"{PACKAGE_NAME}-{expected_version}"
    with tarfile.open(source_distribution_path, mode="r:gz") as source_archive:
        archive_members = _validated_sdist_members(source_archive, expected_version)
        for archive_member in archive_members:
            relative_path = PurePosixPath(archive_member.name)
            destination_path = extraction_directory.joinpath(*relative_path.parts)
            if archive_member.isdir():
                destination_path.mkdir(parents=True, exist_ok=True)
                continue
            if archive_member.issym():
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            extracted_file = source_archive.extractfile(archive_member)
            if extracted_file is None:
                raise RuntimeError(
                    f"source distribution member is not readable: {archive_member.name}"
                )
            with destination_path.open("xb") as destination_file:
                shutil.copyfileobj(extracted_file, destination_file)
    return extraction_directory / expected_root


def _install_and_smoke(
    uv_executable: str,
    python_version: str,
    requirements_path: Path,
    build_constraints_path: Path,
    package_artifact: Path,
    virtual_environment: Path,
    expected_version: str,
) -> None:
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
            "--build-constraints",
            str(build_constraints_path),
            str(package_artifact),
        )
    )
    _run(
        (
            str(python_executable),
            "-I",
            "-c",
            INSTALLED_PACKAGE_SMOKE,
            expected_version,
        )
    )


def _check_artifacts(
    artifact_directory: Path,
    python_version: str,
    expected_version: str,
    *,
    require_checksums: bool,
) -> None:
    if require_checksums:
        wheel_path, source_distribution_path = _verify_checksum_manifest(
            artifact_directory,
            expected_version,
        )
    else:
        wheel_path, source_distribution_path = _artifact_paths(
            artifact_directory,
            expected_version,
            include_manifest=False,
        )
    _check_wheel(wheel_path, expected_version)
    _check_sdist(source_distribution_path, expected_version)

    uv_executable = _uv_executable()
    with tempfile.TemporaryDirectory(prefix="flowspec2-package-install-check-") as temporary_name:
        temporary_directory = Path(temporary_name)
        extracted_project = _extract_sdist(
            source_distribution_path,
            temporary_directory / "source",
            expected_version,
        )
        requirements_path = temporary_directory / "runtime-requirements.txt"
        _run(
            (
                uv_executable,
                "export",
                "--quiet",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--project",
                str(extracted_project),
                "--output-file",
                str(requirements_path),
            )
        )
        build_constraints_path = temporary_directory / "build-constraints.txt"
        _run(
            (
                uv_executable,
                "export",
                "--quiet",
                "--locked",
                "--only-group",
                "build",
                "--no-emit-project",
                "--project",
                str(extracted_project),
                "--output-file",
                str(build_constraints_path),
            )
        )
        _install_and_smoke(
            uv_executable,
            python_version,
            requirements_path,
            build_constraints_path,
            wheel_path,
            temporary_directory / "wheel-venv",
            expected_version,
        )
        _install_and_smoke(
            uv_executable,
            python_version,
            requirements_path,
            build_constraints_path,
            source_distribution_path,
            temporary_directory / "sdist-venv",
            expected_version,
        )


def _build_artifacts(artifact_directory: Path) -> None:
    if _path_exists(artifact_directory):
        raise FileExistsError(f"artifact directory already exists: {artifact_directory}")
    artifact_directory.mkdir(parents=True)
    uv_executable = _uv_executable()
    with tempfile.TemporaryDirectory(prefix="flowspec2-build-constraints-") as temporary_name:
        build_constraints_path = Path(temporary_name) / "build-constraints.txt"
        _run(
            (
                uv_executable,
                "export",
                "--quiet",
                "--locked",
                "--only-group",
                "build",
                "--no-emit-project",
                "--output-file",
                str(build_constraints_path),
            )
        )
        _run(
            (
                uv_executable,
                "build",
                "--no-create-gitignore",
                "--build-constraints",
                str(build_constraints_path),
                "--require-hashes",
                "--out-dir",
                str(artifact_directory),
            )
        )


def _verify_release_source(expected_version: str) -> None:
    if _project_version(PROJECT_ROOT) != expected_version:
        raise RuntimeError("checkout package version does not match the expected release version")
    if _run_output(("git", "status", "--porcelain", "--untracked-files=all")):
        raise RuntimeError("release artifacts require a clean Git checkout")
    head_commit = _run_output(("git", "rev-parse", "HEAD"))
    try:
        tagged_commit = _run_output(
            ("git", "rev-parse", "--verify", f"refs/tags/v{expected_version}^{{commit}}")
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"release tag v{expected_version} does not exist") from error
    if head_commit != tagged_commit:
        raise RuntimeError(f"HEAD is not the immutable v{expected_version} release commit")


def _rename_directory_no_replace(staging_directory: Path, artifact_directory: Path) -> None:
    if _path_exists(artifact_directory):
        raise FileExistsError(f"artifact directory already exists: {artifact_directory}")
    if sys.platform == "win32":
        os.rename(staging_directory, artifact_directory)
        return
    system_library = ctypes.CDLL(None, use_errno=True)
    source_path = os.fsencode(staging_directory)
    destination_path = os.fsencode(artifact_directory)
    if sys.platform == "darwin":
        rename_function = system_library.renamex_np
        rename_function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename_function.restype = ctypes.c_int
        rename_status = rename_function(source_path, destination_path, RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        rename_function = system_library.renameat2
        rename_function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_function.restype = ctypes.c_int
        rename_status = rename_function(
            AT_FDCWD,
            source_path,
            AT_FDCWD,
            destination_path,
            RENAME_NOREPLACE,
        )
    else:
        raise RuntimeError("atomic no-replace release promotion is unsupported on this platform")
    if rename_status == 0:
        return
    rename_error = ctypes.get_errno()
    if rename_error == errno.EEXIST:
        raise FileExistsError(f"artifact directory already exists: {artifact_directory}")
    raise OSError(rename_error, os.strerror(rename_error), artifact_directory)


def _promote_release_directory(staging_directory: Path, artifact_directory: Path) -> None:
    _rename_directory_no_replace(staging_directory, artifact_directory)


def check_package(python_version: str, expected_version: str) -> None:
    """Build in temporary storage and verify both distribution formats."""

    with tempfile.TemporaryDirectory(prefix="flowspec2-package-check-") as temporary_name:
        artifact_directory = Path(temporary_name) / "artifacts"
        _build_artifacts(artifact_directory)
        _check_artifacts(
            artifact_directory,
            python_version,
            expected_version,
            require_checksums=False,
        )


def build_release_artifacts(
    artifact_directory: Path,
    python_version: str,
    expected_version: str,
) -> None:
    """Build and verify one release set before publishing it locally."""

    if _path_exists(artifact_directory):
        raise FileExistsError(f"artifact directory already exists: {artifact_directory}")
    _verify_release_source(expected_version)
    artifact_directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{artifact_directory.name}-staging-",
        dir=artifact_directory.parent,
    ) as staging_name:
        staging_directory = Path(staging_name) / "artifacts"
        _build_artifacts(staging_directory)
        _check_artifacts(
            staging_directory,
            python_version,
            expected_version,
            require_checksums=False,
        )
        _write_checksum_manifest(staging_directory, expected_version)
        _verify_checksum_manifest(staging_directory, expected_version)
        _promote_release_directory(staging_directory, artifact_directory)


def check_release_artifacts(
    artifact_directory: Path,
    python_version: str,
    expected_version: str,
) -> None:
    """Verify a closed checksummed release set without rebuilding it."""

    _verify_release_source(expected_version)
    _check_artifacts(
        artifact_directory,
        python_version,
        expected_version,
        require_checksums=True,
    )


def main() -> None:
    argument_parser = argparse.ArgumentParser(
        description="Build or verify flowspec2 wheel and source-distribution artifacts."
    )
    argument_parser.add_argument("--python-version", default="3.11")
    argument_parser.add_argument(
        "--artifact-directory",
        type=Path,
        help="verify artifacts in this persistent project-relative directory",
    )
    argument_parser.add_argument(
        "--build",
        action="store_true",
        help="build once from a clean matching release tag into --artifact-directory",
    )
    command_arguments = argument_parser.parse_args()
    artifact_directory = command_arguments.artifact_directory
    if command_arguments.build and artifact_directory is None:
        argument_parser.error("--build requires --artifact-directory")
    expected_version = _project_version(PROJECT_ROOT)
    if artifact_directory is None:
        check_package(command_arguments.python_version, expected_version)
        return
    resolved_artifact_directory = (
        artifact_directory
        if artifact_directory.is_absolute()
        else PROJECT_ROOT / artifact_directory
    )
    if command_arguments.build:
        build_release_artifacts(
            resolved_artifact_directory,
            command_arguments.python_version,
            expected_version,
        )
        return
    check_release_artifacts(
        resolved_artifact_directory,
        command_arguments.python_version,
        expected_version,
    )


if __name__ == "__main__":
    main()
