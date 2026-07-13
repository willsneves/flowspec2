"""Packaged AI-authoring corpus contracts and integrity checks."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from flowspec2.authoring import (
    AUTHORING_CASE_FORMAT,
    AUTHORING_CORPUS_FORMAT,
    REFERENCE_AUTHORING_CORPUS_ID,
    AuthoredSource,
    AuthoringRequest,
    load_reference_authoring_corpus,
    run_authoring_benchmark,
)
from flowspec2.authoring.corpus import _load_authoring_corpus

_CORPUS_DIRECTORY = Path(__file__).parents[1] / "src" / "flowspec2" / "authoring" / "corpus"


def _copy_corpus(tmp_path: Path) -> Path:
    copied_directory = tmp_path / "corpus"
    shutil.copytree(_CORPUS_DIRECTORY, copied_directory)
    return copied_directory


def _load_manifest(corpus_directory: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((corpus_directory / "manifest.json").read_text(encoding="utf-8")),
    )


def _write_manifest(corpus_directory: Path, manifest: dict[str, Any]) -> None:
    (corpus_directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _refresh_case_digest(corpus_directory: Path, case_path: str) -> None:
    manifest = _load_manifest(corpus_directory)
    case_digest = hashlib.sha256((corpus_directory / case_path).read_bytes()).hexdigest()
    for case_contract in manifest["cases"]:
        if case_contract["path"] == case_path:
            case_contract["sha256"] = case_digest
            break
    _write_manifest(corpus_directory, manifest)


def test_reference_corpus_is_versioned_sorted_and_content_addressed() -> None:
    first_corpus = load_reference_authoring_corpus()
    second_corpus = load_reference_authoring_corpus()

    assert first_corpus is second_corpus
    assert first_corpus.format_identifier == AUTHORING_CORPUS_FORMAT
    assert first_corpus.case_format_identifier == AUTHORING_CASE_FORMAT
    assert first_corpus.identifier == REFERENCE_AUTHORING_CORPUS_ID
    assert tuple(benchmark_case.identifier for benchmark_case in first_corpus.cases) == (
        "address_subflow",
        "await_and_correction",
        "gated_derivation",
        "linear_collection",
        "terminal_fulfillment",
    )
    assert len(first_corpus.digest) == 64


def test_reference_corpus_runs_as_the_fixture_integrity_baseline() -> None:
    corpus = load_reference_authoring_corpus()
    source_by_identifier = {
        benchmark_case.identifier: benchmark_case.expected_flow_json
        for benchmark_case in corpus.cases
    }

    def fixture_author(authoring_request: AuthoringRequest) -> AuthoredSource:
        return AuthoredSource(source_by_identifier[authoring_request.task.identifier])

    report = run_authoring_benchmark("packaged_reference", corpus.cases, fixture_author)

    assert report.successful_cases == report.total_cases


def test_corpus_rejects_unknown_manifest_properties(tmp_path: Path) -> None:
    corpus_directory = _copy_corpus(tmp_path)
    manifest = _load_manifest(corpus_directory)
    manifest["unknown"] = True
    _write_manifest(corpus_directory, manifest)

    with pytest.raises(ValueError, match="invalid packaged authoring corpus manifest"):
        _load_authoring_corpus(
            corpus_directory,
            expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
        )


@pytest.mark.parametrize("case_path", ["../outside.case.json", ".env.case.json"])
def test_corpus_rejects_unsafe_case_paths(tmp_path: Path, case_path: str) -> None:
    corpus_directory = _copy_corpus(tmp_path)
    manifest = _load_manifest(corpus_directory)
    manifest["cases"][0]["path"] = case_path
    _write_manifest(corpus_directory, manifest)

    with pytest.raises(ValueError, match="invalid packaged authoring corpus manifest"):
        _load_authoring_corpus(
            corpus_directory,
            expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
        )


def test_corpus_rejects_digest_and_identifier_drift(tmp_path: Path) -> None:
    corpus_directory = _copy_corpus(tmp_path)
    manifest = _load_manifest(corpus_directory)
    manifest["cases"][0]["sha256"] = "0" * 64
    _write_manifest(corpus_directory, manifest)

    with pytest.raises(ValueError, match="integrity verification"):
        _load_authoring_corpus(
            corpus_directory,
            expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
        )

    corpus_directory = _copy_corpus(tmp_path / "identifier")
    manifest = _load_manifest(corpus_directory)
    manifest["identifier"] = "different_corpus"
    _write_manifest(corpus_directory, manifest)
    with pytest.raises(ValueError, match="identifier is not the expected contract"):
        _load_authoring_corpus(
            corpus_directory,
            expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
        )


def test_corpus_rejects_unlisted_and_invalid_cases(tmp_path: Path) -> None:
    corpus_directory = _copy_corpus(tmp_path)
    shutil.copy2(
        corpus_directory / "linear.case.json",
        corpus_directory / "unlisted.case.json",
    )
    with pytest.raises(ValueError, match="membership mismatch"):
        _load_authoring_corpus(
            corpus_directory,
            expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
        )

    corpus_directory = _copy_corpus(tmp_path / "invalid")
    case_path = "linear.case.json"
    case_document = cast(
        dict[str, Any],
        json.loads((corpus_directory / case_path).read_text(encoding="utf-8")),
    )
    case_document["source"]["path"] = []
    (corpus_directory / case_path).write_text(
        json.dumps(case_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _refresh_case_digest(corpus_directory, case_path)
    with pytest.raises(ValueError, match="invalid reference flow"):
        _load_authoring_corpus(
            corpus_directory,
            expected_identifier=REFERENCE_AUTHORING_CORPUS_ID,
        )
