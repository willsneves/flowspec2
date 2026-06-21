"""Runtime concerns: terminal idempotency + the as_tool() adapter."""

from __future__ import annotations

from flowspec2 import FlowRuntime, ToolRegistry, default_tool_registry

from conftest import step


def _counting_registry() -> tuple[ToolRegistry, dict]:
    reg = default_tool_registry()
    counter = {"opens": 0}

    async def counting_open(**inputs):
        counter["opens"] += 1
        return {"status": "success", "protocolo": "SGRC-FIXED-1", "message": "ok"}

    reg.register("sgrc_open_ticket", counting_open)
    return reg, counter


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
