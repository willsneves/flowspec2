"""Authorability: the buraco example runs, and a brand-new flow authored inline
from a one-line description compiles and runs without touching the library."""

from __future__ import annotations

from conftest import require_agent_response, step

from flowspec2 import FlowRuntime, validate_flow


async def test_buraco_runs_end_to_end(buraco):
    st = await step(buraco, None, {"buraco_tipo": "buraco", "buraco_tamanho": "grande"})
    assert st.data["buraco_tipo"] == "Buraco no asfalto"
    assert st.data["buraco_tamanho"] == "Grande"
    st = await step(buraco, st, {"address": "Av. Brasil, 1000"})
    st = await step(buraco, st, {"confirmacao": "sim"})
    st = await step(buraco, st, {"identification_method": "anonimo"})
    assert (require_agent_response(st).interactive or {})["field"] == "confirmacao"
    st = await step(buraco, st, {"confirmacao": "sim"})
    assert st.status == "completed"
    assert st.data["protocol_id"].startswith("SGRC-")


# "Citizen reports an abandoned/overgrown vacant lot (mato alto, lixo, or both),
#  needs the address, optional identification, opens a ticket." — authored as JSON,
#  no Python, no subflow changes: just a domain, two slots, a flat path, two `use`s.
ABANDONED_LOT_FLOW = {
    "schema": "flowspec/2",
    "flow": "terreno_baldio",
    "version": "1.0.0",
    "service": {"id": "27001"},
    "route": {
        "description": "Denúncia de terreno baldio: mato alto, lixo acumulado ou foco de insetos."
    },
    "config": {"address_required": True, "identification_required": False, "max_attempts": 3},
    "domains": {
        "ProblemaTerreno": {
            "type": "categorical",
            "values": ["Mato alto", "Lixo acumulado", "Ambos"],
            "normalize": {
                "accent_fold": True,
                "synonyms": {"mato": "Mato alto", "lixo": "Lixo acumulado", "os dois": "Ambos"},
            },
        },
        "SimNao": {"type": "bool", "normalize": {"affirmation": True}},
    },
    "slots": {
        "problema_terreno": {"domain": "ProblemaTerreno", "required": True},
        "ticket_data_confirmed": {"domain": "SimNao"},
    },
    "path": [
        {
            "step": "collect_problema",
            "slot": "problema_terreno",
            "prompt": {"text": "Qual o problema no terreno?"},
            "interactive": {
                "kind": "buttons",
                "field": "problema_terreno",
                "from_domain": "ProblemaTerreno",
            },
        },
        {"use": "address@1"},
        {"use": "identification@2"},
        {
            "step": "confirm_ticket_data",
            "confirm": "ticket_data_confirmed",
            "correctable": True,
            "prompt": {"text": "Confirma a denúncia?"},
            "interactive": {"kind": "buttons", "field": "confirmacao", "from_domain": "SimNao"},
        },
        {"terminal": True},
    ],
    "uses": [
        {"ref": "address@1", "with": {"required": True, "needs_confirmation": True}},
        {"ref": "identification@2", "with": {"required": False}},
    ],
    "confirm": {
        "step": "confirm_ticket_data",
        "slot": "ticket_data_confirmed",
        "on_confirm": "open_ticket",
        "correctable": ["problema_terreno", "address", "cpf", "email", "name"],
    },
    "terminal": {
        "step": "open_ticket",
        "tool": "sgrc_open_ticket",
        "idempotent": True,
        "input": [
            {"param": "problema", "slot": "problema_terreno"},
            {"param": "endereco", "slot": "address"},
            {"param": "solicitante", "slot": "cpf"},
        ],
        "outputs": {"protocol_id": "result.protocolo"},
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    },
    "capabilities": {
        "media_in": {"analyze": ["image"], "route_by": "workflow_sugerido"},
        "session_reset": True,
    },
}


def test_new_flow_validates_against_schema():
    validate_flow(ABANDONED_LOT_FLOW)


async def test_new_flow_compiles_and_runs():
    rt = FlowRuntime(ABANDONED_LOT_FLOW)
    st = await step(rt, None, {"problema_terreno": "mato"})  # synonym -> "Mato alto"
    assert st.data["problema_terreno"] == "Mato alto"
    st = await step(rt, st, {"address": "Rua do Lote, 7"})
    st = await step(rt, st, {"confirmacao": "sim"})  # confirm address
    st = await step(rt, st, {"identification_method": "anonimo"})
    assert (require_agent_response(st).interactive or {})["field"] == "confirmacao"
    st = await step(rt, st, {"confirmacao": "sim"})  # confirm ticket -> open
    assert st.status == "completed"
    assert st.data["protocol_id"].startswith("SGRC-")
