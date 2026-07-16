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
    brazilian_tax_id_lookup_url="https://api.test/brazilian_tax_id",
    govbr_enrich_url="https://api.test/govbr",
    ticketing_url="https://api.test/requests",
    api_key="secret",
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_geocode_tool_parses_and_detects_square():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer secret"  # api_key wired
        return httpx.Response(
            200, json={"results": [{"street": "Central Square", "district": "Downtown"}]}
        )

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("geocode", address="Central Square")
    assert out["status"] == "ok"
    assert out["address"]["kind"] == "square"
    assert out["address"]["district"] == "Downtown"


async def test_ticketing_success_maps_to_success():
    def handler(req):
        return httpx.Response(201, json={"protocol_id": "REQ-REAL-9"})

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("open_service_request", address={"street": "x"})
    assert out["status"] == "success" and out["protocol_id"] == "REQ-REAL-9"


async def test_ticketing_5xx_maps_to_retryable():
    def handler(req):
        return httpx.Response(503, text="upstream down")

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("open_service_request", address={"street": "x"})
    assert out["status"] == "retryable"


async def test_ticketing_4xx_maps_to_fatal():
    def handler(req):
        return httpx.Response(422, text="bad payload")

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("open_service_request", address={"street": "x"})
    assert out["status"] == "fatal"


async def test_ticketing_timeout_maps_to_retryable():
    def handler(req):
        raise httpx.ConnectTimeout("timed out")

    reg = make_registry(CONFIG, transport=_transport(handler))
    out = await reg.call("open_service_request", address={"street": "x"})
    assert out["status"] == "retryable"


async def test_partial_config_falls_back_to_fakes():
    # only geocode configured -> ticketing stays the fake (still opens a ticket)
    reg = make_registry(
        BackendConfig(geocode_url="https://api.test/geocode"),
        transport=_transport(lambda r: httpx.Response(200, json={"street": "Street X"})),
    )
    out = await reg.call("open_service_request", address={"street": "x"})
    assert out["status"] == "success" and out["protocol_id"].startswith("REQ-")


async def test_pothole_end_to_end_over_http_backends(pothole_document):
    routes = {
        "/geocode": httpx.Response(200, json={"street": "Av. Brasil, 1000", "district": "Centro"}),
        "/requests": httpx.Response(201, json={"protocol_id": "REQ-HTTP-1"}),
        "/brazilian_tax_id": httpx.Response(200, json={"name": "", "email": "", "phones": []}),
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return routes[req.url.path]

    reg = make_registry(CONFIG, transport=_transport(handler))
    rt = FlowRuntime(pothole_document, tools=reg)
    st = await step(rt, None, {"pothole_type": "pothole", "pothole_size": "large"})
    st = await step(rt, st, {"address": "Av. Brasil, 1000"})
    st = await step(rt, st, {"confirmation": "yes"})
    st = await step(rt, st, {"identification_method": "anonymous"})
    st = await step(rt, st, {"confirmation": "yes"})
    assert st.status == "completed"
    assert st.data["protocol_id"] == "REQ-HTTP-1"


def test_config_from_env():
    cfg = BackendConfig.from_env(
        {
            "FLOWSPEC2_TICKETING_URL": "https://x/requests",
            "FLOWSPEC2_API_KEY": "k",
            "FLOWSPEC2_HTTP_TIMEOUT": "5",
        }
    )
    assert cfg.ticketing_url == "https://x/requests"
    assert cfg.timeout == 5.0
    assert cfg.headers()["Authorization"] == "Bearer k"
