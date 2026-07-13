"""LLM driver tests.

Helper tests (schema → field spec) always run offline. Integration tests hit a
real Gemini and only run when GEMINI_API_KEY is set AND FLOWSPEC2_RUN_LLM_TESTS=1
(so the default `uv run pytest` never makes network calls / costs).
"""

from __future__ import annotations

import copy
import os

import pytest

from flowspec2 import FlowRuntime
from flowspec2.llm import _enum_of, _fields_spec
from flowspec2.models import AgentResponse

# ── offline unit tests for the schema→field-spec mapping ─────────────────────


def test_enum_of_plain_and_anyof_nullable():
    assert _enum_of({"enum": ["a", "b"]}) == ["a", "b"]
    assert _enum_of({"anyOf": [{"enum": ["x"]}, {"type": "null"}]}) == ["x", "null"]
    assert _enum_of({"type": "string"}) is None


def test_fields_spec_from_payload_schema():
    ar = AgentResponse(
        description="?",
        payload_schema={
            "type": "object",
            "properties": {"luminaria_defeito": {"enum": ["Apagada", "Piscando"]}},
            "required": ["luminaria_defeito"],
        },
    )
    assert _fields_spec(ar) == [("luminaria_defeito", "closed", ["Apagada", "Piscando"])]


def test_fields_spec_falls_back_to_interactive_buttons():
    ar = AgentResponse(
        description="?",
        interactive={
            "field": "confirmacao",
            "buttons": [{"id": "sim", "title": "Sim"}, {"id": "nao", "title": "Não"}],
        },
    )
    assert _fields_spec(ar) == [("confirmacao", "bool", None)]
    ar2 = AgentResponse(
        description="?",
        interactive={
            "field": "identification_method",
            "buttons": [{"id": "cpf", "title": "CPF"}, {"id": "govbr", "title": "Gov.br"}],
        },
    )
    assert _fields_spec(ar2) == [("identification_method", "closed", ["cpf", "govbr"])]


# ── gated integration tests (real Gemini) ───────────────────────────────────

_RUN_LLM = (
    bool(os.environ.get("GEMINI_API_KEY")) and os.environ.get("FLOWSPEC2_RUN_LLM_TESTS") == "1"
)
pytestmark_integration = pytest.mark.skipif(
    not _RUN_LLM, reason="set GEMINI_API_KEY + FLOWSPEC2_RUN_LLM_TESTS=1"
)


def _conversational(doc: dict) -> dict:
    d = copy.deepcopy(doc)
    d.pop("auto_flow", None)
    d["path"] = [s for s in d["path"] if s.get("confirm") != "service_confirmed"]
    return d


@pytestmark_integration
def test_gemini_routes_in_and_out(luminaria_doc):
    from flowspec2.llm import GeminiAgent

    agent = GeminiAgent()
    assert agent.route("a luz da minha rua apagou", [luminaria_doc]) == "reparo_luminaria"
    assert agent.route("quero pagar meu IPTU atrasado", [luminaria_doc]) != "reparo_luminaria"


@pytestmark_integration
def test_gemini_extracts_closed_token():
    from flowspec2.llm import GeminiAgent

    agent = GeminiAgent()
    ar = AgentResponse(
        description="Qual o problema na luminária?",
        payload_schema={
            "type": "object",
            "properties": {
                "luminaria_defeito": {
                    "enum": [
                        "Apagada",
                        "Piscando",
                        "Acesa de dia",
                        "Pendurada",
                        "Danificada",
                        "Com ruído",
                    ]
                }
            },
            "required": ["luminaria_defeito"],
        },
    )
    out = agent.extract("tá tudo escuro, a luz não acende faz dias", ar)
    assert out.get("luminaria_defeito") == "Apagada"


@pytestmark_integration
async def test_gemini_drives_full_conversation(luminaria_doc):
    import asyncio

    from flowspec2.llm import GeminiAgent

    agent = GeminiAgent()
    rt = FlowRuntime(_conversational(luminaria_doc))
    state = rt.new_state("llm-e2e")
    state = await rt.execute(state, {})  # enter -> asks defect
    for msg in [
        "a luz tá apagada",  # defect -> Apagada
        "é uma luminária só",  # quantidade -> uma (no intercaladas branch)
        "fica na rua",  # localizacao -> Rua (quadra gated off)
        "Rua das Acácias, 50, Centro",  # address
        "sim, pode confirmar",  # confirm address
        "perto da escola",  # ponto de referência
        "prefiro não me identificar",  # anonimo
        "pode abrir o chamado",  # confirm ticket -> open
    ]:
        if state.status == "completed":
            break
        payload = await asyncio.to_thread(agent.extract, msg, state.agent_response)
        state = await rt.execute(state, payload)
    assert state.status == "completed"
    assert state.data.get("protocol_id", "").startswith("SGRC-")
