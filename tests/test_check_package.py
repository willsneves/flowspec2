"""Build-once package artifact verification tests."""

from __future__ import annotations

import importlib.util
import io
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, call

import pytest

EXPECTED_VERSION = "0.3.0"


def _load_package_check() -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / "check_package.py"
    module_specification = importlib.util.spec_from_file_location("check_package", script_path)
    if module_specification is None or module_specification.loader is None:
        raise RuntimeError(f"could not load package-check script: {script_path}")
    package_check_module = importlib.util.module_from_spec(module_specification)
    module_specification.loader.exec_module(package_check_module)
    return package_check_module


package_check = _load_package_check()


def _artifact_directory(directory_path: Path) -> Path:
    artifact_directory = directory_path / "artifacts"
    artifact_directory.mkdir()
    wheel_name, source_distribution_name = package_check._archive_names(EXPECTED_VERSION)
    (artifact_directory / wheel_name).write_bytes(b"wheel")
    (artifact_directory / source_distribution_name).write_bytes(b"source distribution")
    return artifact_directory


def _add_tar_member(
    source_archive: tarfile.TarFile,
    member_name: str,
    member_content: bytes,
) -> None:
    archive_member = tarfile.TarInfo(member_name)
    archive_member.size = len(member_content)
    source_archive.addfile(archive_member, io.BytesIO(member_content))


def _minimal_wheel(wheel_path: Path, metadata_version: str) -> None:
    metadata_member = f"flowspec2-{EXPECTED_VERSION}.dist-info/METADATA"
    with zipfile.ZipFile(wheel_path, mode="w") as wheel_archive:
        for member_name in package_check.EXPECTED_WHEEL_MEMBERS:
            wheel_archive.writestr(member_name, "contract")
        wheel_archive.writestr(
            metadata_member,
            f"Metadata-Version: 2.4\nName: flowspec2\nVersion: {metadata_version}\n",
        )


def _minimal_sdist(source_distribution_path: Path, metadata_version: str) -> None:
    source_root = f"flowspec2-{EXPECTED_VERSION}"
    project_document = (f'[project]\nname = "flowspec2"\nversion = "{EXPECTED_VERSION}"\n').encode()
    with tarfile.open(source_distribution_path, mode="w:gz") as source_archive:
        for member_name in package_check.EXPECTED_SDIST_MEMBERS:
            member_content = project_document if member_name == "pyproject.toml" else b"contract"
            _add_tar_member(source_archive, f"{source_root}/{member_name}", member_content)
        _add_tar_member(
            source_archive,
            f"{source_root}/PKG-INFO",
            (f"Metadata-Version: 2.4\nName: flowspec2\nVersion: {metadata_version}\n").encode(),
        )


def test_checksum_manifest_is_canonical_and_detects_tampering(tmp_path: Path) -> None:
    artifact_directory = _artifact_directory(tmp_path)

    package_check._write_checksum_manifest(artifact_directory, EXPECTED_VERSION)
    checksum_path = artifact_directory / package_check.CHECKSUM_MANIFEST_NAME
    recorded_manifest = checksum_path.read_text(encoding="utf-8")
    wheel_path, source_distribution_path = package_check._verify_checksum_manifest(
        artifact_directory,
        EXPECTED_VERSION,
    )

    assert recorded_manifest == package_check._checksum_manifest_content(
        wheel_path,
        source_distribution_path,
    )
    assert [line.split("  ", maxsplit=1)[1] for line in recorded_manifest.splitlines()] == [
        "flowspec2-0.3.0-py3-none-any.whl",
        "flowspec2-0.3.0.tar.gz",
    ]
    source_distribution_path.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum manifest"):
        package_check._verify_checksum_manifest(artifact_directory, EXPECTED_VERSION)


@pytest.mark.parametrize(
    "invalid_manifest",
    [
        "0" * 64 + "  ../flowspec2-0.3.0.tar.gz\n",
        "0" * 64 + " *flowspec2-0.3.0.tar.gz\n",
        "0" * 64 + "  flowspec2-0.3.0.tar.gz\n\n",
        "A" * 64 + "  flowspec2-0.3.0.tar.gz\n",
    ],
)
def test_checksum_manifest_rejects_noncanonical_grammar(
    tmp_path: Path,
    invalid_manifest: str,
) -> None:
    artifact_directory = _artifact_directory(tmp_path)
    (artifact_directory / package_check.CHECKSUM_MANIFEST_NAME).write_text(
        invalid_manifest,
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="checksum manifest"):
        package_check._verify_checksum_manifest(artifact_directory, EXPECTED_VERSION)


