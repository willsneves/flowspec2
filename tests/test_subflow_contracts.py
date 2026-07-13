"""Deterministic JSON Schema boundaries for reusable subflow manifests."""

from __future__ import annotations

from typing import Any

import pytest

from flowspec2.nodes import FlowContext
from flowspec2.subflows import (
    SubflowBuild,
    SubflowDefinition,
    SubflowRegistry,
    SubflowStateKey,
    default_subflows,
)


class _ContractSubflow:
    name = "contract"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        del ctx, with_cfg
        raise AssertionError("contract validation does not build the subflow")


def test_subflow_definition_rejects_non_object_configuration_schema() -> None:
    with pytest.raises(
        ValueError,
        match="configuration_schema cannot validate any object-shaped",
    ):
        SubflowDefinition(
            ref="contract@1",
            description="Reject a scalar configuration boundary.",
            configuration_schema={"type": "string"},
        )


@pytest.mark.parametrize(
    "configuration_schema",
    [
        {"allOf": [{"type": "object"}, {"type": "string"}]},
        {
            "type": "object",
            "minProperties": 1,
            "properties": {},
            "additionalProperties": False,
        },
        {"type": "object", "const": {}, "required": ["external_slot"]},
        {"type": "object", "enum": [{}], "minProperties": 1},
    ],
)
def test_subflow_definition_rejects_impossible_object_configuration_schema(
    configuration_schema: dict[str, Any],
) -> None:
    with pytest.raises(
        ValueError,
        match="configuration_schema cannot validate any object-shaped",
    ):
        SubflowDefinition(
            ref="contract@1",
            description="Reject contradictory configuration types.",
            configuration_schema=configuration_schema,
        )


def test_subflow_definition_rejects_configuration_schema_that_accepts_scalars() -> None:
    with pytest.raises(
        ValueError,
        match="configuration_schema must only validate object-shaped",
    ):
        SubflowDefinition(
            ref="contract@1",
            description="Reject a nullable configuration boundary.",
            configuration_schema={"type": ["object", "null"]},
        )


def test_subflow_definition_accepts_object_union_narrowed_by_negation() -> None:
    definition = SubflowDefinition(
        ref="contract@1",
        description="Narrow an object-or-null configuration boundary to objects.",
        configuration_schema={
            "type": ["object", "null"],
            "not": {"type": "null"},
        },
    )

    assert definition.configuration_schema["type"] == ("object", "null")


@pytest.mark.parametrize(
    "schema_location",
    ["configuration_schema", "exposed_slot_schemas"],
)
def test_subflow_definition_rejects_remote_references(schema_location: str) -> None:
    remote_reference_schema = {"$ref": "https://contracts.example/subflow.json"}
    definition_arguments: dict[str, Any] = {
        "ref": "contract@1",
        "description": "Reject a remotely resolved contract.",
        "configuration_schema": {"type": "object"},
    }
    if schema_location == "configuration_schema":
        definition_arguments["configuration_schema"] = remote_reference_schema
    else:
        definition_arguments["exposed_slots"] = frozenset({"external_slot"})
        definition_arguments["exposed_slot_schemas"] = {"external_slot": remote_reference_schema}

    with pytest.raises(
        ValueError,
        match=rf"{schema_location}.*only support local references",
    ):
        SubflowDefinition(**definition_arguments)


def test_subflow_definition_rejects_invalid_exposed_slot_schema() -> None:
    with pytest.raises(
        ValueError,
        match=r"exposed_slot_schemas\['external_slot'\] is not a valid JSON Schema",
    ):
        SubflowDefinition(
            ref="contract@1",
            description="Reject an invalid exposed slot contract.",
            configuration_schema={"type": "object"},
            exposed_slots=frozenset({"external_slot"}),
            exposed_slot_schemas={"external_slot": {"type": 42}},
        )


def test_subflow_definition_resolves_local_definitions_for_all_boundaries() -> None:
    definition = SubflowDefinition(
        ref="contract@1",
        description="Accept deterministic local schema references.",
        configuration_schema={
            "$defs": {
                "configuration": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"email": {"type": "string", "format": "email"}},
                    "required": ["email"],
                }
            },
            "$ref": "#/$defs/configuration",
        },
        exposed_slots=frozenset({"external_slot"}),
        exposed_slot_schemas={
            "external_slot": {
                "$defs": {"slot": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                "$ref": "#/$defs/slot",
            }
        },
    )
    registry = SubflowRegistry()
    registry.register(_ContractSubflow(), definition=definition)

    registry.validate_configuration(
        "contract@1",
        {"email": "citizen@example.com"},
        location="$.uses[0].with",
    )
    with pytest.raises(
        ValueError,
        match=r"\$\.uses\[0\]\.with\.email: .*not a 'email'",
    ):
        registry.validate_configuration(
            "contract@1",
            {"email": "not-an-email"},
            location="$.uses[0].with",
        )


