"""Optional Gemini transport for the provider-neutral authoring benchmark."""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator

from flowspec2.json_codec import StrictJsonError, strict_json_loads

from .benchmark import AuthoredSource, AuthoringRequest
from .evidence import AuthoringProviderProvenance
from .projection import authoring_projection
from .provider_prompt import (
    AUTHORING_SYSTEM_INSTRUCTION,
    authoring_prompt,
    authoring_prompt_digest,
    canonical_json,
)

DEFAULT_GEMINI_AUTHOR_MODEL: Final[str] = "gemini-2.5-flash"
DEFAULT_GEMINI_AUTHOR_TIMEOUT_MILLISECONDS: Final[int] = 120_000
GEMINI_AUTHOR_PROMPT_FORMAT: Final[str] = "flowspec2/gemini-author-prompt@3"

_PROVIDER_IDENTIFIER: Final[str] = "google_gemini"
_SDK_NAME: Final[str] = "google-genai"
_SYSTEM_INSTRUCTION: Final[str] = AUTHORING_SYSTEM_INSTRUCTION


def _canonical_json(json_document: object) -> str:
    return canonical_json(json_document)


GEMINI_AUTHOR_PROMPT_DIGEST: Final[str] = authoring_prompt_digest(GEMINI_AUTHOR_PROMPT_FORMAT)


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


class GeminiAuthorError(RuntimeError):
    """The optional provider boundary could not return a usable source envelope."""


def _authoring_prompt(authoring_request: AuthoringRequest) -> str:
    return authoring_prompt(authoring_request, GEMINI_AUTHOR_PROMPT_FORMAT)


def _default_client(api_key: str | None) -> _GeminiClient:
    resolved_api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not resolved_api_key:
        raise GeminiAuthorError(
            "Gemini authoring requires GEMINI_API_KEY after explicit network opt-in."
        )
    try:
        from google import genai
    except ImportError as import_error:
        raise GeminiAuthorError(
            "Gemini authoring requires the optional flowspec2[llm] dependency."
        ) from import_error
    try:
        return cast(
            _GeminiClient,
            genai.Client(
                api_key=resolved_api_key,
                http_options={"timeout": DEFAULT_GEMINI_AUTHOR_TIMEOUT_MILLISECONDS},
            ),
        )
    except Exception as client_error:
        raise GeminiAuthorError("Gemini author client initialization failed") from client_error


def _sdk_version() -> str:
    try:
        return version(_SDK_NAME)
    except PackageNotFoundError:
        return "injected-client"


class GeminiAuthor:
    """Generate benchmark sources through a closed Gemini projection envelope."""

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_AUTHOR_MODEL,
        *,
        api_key: str | None = None,
        client: _GeminiClient | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("Gemini author model must be non-empty")
        if api_key is not None and client is not None:
            raise ValueError("Gemini author cannot combine an API key with an injected client")
        self.model = model
        self._client = client if client is not None else _default_client(api_key)
        self._owns_client = client is None
        self._closed = False

    @property
    def generation_configuration(self) -> dict[str, object]:
        projection_artifact = authoring_projection()
        return {
            "response_mime_type": "application/json",
            "projection_schema_digest": projection_artifact.schema_digest,
            "seed": 0,
            "temperature": 0.0,
            "timeout_milliseconds": DEFAULT_GEMINI_AUTHOR_TIMEOUT_MILLISECONDS,
        }

    def provenance(self) -> AuthoringProviderProvenance:
        return AuthoringProviderProvenance.from_configuration(
            identifier=_PROVIDER_IDENTIFIER,
            model=self.model,
            sdk=_SDK_NAME,
            sdk_version=_sdk_version(),
            prompt_format=GEMINI_AUTHOR_PROMPT_FORMAT,
            prompt_digest=GEMINI_AUTHOR_PROMPT_DIGEST,
            generation_configuration=self.generation_configuration,
        )

    def __call__(self, authoring_request: AuthoringRequest) -> AuthoredSource:
        if self._closed:
            raise GeminiAuthorError("Gemini author is closed")
        if authoring_request.format_identifier != "flowspec/2":
            raise GeminiAuthorError("Gemini author supports only the flowspec/2 source adapter")
        projection_artifact = authoring_projection()
        provider_configuration: dict[str, object] = {
            "system_instruction": _SYSTEM_INSTRUCTION,
            "response_mime_type": "application/json",
            "response_json_schema": projection_artifact.schema(),
            "temperature": 0.0,
            "seed": 0,
        }
        try:
            provider_response = self._client.models.generate_content(
                model=self.model,
                contents=_authoring_prompt(authoring_request),
                config=provider_configuration,
            )
        except Exception as provider_error:
            status_code = getattr(
                provider_error,
                "status_code",
                getattr(provider_error, "code", None),
            )
            status_suffix = f", status={status_code}" if isinstance(status_code, int) else ""
            raise GeminiAuthorError(
                "Gemini author request failed for "
                f"{authoring_request.task.identifier!r} correction round "
                f"{authoring_request.correction_round} "
                f"({type(provider_error).__name__}{status_suffix})."
            ) from provider_error

        try:
            response_text = getattr(provider_response, "text", None)
            effective_model_version = getattr(provider_response, "model_version", None)
        except Exception as response_error:
            raise GeminiAuthorError("Gemini author response could not be read") from response_error
        if not isinstance(response_text, str) or not response_text:
            raise GeminiAuthorError("Gemini author returned an empty projection envelope")
        if not isinstance(effective_model_version, str) or not effective_model_version.strip():
            raise GeminiAuthorError("Gemini author response omitted its effective model version")
        try:
            projected_document = strict_json_loads(response_text)
        except (json.JSONDecodeError, StrictJsonError) as decoding_error:
            raise GeminiAuthorError(
                "Gemini author returned an invalid projection envelope"
            ) from decoding_error
        envelope_errors = tuple(
            Draft202012Validator(projection_artifact.schema()).iter_errors(projected_document)
        )
        if envelope_errors:
            raise GeminiAuthorError("Gemini author returned an incompatible projection envelope")
        if not isinstance(projected_document, dict):
            raise AssertionError("projection schema accepted a non-object response")
        return AuthoredSource(
            source=cast(str, projected_document["flow_document_json"]),
            effective_model_version=effective_model_version,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            try:
                self._client.close()
            except Exception as close_error:
                raise GeminiAuthorError(
                    "Gemini author client could not close cleanly"
                ) from close_error
        self._closed = True

    def __enter__(self) -> GeminiAuthor:
        if self._closed:
            raise GeminiAuthorError("Gemini author is closed")
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()
