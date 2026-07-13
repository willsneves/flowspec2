"""Canonical normalization and intermediate-representation contracts."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from flowspec2.ir import FLOW_IR_FORMAT, build_flow_ir, normalize_flow
from flowspec2.nodes import FlowContext
from flowspec2.profiles import FlowProfile, reference_profile
from flowspec2.schema import validate_flow
from flowspec2.subflows import SubflowBuild, SubflowDefinition, default_subflows
from flowspec2.tools import default_tool_registry


class _SchemaContractSubflow:
    name = "schema_contract"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        del ctx, with_cfg
        raise AssertionError("IR generation must not build subflow implementations")


async def _unused_profile_tool(**inputs: Any) -> dict[str, Any]:
    del inputs
    return {"status": "success"}


def test_normalization_is_non_mutating_idempotent_and_schema_valid(
    buraco_doc: dict[str, Any],
) -> None:
    source_document = copy.deepcopy(buraco_doc)

    normalized_document = normalize_flow(buraco_doc)

    assert buraco_doc == source_document
    assert normalize_flow(normalized_document) == normalized_document
    assert normalized_document["config"]["max_attempts"] == 3
    assert normalized_document["domains"]["SimNao"]["normalize"]["emoji_veto"] is False
    assert normalized_document["slots"]["buraco_tipo"]["persist"] == "data"
    assert normalized_document["path"][0]["step"] == "collect_tipo"
    validate_flow(normalized_document)


def test_normalization_does_not_invent_absent_service_metadata(
    buraco_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(buraco_doc)
    flow_document.pop("service")

    normalized_document = normalize_flow(flow_document)
    flow_ir = build_flow_ir(flow_document)

    assert "service" not in normalized_document
    assert "data.service" not in flow_ir.nodes[0].writes


def test_ir_is_stable_across_json_key_order(buraco_doc: dict[str, Any]) -> None:
    reordered_document = json.loads(json.dumps(buraco_doc, sort_keys=True))

    original_ir = build_flow_ir(buraco_doc)
    reordered_ir = build_flow_ir(reordered_document)

    assert original_ir.source_digest == reordered_ir.source_digest
    assert original_ir.digest == reordered_ir.digest
    assert original_ir.canonical_json == reordered_ir.canonical_json


def test_ir_carries_explicit_versioned_format_identity(buraco_doc: dict[str, Any]) -> None:
    flow_ir = build_flow_ir(buraco_doc)

    assert flow_ir.ir_format == FLOW_IR_FORMAT
    assert flow_ir.to_dict()["ir_format"] == FLOW_IR_FORMAT


def test_ir_materializes_nodes_state_access_references_and_capabilities(
    buraco_doc: dict[str, Any],
) -> None:
    flow_ir = build_flow_ir(buraco_doc)

    assert flow_ir.nodes[0].identifier == "__init__"
    assert flow_ir.nodes[1].identifier == "collect_tipo"
    assert flow_ir.nodes[1].writes == ("data.buraco_tipo",)
    assert flow_ir.transitions[0].target == "collect_tipo"
    assert "subflow:address@1" in flow_ir.required_capabilities
    assert "tool:sgrc_open_ticket" in flow_ir.required_capabilities
    assert any(
        reference.source == "/slots/buraco_tipo/domain" and reference.target == "BuracoTipo"
        for reference in flow_ir.references
    )


def test_ir_generates_partitioned_state_schema(luminaria_doc: dict[str, Any]) -> None:
    state_schema = build_flow_ir(luminaria_doc).state_schema()

    Draft202012Validator.check_schema(state_schema)
    assert state_schema["properties"]["data"]["properties"]["luminaria_defeito"] == {
        "enum": luminaria_doc["domains"]["LuminariaDefeito"]["values"]
    }
    location_values = state_schema["properties"]["data"]["properties"]["luminaria_localizacao"][
        "enum"
    ]
    assert None not in location_values
    assert state_schema["properties"]["payload"]["type"] == "object"


def test_ir_preserves_local_references_in_embedded_subflow_slot_schemas(
    buraco_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(buraco_doc)
    flow_document["uses"].append({"ref": "schema_contract@1", "with": {}})
    subflows = default_subflows()
    subflows.register(
        _SchemaContractSubflow(),
        definition=SubflowDefinition(
            ref="schema_contract@1",
            description="Expose a locally referenced state contract.",
            configuration_schema={"type": "object", "additionalProperties": False},
            exposed_slots=frozenset({"external_slot"}),
            exposed_slot_schemas={
                "external_slot": {
                    "$defs": {"slot": {"type": "string", "minLength": 1}},
                    "$ref": "#/$defs/slot",
                }
            },
        ),
    )
    base_profile = reference_profile(subflows=subflows)

    state_schema = build_flow_ir(flow_document, profile=base_profile).state_schema()
    validator = Draft202012Validator(state_schema)
    valid_state = {"data": {"external_slot": "valid"}, "internal": {}, "payload": {}}
    invalid_state = {"data": {"external_slot": 42}, "internal": {}, "payload": {}}

    validator.validate(valid_state)
    assert not validator.is_valid(invalid_state)


def test_ir_await_writes_follow_declared_slot_partitions(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    internal_slots = ("ponto_referencia", "ticket_data_confirmed", "service_confirmed")
    for slot_name in internal_slots:
        flow_document["slots"][slot_name]["persist"] = "internal"
    await_definition = flow_document["capabilities"]["await_external"]
    await_definition["on_resume"]["set"]["ponto_referencia"] = "$token.reference"
    await_definition["on_resume"]["enrich"]["set"]["ticket_data_confirmed"] = "$result.confirmed"
    await_definition["timeout"]["set"]["service_confirmed"] = False

    flow_ir = build_flow_ir(flow_document)
    await_node = next(
        node
        for node in flow_ir.nodes
        if node.kind == "subflow" and "data.govbr_authenticated" in node.writes
    )
    state_schema = flow_ir.state_schema()
    data_properties = state_schema["properties"]["data"]["properties"]
    internal_properties = state_schema["properties"]["internal"]["properties"]

    assert {
        "internal.ponto_referencia",
        "internal.ticket_data_confirmed",
        "internal.service_confirmed",
    } <= set(await_node.writes)
    assert all(slot_name in internal_properties for slot_name in internal_slots)
    assert all(slot_name not in data_properties for slot_name in internal_slots)
    assert "payload.govbr_token" in await_node.reads
    assert "internal._await_external_sent:authenticate_govbr" in await_node.writes
    assert any(
        reference.source == "/capabilities/await_external/resume/correlation"
        and reference.namespace == "token"
        and reference.target == "$token.cpf"
        for reference in flow_ir.references
    )


def test_ir_terminal_node_includes_success_literal_writes(
    buraco_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(buraco_doc)
    flow_document["terminal"]["outcomes"]["success"]["set"] = {"submission_status": "submitted"}

    terminal_node = next(
        node for node in build_flow_ir(flow_document).nodes if node.kind == "terminal"
    )

    assert "data.submission_status" in terminal_node.writes


def test_ir_derive_source_default_preserves_source_domain_schema(
    buraco_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(buraco_doc)
    flow_document["derive"] = [
        {
            "writes": "classification",
            "from": ["buraco_tipo"],
            "after": "collect_tipo",
            "lookup": {"Buraco no asfalto": "mapped"},
            "default": "$from[0]",
        }
    ]

    state_schema = build_flow_ir(flow_document).state_schema()
    validator = Draft202012Validator(state_schema)

    assert validator.is_valid(
        {"data": {"classification": "Cratera"}, "internal": {}, "payload": {}}
    )
    assert validator.is_valid({"data": {"classification": "mapped"}, "internal": {}, "payload": {}})


def test_ir_rejects_unplaced_derive_without_anchor(buraco_doc: dict[str, Any]) -> None:
    flow_document = copy.deepcopy(buraco_doc)
    flow_document["derive"] = [
        {
            "writes": "classification",
            "from": ["buraco_tipo"],
            "lookup": {"Buraco no asfalto": "mapped"},
        }
    ]

    with pytest.raises(ValueError, match="must be placed in path or declare an after anchor"):
        build_flow_ir(flow_document)


def test_ir_returns_fresh_documents(buraco_doc: dict[str, Any]) -> None:
    flow_ir = build_flow_ir(buraco_doc)

    first_document = flow_ir.to_document()
    first_document["flow"] = "mutated"

    assert flow_ir.to_document()["flow"] == buraco_doc["flow"]


def test_external_wait_requires_explicit_out_of_band_send_semantics(
    luminaria_doc: dict[str, Any],
) -> None:
    source_document = copy.deepcopy(luminaria_doc)
    source_document["capabilities"]["await_external"]["interactive"].pop("out_of_band")

    with pytest.raises(ValidationError):
        normalize_flow(source_document)


def test_ir_projection_is_complete_and_json_compatible(buraco_doc: dict[str, Any]) -> None:
    projection = build_flow_ir(buraco_doc).to_dict()

    assert projection["document"]["flow"] == buraco_doc["flow"]
    assert projection["nodes"][0]["identifier"] == "__init__"
    assert projection["profile_digest"]
    assert projection["dependency_digest"]
    assert projection["state_schema"]["type"] == "object"
    assert json.loads(json.dumps(projection, allow_nan=False)) == projection


def test_default_ir_resolves_the_reference_profile_contract(
    buraco_doc: dict[str, Any],
) -> None:
    profile = reference_profile()
    implicit_profile_ir = build_flow_ir(buraco_doc)
    explicit_profile_ir = build_flow_ir(buraco_doc, profile=profile)

    assert implicit_profile_ir.digest == explicit_profile_ir.digest
    assert implicit_profile_ir.profile_digest == explicit_profile_ir.profile_digest
    assert explicit_profile_ir.profile_digest == profile.digest
    assert "data.address" in next(
        node.writes for node in implicit_profile_ir.nodes if node.kind == "subflow"
    )
    assert "capability:geocoding" in implicit_profile_ir.required_capabilities


def test_ir_digest_includes_resolved_profile_contract(buraco_doc: dict[str, Any]) -> None:
    base_profile = reference_profile()
    reduced_profile = FlowProfile(
        identifier=base_profile.identifier,
        tools=base_profile.tools,
        subflows=base_profile.subflows,
        capabilities=frozenset(),
        domain_types=base_profile.domain_types,
    )

    base_ir = build_flow_ir(buraco_doc, profile=base_profile)
    reduced_ir = build_flow_ir(buraco_doc, profile=reduced_profile)

    assert base_ir.profile == reduced_ir.profile
    assert base_ir.profile_digest != reduced_ir.profile_digest
    assert base_ir.dependency_digest != reduced_ir.dependency_digest
    assert base_ir.digest != reduced_ir.digest


def test_ir_includes_transitive_subflow_tool_contract_in_dependency_digest(
    buraco_doc: dict[str, Any],
) -> None:
    base_ir = build_flow_ir(buraco_doc)
    altered_tools = default_tool_registry()
    altered_geocode_definition = replace(
        altered_tools.definition("geocode"),
        description="Altered geocoder contract for dependency tracking.",
    )
    altered_tools.register(
        "geocode",
        _unused_profile_tool,
        definition=altered_geocode_definition,
    )

    altered_ir = build_flow_ir(
        buraco_doc,
        profile=reference_profile(tools=altered_tools),
    )

    assert "tool:geocode" in base_ir.required_capabilities
    assert base_ir.dependency_digest != altered_ir.dependency_digest
    assert base_ir.digest != altered_ir.digest


def test_ir_projects_complete_subflow_owned_state_by_partition(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_ir = build_flow_ir(luminaria_doc)
    state_schema = flow_ir.state_schema()
    validator = Draft202012Validator(state_schema)
    data_properties = state_schema["properties"]["data"]["properties"]
    internal_properties = state_schema["properties"]["internal"]["properties"]

    assert data_properties["address_completed"]["type"] == "boolean"
    assert data_properties["cpf"]["pattern"] == "^[0-9]{11}$"
    assert internal_properties["_cpf_lookup_derived:email"]["type"] == "boolean"
    assert "_cpf_lookup_derived:email" not in data_properties
    assert validator.is_valid(
        {
            "data": {"address_completed": True, "cpf": "12345678901"},
            "internal": {"_cpf_lookup_derived:email": True},
            "payload": {},
        }
    )
    assert not validator.is_valid(
        {
            "data": {"address_completed": "yes"},
            "internal": {"_cpf_lookup_derived:email": "yes"},
            "payload": {},
        }
    )


def test_ir_subflow_node_conservatively_indexes_all_owned_state(
    buraco_doc: dict[str, Any],
) -> None:
    address_node = next(node for node in build_flow_ir(buraco_doc).nodes if node.kind == "subflow")

    assert "data.address" in address_node.reads
    assert "data.address_completed" in address_node.reads
    assert "data.address_completed" in address_node.writes


def test_ir_predicate_references_use_explicit_partition_namespaces(
    buraco_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(buraco_doc)
    flow_document["path"][0]["ask_when"] = {
        "and": [
            {"eq": ["slots.buraco_tipo", "payload.requested_type"]},
            {"is_present": "internal._collection_asked_slot"},
            {"eq": ["address.kind", {"literal": "praca"}]},
            {"eq": ["config.identification_required", False]},
        ]
    }

    predicate_references = {
        (reference.namespace, reference.target)
        for reference in build_flow_ir(flow_document).references
        if reference.source == "/path/0/ask_when"
    }

    assert {
        ("data", "buraco_tipo"),
        ("payload", "requested_type"),
        ("internal", "_collection_asked_slot"),
        ("address", "kind"),
        ("config", "identification_required"),
    } <= predicate_references


def test_ir_execution_digest_ignores_unused_profile_additions(
    buraco_doc: dict[str, Any],
) -> None:
    profile = reference_profile()
    base_ir = build_flow_ir(buraco_doc, profile=profile)

    profile.tools.register("unused_profile_tool", _unused_profile_tool)
    expanded_ir = build_flow_ir(buraco_doc, profile=profile)

    assert base_ir.profile_digest != expanded_ir.profile_digest
    assert base_ir.dependency_digest == expanded_ir.dependency_digest
    assert base_ir.digest == expanded_ir.digest