@pytest.mark.parametrize(
    ("configuration_schema", "error_pattern"),
    [
        (
            {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
            "must use JSON Schema Draft 2020-12",
        ),
        (
            {"type": "object", "$defs": {}, "$ref": "#/$defs/missing"},
            "local schema reference does not resolve",
        ),
        (
            {"type": "object", "$dynamicRef": "#configuration"},
            r"do not support \$dynamicRef",
        ),
    ],
)
def test_subflow_definition_rejects_non_deterministic_configuration_schemas(
    configuration_schema: dict[str, Any],
    error_pattern: str,
) -> None:
    with pytest.raises(ValueError, match=error_pattern):
        SubflowDefinition(
            ref="contract@1",
            description="Reject a non-deterministic schema contract.",
            configuration_schema=configuration_schema,
        )


@pytest.mark.parametrize(
    "recursive_schema",
    [
        {"$ref": "#"},
        {
            "$defs": {
                "first": {"$ref": "#/$defs/second"},
                "second": {"$ref": "#/$defs/first"},
            },
            "$ref": "#/$defs/first",
        },
    ],
)
@pytest.mark.parametrize("schema_boundary", ["configuration", "exposed", "state"])
def test_subflow_definition_rejects_local_reference_cycles(
    recursive_schema: dict[str, Any],
    schema_boundary: str,
) -> None:
    definition_arguments: dict[str, Any] = {
        "ref": "contract@1",
        "description": "Reject recursive local schema resources.",
        "configuration_schema": {"type": "object"},
    }
    with pytest.raises(ValueError, match="local schema reference cycles"):
        if schema_boundary == "configuration":
            definition_arguments["configuration_schema"] = recursive_schema
        elif schema_boundary == "exposed":
            definition_arguments.update(
                exposed_slots=frozenset({"external_slot"}),
                exposed_slot_schemas={"external_slot": recursive_schema},
            )
        else:
            definition_arguments.update(
                exposed_slots=frozenset(),
                exposed_slot_schemas={},
                state_keys={
                    "auxiliary": SubflowStateKey("data", recursive_schema),
                },
            )
        SubflowDefinition(**definition_arguments)


@pytest.mark.parametrize(
    "configuration_schema",
    [
        {
            "type": "object",
            "required": ["required_value"],
            "properties": {"required_value": False},
        },
        {"type": "object", "propertyNames": False, "minProperties": 1},
        {
            "type": "object",
            "required": ["trigger"],
            "dependentSchemas": {"trigger": False},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "patternProperties": {".*": False},
            "minProperties": 1,
        },
        {
            "type": "object",
            "required": ["required_value"],
            "allOf": [
                {"properties": {"required_value": {"type": "string"}}},
                {"properties": {"required_value": {"type": "integer"}}},
            ],
        },
    ],
)
def test_subflow_definition_rejects_provably_impossible_object_boundaries(
    configuration_schema: dict[str, Any],
) -> None:
    with pytest.raises(
        ValueError,
        match="configuration_schema cannot validate any object-shaped",
    ):
        SubflowDefinition(
            ref="contract@1",
            description="Reject a provably impossible object contract.",
            configuration_schema=configuration_schema,
        )


def test_subflow_manifest_state_and_tool_contracts_are_immutable_and_explicit() -> None:
    address_definition = default_subflows().definition("address@1")

    assert address_definition.state_ownership_complete
    assert address_definition.required_tools == {"geocode": "1"}
    assert address_definition.owned_state_keys["address_completed"].partition == "data"
    assert "collect_address" in address_definition.node_identifiers
    with pytest.raises(TypeError):
        address_definition.required_tools["geocode"] = "2"  # type: ignore[index]
    with pytest.raises(TypeError):
        address_definition.owned_state_keys["address_completed"].schema["type"] = "string"  # type: ignore[index]


def test_legacy_manifest_preserves_known_exposures_without_claiming_complete_ownership() -> None:
    legacy_compatible_definition = SubflowDefinition(
        ref="contract@1",
        description="Preserve the previous custom-manifest surface.",
        configuration_schema={"type": "object"},
        exposed_slots=frozenset({"external_slot"}),
        exposed_slot_schemas={"external_slot": {"type": "string"}},
    )

    assert not legacy_compatible_definition.state_ownership_complete
    assert legacy_compatible_definition.owned_state_keys["external_slot"].partition == "data"
