"""Offline and explicitly opted-in Codex routing and extraction tests."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any

import pytest

from flowspec2 import AgentResponse, CodexAgent, FlowRuntime
from flowspec2.codex_transport import DEFAULT_CODEX_MODEL
from flowspec2.llm import build_route_request


@dataclass(frozen=True)
class FakeCodexTurn:
    text: str
    model: str = "effective-test-model"


class FakeCodexProvider:
    def __init__(self, response_texts: list[str], *, model: str = DEFAULT_CODEX_MODEL) -> None:
        self.model = model
        self.response_texts = list(response_texts)
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        messages: list[dict[str, str]],
        system: str,
        response_schema: dict[str, Any],
        ephemeral: bool,
        persist_session: bool,
        timeout_s: float,
    ) -> FakeCodexTurn:
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "response_schema": response_schema,
                "ephemeral": ephemeral,
                "persist_session": persist_session,
                "timeout_s": timeout_s,
            }
        )
        return FakeCodexTurn(self.response_texts.pop(0))


def test_codex_agent_routes_with_closed_ephemeral_output() -> None:
    fake_provider = FakeCodexProvider(['{"service":"repair_light"}'])
    agent = CodexAgent(provider=fake_provider, timeout_seconds=30.0)
    flows = [
        {
            "flow": "repair_light",
            "route": {
                "description": "Repair street lights.",
                "trigger_phrases": ["street light is off"],
            },
        }
    ]

    assert agent.route("the street light is off", flows) == "repair_light"

    provider_call = fake_provider.calls[0]
    expected_request = build_route_request("the street light is off", flows)
    assert provider_call["ephemeral"] is True
    assert provider_call["persist_session"] is False
    assert provider_call["timeout_s"] == 30.0
    assert provider_call["system"] == expected_request.system
    assert provider_call["messages"] == [{"role": "user", "content": expected_request.prompt}]
    assert '"trigger_phrases":["street light is off"]' in expected_request.prompt
    assert provider_call["response_schema"]["properties"]["service"]["enum"] == [
        "repair_light",
        None,
    ]


def test_codex_agent_rejects_output_outside_closed_extraction_schema() -> None:
    agent = CodexAgent(
        provider=FakeCodexProvider(
            [
                '{"defect":"Invented"}',
                '{"defect":"Off"}',
            ]
        )
    )
    agent_response = AgentResponse(
        description="What is wrong?",
        payload_schema={
            "type": "object",
            "properties": {"defect": {"enum": ["Off", "Flashing"]}},
            "required": ["defect"],
        },
    )

    assert agent.extract("invented", agent_response) == {}
    assert agent.extract("it is dark", agent_response) == {"defect": "Off"}


def test_codex_agent_projects_and_revalidates_correction_union() -> None:
    fake_provider = FakeCodexProvider(
        [
            '{"ticket_data_confirmed":null,"correcao":"address"}',
            '{"ticket_data_confirmed":true,"correcao":null}',
        ]
    )
    agent = CodexAgent(provider=fake_provider)
    agent_response = AgentResponse(
        description="Confirm?",
        payload_schema={
            "type": "object",
            "properties": {"ticket_data_confirmed": {"type": "boolean"}},
            "required": ["ticket_data_confirmed"],
            "x-flowspec2-correction-targets": ["address"],
        },
    )

    assert agent.extract("correct the address", agent_response) == {"correcao": "address"}
    assert agent.extract("yes", agent_response) == {"ticket_data_confirmed": True}
    provider_schema = fake_provider.calls[0]["response_schema"]
    assert "oneOf" not in provider_schema
    assert provider_schema["required"] == ["ticket_data_confirmed", "correcao"]
    assert provider_schema["properties"]["correcao"]["enum"] == ["address", None]


def test_codex_agent_rejects_injected_provider_model_mismatch() -> None:
    with pytest.raises(ValueError, match="provider model"):
        CodexAgent(model="requested-model", provider=FakeCodexProvider([], model="other-model"))


_RUN_CODEX = os.environ.get("FLOWSPEC2_RUN_CODEX_TESTS") == "1"
pytestmark_live = pytest.mark.skipif(
    not _RUN_CODEX,
    reason="set FLOWSPEC2_RUN_CODEX_TESTS=1 and provide the local llmgate project",
)


def _conversational(flow_document: dict[str, Any]) -> dict[str, Any]:
    conversational_document = copy.deepcopy(flow_document)
    conversational_document.pop("auto_flow", None)
    conversational_document["path"] = [
        path_step
        for path_step in conversational_document["path"]
        if path_step.get("confirm") != "service_confirmed"
    ]
    return conversational_document


@pytestmark_live
def test_codex_routes_in_and_out(luminaria_doc: dict[str, Any]) -> None:
    agent = CodexAgent()

    assert agent.route("a luz da minha rua apagou", [luminaria_doc]) == "reparo_luminaria"
    assert agent.route("quero pagar meu IPTU atrasado", [luminaria_doc]) != "reparo_luminaria"


@pytestmark_live
def test_codex_extracts_closed_token() -> None:
    agent = CodexAgent()
    agent_response = AgentResponse(
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

    extracted_payload = agent.extract(
        "tá tudo escuro, a luz não acende faz dias",
        agent_response,
    )

    assert extracted_payload.get("luminaria_defeito") == "Apagada"


@pytestmark_live
@pytest.mark.asyncio
async def test_codex_drives_full_conversation(luminaria_doc: dict[str, Any]) -> None:
    import asyncio

    agent = CodexAgent()
    runtime = FlowRuntime(_conversational(luminaria_doc))
    state = runtime.new_state("codex-e2e")
    state = await runtime.execute(state, {})
    for citizen_message in (
        "a luz tá apagada",
        "é uma luminária só",
        "fica na rua",
        "Rua das Acácias, 50, Centro",
        "sim, pode confirmar",
        "perto da escola",
        "prefiro não me identificar",
        "pode abrir o chamado",
    ):
        if state.status == "completed":
            break
        extracted_payload = await asyncio.to_thread(
            agent.extract,
            citizen_message,
            state.agent_response,
        )
        state = await runtime.execute(state, extracted_payload)

    assert state.status == "completed"
    assert state.data.get("protocol_id", "").startswith("SGRC-")