def test_release_set_rejects_extra_files(tmp_path: Path) -> None:
    artifact_directory = _artifact_directory(tmp_path)
    package_check._write_checksum_manifest(artifact_directory, EXPECTED_VERSION)
    (artifact_directory / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(RuntimeError, match="closed set"):
        package_check._verify_checksum_manifest(artifact_directory, EXPECTED_VERSION)


def test_release_set_rejects_symlinked_artifact(tmp_path: Path) -> None:
    artifact_directory = _artifact_directory(tmp_path)
    wheel_name, _source_distribution_name = package_check._archive_names(EXPECTED_VERSION)
    wheel_path = artifact_directory / wheel_name
    wheel_target = tmp_path / "wheel-target"
    wheel_target.write_bytes(wheel_path.read_bytes())
    wheel_path.unlink()
    wheel_path.symlink_to(wheel_target)

    with pytest.raises(RuntimeError, match="regular file"):
        package_check._write_checksum_manifest(artifact_directory, EXPECTED_VERSION)


@pytest.mark.parametrize("existing_path_kind", ["directory", "file", "symlink"])
def test_build_artifacts_refuses_every_existing_path_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_path_kind: str,
) -> None:
    artifact_directory = tmp_path / "artifacts"
    if existing_path_kind == "directory":
        artifact_directory.mkdir()
    elif existing_path_kind == "file":
        artifact_directory.write_text("occupied", encoding="utf-8")
    else:
        artifact_directory.symlink_to(tmp_path / "missing-target")
    command_runner = Mock()
    monkeypatch.setattr(package_check, "_run", command_runner)

    with pytest.raises(FileExistsError, match="artifact directory already exists"):
        package_check._build_artifacts(artifact_directory)

    command_runner.assert_not_called()


def test_build_disables_implicit_gitignore_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_runner = Mock()
    monkeypatch.setattr(package_check, "_run", command_runner)
    monkeypatch.setattr(package_check, "_uv_executable", Mock(return_value="uv"))
    artifact_directory = tmp_path / "artifacts"

    package_check._build_artifacts(artifact_directory)

    assert command_runner.call_count == 2
    export_arguments = command_runner.call_args_list[0].args[0]
    build_arguments = command_runner.call_args_list[1].args[0]
    assert export_arguments[:7] == (
        "uv",
        "export",
        "--quiet",
        "--locked",
        "--only-group",
        "build",
        "--no-emit-project",
    )
    assert build_arguments == (
        "uv",
        "build",
        "--no-create-gitignore",
        "--build-constraints",
        export_arguments[-1],
        "--require-hashes",
        "--out-dir",
        str(artifact_directory),
    )


def test_failed_release_verification_leaves_no_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_directory = tmp_path / "release"

    def fake_build(staging_directory: Path) -> None:
        staging_directory.mkdir()

    monkeypatch.setattr(package_check, "_verify_release_source", Mock())
    monkeypatch.setattr(package_check, "_build_artifacts", fake_build)
    monkeypatch.setattr(
        package_check,
        "_check_artifacts",
        Mock(side_effect=RuntimeError("verification failed")),
    )

    with pytest.raises(RuntimeError, match="verification failed"):
        package_check.build_release_artifacts(
            artifact_directory,
            "3.11",
            EXPECTED_VERSION,
        )

    assert not package_check._path_exists(artifact_directory)


def test_successful_release_build_promotes_only_verified_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_directory = tmp_path / "release"

    def fake_build(staging_directory: Path) -> None:
        staging_directory.mkdir()
        wheel_name, source_distribution_name = package_check._archive_names(EXPECTED_VERSION)
        (staging_directory / wheel_name).write_bytes(b"wheel")
        (staging_directory / source_distribution_name).write_bytes(b"source distribution")

    monkeypatch.setattr(package_check, "_verify_release_source", Mock())
    monkeypatch.setattr(package_check, "_build_artifacts", fake_build)
    artifact_checker = Mock()
    monkeypatch.setattr(package_check, "_check_artifacts", artifact_checker)

    package_check.build_release_artifacts(
        artifact_directory,
        "3.11",
        EXPECTED_VERSION,
    )

    package_check._verify_checksum_manifest(artifact_directory, EXPECTED_VERSION)
    assert {artifact_path.name for artifact_path in artifact_directory.iterdir()} == {
        "SHA256SUMS",
        "flowspec2-0.3.0-py3-none-any.whl",
        "flowspec2-0.3.0.tar.gz",
    }
    artifact_checker.assert_called_once()


def test_promotion_refuses_destination_created_during_build(tmp_path: Path) -> None:
    staging_directory = _artifact_directory(tmp_path)
    package_check._write_checksum_manifest(staging_directory, EXPECTED_VERSION)
    artifact_directory = tmp_path / "release"
    artifact_directory.mkdir()

    with pytest.raises(FileExistsError, match="artifact directory already exists"):
        package_check._promote_release_directory(staging_directory, artifact_directory)

    assert (staging_directory / package_check.CHECKSUM_MANIFEST_NAME).is_file()


