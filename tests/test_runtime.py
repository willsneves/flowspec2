"""Runtime concerns: terminal idempotency + the as_tool() adapter."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from conftest import require_agent_response, step
from pydantic import ValidationError

import flowspec2.nodes as flow_nodes
from flowspec2 import (
    AgentResponse,
    FlowRuntime,
    ServiceMetadata,
    ServiceState,
    SnowflakeIdGenerator,
    ToolRegistry,
    default_tool_registry,
)
from flowspec2.observability import SNOWFLAKE_EPOCH_MILLISECONDS


def _counting_registry() -> tuple[ToolRegistry, dict]:
    reg = default_tool_registry()
    counter = {"opens": 0}

    async def counting_open(**inputs):
        counter["opens"] += 1
        return {"status": "success", "protocol_id": "REQ-FIXED-1", "message": "ok"}

    reg.register("open_service_request", counting_open)
    return reg, counter


async def _failing_entry_tool(**inputs: Any) -> dict[str, Any]:
    del inputs
    raise RuntimeError("entry unavailable")


async def _unused_successful_tool(**inputs: Any) -> dict[str, Any]:
    del inputs
    return {"status": "success"}


class _ReplacementTerminal:
    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(self, **inputs: Any) -> dict[str, Any]:
        del inputs
        self.call_count += 1
        return {"status": "success", "protocol_id": "MUTATED"}


async def _drive_pothole_to_open(rt: FlowRuntime, user: str):
    st = await step(rt, None, {"pothole_type": "pothole", "pothole_size": "large"}, user=user)
    st = await step(rt, st, {"address": "Street Y, 50"}, user=user)
    st = await step(rt, st, {"confirmation": "yes"}, user=user)
    st = await step(rt, st, {"identification_method": "anonymous"}, user=user)
    st = await step(rt, st, {"confirmation": "yes"}, user=user)
    return st


@pytest.mark.parametrize(
    ("model_type", "model_arguments"),
    [
        (AgentResponse, {"descriptin": "typo"}),
        (ServiceMetadata, {"saveed": True}),
        (ServiceState, {"user_id": "user", "service_name": "service", "statsu": "error"}),
    ],
)
def test_runtime_models_reject_unknown_contract_fields(
    model_type: type[Any],
    model_arguments: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model_type(**model_arguments)


async def test_terminal_idempotency_replays_on_identical_inputs(pothole_document):
    reg, counter = _counting_registry()
    # two independent runtimes share the registry (and its replay cache); same
    # user + identical answers => the side effect fires ONCE.
    rt1 = FlowRuntime(pothole_document, tools=reg)
    rt2 = FlowRuntime(pothole_document, tools=reg)
    s1 = await _drive_pothole_to_open(rt1, user="dup")
    s2 = await _drive_pothole_to_open(rt2, user="dup")
    assert s1.status == "completed" and s2.status == "completed"
    assert s1.data["protocol_id"] == s2.data["protocol_id"] == "REQ-FIXED-1"
    assert counter["opens"] == 1  # replayed, not re-fired


async def test_terminal_idempotency_namespace_changes_with_flow_revision(
    pothole_document: dict[str, Any],
) -> None:
    registry, counter = _counting_registry()
    upgraded_document = copy.deepcopy(pothole_document)
    upgraded_document["version"] = "1.0.1"
    first_runtime = FlowRuntime(pothole_document, tools=registry)
    upgraded_runtime = FlowRuntime(upgraded_document, tools=registry)

    first_state = await _drive_pothole_to_open(first_runtime, user="revision-isolation")
    upgraded_state = await _drive_pothole_to_open(
        upgraded_runtime,
        user="revision-isolation",
    )

    assert first_state.status == "completed"
    assert upgraded_state.status == "completed"
    assert counter["opens"] == 2


async def test_terminal_resolves_every_output_before_committing_state(
    pothole_document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_document = copy.deepcopy(pothole_document)
    flow_document["terminal"]["outputs"] = {
        "protocol_id": "result.protocol_id",
        "duplicate_protocol": "result.protocol_id",
    }
    original_dig = flow_nodes._dig
    resolved_output_count = 0

    def fail_after_first_output(tool_result: Any, result_path: str) -> Any:
        nonlocal resolved_output_count
        resolved_output_count += 1
        if resolved_output_count == 2:
            raise KeyError("simulated second-output resolution failure")
        return original_dig(tool_result, result_path)

    monkeypatch.setattr(flow_nodes, "_dig", fail_after_first_output)
    runtime = FlowRuntime(flow_document)

    state = await _drive_pothole_to_open(runtime, user="atomic-terminal-output")

    assert state.status == "error"
    assert "protocol_id" not in state.data
    assert "duplicate_protocol" not in state.data
    assert state.agent_response is not None
    assert state.agent_response.log_id is not None


async def test_as_tool_adapter(pothole_document):
    rt = FlowRuntime(pothole_document)
    tool = rt.as_tool()
    out = await tool("pothole_repair", "5521000", {"pothole_type": "large crater"})
    assert out["status"] == "progress"
    assert "payload_schema" in out and out["payload_schema"]["properties"]  # asks size next
    assert out["data"]["pothole_type"] == "Crater"


async def test_as_tool_serializes_load_execute_save_for_one_user_without_lock_leaks(
    pothole_document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FlowRuntime(pothole_document)
    first_tool = runtime.as_tool()
    second_tool = runtime.as_tool()
    first_call_started = asyncio.Event()
    release_first_call = asyncio.Event()

    async def controlled_execute(
        state: ServiceState,
        payload: dict[str, Any] | None = None,
    ) -> ServiceState:
        answer_payload = payload or {}
        answer_name = str(answer_payload["answer"])
        if answer_name == "first":
            first_call_started.set()
            await release_first_call.wait()
        state.data[answer_name] = True
        return state

    monkeypatch.setattr(runtime, "execute", controlled_execute)
    first_call = asyncio.ensure_future(
        first_tool(runtime.flow, "concurrent-user", {"answer": "first"})
    )
    await first_call_started.wait()
    second_call = asyncio.ensure_future(
        second_tool(runtime.flow, "concurrent-user", {"answer": "second"})
    )
    await asyncio.sleep(0)
    release_first_call.set()

    _, second_output = await asyncio.gather(first_call, second_call)

    assert runtime._store["concurrent-user"].data["first"] is True
    assert runtime._store["concurrent-user"].data["second"] is True
    assert second_output["data"]["first"] is True
    assert second_output["data"]["second"] is True
    assert runtime._user_execution_locks == {}


async def test_as_tool_does_not_serialize_different_users_globally(
    pothole_document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FlowRuntime(pothole_document)
    tool = runtime.as_tool()
    blocked_user_started = asyncio.Event()
    release_blocked_user = asyncio.Event()
    independent_user_executed = asyncio.Event()

    async def controlled_execute(
        state: ServiceState,
        payload: dict[str, Any] | None = None,
    ) -> ServiceState:
        del payload
        if state.user_id == "blocked-user":
            blocked_user_started.set()
            await release_blocked_user.wait()
        else:
            independent_user_executed.set()
        state.data["executed"] = True
        return state

    monkeypatch.setattr(runtime, "execute", controlled_execute)
    blocked_call = asyncio.ensure_future(tool(runtime.flow, "blocked-user", {}))
    await blocked_user_started.wait()
    independent_call = asyncio.ensure_future(tool(runtime.flow, "independent-user", {}))
    try:
        await asyncio.wait_for(independent_user_executed.wait(), timeout=1)
        independent_output = await independent_call
    finally:
        release_blocked_user.set()
        await asyncio.gather(blocked_call, independent_call)

    assert independent_output["data"]["executed"] is True
    assert runtime._user_execution_locks == {}


async def test_as_tool_rejects_a_service_name_from_another_flow(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)

    with pytest.raises(ValueError, match="does not match runtime flow"):
        await runtime.as_tool()("another_flow", "wrong-service", {})

    assert "wrong-service" not in runtime._store
    assert runtime._user_execution_locks == {}


async def test_as_tool_returns_owned_deep_copies(pothole_document: dict[str, Any]) -> None:
    runtime = FlowRuntime(pothole_document)
    user_id = "owned-output"

    tool_output = await runtime.as_tool()(runtime.flow, user_id, {})
    stored_state = runtime._store[user_id]
    stored_data = copy.deepcopy(stored_state.data)
    assert stored_state.agent_response is not None
    stored_payload_schema = copy.deepcopy(stored_state.agent_response.payload_schema)
    stored_interactive = copy.deepcopy(stored_state.agent_response.interactive)

    tool_output["data"]["service"]["id"] = "caller-mutation"
    tool_output["payload_schema"]["properties"]["pothole_type"]["description"] = "caller-mutation"
    tool_output["interactive"]["sections"][0]["rows"][0]["title"] = "caller-mutation"

    assert stored_state.data == stored_data
    assert stored_state.agent_response.payload_schema == stored_payload_schema
    assert stored_state.agent_response.interactive == stored_interactive


def test_new_state_validates_normalizes_and_owns_declared_slot_seeds(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)
    caller_data: dict[str, Any] = {
        "pothole_type": "large crater",
        "host_metadata": {"labels": ["restored"]},
    }

    state = runtime.new_state("normalized-seed", caller_data)
    caller_data["host_metadata"]["labels"].append("caller-mutation")

    assert state.data["pothole_type"] == "Crater"
    assert state.data["host_metadata"] == {"labels": ["restored"]}

    with pytest.raises(ValueError) as validation_error:
        runtime.new_state("invalid-seed", {"pothole_type": {"unexpected": True}})
    assert "pothole_type" in str(validation_error.value)


def test_new_state_places_declared_slot_seeds_in_their_partition(
    pothole_document: dict[str, Any],
) -> None:
    internal_slot_document = copy.deepcopy(pothole_document)
    internal_slot_document["slots"]["ticket_data_confirmed"]["persist"] = "internal"
    runtime = FlowRuntime(internal_slot_document)

    state = runtime.new_state(
        "internal-seed",
        {"ticket_data_confirmed": "yes", "host_metadata": {"restored": True}},
    )

    assert "ticket_data_confirmed" not in state.data
    assert state.internal["ticket_data_confirmed"] is True
    assert state.data["host_metadata"] == {"restored": True}


def test_new_state_validates_profile_exposed_slot_seeds(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)

    state = runtime.new_state(
        "valid-exposed-seed",
        {
            "address": {
                "street": "Street da Assembleia",
                "kind": "street",
                "district": "Centro",
                "city": "Rio de Janeiro",
            }
        },
    )

    assert state.data["address"]["district"] == "Centro"
    with pytest.raises(ValueError, match=r"address.*not valid"):
        runtime.new_state("invalid-exposed-seed", {"address": "invalid"})
    with pytest.raises(ValueError, match=r"address"):
        runtime.new_state("empty-exposed-seed", {"address": {}})
    with pytest.raises(ValueError, match=r"address"):
        runtime.new_state("blank-exposed-seed", {"address": {"street": ""}})


@pytest.mark.parametrize("invalid_host_value", [object(), {"not-json"}, math.nan])
def test_new_state_rejects_non_json_host_values(
    pothole_document: dict[str, Any],
    invalid_host_value: object,
) -> None:
    runtime = FlowRuntime(pothole_document)

    with pytest.raises(ValueError, match=r"host_metadata"):
        runtime.new_state("invalid-host-json", {"host_metadata": invalid_host_value})


async def test_execute_rejects_invalid_restored_slot_before_running_flow(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)
    state = runtime.new_state("invalid-restored-state")
    state.data["pothole_type"] = "not-a-domain-value"

    restored_state = await runtime.execute(state, {"pothole_size": "Large"})

    agent_response = require_agent_response(restored_state)
    assert restored_state.status == "error"
    assert "pothole_type" in (agent_response.error_message or "")
    assert agent_response.log_id is not None
    assert "pothole_size" not in restored_state.data


async def test_execute_rejects_non_json_turn_payload_before_graph_effects(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)

    state = await runtime.execute(
        runtime.new_state("invalid-turn-json"),
        {"unknown": {"not-json"}},
    )

    agent_response = require_agent_response(state)
    assert state.status == "error"
    assert "unsupported Python type set" in (agent_response.error_message or "")
    assert agent_response.log_id is not None
    assert state.payload == {}


async def test_execute_rejects_restored_slot_in_the_wrong_partition(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)
    state = runtime.new_state("mispartitioned-restored-state")
    state.internal["pothole_type"] = "Crater"

    restored_state = await runtime.execute(state, {})

    agent_response = require_agent_response(restored_state)
    assert restored_state.status == "error"
    assert "belongs in 'data'" in (agent_response.error_message or "")
    assert agent_response.log_id is not None


async def test_execute_rejects_invalid_restored_profile_exposed_slot(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)
    state = runtime.new_state("invalid-restored-exposed-state")
    state.data["address"] = "invalid"
    state.internal["address_completed"] = True

    restored_state = await runtime.execute(state, {})

    agent_response = require_agent_response(restored_state)
    assert restored_state.status == "error"
    assert "address" in (agent_response.error_message or "")
    assert agent_response.log_id is not None


@pytest.mark.parametrize("partition_name", ["internal", "payload"])
async def test_execute_rejects_profile_exposed_slot_in_non_data_partition(
    pothole_document: dict[str, Any],
    partition_name: str,
) -> None:
    runtime = FlowRuntime(pothole_document)
    state = runtime.new_state(f"mispartitioned-exposed-{partition_name}")
    getattr(state, partition_name)["address"] = {
        "street": "Assembly Street",
    }

    restored_state = await runtime.execute(state, {})

    agent_response = require_agent_response(restored_state)
    assert restored_state.status == "error"
    assert "belongs in 'data'" in (agent_response.error_message or "")
    assert agent_response.log_id is not None


async def test_failed_restore_validation_does_not_partially_normalize_state(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)
    state = runtime.new_state("atomic-restored-validation")
    state.data["pothole_type"] = "large crater"
    state.data["address"] = {}

    restored_state = await runtime.execute(state, {})

    assert restored_state.status == "error"
    assert restored_state.data["pothole_type"] == "large crater"
    assert restored_state.data["address"] == {}


async def test_execute_rejects_state_from_another_flow_contract(
    pothole_document: dict[str, Any],
) -> None:
    runtime = FlowRuntime(pothole_document)
    state = runtime.new_state("stale-flow-contract")
    state.metadata.flow_ir_digest = "0" * 64

    restored_state = await runtime.execute(state, {})

    agent_response = require_agent_response(restored_state)
    assert restored_state.status == "error"
    assert "flow_ir_digest" in (agent_response.error_message or "")
    assert agent_response.log_id is not None


async def test_execute_rejects_state_when_profile_catalog_changes(
    pothole_document: dict[str, Any],
) -> None:
    original_runtime = FlowRuntime(pothole_document)
    state = original_runtime.new_state("compatible-expanded-profile")
    expanded_registry = default_tool_registry()
    expanded_registry.register("unused_profile_tool", _unused_successful_tool)
    expanded_runtime = FlowRuntime(pothole_document, tools=expanded_registry)

    restored_state = await expanded_runtime.execute(state, {})

    agent_response = require_agent_response(restored_state)
    assert restored_state.status == "error"
    assert "profile_digest" in (agent_response.error_message or "")
    assert agent_response.log_id is not None


async def test_execute_rejects_mutated_used_tool_binding_before_effects(
    pothole_document: dict[str, Any],
) -> None:
    registry = default_tool_registry()
    runtime = FlowRuntime(pothole_document, tools=registry)
    state = runtime.new_state("mutated-tool-binding")
    replacement_terminal = _ReplacementTerminal()
    registry.register("open_service_request", replacement_terminal)
    state = await runtime.execute(
        state,
        {"pothole_type": "pothole"},
    )

    assert state.status == "error"
    assert state.agent_response is not None
    assert "changed after this runtime was compiled" in (state.agent_response.error_message or "")
    assert replacement_terminal.call_count == 0


async def test_non_blocking_entry_failure_is_logged_and_flow_continues(
    streetlight_document: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = default_tool_registry()
    registry.register("hub_search", _failing_entry_tool)
    runtime = FlowRuntime(streetlight_document, tools=registry)

    with caplog.at_level(logging.WARNING, logger="flowspec2.nodes"):
        state = await runtime.execute(
            runtime.new_state("entry-log"),
            {"_source": "whatsapp_flow"},
        )

    assert state.internal["_entry_done"] is True
    entry_failure_records = [
        record
        for record in caplog.records
        if record.getMessage() == "Non-blocking entry tool failed"
    ]
    assert len(entry_failure_records) == 1
    assert getattr(entry_failure_records[0], "operation", None) == "__init__"
    assert getattr(entry_failure_records[0], "tool", None) == "hub_search"
    assert str(getattr(entry_failure_records[0], "log_id", "")).isdigit()


async def test_error_response_and_tool_adapter_propagate_the_log_id(
    pothole_document: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    generator = SnowflakeIdGenerator(
        worker_id=21,
        clock_milliseconds=lambda: SNOWFLAKE_EPOCH_MILLISECONDS + 500,
    )
    runtime = FlowRuntime(pothole_document, log_id_generator=generator)

    with caplog.at_level(logging.WARNING, logger="flowspec2.runtime"):
        state = await runtime.execute(
            runtime.new_state("correlated-error"),
            {"pothole_type": "outside the domain"},
        )

    agent_response = require_agent_response(state)
    assert agent_response.error_message is not None
    assert agent_response.log_id is not None
    assert agent_response.log_id.isdigit()
    assert any(
        getattr(record, "log_id", None) == agent_response.log_id for record in caplog.records
    )

    tool_output = await runtime.as_tool()(
        "pothole_repair",
        "correlated-tool-error",
        {"pothole_type": "also outside the domain"},
    )
    assert tool_output["error_message"]
    assert str(tool_output["log_id"]).isdigit()


async def test_runtime_uses_the_injected_clock_for_state_creation_and_touch(
    pothole_document: dict[str, Any],
) -> None:
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    updated_at = created_at + timedelta(minutes=5)
    clock_timestamps = iter((created_at, updated_at))
    runtime = FlowRuntime(pothole_document, clock=clock_timestamps.__next__)

    state = runtime.new_state("metadata-clock")

    assert state.metadata.created_at == created_at
    assert state.metadata.updated_at == created_at

    state = await runtime.execute(state, {"pothole_type": "pothole"})

    assert state.metadata.created_at == created_at
    assert state.metadata.updated_at == updated_at


async def test_auto_flow_uses_the_injected_clock_when_it_saves_state(
    streetlight_document: dict[str, Any],
) -> None:
    created_at = datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc)
    updated_at = created_at + timedelta(minutes=2)
    clock_timestamps = iter((created_at, updated_at))
    runtime = FlowRuntime(streetlight_document, clock=clock_timestamps.__next__)

    state = await runtime.execute(runtime.new_state("auto-flow-clock"), {})

    assert state.metadata.saved is True
    assert state.metadata.created_at == created_at
    assert state.metadata.updated_at == updated_at


def test_runtime_rejects_an_injected_clock_without_timezone(
    pothole_document: dict[str, Any],
) -> None:
    naive_timestamp = datetime(2026, 7, 13, 14, 0)
    runtime = FlowRuntime(pothole_document, clock=lambda: naive_timestamp)

    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.new_state("naive-metadata-clock")
