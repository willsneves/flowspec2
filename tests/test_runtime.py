"""Runtime concerns: terminal idempotency + the as_tool() adapter."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from conftest import require_agent_response, step

from flowspec2 import FlowRuntime, SnowflakeIdGenerator, ToolRegistry, default_tool_registry
from flowspec2.observability import SNOWFLAKE_EPOCH_MILLISECONDS


def _counting_registry() -> tuple[ToolRegistry, dict]:
    reg = default_tool_registry()
    counter = {"opens": 0}

    async def counting_open(**inputs):
        counter["opens"] += 1
        return {"status": "success", "protocolo": "SGRC-FIXED-1", "message": "ok"}

    reg.register("sgrc_open_ticket", counting_open)
    return reg, counter


async def _failing_entry_tool(**inputs: Any) -> dict[str, Any]:
    del inputs
    raise RuntimeError("entry unavailable")


async def _drive_buraco_to_open(rt: FlowRuntime, user: str):
    st = await step(rt, None, {"buraco_tipo": "buraco", "buraco_tamanho": "grande"}, user=user)
    st = await step(rt, st, {"address": "Rua Y, 50"}, user=user)
    st = await step(rt, st, {"confirmacao": "sim"}, user=user)
    st = await step(rt, st, {"identification_method": "anonimo"}, user=user)
    st = await step(rt, st, {"confirmacao": "sim"}, user=user)
    return st


async def test_terminal_idempotency_replays_on_identical_inputs(buraco_doc):
    reg, counter = _counting_registry()
    # two independent runtimes share the registry (and its replay cache); same
    # user + identical answers => the side effect fires ONCE.
    rt1 = FlowRuntime(buraco_doc, tools=reg)
    rt2 = FlowRuntime(buraco_doc, tools=reg)
    s1 = await _drive_buraco_to_open(rt1, user="dup")
    s2 = await _drive_buraco_to_open(rt2, user="dup")
    assert s1.status == "completed" and s2.status == "completed"
    assert s1.data["protocol_id"] == s2.data["protocol_id"] == "SGRC-FIXED-1"
    assert counter["opens"] == 1  # replayed, not re-fired


async def test_as_tool_adapter(buraco_doc):
    rt = FlowRuntime(buraco_doc)
    tool = rt.as_tool()
    out = await tool("reparo_buraco", "5521000", {"buraco_tipo": "cratera enorme"})
    assert out["status"] == "progress"
    assert "payload_schema" in out and out["payload_schema"]["properties"]  # asks tamanho next
    assert out["data"]["buraco_tipo"] == "Cratera"


async def test_non_blocking_entry_failure_is_logged_and_flow_continues(
    luminaria_doc: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = default_tool_registry()
    registry.register("hub_search", _failing_entry_tool)
    runtime = FlowRuntime(luminaria_doc, tools=registry)

    with caplog.at_level(logging.WARNING, logger="flowspec2.nodes"):
        state = await runtime.execute(
            runtime.new_state("entry-log"),
            {"_source": "whatsapp_flow"},
        )

    assert state.data["_entry_done"] is True
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
    buraco_doc: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    generator = SnowflakeIdGenerator(
        worker_id=21,
        clock_milliseconds=lambda: SNOWFLAKE_EPOCH_MILLISECONDS + 500,
    )
    runtime = FlowRuntime(buraco_doc, log_id_generator=generator)

    with caplog.at_level(logging.WARNING, logger="flowspec2.runtime"):
        state = await runtime.execute(
            runtime.new_state("correlated-error"),
            {"buraco_tipo": "fora do domínio"},
        )

    agent_response = require_agent_response(state)
    assert agent_response.error_message is not None
    assert agent_response.log_id is not None
    assert agent_response.log_id.isdigit()
    assert any(
        getattr(record, "log_id", None) == agent_response.log_id for record in caplog.records
    )

    tool_output = await runtime.as_tool()(
        "reparo_buraco",
        "correlated-tool-error",
        {"buraco_tipo": "também fora do domínio"},
    )
    assert tool_output["error_message"]
    assert str(tool_output["log_id"]).isdigit()


async def test_runtime_uses_the_injected_clock_for_state_creation_and_touch(
    buraco_doc: dict[str, Any],
) -> None:
    created_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    updated_at = created_at + timedelta(minutes=5)
    clock_timestamps = iter((created_at, updated_at))
    runtime = FlowRuntime(buraco_doc, clock=clock_timestamps.__next__)

    state = runtime.new_state("metadata-clock")

    assert state.metadata.created_at == created_at
    assert state.metadata.updated_at == created_at

    state = await runtime.execute(state, {"buraco_tipo": "buraco"})

    assert state.metadata.created_at == created_at
    assert state.metadata.updated_at == updated_at


async def test_auto_flow_uses_the_injected_clock_when_it_saves_state(
    luminaria_doc: dict[str, Any],
) -> None:
    created_at = datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc)
    updated_at = created_at + timedelta(minutes=2)
    clock_timestamps = iter((created_at, updated_at))
    runtime = FlowRuntime(luminaria_doc, clock=clock_timestamps.__next__)

    state = await runtime.execute(runtime.new_state("auto-flow-clock"), {})

    assert state.metadata.saved is True
    assert state.metadata.created_at == created_at
    assert state.metadata.updated_at == updated_at


def test_runtime_rejects_an_injected_clock_without_timezone(
    buraco_doc: dict[str, Any],
) -> None:
    naive_timestamp = datetime(2026, 7, 13, 14, 0)
    runtime = FlowRuntime(buraco_doc, clock=lambda: naive_timestamp)

    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.new_state("naive-metadata-clock")
