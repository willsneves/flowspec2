"""LLM driver tests.

Helper tests (schema → field spec) always run offline. Integration tests hit a
real Gemini and only run when GEMINI_API_KEY is set AND FLOWSPEC2_RUN_LLM_TESTS=1
(so the default `uv run pytest` never makes network calls / costs).
"""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flowspec2 import FlowRuntime
from flowspec2.llm import (
    EXTRACT_SYS,
    ROUTE_SYS,
    GeminiAgent,
    _enum_of,
    _extraction_response_schema,
    _fields_spec,
    _route_response_schema,
    build_extraction_request,
    build_route_request,
)
from flowspec2.models import CORRECTION_TARGETS_SCHEMA_KEY, AgentResponse

# ── offline unit tests for the schema→field-spec mapping ─────────────────────


def test_enum_of_plain_and_anyof_nullable():
    assert _enum_of({"enum": ["a", "b"]}) == ["a", "b"]
    assert _enum_of({"anyOf": [{"enum": ["x"]}, {"type": "null"}]}) == ["x", None]
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
    assert _fields_spec(ar) == [("luminaria_defeito", "closed", ["Apagada", "Piscando"], False)]


def test_fields_spec_preserves_boolean_kind_for_nullable_schema():
    response = AgentResponse(
        description="?",
        payload_schema={
            "type": "object",
            "properties": {"optional_confirmation": {"type": ["boolean", "null"]}},
            "required": ["optional_confirmation"],
        },
    )

    assert _fields_spec(response) == [("optional_confirmation", "bool", None, True)]


def test_fields_spec_preserves_numeric_kinds_for_nullable_schemas() -> None:
    response = AgentResponse(
        description="?",
        payload_schema={
            "type": "object",
            "properties": {
                "quantity": {"type": "integer"},
                "distance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            },
            "required": ["quantity", "distance"],
        },
    )

    assert _fields_spec(response) == [
        ("quantity", "integer", None, False),
        ("distance", "number", None, True),
    ]


def test_fields_spec_falls_back_to_interactive_buttons():
    ar = AgentResponse(
        description="?",
        interactive={
            "field": "confirmacao",
            "buttons": [{"id": "sim", "title": "Sim"}, {"id": "nao", "title": "Não"}],
        },
    )
    assert _fields_spec(ar) == [("confirmacao", "bool", None, False)]
    ar2 = AgentResponse(
        description="?",
        interactive={
            "field": "identification_method",
            "buttons": [{"id": "cpf", "title": "CPF"}, {"id": "govbr", "title": "Gov.br"}],
        },
    )
    assert _fields_spec(ar2) == [("identification_method", "closed", ["cpf", "govbr"], False)]


def _capture_extraction_prompt(agent_response: AgentResponse) -> str:
    return build_extraction_request("resposta", agent_response).prompt


def test_extraction_prompt_uses_exact_json_scalar_types_and_null() -> None:
    prompt = _capture_extraction_prompt(
        AgentResponse(
            description="Informe os valores.",
            payload_schema={
                "type": "object",
                "properties": {
                    "quantity": {"type": "integer"},
                    "distance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "label": {"type": ["string", "null"]},
                    "enabled": {"type": ["boolean", "null"]},
                    "choice": {"enum": ["alpha", None]},
                },
                "required": ["quantity", "distance", "label", "enabled", "choice"],
            },
        )
    )

    assert '"quantity": número inteiro em JSON, sem aspas' in prompt
    assert '"distance": número finito em JSON, sem aspas ou null' in prompt
    assert '"label": string JSON com o texto informado ou null' in prompt
    assert '"enabled": true (sim/afirmativo), false (não/negativo) ou null' in prompt
    assert '"choice": um destes valores EXATOS: ["alpha", null]' in prompt


def test_extraction_prompt_only_mentions_correction_for_correction_hub() -> None:
    ordinary_response = AgentResponse(
        description="Informe.",
        payload_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )
    correction_response = ordinary_response.model_copy(
        update={
            "payload_schema": {
                **(ordinary_response.payload_schema or {}),
                CORRECTION_TARGETS_SCHEMA_KEY: ["address", "cpf"],
            }
        }
    )

    assert "CORRIGIR" not in _capture_extraction_prompt(ordinary_response)
    assert "CORRIGIR" in _capture_extraction_prompt(correction_response)


def test_route_response_schema_closes_service_to_catalog() -> None:
    response_schema = _route_response_schema(
        [
            {"flow": "repair_light"},
            {"flow": "repair_road"},
            {"flow": "repair_light"},
        ]
    )

    assert response_schema["additionalProperties"] is False
    assert response_schema["properties"]["service"]["enum"] == [
        "repair_light",
        "repair_road",
        None,
    ]


def test_route_request_renders_description_first_and_trigger_phrases_as_examples() -> None:
    request = build_route_request(
        "o poste da esquina apagou",
        [
            {
                "flow": "repair_light",
                "route": {
                    "description": "Registra defeitos na iluminação pública.",
                    "trigger_phrases": ["poste apagado", "luz piscando"],
                },
            },
            {
                "flow": "repair_road",
                "route": {"description": "Registra defeitos no pavimento."},
            },
        ],
    )

    assert request.system == ROUTE_SYS
    assert request.prompt == (
        "Catálogo de serviços em JSON:\n"
        '[{"description":"Registra defeitos na iluminação pública.","service":"repair_light",'
        '"trigger_phrases":["poste apagado","luz piscando"]},{"description":"Registra '
        'defeitos no pavimento.","service":"repair_road","trigger_phrases":[]}]\n\n'
        "Use description como a definição principal de cada serviço. trigger_phrases contém "
        "apenas exemplos de mensagens compatíveis; não trate esses exemplos como lista "
        "exclusiva nem como garantia de correspondência.\n\n"
        'Mensagem do cidadão: "o poste da esquina apagou"\n\n'
        'Devolva {"service": "<nome do serviço>"} se algum atende, ou {"service": null} se '
        "nenhum atende."
    )
    assert request.response_schema["properties"]["service"]["enum"] == [
        "repair_light",
        "repair_road",
        None,
    ]


def test_extraction_request_preserves_extract_hint_in_closed_response_schema() -> None:
    request = build_extraction_request(
        "tá tudo escuro",
        AgentResponse(
            description="Qual é o defeito?",
            payload_schema={
                "type": "object",
                "properties": {
                    "defect": {
                        "description": "Mapeie escuridão para Off.",
                        "enum": ["Off", "Flashing"],
                    }
                },
                "required": ["defect"],
            },
        ),
    )

    assert request.system == EXTRACT_SYS
    assert request.prompt == (
        'Pergunta do bot: "Qual é o defeito?"\n'
        'Campos a extrair:\n- "defect": um destes valores EXATOS: ["Off", "Flashing"]\n\n'
        'Resposta do cidadão: "tá tudo escuro"\n\n'
        "Regras:\n- Use SOMENTE os valores permitidos nas listas fechadas.\n"
        "- Devolva apenas o JSON com os campos pedidos."
    )
    assert request.response_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "defect": {
                "description": "Mapeie escuridão para Off.",
                "enum": ["Off", "Flashing"],
            }
        },
        "required": ["defect"],
    }


