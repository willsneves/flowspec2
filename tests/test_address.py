"""Address subflow configuration and exhaustion contracts."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from flowspec2 import FlowRuntime, ToolRegistry, default_tool_registry


def _address_flow_document(
    *,
    required: bool | None = True,
    address_required: bool = True,
    needs_confirmation: bool = False,
    max_attempts: int = 1,
    on_exhaust: str = "reask",
) -> dict[str, Any]:
    subflow_configuration: dict[str, Any] = {
        "needs_confirmation": needs_confirmation,
        "max_attempts": max_attempts,
        "on_exhaust": on_exhaust,
    }
    if required is not None:
        subflow_configuration["required"] = required
    return {
        "schema": "flowspec/2",
        "flow": "address_contract",
        "version": "1.0.0",
        "route": {"description": "Collect an address."},
        "config": {"address_required": address_required, "max_attempts": max_attempts},
        "domains": {"AddressText": {"type": "free_text"}},
        "path": [{"use": "address@1"}],
        "uses": [{"ref": "address@1", "with": subflow_configuration}],
    }


def _unresolved_address_registry() -> ToolRegistry:
    tool_registry = default_tool_registry()

    async def unresolved_geocode(**inputs: Any) -> dict[str, Any]:
        del inputs
        return {"status": "not_found", "error": "address not found"}

    tool_registry.register("geocode", unresolved_geocode)
    return tool_registry


async def test_optional_address_accepts_explicit_null_and_uses_config_fallback() -> None:
    flow_document = _address_flow_document(required=None, address_required=False)
    runtime = FlowRuntime(flow_document)
    state = runtime.new_state("optional-address")

    state = await runtime.execute(state, {})
    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    assert state.agent_response.payload_schema["properties"]["address"]["type"] == [
        "string",
        "null",
    ]

    state = await runtime.execute(state, {"address": None})

    assert state.status == "completed"
    assert state.data["address_completed"] is True
    assert state.data["address_skipped"] is True
    assert "address" not in state.data


async def test_required_address_treats_null_as_a_failed_attempt() -> None:
    runtime = FlowRuntime(_address_flow_document(max_attempts=2))
    state = runtime.new_state("required-address")

    state = await runtime.execute(state, {"address": None})

    assert state.status == "progress"
    assert state.data["address_attempts"] == 1
    assert state.agent_response is not None
    assert state.agent_response.error_message == "the address is required"
    assert state.agent_response.payload_schema is not None
    assert state.agent_response.payload_schema["properties"]["address"]["type"] == "string"


async def test_address_reask_resets_attempt_budget_after_exhaustion() -> None:
    runtime = FlowRuntime(
        _address_flow_document(on_exhaust="reask"),
        tools=_unresolved_address_registry(),
    )

    state = await runtime.execute(runtime.new_state("address-reask"), {"address": "missing"})

    assert state.status == "progress"
    assert "address_attempts" not in state.data
    assert state.agent_response is not None
    assert state.agent_response.error_message == "maximum attempts reached; let us try again"


async def test_address_skip_advances_without_storing_a_default() -> None:
    runtime = FlowRuntime(
        _address_flow_document(on_exhaust="skip"),
        tools=_unresolved_address_registry(),
    )

    state = await runtime.execute(runtime.new_state("address-skip"), {"address": "missing"})

    assert state.status == "completed"
    assert state.data["address_completed"] is True
    assert state.data["address_skipped"] is True
    assert "address" not in state.data


async def test_address_default_advances_with_an_explicit_null_value() -> None:
    runtime = FlowRuntime(
        _address_flow_document(on_exhaust="default"),
        tools=_unresolved_address_registry(),
    )

    state = await runtime.execute(runtime.new_state("address-default"), {"address": "missing"})

    assert state.status == "completed"
    assert state.data["address_completed"] is True
    assert state.data["address_defaulted"] is True
    assert state.data["address"] is None


async def test_address_handoff_pauses_with_the_handoff_message() -> None:
    runtime = FlowRuntime(
        _address_flow_document(on_exhaust="handoff"),
        tools=_unresolved_address_registry(),
    )

    state = await runtime.execute(runtime.new_state("address-handoff"), {"address": "missing"})

    assert state.status == "progress"
    assert state.agent_response is not None
    assert "support agent" in state.agent_response.description
    assert state.data["address_attempts"] == 1


async def test_address_end_completes_with_a_correlated_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = FlowRuntime(
        _address_flow_document(on_exhaust="END"),
        tools=_unresolved_address_registry(),
    )

    with caplog.at_level(logging.WARNING, logger="flowspec2.subflows.address"):
        state = await runtime.execute(
            runtime.new_state("address-end"),
            {"address": "missing"},
        )

    assert state.status == "completed"
    assert state.agent_response is not None
    assert state.agent_response.log_id is not None
    assert any(
        getattr(log_record, "log_id", None) == state.agent_response.log_id
        for log_record in caplog.records
    )


async def test_geocode_call_failure_is_correlated_and_retryable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool_registry = default_tool_registry()
    geocode_calls = 0

    async def flaky_geocode(address: str) -> dict[str, Any]:
        nonlocal geocode_calls
        geocode_calls += 1
        if geocode_calls == 1:
            raise RuntimeError("geocoder unavailable")
        return {
            "status": "ok",
            "needs_confirmation": False,
            "address": {
                "street": address,
                "kind": "street",
                "district": "Centro",
                "city": "Rio de Janeiro",
            },
        }

    tool_registry.register("geocode", flaky_geocode)
    runtime = FlowRuntime(_address_flow_document(), tools=tool_registry)

    with caplog.at_level(logging.ERROR, logger="flowspec2.nodes"):
        state = await runtime.execute(
            runtime.new_state("address-tool-retry"),
            {"address": "Street das Flores, 100"},
        )

    assert state.status == "error"
    assert state.agent_response is not None
    assert state.agent_response.log_id is not None
    assert "address" not in state.data
    assert any(
        getattr(log_record, "log_id", None) == state.agent_response.log_id
        for log_record in caplog.records
    )

    state = await runtime.execute(state, {"address": "Street das Flores, 100"})

    assert state.status == "completed"
    assert state.data["address"]["street"] == "Street das Flores, 100"


@pytest.mark.parametrize(
    "malformed_geocode_result",
    [
        {"status": "ok", "needs_confirmation": False, "address": {}},
        {"status": "ok", "needs_confirmation": False, "address": "not-an-object"},
    ],
)
async def test_geocode_success_requires_a_meaningful_address(
    malformed_geocode_result: dict[str, Any],
) -> None:
    tool_registry = default_tool_registry()

    async def malformed_geocode(address: str) -> dict[str, Any]:
        del address
        return malformed_geocode_result

    tool_registry.register("geocode", malformed_geocode)
    runtime = FlowRuntime(_address_flow_document(), tools=tool_registry)

    state = await runtime.execute(
        runtime.new_state("address-contract-failure"),
        {"address": "Street das Flores, 100"},
    )

    assert state.status == "error"
    assert state.agent_response is not None
    assert state.agent_response.log_id is not None
    assert "address" not in state.data


async def test_ambiguous_address_confirmation_reasks_without_clearing_address() -> None:
    runtime = FlowRuntime(_address_flow_document(needs_confirmation=True, max_attempts=2))
    state = await runtime.execute(
        runtime.new_state("ambiguous-address-confirmation"),
        {"address": "Street das Flores, 100"},
    )

    state = await runtime.execute(state, {"confirmation": "talvez"})

    assert state.status == "progress"
    assert state.data["address"]["street"] == "Street das Flores, 100"
    assert state.data["address_attempts"] == 1
    assert state.agent_response is not None
    assert state.agent_response.error_message == (
        "the response could not be interpreted as yes or no"
    )

    state = await runtime.execute(state, {"confirmation": "yes"})

    assert state.status == "completed"
    assert state.data["address_confirmed"] is True
    assert "address_attempts" not in state.data


async def test_ambiguous_address_confirmation_uses_exhaustion_policy() -> None:
    runtime = FlowRuntime(
        _address_flow_document(
            needs_confirmation=True,
            max_attempts=1,
            on_exhaust="skip",
        )
    )
    state = await runtime.execute(
        runtime.new_state("ambiguous-address-exhaustion"),
        {"address": "Street das Flores, 100"},
    )

    state = await runtime.execute(state, {"confirmation": None})

    assert state.status == "completed"
    assert state.data["address_skipped"] is True
    assert "address" not in state.data


async def test_negative_address_confirmation_alone_clears_and_recollects() -> None:
    runtime = FlowRuntime(_address_flow_document(needs_confirmation=True, max_attempts=2))
    state = await runtime.execute(
        runtime.new_state("negative-address-confirmation"),
        {"address": "Street das Flores, 100"},
    )

    state = await runtime.execute(state, {"confirmation": "no"})

    assert state.status == "progress"
    assert "address" not in state.data
    assert "address_completed" not in state.data
    assert "address_attempts" not in state.data
    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    assert "complete address" in state.agent_response.description.lower()
