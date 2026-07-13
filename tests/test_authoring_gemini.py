"""Gemini transport for the provider-neutral authoring repair loop."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from flowspec2.authoring import (
    GEMINI_AUTHOR_PROMPT_DIGEST,
    AuthoringRequest,
    GeminiAuthor,
    GeminiAuthorError,
    load_reference_authoring_corpus,
    project_flow_document,
    run_authoring_benchmark,
)
from flowspec2.authoring.gemini import _authoring_prompt
from flowspec2.diagnostics import FlowDiagnostic
from flowspec2.profiles import reference_profile


class FakeGeminiModels:
    def __init__(self, response_texts: list[str] | None = None) -> None:
        self.response_texts = list(response_texts or [])
        self.calls: list[dict[str, object]] = []
        self.provider_error: Exception | None = None
        self.model_version: str | None = "gemini-test-001"

    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: dict[str, object],
    ) -> object:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.provider_error is not None:
            raise self.provider_error
        return SimpleNamespace(
            text=self.response_texts.pop(0),
            model_version=self.model_version,
        )


class FakeGeminiClient:
    def __init__(self, response_texts: list[str] | None = None) -> None:
        self.models = FakeGeminiModels(response_texts)
        self.closed = False

    def close(self) -> None:
        self.closed = True


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


def test_prompt_is_canonical_whitelisted_and_contains_exact_profile_contract() -> None:
    initial_request = _request()
    first_prompt = _authoring_prompt(initial_request)
    second_prompt = _authoring_prompt(initial_request)

    assert first_prompt == second_prompt
    prompt_document = json.loads(first_prompt)
    assert tuple(sorted(prompt_document)) == (
        "case_identifier",
        "correction_round",
        "format",
        "format_identifier",
        "normative_schema",
        "previous_diagnostics",
        "previous_source",
        "profile_contract",
        "profile_identifier",
        "task",
    )
    assert prompt_document["profile_contract"] == json.loads(initial_request.profile_contract_json)
    expected_flow_json = load_reference_authoring_corpus().cases[0].expected_flow_json
    assert expected_flow_json not in first_prompt
    assert "required_constructs" not in first_prompt
    assert "forbidden_constructs" not in first_prompt

    correction_prompt = json.loads(_authoring_prompt(_request(correction=True)))
    assert correction_prompt["previous_source"] == '{"invalid":true}'
    assert correction_prompt["previous_diagnostics"] == [
        initial_diagnostic.to_dict()
        for initial_diagnostic in _request(correction=True).previous_diagnostics
    ]


def test_gemini_author_returns_inner_source_verbatim_with_closed_configuration() -> None:
    expected_source = load_reference_authoring_corpus().cases[0].expected_flow_json
    fake_client = FakeGeminiClient([_projection_response(expected_source)])
    author = GeminiAuthor(model="test-model", client=fake_client)

    authored_response = author(_request())
    assert authored_response.source == expected_source
    assert authored_response.effective_model_version == "gemini-test-001"
    provider_call = fake_client.models.calls[0]
    assert provider_call["model"] == "test-model"
    assert json.loads(str(provider_call["contents"]))["task"] == _request().task.prompt
    configuration = provider_call["config"]
    assert isinstance(configuration, dict)
    assert configuration["response_mime_type"] == "application/json"
    assert configuration["temperature"] == 0.0
    assert configuration["seed"] == 0
    assert configuration["response_json_schema"]["additionalProperties"] is False
    assert author.provenance().prompt_digest == GEMINI_AUTHOR_PROMPT_DIGEST

    author.close()
    assert fake_client.closed is False
    with pytest.raises(GeminiAuthorError, match="closed"):
        author(_request())


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
def test_gemini_author_rejects_invalid_projection_envelopes(response_text: str) -> None:
    author = GeminiAuthor(client=FakeGeminiClient([response_text]))

    with pytest.raises(GeminiAuthorError, match="projection envelope"):
        author(_request())


def test_gemini_author_requires_effective_model_version() -> None:
    expected_source = load_reference_authoring_corpus().cases[0].expected_flow_json
    fake_client = FakeGeminiClient([_projection_response(expected_source)])
    fake_client.models.model_version = None
    author = GeminiAuthor(client=fake_client)

    with pytest.raises(GeminiAuthorError, match="effective model version"):
        author(_request())


def test_gemini_author_wraps_provider_failures_without_leaking_details() -> None:
    class ProviderFailure(RuntimeError):
        code = 429

    fake_client = FakeGeminiClient()
    provider_failure = ProviderFailure("sentinel-secret-provider-message")
    fake_client.models.provider_error = provider_failure
    author = GeminiAuthor(client=fake_client)

    with pytest.raises(GeminiAuthorError) as captured_error:
        author(_request())

    assert captured_error.value.__cause__ is provider_failure
    assert "status=429" in str(captured_error.value)
    assert "sentinel-secret-provider-message" not in str(captured_error.value)


def test_gemini_author_rejects_unsupported_format_before_provider_call() -> None:
    fake_client = FakeGeminiClient()
    author = GeminiAuthor(client=fake_client)

    with pytest.raises(GeminiAuthorError, match="only the flowspec/2"):
        author(_request(format_identifier="candidate/1"))

    assert fake_client.models.calls == []


def test_gemini_author_drives_a_diagnostic_repair_round() -> None:
    benchmark_case = load_reference_authoring_corpus().cases[0]
    first_projection = {
        **project_flow_document(json.loads(benchmark_case.expected_flow_json)),
        "flow_document_json": "{}",
    }
    fake_client = FakeGeminiClient(
        [
            json.dumps(first_projection, separators=(",", ":"), sort_keys=True),
            _projection_response(benchmark_case.expected_flow_json),
        ]
    )
    author = GeminiAuthor(client=fake_client)

    report = run_authoring_benchmark("gemini_repair", (benchmark_case,), author)

    assert report.successful_cases == 1
    assert len(report.case_results[0].attempts) == 2
    repair_prompt = json.loads(str(fake_client.models.calls[1]["contents"]))
    assert repair_prompt["previous_source"] == "{}"
    assert repair_prompt["previous_diagnostics"]


def test_gemini_author_missing_key_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(GeminiAuthorError, match="GEMINI_API_KEY"):
        GeminiAuthor()


def test_gemini_author_closes_only_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeGeminiClient()
    monkeypatch.setattr(
        "flowspec2.authoring.gemini._default_client",
        lambda _api_key: fake_client,
    )

    with GeminiAuthor():
        pass

    assert fake_client.closed is True