def test_release_check_never_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_checker = Mock()
    artifact_builder = Mock()
    release_source_verifier = Mock()
    monkeypatch.setattr(package_check, "_check_artifacts", artifact_checker)
    monkeypatch.setattr(package_check, "_build_artifacts", artifact_builder)
    monkeypatch.setattr(package_check, "_verify_release_source", release_source_verifier)

    package_check.check_release_artifacts(tmp_path, "3.11", EXPECTED_VERSION)

    artifact_checker.assert_called_once_with(
        tmp_path,
        "3.11",
        EXPECTED_VERSION,
        require_checksums=True,
    )
    artifact_builder.assert_not_called()
    release_source_verifier.assert_called_once_with(EXPECTED_VERSION)


def test_wheel_version_must_match_expected_version(tmp_path: Path) -> None:
    wheel_path = tmp_path / "flowspec2-0.3.0-py3-none-any.whl"
    _minimal_wheel(wheel_path, "9.9.9")

    with pytest.raises(RuntimeError, match="wheel metadata version"):
        package_check._check_wheel(wheel_path, EXPECTED_VERSION)


@pytest.mark.parametrize("unsafe_member_name", ["../escaped", "/absolute", "flowspec2\\file"])
def test_wheel_rejects_unsafe_paths(
    tmp_path: Path,
    unsafe_member_name: str,
) -> None:
    wheel_path = tmp_path / "flowspec2-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, mode="w") as wheel_archive:
        wheel_archive.writestr(unsafe_member_name, "unsafe")

    with pytest.raises(RuntimeError, match="unsafe member"):
        package_check._check_wheel(wheel_path, EXPECTED_VERSION)


def test_wheel_rejects_symlink_member(tmp_path: Path) -> None:
    wheel_path = tmp_path / "flowspec2-0.3.0-py3-none-any.whl"
    symlink_entry = zipfile.ZipInfo("flowspec2/unsafe-link")
    symlink_entry.create_system = 3
    symlink_entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel_path, mode="w") as wheel_archive:
        wheel_archive.writestr(symlink_entry, "../../outside")

    with pytest.raises(RuntimeError, match="unsafe member"):
        package_check._check_wheel(wheel_path, EXPECTED_VERSION)


def test_sdist_versions_must_match_expected_version(tmp_path: Path) -> None:
    source_distribution_path = tmp_path / "flowspec2-0.3.0.tar.gz"
    _minimal_sdist(source_distribution_path, "9.9.9")

    with pytest.raises(RuntimeError, match="source distribution metadata version"):
        package_check._check_sdist(source_distribution_path, EXPECTED_VERSION)


def test_sdist_rejects_path_traversal(tmp_path: Path) -> None:
    source_distribution_path = tmp_path / "flowspec2-0.3.0.tar.gz"
    with tarfile.open(source_distribution_path, mode="w:gz") as source_archive:
        _add_tar_member(source_archive, "flowspec2-0.3.0/../escaped", b"unsafe")

    with pytest.raises(RuntimeError, match="unsafe member"):
        package_check._check_sdist(source_distribution_path, EXPECTED_VERSION)


def test_sdist_rejects_unexpected_symlink(tmp_path: Path) -> None:
    source_distribution_path = tmp_path / "flowspec2-0.3.0.tar.gz"
    with tarfile.open(source_distribution_path, mode="w:gz") as source_archive:
        archive_member = tarfile.TarInfo("flowspec2-0.3.0/unsafe-link")
        archive_member.type = tarfile.SYMTYPE
        archive_member.linkname = "../../outside"
        source_archive.addfile(archive_member)

    with pytest.raises(RuntimeError, match="non-regular member"):
        package_check._check_sdist(source_distribution_path, EXPECTED_VERSION)


def test_release_source_requires_clean_matching_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_output = Mock(side_effect=["", "release-commit", "release-commit"])
    monkeypatch.setattr(package_check, "_project_version", Mock(return_value=EXPECTED_VERSION))
    monkeypatch.setattr(package_check, "_run_output", command_output)

    package_check._verify_release_source(EXPECTED_VERSION)

    assert command_output.call_args_list == [
        call(("git", "status", "--porcelain", "--untracked-files=all")),
        call(("git", "rev-parse", "HEAD")),
        call(("git", "rev-parse", "--verify", "refs/tags/v0.3.0^{commit}")),
    ]


def test_build_without_artifact_directory_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_package.py", "--build"])

    with pytest.raises(SystemExit, match="2"):
        package_check.main()
