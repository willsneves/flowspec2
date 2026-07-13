"""Provider-neutral authoring projection contracts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from flowspec2.authoring import (
    AUTHORING_PROJECTION_FORMAT,
    AUTHORING_PROJECTION_VERSION,
    AuthoringProjectionError,
    authoring_projection,
    authoring_projection_diagnostics,
    canonical_projection_json,
    lower_authoring_projection,
    project_flow_document,
)
from flowspec2.schema import schema

_AUTHORING_FIXTURE = (
    Path(__file__).parents[1] / "src" / "flowspec2" / "authoring" / "corpus" / "linear.case.json"
)


def _canonical_json(json_document: object) -> str:
    return json.dumps(
        json_document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fixture_source() -> dict[str, Any]:
    fixture_document = json.loads(_AUTHORING_FIXTURE.read_text(encoding="utf-8"))
    return fixture_document["source"]


def _schema_nodes(json_node: object) -> list[dict[str, Any]]:
    discovered_nodes: list[dict[str, Any]] = []
    if isinstance(json_node, dict):
        discovered_nodes.append(json_node)
        for nested_node in json_node.values():
            discovered_nodes.extend(_schema_nodes(nested_node))
    elif isinstance(json_node, list):
        for nested_node in json_node:
            discovered_nodes.extend(_schema_nodes(nested_node))
    return discovered_nodes


def test_projection_artifact_is_closed_deterministic_and_normative_derived() -> None:
    projection_artifact = authoring_projection()
    projection_schema = projection_artifact.schema()
    normative_schema_json = _canonical_json(schema())

    Draft202012Validator.check_schema(projection_schema)
    assert projection_artifact.format_identifier == AUTHORING_PROJECTION_FORMAT
    assert projection_artifact.version == AUTHORING_PROJECTION_VERSION
    assert (
        projection_artifact.normative_schema_digest
        == hashlib.sha256(normative_schema_json.encode("utf-8")).hexdigest()
    )
    assert (
        projection_artifact.schema_digest
        == hashlib.sha256(projection_artifact.canonical_schema_json.encode("utf-8")).hexdigest()
    )
    assert not any("$ref" in schema_node for schema_node in _schema_nodes(projection_schema))
    assert not any(
        schema_keyword.startswith("$")
        for schema_node in _schema_nodes(projection_schema)
        for schema_keyword in schema_node
    )
    assert all(
        schema_node.get("additionalProperties") is False
        for schema_node in _schema_nodes(projection_schema)
        if schema_node.get("type") == "object"
    )


def test_projection_artifact_and_lowering_return_defensive_copies() -> None:
    source_document = _fixture_source()
    projection_artifact = authoring_projection()
    first_schema = projection_artifact.schema()
    first_schema["title"] = "mutated"
    projected_document = project_flow_document(source_document)
    first_lowering = lower_authoring_projection(projected_document)
    first_lowering["flow"] = "mutated"

    assert projection_artifact.schema()["title"] != "mutated"
    assert lower_authoring_projection(projected_document)["flow"] == source_document["flow"]


def test_projection_round_trip_is_canonical_and_normatively_valid() -> None:
    source_document = _fixture_source()
    projected_document = project_flow_document(source_document)

    assert projected_document == {
        "format": AUTHORING_PROJECTION_FORMAT,
        "version": AUTHORING_PROJECTION_VERSION,
        "normative_schema_digest": authoring_projection().normative_schema_digest,
        "flow_document_json": _canonical_json(source_document),
    }
    assert authoring_projection_diagnostics(projected_document) == ()
    assert lower_authoring_projection(projected_document) == source_document
    assert canonical_projection_json(projected_document) == _canonical_json(projected_document)


def test_projection_rejects_noncanonical_flow_json() -> None:
    projected_document = project_flow_document(_fixture_source())
    decoded_flow = json.loads(projected_document["flow_document_json"])
    projected_document["flow_document_json"] = json.dumps(decoded_flow, indent=2)

    projection_diagnostics = authoring_projection_diagnostics(projected_document)

    assert [diagnostic.code for diagnostic in projection_diagnostics] == [
        "FLOWSPEC_AUTHORING_PROJECTION_JSON_NOT_CANONICAL"
    ]
    with pytest.raises(AuthoringProjectionError):
        lower_authoring_projection(projected_document)


def test_projection_rejects_invalid_inner_flow_through_normative_checker() -> None:
    invalid_source = copy.deepcopy(_fixture_source())
    invalid_source["flow"] = "Invalid Flow"
    projection_artifact = authoring_projection()
    projected_document = {
        "format": AUTHORING_PROJECTION_FORMAT,
        "version": AUTHORING_PROJECTION_VERSION,
        "normative_schema_digest": projection_artifact.normative_schema_digest,
        "flow_document_json": _canonical_json(invalid_source),
    }

    projection_diagnostics = authoring_projection_diagnostics(projected_document)

    assert [(diagnostic.code, diagnostic.path) for diagnostic in projection_diagnostics] == [
        ("FLOWSPEC_SCHEMA_PATTERN", "/flow")
    ]
    with pytest.raises(AuthoringProjectionError):
        lower_authoring_projection(projected_document)


def test_projection_rejects_open_envelope_extensions() -> None:
    projected_document = cast(
        dict[str, object],
        project_flow_document(_fixture_source()),
    )
    projected_document["provider_hint"] = "ignored"

    projection_diagnostics = authoring_projection_diagnostics(projected_document)

    assert [diagnostic.code for diagnostic in projection_diagnostics] == [
        "FLOWSPEC_AUTHORING_PROJECTION_SCHEMA_ADDITIONALPROPERTIES"
    ]


def test_projection_creation_rejects_structurally_invalid_source() -> None:
    invalid_source = copy.deepcopy(_fixture_source())
    invalid_source.pop("path")

    with pytest.raises(AuthoringProjectionError) as projection_error:
        project_flow_document(invalid_source)

    assert "/path" in {diagnostic.path for diagnostic in projection_error.value.diagnostics}
