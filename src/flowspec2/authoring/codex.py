"""Codex subscription transport for the provider-neutral authoring benchmark."""

from __future__ import annotations

import json
from typing import Final, cast

from jsonschema import Draft202012Validator

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
from flowspec2.json_codec import StrictJsonError, strict_json_loads

from .benchmark import AuthoredSource, AuthoringRequest
from .evidence import AuthoringProviderProvenance
from .projection import authoring_projection
from .provider_prompt import (
    AUTHORING_SYSTEM_INSTRUCTION,
    authoring_prompt,
    authoring_prompt_digest,
)

CODEX_AUTHOR_PROMPT_FORMAT: Final[str] = "flowspec2/codex-author-prompt@3"
CODEX_AUTHOR_PROMPT_DIGEST: Final[str] = authoring_prompt_digest(CODEX_AUTHOR_PROMPT_FORMAT)


class CodexAuthorError(RuntimeError):
    """The Codex author boundary could not return a usable source envelope."""


class CodexAuthor:
    """Generate benchmark sources through isolated Codex structured output."""

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
                raise ValueError(
                    "Codex author cannot set an SDK version without an injected provider"
                )
            try:
                resolved_provider, resolved_sdk_version = create_codex_provider(
                    model=model,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                )
            except CodexTransportError as transport_error:
                raise CodexAuthorError(str(transport_error)) from transport_error
        else:
            if provider.model != model:
                raise ValueError("injected Codex provider model does not match the author model")
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
        projection_artifact = authoring_projection()
        return {
            "billing_mode": "chatgpt_subscription",
            "builtin_tools": "disabled",
            "environment_policy": "allowlisted",
            "ephemeral": True,
            "isolation": "safe_mode",
            "projection_schema_digest": projection_artifact.schema_digest,
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
            prompt_format=CODEX_AUTHOR_PROMPT_FORMAT,
            prompt_digest=CODEX_AUTHOR_PROMPT_DIGEST,
            generation_configuration=self.generation_configuration,
        )

    def __call__(self, authoring_request: AuthoringRequest) -> AuthoredSource:
        if self._closed:
            raise CodexAuthorError("Codex author is closed")
        if authoring_request.format_identifier != "flowspec/2":
            raise CodexAuthorError("Codex author supports only the flowspec/2 source adapter")
        projection_artifact = authoring_projection()
        try:
            provider_turn = run_codex_structured_output(
                self._provider,
                system=AUTHORING_SYSTEM_INSTRUCTION,
                prompt=authoring_prompt(authoring_request, CODEX_AUTHOR_PROMPT_FORMAT),
                response_schema=projection_artifact.schema(),
                timeout_seconds=self.timeout_seconds,
            )
        except CodexTransportError as transport_error:
            raise CodexAuthorError(
                "Codex author request failed for "
                f"{authoring_request.task.identifier!r} correction round "
                f"{authoring_request.correction_round} ({type(transport_error).__name__})."
            ) from transport_error
        if not isinstance(provider_turn.text, str) or not provider_turn.text:
            raise CodexAuthorError("Codex author returned an empty projection envelope")
        try:
            projected_document = strict_json_loads(provider_turn.text)
        except (json.JSONDecodeError, StrictJsonError) as decoding_error:
            raise CodexAuthorError(
                "Codex author returned an invalid projection envelope"
            ) from decoding_error
        envelope_errors = tuple(
            Draft202012Validator(projection_artifact.schema()).iter_errors(projected_document)
        )
        if envelope_errors:
            raise CodexAuthorError("Codex author returned an incompatible projection envelope")
        if not isinstance(projected_document, dict):
            raise AssertionError("projection schema accepted a non-object response")
        return AuthoredSource(source=cast(str, projected_document["flow_document_json"]))

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> CodexAuthor:
        if self._closed:
            raise CodexAuthorError("Codex author is closed")
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.close()
