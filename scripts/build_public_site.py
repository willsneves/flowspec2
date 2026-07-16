"""Build the deterministic static site for public FlowSpec contract identifiers."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import jsonschema

from flowspec2.authoring import (
    authoring_presentation_review_signature_schema,
    presentation_review_schema,
)
from flowspec2.compat import OPEN_WORKFLOW_PROFILE_ID

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
PUBLIC_BASE_URL: Final[str] = "https://wllsena.github.io/flowspec2/"
PUBLIC_SCHEMA_NAMESPACE: Final[str] = f"{PUBLIC_BASE_URL}schemas/"
PUBLIC_PROFILE_NAMESPACE: Final[str] = f"{PUBLIC_BASE_URL}profiles/"
OPEN_WORKFLOW_PROFILE_SCHEMA_IDENTIFIER: Final[str] = (
    f"{PUBLIC_SCHEMA_NAMESPACE}open-workflow-conversation-1.json"
)
PUBLIC_SITE_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
PUBLIC_SITE_VERIFICATION_ATTEMPTS: Final[int] = 12
PUBLIC_SITE_RETRY_DELAY_SECONDS: Final[float] = 5.0
PUBLIC_SITE_USER_AGENT: Final[str] = "flowspec2-public-contract-verifier/1"
SOURCE_PACKAGE_DIRECTORY: Final[Path] = PROJECT_ROOT / "src" / "flowspec2"
STATIC_SCHEMA_PATHS: Final[tuple[Path, ...]] = (
    SOURCE_PACKAGE_DIRECTORY / "flowspec-2.schema.json",
    SOURCE_PACKAGE_DIRECTORY / "authoring" / "authoring-evidence.schema.json",
    SOURCE_PACKAGE_DIRECTORY / "authoring" / "authoring-evidence-signature.schema.json",
    SOURCE_PACKAGE_DIRECTORY / "authoring" / "authoring-operational-evidence.schema.json",
    SOURCE_PACKAGE_DIRECTORY / "experimental" / "flowspec-3-draft.schema.json",
    SOURCE_PACKAGE_DIRECTORY / "compat" / "schemas" / "open-workflow-conversation-1.schema.json",
)
GENERATED_SCHEMA_BUILDERS: Final[tuple[Callable[[], dict[str, Any]], ...]] = (
    presentation_review_schema,
    authoring_presentation_review_signature_schema,
)


def _load_json_mapping(document_path: Path) -> dict[str, Any]:
    loaded_document: object = json.loads(document_path.read_text(encoding="utf-8"))
    if not isinstance(loaded_document, dict) or not all(
        isinstance(document_key, str) for document_key in loaded_document
    ):
        raise RuntimeError(f"expected a string-keyed JSON object in {document_path}")
    return dict(loaded_document)


def public_schema_documents() -> Mapping[str, dict[str, Any]]:
    """Return every project-owned public schema keyed by its canonical identifier."""

    discovered_schema_paths = tuple(sorted(SOURCE_PACKAGE_DIRECTORY.rglob("*.schema.json")))
    if discovered_schema_paths != tuple(sorted(STATIC_SCHEMA_PATHS)):
        raise RuntimeError("static public schema registry does not match package schema sources")
    schema_documents = (
        *(_load_json_mapping(schema_path) for schema_path in STATIC_SCHEMA_PATHS),
        *(schema_builder() for schema_builder in GENERATED_SCHEMA_BUILDERS),
    )
    schemas_by_identifier: dict[str, dict[str, Any]] = {}
    for schema_document in schema_documents:
        schema_identifier = schema_document.get("$id")
        if not isinstance(schema_identifier, str) or not schema_identifier.startswith(
            PUBLIC_SCHEMA_NAMESPACE
        ):
            raise RuntimeError("public schema must declare an identifier in the project namespace")
        if schema_identifier in schemas_by_identifier:
            raise RuntimeError(f"duplicate public schema identifier: {schema_identifier}")
        jsonschema.Draft202012Validator.check_schema(schema_document)
        schemas_by_identifier[schema_identifier] = schema_document
    return schemas_by_identifier


def _identifier_path(identifier: str) -> PurePosixPath:
    parsed_identifier = urlsplit(identifier)
    parsed_base_url = urlsplit(PUBLIC_BASE_URL)
    expected_path_prefix = f"{parsed_base_url.path.rstrip('/')}/"
    if (
        parsed_identifier.scheme != parsed_base_url.scheme
        or parsed_identifier.netloc != parsed_base_url.netloc
        or not parsed_identifier.path.startswith(expected_path_prefix)
        or parsed_identifier.query
        or parsed_identifier.fragment
    ):
        raise RuntimeError(f"identifier is outside the public site namespace: {identifier}")
    relative_path = PurePosixPath(parsed_identifier.path.removeprefix(expected_path_prefix))
    if not relative_path.parts or any(
        path_part in {"", ".", ".."} for path_part in relative_path.parts
    ):
        raise RuntimeError(f"identifier has an unsafe public path: {identifier}")
    return relative_path


def _write_json_document(document_path: Path, document: Mapping[str, Any]) -> None:
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_text(
        f"{json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def _page(title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    body {{ font: 1rem/1.6 system-ui, sans-serif; max-width: 52rem; margin: 4rem auto; padding: 0 1.5rem; color: #18212f; }}
    a {{ color: #0757b3; }} code {{ background: #eef2f7; padding: .15rem .35rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{escaped_title}</h1>
{body}
  </main>
</body>
</html>
"""


