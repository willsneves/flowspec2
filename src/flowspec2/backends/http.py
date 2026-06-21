"""HTTP implementations of the flow tools (geocode / cpf / gov.br / SGRC).

Each factory returns an async ``(**kwargs) -> dict`` matching the tool protocol.
The contracts below are what these adapters speak; point the configured URL at a
real Prefeitura endpoint (or a thin adapter that conforms to them).

Error handling is the point: the SGRC terminal maps transport/5xx to
``retryable`` (the terminal node preserves state and re-fires next turn) and 4xx
to ``fatal`` (resets), so the format's outcome trichotomy is driven by real HTTP
semantics. Geocode/cpf failures are non-fatal (re-ask / best-effort).

``transport`` is injectable so the suite drives these with ``httpx.MockTransport``
— no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # avoid importing httpx at module import time
    import httpx

    from . import BackendConfig


def _client(config: "BackendConfig", transport: "Optional[httpx.BaseTransport]"):
    import httpx

    return httpx.AsyncClient(timeout=config.timeout, transport=transport, headers=config.headers())


def _kind_of(street: str, explicit: Optional[str]) -> str:
    if explicit:
        folded = explicit.strip().lower()
        if folded.startswith("pra"):  # praça / praca
            return "praca"
        return folded
    s = (street or "").strip().lower()
    return "praca" if (s.startswith("praça") or s.startswith("praca") or "praça" in s or "praca" in s) else "rua"


def _parse_geocode(data: Any, fallback: str) -> Optional[dict[str, Any]]:
    """Accept ``{"results":[{...}]}`` or a flat ``{...}`` address object."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data = data["results"][0] if data["results"] else None
    if not isinstance(data, dict):
        return None
    street = data.get("logradouro") or data.get("address") or fallback
    return {
        "logradouro": street,
        "kind": _kind_of(street, data.get("kind") or data.get("tipo_logradouro")),
        "bairro": data.get("bairro", ""),
        "municipio": data.get("municipio", "Rio de Janeiro"),
    }


def make_geocode(config: "BackendConfig", transport=None):
    async def geocode(address: str = "", **_: Any) -> dict[str, Any]:
        import httpx

        text = (address or "").strip()
        if not text:
            return {"status": "not_found", "error": "endereço vazio"}
        try:
            async with _client(config, transport) as client:
                resp = await client.post(config.geocode_url, json={"address": text})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {"status": "error", "error": str(exc)}
        if resp.status_code >= 500:
            return {"status": "error", "error": f"geocode {resp.status_code}"}
        if resp.status_code >= 400:
            return {"status": "not_found", "error": f"geocode {resp.status_code}"}
        addr = _parse_geocode(resp.json(), text)
        if not addr:
            return {"status": "not_found"}
        needs = resp.json().get("needs_confirmation", True) if isinstance(resp.json(), dict) else True
        return {"status": "ok", "needs_confirmation": needs, "address": addr}

    return geocode


def make_cpf_lookup(config: "BackendConfig", transport=None):
    async def cpf_lookup(cpf: str = "", **_: Any) -> dict[str, Any]:
        import httpx

        try:
            async with _client(config, transport) as client:
                resp = await client.get(config.cpf_lookup_url, params={"cpf": cpf})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {"status": "error", "error": str(exc)}  # best-effort: subflow tolerates
        if resp.status_code >= 400:
            return {"status": "ok", "name": "", "email": "", "phones": []}
        body = resp.json()
        return {
            "status": "ok",
            "name": body.get("name", ""),
            "email": body.get("email", ""),
            "phones": body.get("phones", []),
        }

    return cpf_lookup


def make_govbr_enrich(config: "BackendConfig", transport=None):
    async def get_user_info(cpf: str = "", **_: Any) -> dict[str, Any]:
        import httpx

        try:
            async with _client(config, transport) as client:
                resp = await client.get(config.govbr_enrich_url, params={"cpf": cpf})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {"status": "error", "error": str(exc)}
        if resp.status_code >= 400:
            return {"status": "ok", "phones": []}
        body = resp.json()
        return {"status": "ok", "phones": body.get("phones", []), "email": body.get("email", ""), "name": body.get("name", "")}

    return get_user_info


def make_sgrc_open_ticket(config: "BackendConfig", transport=None):
    async def sgrc_open_ticket(**inputs: Any) -> dict[str, Any]:
        import httpx

        try:
            async with _client(config, transport) as client:
                resp = await client.post(config.sgrc_url, json=inputs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {"status": "retryable", "error": str(exc)}  # transient → terminal re-fires
        if resp.status_code in (200, 201):
            body = resp.json()
            protocol = body.get("protocolo") or body.get("protocol_id")
            if not protocol:
                return {"status": "fatal", "error": "resposta do SGRC sem protocolo"}
            return {"status": "success", "protocolo": protocol, "message": body.get("message", "Chamado aberto com sucesso.")}
        if resp.status_code >= 500:
            return {"status": "retryable", "error": f"SGRC {resp.status_code}"}
        return {"status": "fatal", "error": f"SGRC {resp.status_code}: {resp.text[:200]}"}

    return sgrc_open_ticket
