"""Gemini and subscription-authenticated Codex operational probe executors."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version
from typing import Final, Protocol, cast

from flowspec2.codex_transport import (
    CODEX_PROVIDER_IDENTIFIER,
    CODEX_SDK_NAME,
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    CodexProvider,
    CodexTransportError,
    create_codex_provider,
    run_codex_structured_output,
    validate_codex_configuration,
)

from .evidence import AuthoringProviderProvenance
from .operational import (
    OPERATIONAL_PROMPT_DIGEST,
    OPERATIONAL_PROMPT_FORMAT,
    OperationalModelResponse,
    OperationalProbeRequest,
)

DEFAULT_GEMINI_OPERATIONAL_MODEL: Final[str] = "gemini-2.5-flash"
DEFAULT_GEMINI_OPERATIONAL_TIMEOUT_MILLISECONDS: Final[int] = 120_000
_GEMINI_PROVIDER_IDENTIFIER: Final[str] = "google_gemini"
_GEMINI_SDK_NAME: Final[str] = "google-genai"


class _GeminiModels(Protocol):
    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: dict[str, object],
    ) -> object: ...


class _GeminiClient(Protocol):
    @property
    def models(self) -> _GeminiModels: ...

    def close(self) -> None: ...


class OperationalProviderError(RuntimeError):
    """A provider failed before producing a completed operational turn."""


def _gemini_sdk_version() -> str:
    try:
        return version(_GEMINI_SDK_NAME)
    except PackageNotFoundError:
        return "injected-client"


def _create_gemini_client() -> _GeminiClient:
    resolved_api_key = os.environ.get("GEMINI_API_KEY")
    if not resolved_api_key:
        raise OperationalProviderError(
            "Gemini operational probes require GEMINI_API_KEY after explicit network opt-in"
        )
    try:
        from google import genai
    except ImportError as import_error:
        raise OperationalProviderError(
            "Gemini operational probes require the optional flowspec2[llm] dependency"
        ) from import_error
    try:
        return cast(
            _GeminiClient,
            genai.Client(
                api_key=resolved_api_key,
                http_options={"timeout": DEFAULT_GEMINI_OPERATIONAL_TIMEOUT_MILLISECONDS},
            ),
        )
    except Exception as provider_error:
        raise OperationalProviderError("Gemini operational client initialization failed") from (
            provider_error
        )


class GeminiOperationalExecutor:
    """Execute exact operational requests through Gemini structured output."""

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_OPERATIONAL_MODEL,
        *,
        client: _GeminiClient | None = None,
        sdk_version: str | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("Gemini operational model must be non-empty")
        if client is None and sdk_version is not None:
            raise ValueError("Gemini SDK version requires an injected client")
        self.model = model
        self._client = client if client is not None else _create_gemini_client()
        self._sdk_version = sdk_version or _gemini_sdk_version()
        self._owns_client = client is None
        self._closed = False

    @property
    def generation_configuration(self) -> dict[str, object]:
        return {
            "billing_mode": "developer_api",
            "response_mime_type": "application/json",
            "seed": 0,
            "temperature": 0.0,
            "timeout_milliseconds": DEFAULT_GEMINI_OPERATIONAL_TIMEOUT_MILLISECONDS,
        }

    def provenance(self) -> AuthoringProviderProvenance:
        return AuthoringProviderProvenance.from_configuration(
            identifier=_GEMINI_PROVIDER_IDENTIFIER,
            model=self.model,
            sdk=_GEMINI_SDK_NAME,
            sdk_version=self._sdk_version,
            prompt_format=OPERATIONAL_PROMPT_FORMAT,
            prompt_digest=OPERATIONAL_PROMPT_DIGEST,
            generation_configuration=self.generation_configuration,
        )

    def __call__(self, operational_request: OperationalProbeRequest) -> OperationalModelResponse:
        if self._closed:
            raise OperationalProviderError("Gemini operational executor is closed")
        provider_configuration: dict[str, object] = {
            **self.generation_configuration,
            "system_instruction": operational_request.system_instruction,
            "response_json_schema": operational_request.response_schema(),
        }
        try:
            provider_response = self._client.models.generate_content(
                model=self.model,
                contents=operational_request.prompt,
                config=provider_configuration,
            )
            response_text = getattr(provider_response, "text", None)
            effective_model_version = getattr(provider_response, "model_version", None)
        except Exception as provider_error:
            raise OperationalProviderError(
                f"Gemini operational request failed for {operational_request.probe_identifier!r}"
            ) from provider_error
        if not isinstance(response_text, str):
            raise OperationalProviderError("Gemini operational response omitted text")
        if not isinstance(effective_model_version, str) or not effective_model_version.strip():
            raise OperationalProviderError(
                "Gemini operational response omitted its effective model version"
            )
        return OperationalModelResponse(
            raw_output=response_text,
            effective_model_version=effective_model_version,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            try:
                self._client.close()
            except Exception as close_error:
                raise OperationalProviderError(
                    "Gemini operational client could not close cleanly"
                ) from close_error
        self._closed = True

    def __enter__(self) -> GeminiOperationalExecutor:
        if self._closed:
            raise OperationalProviderError("Gemini operational executor is closed")
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()


class CodexOperationalExecutor:
    """Execute exact probes through isolated ChatGPT-subscription Codex turns."""

    def __init__(
        self,
        model: str = DEFAULT_CODEX_MODEL,
        *,
        effort: str = DEFAULT_CODEX_EFFORT,
        timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
        provider: CodexProvider | None = None,
        sdk_version: str | None = None,
    ) -> None:
        validate_codex_configuration(model, effort, timeout_seconds)
        if provider is None:
            if sdk_version is not None:
                raise ValueError("Codex SDK version requires an injected provider")
            try:
                resolved_provider, resolved_sdk_version = create_codex_provider(
                    model=model,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                )
            except CodexTransportError as transport_error:
                raise OperationalProviderError(str(transport_error)) from transport_error
        else:
            if provider.model != model:
                raise ValueError("injected Codex provider model does not match executor model")
            resolved_provider = provider
            resolved_sdk_version = sdk_version or "injected-provider"
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds
        self._provider = resolved_provider
        self._sdk_version = resolved_sdk_version
        self._closed = False

    @property
    def generation_configuration(self) -> dict[str, object]:
        return {
            "billing_mode": "chatgpt_subscription",
            "builtin_tools": "disabled",
            "environment_policy": "allowlisted",
            "ephemeral": True,
            "isolation": "safe_mode",
            "reasoning_effort": self.effort,
            "sandbox": "read-only",
            "timeout_seconds": self.timeout_seconds,
        }

    def provenance(self) -> AuthoringProviderProvenance:
        return AuthoringProviderProvenance.from_configuration(
            identifier=CODEX_PROVIDER_IDENTIFIER,
            model=self.model,
            sdk=CODEX_SDK_NAME,
            sdk_version=self._sdk_version,
            prompt_format=OPERATIONAL_PROMPT_FORMAT,
            prompt_digest=OPERATIONAL_PROMPT_DIGEST,
            generation_configuration=self.generation_configuration,
        )

    def __call__(self, operational_request: OperationalProbeRequest) -> OperationalModelResponse:
        if self._closed:
            raise OperationalProviderError("Codex operational executor is closed")
        try:
            provider_turn = run_codex_structured_output(
                self._provider,
                system=operational_request.system_instruction,
                prompt=operational_request.prompt,
                response_schema=operational_request.response_schema(),
                timeout_seconds=self.timeout_seconds,
            )
        except CodexTransportError as transport_error:
            raise OperationalProviderError(
                f"Codex operational request failed for {operational_request.probe_identifier!r}"
            ) from transport_error
        return OperationalModelResponse(
            raw_output=provider_turn.text,
            effective_model_version=None,
        )

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> CodexOperationalExecutor:
        if self._closed:
            raise OperationalProviderError("Codex operational executor is closed")
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()
