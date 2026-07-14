"""Lazy, subscription-authenticated structured output through the local llmgate."""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Mapping
from importlib import import_module
from typing import Any, Final, Protocol, cast

MINIMUM_LLMGATE_VERSION: Final[str] = "0.43.0"
DEFAULT_CODEX_MODEL: Final[str] = "gpt-5.6-terra"
DEFAULT_CODEX_EFFORT: Final[str] = "high"
DEFAULT_CODEX_TIMEOUT_SECONDS: Final[float] = 300.0
CODEX_PROVIDER_IDENTIFIER: Final[str] = "openai_codex"
CODEX_SDK_NAME: Final[str] = "llmgate"

_SUPPORTED_EFFORTS: Final[frozenset[str]] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


class CodexTransportError(RuntimeError):
    """The isolated Codex transport could not return structured output."""


class CodexTurn(Protocol):
    """The subset of an llmgate turn consumed by flowspec2."""

    text: str
    model: str


class CodexProvider(Protocol):
    """The subset of LlmProvider used by the synchronous FlowSpec boundaries."""

    model: str

    async def run(
        self,
        *,
        messages: list[dict[str, str]],
        system: str,
        response_schema: dict[str, Any],
        ephemeral: bool,
        persist_session: bool,
        timeout_s: float,
    ) -> Any: ...


def validate_codex_configuration(model: str, effort: str, timeout_seconds: float) -> None:
    """Reject ambiguous or unbounded Codex configuration before model execution."""

    if not model.strip():
        raise ValueError("Codex model must be non-empty")
    if effort not in _SUPPORTED_EFFORTS:
        raise ValueError(f"unsupported Codex reasoning effort: {effort!r}")
    if timeout_seconds <= 0:
        raise ValueError("Codex timeout must be positive")


def _version_tuple(version_text: str) -> tuple[int, int, int]:
    matched_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[.+-].*)?", version_text)
    if matched_version is None:
        raise CodexTransportError("the local llmgate has an unsupported version identifier")
    major_version, minor_version, patch_version = matched_version.groups()
    return int(major_version), int(minor_version), int(patch_version)


def create_codex_provider(
    *,
    model: str,
    effort: str,
    timeout_seconds: float,
) -> tuple[CodexProvider, str]:
    """Load the known sibling llmgate and require ChatGPT subscription authentication."""

    validate_codex_configuration(model, effort, timeout_seconds)
    if sys.version_info < (3, 12):
        raise CodexTransportError("the local llmgate Codex transport requires Python 3.12 or newer")
    try:
        llmgate = import_module("llmgate")
    except ImportError as import_error:
        raise CodexTransportError(
            "Codex execution requires the local ~/Code/llmgate project; "
            "do not install the unrelated PyPI package with the same name"
        ) from import_error

    sdk_version = getattr(llmgate, "__version__", None)
    provider_class = getattr(llmgate, "LlmProvider", None)
    if not isinstance(sdk_version, str) or provider_class is None:
        raise CodexTransportError(
            "the imported llmgate is not the expected wllsena/llmgate distribution"
        )
    if _version_tuple(sdk_version) < _version_tuple(MINIMUM_LLMGATE_VERSION):
        raise CodexTransportError(f"llmgate {MINIMUM_LLMGATE_VERSION} or newer is required")

    try:
        provider = provider_class(
            "codex",
            model=model,
            effort=effort,
            timeout_s=timeout_seconds,
            isolation="safe_mode",
            codex_inherit_api_key=False,
            codex_inherit_environment=False,
            codex_require_chatgpt_auth=True,
            codex_disable_builtin_tools=True,
            codex_ignore_user_config=True,
            codex_approval_policy="never",
            codex_sandbox="read-only",
        )
    except Exception as provider_error:
        raise CodexTransportError(
            "Codex provider initialization failed under ChatGPT subscription authentication"
        ) from provider_error
    return cast(CodexProvider, provider), sdk_version


async def _run_provider(
    provider: CodexProvider,
    *,
    system: str,
    prompt: str,
    response_schema: dict[str, Any],
    timeout_seconds: float,
) -> CodexTurn:
    provider_turn = await provider.run(
        messages=[{"role": "user", "content": prompt}],
        system=system,
        response_schema=response_schema,
        ephemeral=True,
        persist_session=False,
        timeout_s=timeout_seconds,
    )
    if not isinstance(getattr(provider_turn, "text", None), str) or not isinstance(
        getattr(provider_turn, "model", None), str
    ):
        raise CodexTransportError("Codex returned an incompatible llmgate turn")
    return cast(CodexTurn, provider_turn)


def run_codex_structured_output(
    provider: CodexProvider,
    *,
    system: str,
    prompt: str,
    response_schema: Mapping[str, Any],
    timeout_seconds: float,
) -> CodexTurn:
    """Run one ephemeral Codex turn from a synchronous application boundary."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise CodexTransportError(
            "synchronous Codex execution cannot run on an active event-loop thread"
        )

    try:
        return asyncio.run(
            _run_provider(
                provider,
                system=system,
                prompt=prompt,
                response_schema=dict(response_schema),
                timeout_seconds=timeout_seconds,
            )
        )
    except CodexTransportError:
        raise
    except Exception as provider_error:
        raise CodexTransportError("Codex model execution failed") from provider_error
