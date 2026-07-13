"""HTTP backends driven offline via httpx.MockTransport.

Covers the tool contracts and — the point of the layer — the mapping of HTTP
status to the terminal success/retryable/fatal trichotomy.
"""

from __future__ import annotations

import httpx
from conftest import step

from flowspec2 import FlowRuntime
from flowspec2.backends import BackendConfig, make_registry

CONFIG = BackendConfig(
    geocode_url="https://api.test/geocode",
    cpf_lookup_url="https://api.test/cpf",
    govbr_enrich_url="https://api.test/govbr",
    sgrc_url="https://api.test/sgrc",
    api_key="secret",
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_geocode_tool_parses_and_detects_praca():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer secret"  # api_key wired
        return httpx.Response(
            200, json={"results": [{"logradouro": "Praça Mauá", "bairro": "Centro"}]}
        )

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("geocode", address="Praça Mauá")
    assert out["status"] == "ok"
    assert out["address"]["kind"] == "praca"
    assert out["address"]["bairro"] == "Centro"


async def test_sgrc_success_maps_to_success():
    def handler(req):
        return httpx.Response(201, json={"protocolo": "SGRC-REAL-9"})

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("sgrc_open_ticket", endereco="x")
    assert out["status"] == "success" and out["protocolo"] == "SGRC-REAL-9"


async def test_sgrc_5xx_maps_to_retryable():
    def handler(req):
        return httpx.Response(503, text="upstream down")

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("sgrc_open_ticket", endereco="x")
    assert out["status"] == "retryable"


async def test_sgrc_4xx_maps_to_fatal():
    def handler(req):
        return httpx.Response(422, text="bad payload")

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("sgrc_open_ticket", endereco="x")
    assert out["status"] == "fatal"


async def test_sgrc_timeout_maps_to_retryable():
    def handler(req):
        raise httpx.ConnectTimeout("timed out")

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("sgrc_open_ticket", endereco="x")
    assert out["status"] == "retryable"


async def test_partial_config_falls_back_to_fakes():
    # only geocode configured -> sgrc stays the fake (still opens a ticket)
    reg = make_registry(
        BackendConfig(geocode_url="https://api.test/geocode"),
        transport=_transport(lambda r: httpx.Response(200, json={"logradouro": "Rua X"})),
    )
    out = await reg.call("sgrc_open_ticket", endereco="x")
    assert out["status"] == "success" and out["protocolo"].startswith("SGRC-")


async def test_buraco_end_to_end_over_http_backends(buraco_doc):
    routes = {
        "/geocode": httpx.Response(
            200, json={"logradouro": "Av. Brasil, 1000", "bairro": "Centro"}
        ),
        "/sgrc": httpx.Response(201, json={"protocolo": "SGRC-HTTP-1"}),
        "/cpf": httpx.Response(200, json={"name": "", "email": "", "phones": []}),
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return routes[req.url.path]

    reg = make_registry(CONFIG, transport=_transport(handler))
    rt = FlowRuntime(buraco_doc, tools=reg)
    st = await step(rt, None, {"buraco_tipo": "buraco", "buraco_tamanho": "grande"})
    st = await step(rt, st, {"address": "Av. Brasil, 1000"})
    st = await step(rt, st, {"confirmacao": "sim"})
    st = await step(rt, st, {"identification_method": "anonimo"})
    st = await step(rt, st, {"confirmacao": "sim"})
    assert st.status == "completed"
    assert st.data["protocol_id"] == "SGRC-HTTP-1"


def test_config_from_env():
    cfg = BackendConfig.from_env(
        {
            "FLOWSPEC2_SGRC_URL": "https://x/sgrc",
            "FLOWSPEC2_API_KEY": "k",
            "FLOWSPEC2_HTTP_TIMEOUT": "5",
        }
    )
    assert cfg.sgrc_url == "https://x/sgrc"
    assert cfg.timeout == 5.0
    assert cfg.headers()["Authorization"] == "Bearer k"