def _write_index(site_directory: Path, schema_identifiers: tuple[str, ...]) -> None:
    schema_links = "\n".join(
        f'      <li><a href="{html.escape(schema_identifier)}"><code>{html.escape(schema_identifier)}</code></a></li>'
        for schema_identifier in schema_identifiers
    )
    profile_identifier = html.escape(OPEN_WORKFLOW_PROFILE_ID)
    site_directory.joinpath("index.html").write_text(
        _page(
            "flowspec2 public contracts",
            f"""    <p>Canonical, versioned schemas and compatibility profiles published by flowspec2.</p>
    <h2>Schemas</h2>
    <ul>
{schema_links}
    </ul>
    <h2>Profiles</h2>
    <ul>
      <li><a href="{profile_identifier}"><code>{profile_identifier}</code></a></li>
    </ul>""",
        ),
        encoding="utf-8",
    )


def _write_open_workflow_profile(site_directory: Path) -> None:
    profile_directory = site_directory / _identifier_path(OPEN_WORKFLOW_PROFILE_ID)
    profile_directory.mkdir(parents=True)
    schema_identifier = html.escape(OPEN_WORKFLOW_PROFILE_SCHEMA_IDENTIFIER)
    profile_identifier = html.escape(OPEN_WORKFLOW_PROFILE_ID)
    profile_directory.joinpath("index.html").write_text(
        _page(
            "Open Workflow conversational profile",
            f"""    <p><code>{profile_identifier}</code> identifies the lossless flowspec2 conversational subset of Open Workflow Specification.</p>
    <p><a href="{schema_identifier}">Profile JSON Schema</a></p>
    <p><a href="https://github.com/wllsena/flowspec2/blob/main/docs/COMPATIBILITY.md">Compatibility documentation</a></p>""",
        ),
        encoding="utf-8",
    )


def build_public_site(site_directory: Path) -> None:
    """Build a complete public-contract site into a new destination directory."""

    if site_directory.exists() or site_directory.is_symlink():
        raise FileExistsError(f"public site directory already exists: {site_directory}")
    site_directory.mkdir(parents=True)
    try:
        schemas_by_identifier = public_schema_documents()
        for schema_identifier, schema_document in schemas_by_identifier.items():
            _write_json_document(
                site_directory / _identifier_path(schema_identifier), schema_document
            )
        sorted_schema_identifiers = tuple(sorted(schemas_by_identifier))
        _write_index(site_directory, sorted_schema_identifiers)
        _write_open_workflow_profile(site_directory)
        site_directory.joinpath(".nojekyll").touch()
    except BaseException:
        shutil.rmtree(site_directory)
        raise


def _read_public_resource(resource_url: str) -> bytes:
    resource_request = Request(resource_url, headers={"User-Agent": PUBLIC_SITE_USER_AGENT})
    with urlopen(resource_request, timeout=PUBLIC_SITE_REQUEST_TIMEOUT_SECONDS) as public_resource:
        return public_resource.read()


def _public_resource_url(site_base_url: str, identifier: str) -> str:
    relative_path = _identifier_path(identifier).as_posix()
    return f"{site_base_url.rstrip('/')}/{relative_path}"


def _verify_public_site_once(site_base_url: str) -> None:
    schemas_by_identifier = public_schema_documents()
    index_document = _read_public_resource(f"{site_base_url.rstrip('/')}/").decode("utf-8")
    for schema_identifier, canonical_schema in schemas_by_identifier.items():
        published_schema_object: object = json.loads(
            _read_public_resource(_public_resource_url(site_base_url, schema_identifier))
        )
        if published_schema_object != canonical_schema:
            raise RuntimeError(f"published schema differs from its source: {schema_identifier}")
        if schema_identifier not in index_document:
            raise RuntimeError(f"public site index omits schema: {schema_identifier}")

    profile_document = _read_public_resource(
        _public_resource_url(site_base_url, OPEN_WORKFLOW_PROFILE_ID)
    ).decode("utf-8")
    if (
        OPEN_WORKFLOW_PROFILE_ID not in profile_document
        or OPEN_WORKFLOW_PROFILE_SCHEMA_IDENTIFIER not in profile_document
        or OPEN_WORKFLOW_PROFILE_ID not in index_document
    ):
        raise RuntimeError("published compatibility profile is incomplete")


def verify_public_site(site_base_url: str) -> None:
    """Verify deployed resources with bounded retries for Pages propagation."""

    last_verification_error: Exception | None = None
    for verification_attempt in range(PUBLIC_SITE_VERIFICATION_ATTEMPTS):
        try:
            _verify_public_site_once(site_base_url)
            return
        except Exception as verification_error:
            last_verification_error = verification_error
            if verification_attempt + 1 < PUBLIC_SITE_VERIFICATION_ATTEMPTS:
                time.sleep(PUBLIC_SITE_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        "public contract site failed deployed-resource verification"
    ) from last_verification_error


def main() -> None:
    argument_parser = argparse.ArgumentParser(
        description="build the deterministic flowspec2 public-contract site"
    )
    output_group = argument_parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--output", type=Path)
    output_group.add_argument("--check", action="store_true")
    output_group.add_argument("--verify-url")
    command_arguments = argument_parser.parse_args()

    if command_arguments.check:
        with tempfile.TemporaryDirectory(prefix="flowspec2-public-site-check-") as temporary_name:
            build_public_site(Path(temporary_name) / "site")
        return
    if command_arguments.verify_url:
        verify_public_site(command_arguments.verify_url)
        return
    build_public_site(command_arguments.output)


if __name__ == "__main__":
    main()
