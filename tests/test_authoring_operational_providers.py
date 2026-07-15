"""Provider adapters for report-only operational probes."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from flowspec2.authoring import (
    OPERATIONAL_PROMPT_DIGEST,
    GeminiOperationalExecutor,
    OperationalProbeRequest,
    OperationalProviderError,
)


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
