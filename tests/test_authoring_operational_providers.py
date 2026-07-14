"""Provider adapters for report-only operational probes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import pytest

from flowspec2.authoring import (
    OPERATIONAL_PROMPT_DIGEST,
    OPERATIONAL_PROMPT_FORMAT,
    CodexOperationalExecutor,
    GeminiOperationalExecutor,
    OperationalProbeRequest,
    OperationalProviderError,
)
from flowspec2.codex_transport import DEFAULT_CODEX_MODEL
from flowspec2.llm import build_route_request


@dataclass(frozen=True)
class FakeProviderTurn:
    text: str
    model: str = "effective-codex-model"


class FakeCodexProvider:
    def __init__(self, response_text: str) -> None:
        self.model = DEFAULT_CODEX_MODEL
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    async def run(self, **arguments: Any) -> FakeProviderTurn:
        self.calls.append(arguments)
        return FakeProviderTurn(self.response_text)


@dataclass(frozen=True)
class FakeGeminiResponse:
    text: str
    model_version: str | None = "effective-gemini-model"


class FakeGeminiModels:
    def __init__(self, provider_response: FakeGeminiResponse) -> None:
        self.provider_response = provider_response
        self.calls: list[dict[str, object]] = []

    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: dict[str, object],
    ) -> FakeGeminiResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self.provider_response


class FakeGeminiClient:
    def __init__(self, provider_response: FakeGeminiResponse) -> None:
        self._models = FakeGeminiModels(provider_response)
        self.closed = False

    @property
    def models(self) -> FakeGeminiModels:
        return self._models

    def close(self) -> None:
        self.closed = True


def _request() -> OperationalProbeRequest:
    return OperationalProbeRequest(
        probe_identifier="route-probe",
        operation="route",
        system_instruction="Route the request.",
        prompt="Citizen request.",
        response_schema_json=(
            '{"additionalProperties":false,"properties":{"service":{"enum":["flow",null]}},'
            '"required":["service"],"type":"object"}'
        ),
    )


def test_codex_operational_executor_uses_ephemeral_closed_request() -> None:
    fake_provider = FakeCodexProvider('{"service":"flow"}')
    executor = CodexOperationalExecutor(
        provider=fake_provider,
        sdk_version="test-public-provider",
        timeout_seconds=45.0,
    )

    model_response = executor(_request())

    assert model_response.raw_output == '{"service":"flow"}'
    assert model_response.effective_model_version is None
    provider_call = fake_provider.calls[0]
    assert provider_call["ephemeral"] is True
    assert provider_call["persist_session"] is False
    assert provider_call["system"] == "Route the request."
    assert provider_call["response_schema"] == _request().response_schema()
    provenance = executor.provenance()
    assert provenance.prompt_format == OPERATIONAL_PROMPT_FORMAT
    assert provenance.prompt_digest == OPERATIONAL_PROMPT_DIGEST
    assert provenance.generation_configuration_json.find("chatgpt_subscription") > 0


def test_gemini_operational_executor_captures_effective_model_and_exact_schema() -> None:
    fake_client = FakeGeminiClient(FakeGeminiResponse('{"service":"flow"}'))
    executor = GeminiOperationalExecutor(
        model="gemini-test",
        client=fake_client,
        sdk_version="test-google-genai",
    )

    model_response = executor(_request())

    assert model_response.effective_model_version == "effective-gemini-model"
    provider_call = fake_client.models.calls[0]
    assert provider_call["contents"] == "Citizen request."
    configuration = provider_call["config"]
    assert isinstance(configuration, dict)
    assert configuration["system_instruction"] == "Route the request."
    assert configuration["response_json_schema"] == _request().response_schema()
    assert executor.provenance().prompt_digest == OPERATIONAL_PROMPT_DIGEST


def test_gemini_operational_executor_rejects_missing_effective_model() -> None:
    fake_client = FakeGeminiClient(FakeGeminiResponse('{"service":"flow"}', model_version=None))
    executor = GeminiOperationalExecutor(
        client=fake_client,
        sdk_version="test-google-genai",
    )

    with pytest.raises(OperationalProviderError, match="effective model"):
        executor(_request())


def test_owned_gemini_client_is_closed_but_injected_client_is_not() -> None:
    fake_client = FakeGeminiClient(FakeGeminiResponse('{"service":"flow"}'))
    executor = GeminiOperationalExecutor(
        client=fake_client,
        sdk_version="test-google-genai",
    )

    executor.close()

    assert fake_client.closed is False
    with pytest.raises(OperationalProviderError, match="closed"):
        executor(_request())


@pytest.mark.skipif(
    os.environ.get("FLOWSPEC2_RUN_CODEX_TESTS") != "1",
    reason="set FLOWSPEC2_RUN_CODEX_TESTS=1 and provide the local public-provider project",
)
def test_codex_operational_executor_routes_from_trigger_example_live() -> None:
    structured_request = build_route_request(
        "the street light is off",
        [
            {
                "flow": "repair_light",
                "route": {
                    "description": "Repair public street lights.",
                    "trigger_phrases": ["street light is off"],
                },
            }
        ],
    )
    operational_request = OperationalProbeRequest(
        probe_identifier="live-route-trigger",
        operation="route",
        system_instruction=structured_request.system,
        prompt=structured_request.prompt,
        response_schema_json=json.dumps(
            structured_request.response_schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )

    with CodexOperationalExecutor() as executor:
        model_response = executor(operational_request)

    assert json.loads(model_response.raw_output) == {"service": "repair_light"}
    assert model_response.effective_model_version is None
