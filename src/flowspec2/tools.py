"""Tool registry + injectable backends.

A *tool* is the unit a flow's ``entry.tool`` / ``terminal.tool`` /
``capabilities.await_external.on_resume.enrich`` binds to. Each is an async
callable ``(**kwargs) -> dict``. Backends default to in-memory fakes so the
suite runs offline; swap any of them for a real integration by passing a custom
``ToolRegistry`` to :class:`~flowspec2.runtime.FlowRuntime`.

Terminal idempotency lives here: ``replay`` caches a successful result under a
sha256 of the call inputs, keyed per user, so a re-entered terminal node never
double-fires the side effect.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable

Tool = Callable[..., Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._replay: dict[str, dict[str, Any]] = {}

    def register(self, name: str, tool: Tool) -> None:
        self._tools[name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    async def call(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"tool not registered: {name!r}")
        return await self._tools[name](**kwargs)

    # ── idempotency replay cache ─────────────────────────────────────────────

    @staticmethod
    def idempotency_key(user_id: str, tool: str, inputs: dict[str, Any]) -> str:
        blob = json.dumps({"u": user_id, "t": tool, "i": inputs}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def replay_get(self, key: str) -> dict[str, Any] | None:
        return self._replay.get(key)

    def replay_put(self, key: str, result: dict[str, Any]) -> None:
        self._replay[key] = result


# ── default fake backends ────────────────────────────────────────────────────


async def _fake_hub_search(**kwargs: Any) -> dict[str, Any]:
    """Best-effort knowledge load (entry.tool). Never blocks the flow."""
    return {
        "status": "ok",
        "nome": "Reparo de luminária",
        "resumo": "Conserto de iluminação pública.",
        "prazo": "3 dias úteis",
    }


async def _fake_geocode(address: str = "", **_: Any) -> dict[str, Any]:
    """Geocode/validate an address string (address subflow backend)."""
    text = (address or "").strip()
    if not text:
        return {"status": "not_found", "error": "endereço vazio"}
    folded = text.lower()
    kind = (
        "praca" if ("praca" in folded or "praça" in folded or folded.startswith("praça")) else "rua"
    )
    return {
        "status": "ok",
        "needs_confirmation": True,
        "address": {
            "logradouro": text,
            "kind": kind,
            "bairro": "Centro",
            "municipio": "Rio de Janeiro",
        },
    }


async def _fake_cpf_lookup(cpf: str = "", **_: Any) -> dict[str, Any]:
    """Look up a citizen's registry by CPF (identification backend)."""
    return {"status": "ok", "name": "", "email": "", "phones": []}


async def _fake_govbr_enrich(cpf: str = "", **_: Any) -> dict[str, Any]:
    """Enrich gov.br data with internal registry (await_external.on_resume.enrich)."""
    return {"status": "ok", "phones": ["5521999999999"]}


async def _fake_sgrc_open_ticket(**inputs: Any) -> dict[str, Any]:
    """Open an SGRC ticket (terminal.tool). Returns a protocol id."""
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "status": "success",
        "protocolo": f"SGRC-{digest[:10].upper()}",
        "message": "Chamado aberto com sucesso.",
    }


def default_tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("hub_search", _fake_hub_search)
    reg.register("geocode", _fake_geocode)
    reg.register("cpf_lookup", _fake_cpf_lookup)
    reg.register("get_user_info", _fake_govbr_enrich)
    reg.register("sgrc_open_ticket", _fake_sgrc_open_ticket)
    return reg
