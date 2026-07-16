"""Public contract-site generation tests."""

from __future__ import annotations

import importlib.util
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_public_site_builder() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "build_public_site.py"
    module_specification = importlib.util.spec_from_file_location("build_public_site", script_path)
    if module_specification is None or module_specification.loader is None:
        raise RuntimeError(f"could not load public-site builder: {script_path}")
    public_site_builder = importlib.util.module_from_spec(module_specification)
    module_specification.loader.exec_module(public_site_builder)
    return public_site_builder


public_site_builder = _load_public_site_builder()


class QuietStaticRequestHandler(SimpleHTTPRequestHandler):
    """Serve generated fixtures without writing request logs to the test output."""

    def log_message(self, format: str, *arguments: object) -> None:
        pass


def _load_json_object(document_path: Path) -> dict[str, Any]:
    loaded_document = json.loads(document_path.read_text(encoding="utf-8"))
    assert isinstance(loaded_document, dict)
    return loaded_document


def test_public_site_publishes_every_canonical_schema_without_drift(tmp_path: Path) -> None:
    site_directory = tmp_path / "site"
    public_site_builder.build_public_site(site_directory)

    schemas_by_identifier = public_site_builder.public_schema_documents()
    published_schema_paths = sorted((site_directory / "schemas").glob("*.json"))
    assert len(published_schema_paths) == len(schemas_by_identifier)
    for schema_identifier, canonical_schema in schemas_by_identifier.items():
        schema_path = site_directory / public_site_builder._identifier_path(schema_identifier)
        assert _load_json_object(schema_path) == canonical_schema


def test_public_site_resolves_the_profile_identifier_and_indexes_contracts(
    tmp_path: Path,
) -> None:
    site_directory = tmp_path / "site"
    public_site_builder.build_public_site(site_directory)

    profile_page = (
        site_directory
        / public_site_builder._identifier_path(public_site_builder.OPEN_WORKFLOW_PROFILE_ID)
        / "index.html"
    )
    index_document = (site_directory / "index.html").read_text(encoding="utf-8")
    assert profile_page.is_file()
    assert public_site_builder.OPEN_WORKFLOW_PROFILE_ID in index_document
    assert all(
        schema_identifier in index_document
        for schema_identifier in public_site_builder.public_schema_documents()
    )
    assert (site_directory / ".nojekyll").is_file()


def test_public_site_refuses_an_existing_destination(tmp_path: Path) -> None:
    site_directory = tmp_path / "site"
    site_directory.mkdir()

    with pytest.raises(FileExistsError, match="already exists"):
        public_site_builder.build_public_site(site_directory)


def test_deployed_public_site_verifier_reads_every_resource(tmp_path: Path) -> None:
    site_directory = tmp_path / "site"
    public_site_builder.build_public_site(site_directory)
    request_handler = partial(QuietStaticRequestHandler, directory=str(site_directory))
    static_server = ThreadingHTTPServer(("127.0.0.1", 0), request_handler)
    server_thread = threading.Thread(target=static_server.serve_forever, daemon=True)
    server_thread.start()
    try:
        public_site_builder.verify_public_site(f"http://127.0.0.1:{static_server.server_port}/")
    finally:
        static_server.shutdown()
        static_server.server_close()
        server_thread.join()
