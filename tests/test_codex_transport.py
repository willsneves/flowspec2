"""Security and compatibility checks for lazy local llmgate loading."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from flowspec2 import codex_transport
from flowspec2.codex_transport import CodexTransportError, create_codex_provider


class CapturingProvider:
    calls: list[tuple[str, dict[str, object]]] = []

    def __init__(self, provider: str, **configuration: object) -> None:
        self.model = str(configuration["model"])
        self.calls.append((provider, configuration))

    async def run(self, **_arguments: Any) -> object:
        raise AssertionError("model execution is not part of provider construction")


def test_default_provider_requires_expected_version_and_safe_subscription_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CapturingProvider.calls.clear()
    monkeypatch.setattr(codex_transport.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(
        codex_transport,
        "import_module",
        lambda _module_name: SimpleNamespace(
            __version__="0.43.0",
            LlmProvider=CapturingProvider,
        ),
    )

    provider, sdk_version = create_codex_provider(
        model="test-model",
        effort="high",
        timeout_seconds=90.0,
    )

    assert provider.model == "test-model"
    assert sdk_version == "0.43.0"
    provider_name, configuration = CapturingProvider.calls[0]
    assert provider_name == "codex"
    assert configuration["codex_inherit_api_key"] is False
    assert configuration["codex_inherit_environment"] is False
    assert configuration["codex_require_chatgpt_auth"] is True
    assert configuration["codex_disable_builtin_tools"] is True
    assert configuration["codex_ignore_user_config"] is True
    assert configuration["codex_approval_policy"] == "never"
    assert configuration["codex_sandbox"] == "read-only"
    assert configuration["isolation"] == "safe_mode"


@pytest.mark.parametrize(
    "module",
    [
        SimpleNamespace(__version__="0.42.0", LlmProvider=CapturingProvider),
        SimpleNamespace(__version__="0.8.3"),
    ],
)
def test_default_provider_rejects_stale_or_unrelated_llmgate(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
) -> None:
    monkeypatch.setattr(codex_transport.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(codex_transport, "import_module", lambda _module_name: module)

    with pytest.raises(CodexTransportError, match="llmgate"):
        create_codex_provider(model="test-model", effort="high", timeout_seconds=90.0)


def test_default_provider_fails_before_import_on_unsupported_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_transport.sys, "version_info", (3, 11, 9))

    with pytest.raises(CodexTransportError, match="Python 3.12"):
        create_codex_provider(model="test-model", effort="high", timeout_seconds=90.0)
