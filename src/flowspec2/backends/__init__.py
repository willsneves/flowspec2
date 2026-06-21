"""Pluggable backends behind the flow tool protocol.

The runtime calls tools by name (``geocode``, ``cpf_lookup``, ``get_user_info``,
``sgrc_open_ticket``). By default those are in-memory fakes
(:func:`flowspec2.tools.default_tool_registry`). :func:`make_registry` overlays
real HTTP implementations onto that fake base for whichever URLs are configured —
so a partial configuration still works (fakes fill the gaps), and the terminal's
idempotency replay cache (which lives on the registry) is preserved.

    from flowspec2 import FlowRuntime
    from flowspec2.backends import BackendConfig, make_registry

    rt = FlowRuntime(doc, tools=make_registry(BackendConfig.from_env()))
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..tools import ToolRegistry, default_tool_registry


@dataclass
class BackendConfig:
    geocode_url: Optional[str] = None
    cpf_lookup_url: Optional[str] = None
    govbr_enrich_url: Optional[str] = None
    sgrc_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: float = 10.0

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "BackendConfig":
        env = env if env is not None else dict(os.environ)
        timeout = env.get("FLOWSPEC2_HTTP_TIMEOUT")
        return cls(
            geocode_url=env.get("FLOWSPEC2_GEOCODE_URL"),
            cpf_lookup_url=env.get("FLOWSPEC2_CPF_LOOKUP_URL"),
            govbr_enrich_url=env.get("FLOWSPEC2_GOVBR_ENRICH_URL"),
            sgrc_url=env.get("FLOWSPEC2_SGRC_URL"),
            api_key=env.get("FLOWSPEC2_API_KEY"),
            timeout=float(timeout) if timeout else 10.0,
        )


def make_registry(
    config: BackendConfig,
    *,
    transport=None,
    base: Optional[ToolRegistry] = None,
) -> ToolRegistry:
    """Overlay HTTP tools onto a (fake-by-default) registry per configured URL."""
    from .http import make_cpf_lookup, make_geocode, make_govbr_enrich, make_sgrc_open_ticket

    registry = base or default_tool_registry()
    if config.geocode_url:
        registry.register("geocode", make_geocode(config, transport))
    if config.cpf_lookup_url:
        registry.register("cpf_lookup", make_cpf_lookup(config, transport))
    if config.govbr_enrich_url:
        registry.register("get_user_info", make_govbr_enrich(config, transport))
    if config.sgrc_url:
        registry.register("sgrc_open_ticket", make_sgrc_open_ticket(config, transport))
    return registry


__all__ = ["BackendConfig", "make_registry"]
