"""Codex transport for the provider-neutral authoring repair loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from flowspec2.authoring import (
    CODEX_AUTHOR_PROMPT_DIGEST,
    CODEX_AUTHOR_PROMPT_FORMAT,
    AuthoringRequest,
    CodexAuthor,
    CodexAuthorError,
    load_reference_authoring_corpus,
    project_flow_document,
    run_authoring_benchmark,
)
from flowspec2.codex_transport import DEFAULT_CODEX_MODEL, CodexProvider
from flowspec2.diagnostics import FlowDiagnostic
from flowspec2.profiles import reference_profile


@dataclass(frozen=True)
class FakeCodexTurn:
    text: str
    model: str = "effective-test-model"


class FakeCodexProvider:
    def __init__(
        self,
        response_texts: list[str] | None = None,
        *,
        model: str = DEFAULT_CODEX_MODEL,
    ) -> None:
        self.model = model
        self.response_texts = list(response_texts or [])
        self.calls: list[dict[str, Any]] = []
        self.provider_error: Exception | None = None

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
        if self.provider_error is not None:
            raise self.provider_error
        return FakeCodexTurn(self.response_texts.pop(0))


def _request(
    *, correction: bool = False, format_identifier: str = "flowspec/2"
) -> AuthoringRequest:
    benchmark_case = load_reference_authoring_corpus().cases[0]
    profile = reference_profile()
    diagnostics = (
        FlowDiagnostic(
            code="FLOWSPEC_TEST_DIAGNOSTIC",
            severity="error",
            path="/path",
            message="Replace the invalid path.",
            suggested_fix="Restore the required path.",
        ),
    )
    return AuthoringRequest(
        task=benchmark_case.authoring_task(),
        format_identifier=format_identifier,
        profile_identifier=profile.identifier,
        profile_contract_json=profile.canonical_json(),
        correction_round=1 if correction else 0,
        previous_source='{"invalid":true}' if correction else None,
        previous_diagnostics=diagnostics if correction else (),
    )


def _projection_response(flow_source: str) -> str:
    projection = project_flow_document(json.loads(flow_source))
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_codex_author_uses_ephemeral_closed_output_and_subscription_provenance() -> None:
    expected_source = load_reference_authoring_corpus().cases[0].expected_flow_json
    fake_provider = FakeCodexProvider([_projection_response(expected_source)], model="test-model")
    author = CodexAuthor(
        model="test-model",
        effort="high",
        timeout_seconds=45.0,
        provider=fake_provider,
        sdk_version="0.43.0",
    )

    authored_response = author(_request())

    assert authored_response.source == expected_source
    assert authored_response.effective_model_version is None
    provider_call = fake_provider.calls[0]
    assert provider_call["ephemeral"] is True
    assert provider_call["persist_session"] is False
    assert provider_call["timeout_s"] == 45.0
    prompt_document = json.loads(provider_call["messages"][0]["content"])
    assert prompt_document["task"] == _request().task.prompt
    assert prompt_document["acceptance_contract"] == _request().task.acceptance.to_dict()
    assert provider_call["response_schema"]["additionalProperties"] is False
    provenance = author.provenance()
    assert provenance.identifier == "openai_codex"
    assert provenance.model == "test-model"
    assert provenance.sdk == "llmgate"
    assert provenance.sdk_version == "0.43.0"
    assert provenance.prompt_format == CODEX_AUTHOR_PROMPT_FORMAT
    assert provenance.prompt_digest == CODEX_AUTHOR_PROMPT_DIGEST
    assert json.loads(provenance.generation_configuration_json)["billing_mode"] == (
        "chatgpt_subscription"
    )


@pytest.mark.parametrize(
    "response_text",
    [
        "",
        "not-json",
        "[]",
        '{"format":"wrong"}',
        '{"format":"flowspec2/authoring-projection","format":"duplicate"}',
    ],
)
def test_codex_author_rejects_invalid_projection_envelopes(response_text: str) -> None:
    author = CodexAuthor(provider=FakeCodexProvider([response_text]))

    with pytest.raises(CodexAuthorError, match="projection envelope"):
        author(_request())


def test_codex_author_wraps_provider_failures_without_leaking_details() -> None:
    fake_provider = FakeCodexProvider()
    provider_failure = RuntimeError("sentinel-secret-provider-message")
    fake_provider.provider_error = provider_failure
    author = CodexAuthor(provider=fake_provider)

    with pytest.raises(CodexAuthorError) as captured_error:
        author(_request())

    assert captured_error.value.__cause__ is not None
    assert "sentinel-secret-provider-message" not in str(captured_error.value)


def test_codex_author_rejects_unsupported_format_before_provider_call() -> None:
    fake_provider = FakeCodexProvider()
    author = CodexAuthor(provider=fake_provider)

    with pytest.raises(CodexAuthorError, match="only the flowspec/2"):
        author(_request(format_identifier="candidate/1"))

    assert fake_provider.calls == []


def test_codex_author_drives_a_diagnostic_repair_round() -> None:
    benchmark_case = load_reference_authoring_corpus().cases[0]
    first_projection = {
        **project_flow_document(json.loads(benchmark_case.expected_flow_json)),
        "flow_document_json": "{}",
    }
    fake_provider = FakeCodexProvider(
        [
            json.dumps(first_projection, separators=(",", ":"), sort_keys=True),
            _projection_response(benchmark_case.expected_flow_json),
        ]
    )
    author = CodexAuthor(provider=fake_provider)

    report = run_authoring_benchmark("codex_repair", (benchmark_case,), author)

    assert report.successful_cases == 1
    assert len(report.case_results[0].attempts) == 2
    repair_prompt = json.loads(fake_provider.calls[1]["messages"][0]["content"])
    assert repair_prompt["previous_source"] == "{}"
    assert repair_prompt["previous_diagnostics"]


def test_codex_author_closes_without_owning_persistent_provider_state() -> None:
    author = CodexAuthor(provider=FakeCodexProvider())

    author.close()
    author.close()

    with pytest.raises(CodexAuthorError, match="closed"):
        author(_request())


def test_codex_author_rejects_injected_provider_model_mismatch() -> None:
    with pytest.raises(ValueError, match="provider model"):
        CodexAuthor(model="requested-model", provider=FakeCodexProvider(model="other-model"))


def test_fake_provider_satisfies_codex_protocol() -> None:
    provider: CodexProvider = FakeCodexProvider()
    assert provider.model == DEFAULT_CODEX_MODEL
