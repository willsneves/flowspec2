"""Profile-aware semantic checking contracts."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from flowspec2 import FlowRuntime, check_flow
from flowspec2.nodes import FlowContext
from flowspec2.profiles import FlowProfile, reference_profile
from flowspec2.subflows import SubflowBuild, SubflowDefinition, default_subflows
from flowspec2.tools import ToolDefinition, ToolRegistry, default_tool_registry


class _ConfigurationFormatSubflow:
    name = "configuration_format"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        del ctx, with_cfg
        raise AssertionError("profile checking must not build subflow implementations")


class _LegacySubflow:
    name = "legacy_contract"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        del ctx, with_cfg
        raise AssertionError("profile checking must not build subflow implementations")


async def _contract_tool(**inputs: Any) -> dict[str, Any]:
    del inputs
    return {"status": "ok"}


def test_reference_profile_resolves_default_subflow_exposures(
    streetlight_document: dict[str, Any],
) -> None:
    report = check_flow(streetlight_document, compile_document=False)

    assert report.is_valid
    assert report.diagnostics == ()


@pytest.mark.asyncio
async def test_reference_subflow_tools_link_and_execute_end_to_end(
    streetlight_document: dict[str, Any],
) -> None:
    report = check_flow(streetlight_document, compile_document=False)
    runtime = FlowRuntime(streetlight_document)

    updated_state = await runtime.execute(
        runtime.new_state(user_id="transitive-tools-user"),
        {},
    )

    assert report.is_valid
    assert updated_state.agent_response is not None


def test_profile_reports_all_unavailable_tools_and_subflows(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["entry"] = {"tool": "missing_entry"}
    invalid_flow["terminal"]["tool"] = "missing_terminal"
    empty_reference_profile = reference_profile()
    empty_profile = FlowProfile(
        identifier="test/empty@1",
        tools=type(empty_reference_profile.tools)(),
        subflows=type(empty_reference_profile.subflows)(),
        capabilities=frozenset(),
        domain_types=empty_reference_profile.domain_types,
    )

    report = check_flow(invalid_flow, compile_document=False, profile=empty_profile)

    assert {
        "FLOWSPEC_PROFILE_TOOL_UNAVAILABLE",
        "FLOWSPEC_PROFILE_SUBFLOW_UNAVAILABLE",
        "FLOWSPEC_PROFILE_CAPABILITY_UNAVAILABLE",
    } <= {diagnostic.code for diagnostic in report.diagnostics}


def test_profile_rejects_subflow_configuration_before_compilation(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["uses"][0]["with"]["service_id"] = "not-an-address-option"

    report = check_flow(invalid_flow, compile_document=False)

    assert "FLOWSPEC_PROFILE_SUBFLOW_CONFIGURATION_INVALID" in {
        diagnostic.code for diagnostic in report.diagnostics
    }


def test_profile_enforces_subflow_configuration_formats(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["uses"].append(
        {"ref": "configuration_format@1", "with": {"email": "not-an-email"}}
    )
    terminal_index = next(
        index for index, path_step in enumerate(invalid_flow["path"]) if "terminal" in path_step
    )
    invalid_flow["path"].insert(terminal_index, {"use": "configuration_format@1"})
    subflows = default_subflows()
    subflows.register(
        _ConfigurationFormatSubflow(),
        definition=SubflowDefinition(
            ref="configuration_format@1",
            description="Validate configuration formats during profile linking.",
            configuration_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"email": {"type": "string", "format": "email"}},
                "required": ["email"],
            },
            exposed_slots=frozenset(),
            exposed_slot_schemas={},
        ),
    )

    report = check_flow(
        invalid_flow,
        compile_document=False,
        profile=reference_profile(subflows=subflows),
    )

    assert any(
        diagnostic.code == "FLOWSPEC_PROFILE_SUBFLOW_CONFIGURATION_INVALID"
        and diagnostic.path.endswith("/with/email")
        for diagnostic in report.diagnostics
    )


def test_profile_rejects_missing_transitive_subflow_tool(
    pothole_document: dict[str, Any],
) -> None:
    empty_tools = ToolRegistry()

    report = check_flow(
        pothole_document,
        compile_document=False,
        profile=reference_profile(tools=empty_tools),
    )

    assert any(
        diagnostic.code == "FLOWSPEC_PROFILE_SUBFLOW_TOOL_UNAVAILABLE"
        and "geocode" in diagnostic.message
        for diagnostic in report.diagnostics
    )


def test_profile_rejects_wrong_transitive_subflow_tool_version(
    pothole_document: dict[str, Any],
) -> None:
    wrong_version_tools = ToolRegistry()
    wrong_version_tools.register(
        "geocode",
        _contract_tool,
        definition=ToolDefinition(
            name="geocode",
            version="2",
            description="Deliberately incompatible geocoder contract.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
    )

    report = check_flow(
        pothole_document,
        compile_document=False,
        profile=reference_profile(tools=wrong_version_tools),
    )

    assert "FLOWSPEC_PROFILE_SUBFLOW_TOOL_VERSION_MISMATCH" in {
        diagnostic.code for diagnostic in report.diagnostics
    }


def test_profile_rejects_authored_writer_for_subflow_owned_auxiliary_state(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["slots"]["address_completed"] = {
        "domain": "YesNo",
        "required": False,
    }

    report = check_flow(invalid_flow, compile_document=False)

    assert any(
        diagnostic.code == "FLOWSPEC_PROFILE_SUBFLOW_STATE_COLLISION"
        and diagnostic.path == "/slots/address_completed"
        for diagnostic in report.diagnostics
    )


def test_profile_rejects_derive_entry_and_terminal_subflow_state_collisions(
    pothole_document: dict[str, Any],
) -> None:
    collision_documents: list[tuple[dict[str, Any], str]] = []

    derive_collision = copy.deepcopy(pothole_document)
    derive_collision["derive"] = [
        {
            "writes": "address_completed",
            "from": ["pothole_type"],
            "after": "collect_type",
            "lookup": {"Asphalt pothole": "completed"},
            "default": "pending",
        }
    ]
    collision_documents.append((derive_collision, "/derive/0/writes"))

    entry_collision = copy.deepcopy(pothole_document)
    entry_collision["entry"] = {
        "tool": "hub_search",
        "writes": "address_completed",
    }
    collision_documents.append((entry_collision, "/entry/writes"))

    terminal_collision = copy.deepcopy(pothole_document)
    terminal_collision["terminal"]["outputs"]["address_completed"] = "result.status"
    collision_documents.append((terminal_collision, "/terminal/outputs/address_completed"))

    for invalid_flow, expected_path in collision_documents:
        report = check_flow(invalid_flow, compile_document=False)

        assert any(
            diagnostic.code == "FLOWSPEC_PROFILE_SUBFLOW_STATE_COLLISION"
            and diagnostic.path == expected_path
            for diagnostic in report.diagnostics
        )


def test_legacy_subflow_provenance_is_explicit_and_requires_profile_opt_in(
    pothole_document: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(pothole_document)
    terminal_index = next(
        index for index, path_step in enumerate(flow_document["path"]) if "terminal" in path_step
    )
    flow_document["uses"].append({"ref": "legacy_contract@1", "with": {}})
    flow_document["path"].insert(terminal_index, {"use": "legacy_contract@1"})
    subflows = default_subflows()
    subflows.register(_LegacySubflow())
    reference = reference_profile(subflows=subflows)

    rejected_report = check_flow(
        flow_document,
        compile_document=False,
        profile=reference,
    )
    compatible_profile = FlowProfile(
        identifier="test/legacy-compatible@1",
        tools=reference.tools,
        subflows=reference.subflows,
        capabilities=reference.capabilities,
        domain_types=reference.domain_types,
        allow_legacy_contracts=True,
    )
    compatible_report = check_flow(
        flow_document,
        compile_document=False,
        profile=compatible_profile,
    )

    definition = subflows.definition("legacy_contract@1")
    assert definition.legacy_manifest
    assert definition.as_dict()["legacy_manifest"] is True
    assert "FLOWSPEC_PROFILE_LEGACY_SUBFLOW_MANIFEST_FORBIDDEN" in {
        diagnostic.code for diagnostic in rejected_report.diagnostics
    }
    assert "FLOWSPEC_PROFILE_LEGACY_SUBFLOW_MANIFEST_FORBIDDEN" not in {
        diagnostic.code for diagnostic in compatible_report.diagnostics
    }


def test_legacy_tool_provenance_requires_profile_opt_in(
    pothole_document: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(pothole_document)
    flow_document["terminal"]["tool"] = "legacy_terminal"
    tools = default_tool_registry()
    tools.register("legacy_terminal", _contract_tool)
    reference = reference_profile(tools=tools)

    rejected_report = check_flow(
        flow_document,
        compile_document=False,
        profile=reference,
    )
    compatible_profile = FlowProfile(
        identifier="test/legacy-compatible@1",
        tools=reference.tools,
        subflows=reference.subflows,
        capabilities=reference.capabilities,
        domain_types=reference.domain_types,
        allow_legacy_contracts=True,
    )
    compatible_report = check_flow(
        flow_document,
        compile_document=False,
        profile=compatible_profile,
    )

    assert tools.definition("legacy_terminal").legacy_contract
    assert "FLOWSPEC_PROFILE_LEGACY_TOOL_CONTRACT_FORBIDDEN" in {
        diagnostic.code for diagnostic in rejected_report.diagnostics
    }
    assert "FLOWSPEC_PROFILE_LEGACY_TOOL_CONTRACT_FORBIDDEN" not in {
        diagnostic.code for diagnostic in compatible_report.diagnostics
    }


def test_legacy_await_contract_requires_profile_opt_in(
    streetlight_document: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(streetlight_document)
    flow_document["capabilities"]["await_external"].pop("resume")
    reference = reference_profile()

    rejected_report = check_flow(
        flow_document,
        compile_document=False,
        profile=reference,
    )
    compatible_profile = FlowProfile(
        identifier="test/legacy-compatible@1",
        tools=reference.tools,
        subflows=reference.subflows,
        capabilities=reference.capabilities,
        domain_types=reference.domain_types,
        allow_legacy_contracts=True,
    )
    compatible_report = check_flow(
        flow_document,
        compile_document=False,
        profile=compatible_profile,
    )

    assert "FLOWSPEC_PROFILE_LEGACY_AWAIT_CONTRACT_FORBIDDEN" in {
        diagnostic.code for diagnostic in rejected_report.diagnostics
    }
    assert "FLOWSPEC_PROFILE_LEGACY_AWAIT_CONTRACT_FORBIDDEN" not in {
        diagnostic.code for diagnostic in compatible_report.diagnostics
    }


def test_profile_contract_projection_is_deterministic_and_owned() -> None:
    profile = reference_profile()

    first_contract = profile.as_dict()
    second_contract = profile.as_dict()
    first_contract["capabilities"].append("mutated")

    assert first_contract != second_contract
    assert profile.as_dict() == second_contract
    assert json.loads(profile.canonical_json()) == second_contract