def test_extraction_response_schema_supports_payload_or_correction() -> None:
    response_schema = _extraction_response_schema(
        AgentResponse(
            description="?",
            payload_schema={
                "type": "object",
                "properties": {"defect": {"enum": ["off", "flashing"]}},
                "required": ["defect"],
                CORRECTION_TARGETS_SCHEMA_KEY: ["defect", "address"],
            },
        )
    )

    assert response_schema["additionalProperties"] is False
    assert response_schema["properties"]["defect"] == {"enum": ["off", "flashing"]}
    assert response_schema["properties"]["correcao"] == {"enum": ["defect", "address"]}
    assert {tuple(branch["required"]) for branch in response_schema["oneOf"]} == {
        ("defect",),
        ("correcao",),
    }


def test_extraction_response_schema_does_not_offer_correction_outside_hub() -> None:
    response_schema = _extraction_response_schema(
        AgentResponse(
            description="?",
            payload_schema={
                "type": "object",
                "properties": {"defect": {"enum": ["off", "flashing"]}},
                "required": ["defect"],
            },
        )
    )

    assert response_schema["required"] == ["defect"]
    assert "correcao" not in response_schema["properties"]


def test_gemini_request_uses_json_schema_and_rejects_invalid_provider_output() -> None:
    captured_config: list[Any] = []

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> SimpleNamespace:
            captured_config.append(kwargs["config"])
            return SimpleNamespace(text='{"service":"unknown"}')

    agent = object.__new__(GeminiAgent)
    agent.model = "test-model"
    cast(Any, agent).client = SimpleNamespace(models=FakeModels())
    response_schema = _route_response_schema([{"flow": "repair_light"}])

    assert agent._json("system", "prompt", response_schema) == {}
    assert captured_config[0].response_json_schema == response_schema


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
