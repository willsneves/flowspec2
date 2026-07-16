"""Real end-to-end simulations of the streetlight_repair flow.

Drives the compiled LangGraph flow as realistic citizen conversations, over the
REAL HTTP backends (a deterministic geocoder + ticketing system stub via httpx.MockTransport
— no network), printing a turn-by-turn transcript.

Run:  uv run python examples/simulate.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from flowspec2 import FlowRuntime, load_flow
from flowspec2.backends import BackendConfig, make_registry

DOC = load_flow(str(Path(__file__).with_name("streetlight_repair.flow.json")))
CONFIG = BackendConfig(
    geocode_url="https://services.example/geocode",
    brazilian_tax_id_lookup_url="https://services.example/brazilian_tax_id",
    govbr_enrich_url="https://services.example/govbr",
    ticketing_url="https://services.example/requests",
    api_key="demo-token",
)

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def make_transport(ticketing_handler) -> httpx.MockTransport:
    """A deterministic stand-in for a civic-service backend."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/geocode"):
            address = json.loads(request.content or b"{}").get("address", "")
            return httpx.Response(
                200, json={"street": address, "district": "Downtown", "city": "Example City"}
            )
        if path.endswith("/brazilian_tax_id"):
            return httpx.Response(200, json={"name": "", "email": "", "phones": []})
        if path.endswith("/govbr"):
            return httpx.Response(200, json={"phones": ["5521988887777"]})
        if path.endswith("/requests"):
            return ticketing_handler(request)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def runtime(ticketing_handler) -> FlowRuntime:
    return FlowRuntime(
        DOC, tools=make_registry(CONFIG, transport=make_transport(ticketing_handler))
    )


def _render(state) -> None:
    ar = state.agent_response
    iv = ar.interactive or {}
    print(f"  {CYAN}🤖{RESET} {ar.description}")
    if iv.get("status") == "flow_sent":
        print(f"     {DIM}↳ [sent the WhatsApp Flow form for completion]{RESET}")
    elif iv.get("out_of_band_sent"):
        print(f"     {DIM}↳ [sent the gov.br login button; awaiting authentication]{RESET}")
    elif iv.get("buttons"):
        print(f"     {DIM}↳ buttons: {' · '.join(b['title'] for b in iv['buttons'])}{RESET}")
    elif iv.get("sections"):
        rows = [r["title"] for s in iv["sections"] for r in s["rows"]]
        print(f"     {DIM}↳ list: {' · '.join(rows)}{RESET}")
    if state.status == "completed" and state.data.get("protocol_id"):
        print(f"     {GREEN}✅ request opened — protocol_id {state.data['protocol_id']}{RESET}")
    elif state.status == "error":
        print(f"     {YELLOW}⚠️  {ar.error_message}{RESET}")


class Conversation:
    def __init__(self, rt: FlowRuntime, user: str = "5521999990000") -> None:
        self.rt = rt
        self.user = user
        self.state = rt.new_state(user)

    async def say(self, human: str, payload: dict) -> None:
        print(f"  👤 {human}")
        self.state = await self.rt.execute(self.state, payload)
        _render(self.state)
        print()


def banner(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}\n")


async def scenario_flow_happy():
    banner("Scenario 1 — production path (WhatsApp Flow), anonymous user")
    conversation = Conversation(
        runtime(lambda r: httpx.Response(201, json={"protocol_id": "RLU-2026-000481"}))
    )
    await conversation.say("the streetlight on my street has been out for days", {})
    await conversation.say(
        "[completes the form]",
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "block",
            "location": "Street",
        },
    )
    await conversation.say("Orange Tree Street, 300", {"address": "Orange Tree Street, 300"})
    await conversation.say("yes, confirm it", {"confirmation": "yes"})
    await conversation.say("across from the market", {"reference_point": "across from the market"})
    await conversation.say("I prefer to remain anonymous", {"identification_method": "anonymous"})
    await conversation.say("open it 👍", {"confirmation": "yes"})


async def scenario_correction():
    banner("Scenario 2 — user corrects the address during confirmation")
    conversation = Conversation(
        runtime(lambda r: httpx.Response(201, json={"protocol_id": "RLU-2026-000482"}))
    )
    await conversation.say("the streetlight keeps flickering", {})
    await conversation.say(
        "[completes the form]",
        {
            "_source": "whatsapp_flow",
            "defect_type": "Flickering",
            "qty_pattern": "single",
            "location": "Street",
        },
    )
    await conversation.say("Oak Street, 200", {"address": "Oak Street, 200"})
    await conversation.say("yes", {"confirmation": "yes"})
    await conversation.say("near the pharmacy", {"reference_point": "near the pharmacy"})
    await conversation.say("anonymous", {"identification_method": "anonymous"})
    await conversation.say("no, the address is wrong", {"correction": "address"})
    await conversation.say("it is on Pine Street, 150", {"address": "Pine Street, 150"})
    await conversation.say("now it is correct", {"confirmation": "yes"})
    await conversation.say(
        "at the corner of Main Street", {"reference_point": "at the corner of Main Street"}
    )
    await conversation.say("open it", {"confirmation": "yes"})


async def scenario_govbr():
    banner("Scenario 3 — identification through gov.br (out of band)")
    conversation = Conversation(
        runtime(lambda r: httpx.Response(201, json={"protocol_id": "RLU-2026-000483"}))
    )
    await conversation.say("damaged streetlight on my street", {})
    await conversation.say(
        "[completes the form]",
        {"_source": "whatsapp_flow", "defect_type": "Damaged", "location": "Street"},
    )
    await conversation.say("Ocean Avenue, 1700", {"address": "Ocean Avenue, 1700"})
    await conversation.say("yes", {"confirmation": "yes"})
    await conversation.say("beside the kiosk", {"reference_point": "beside the kiosk"})
    await conversation.say("I want to use gov.br", {"identification_method": "govbr"})
    await conversation.say(
        "[logs in to gov.br and returns]",
        {
            "govbr_token": {
                "brazilian_tax_id": "52998224725",
                "name": "Jane Smith",
                "email": "jane@example.com",
            }
        },
    )
    await conversation.say("confirm it", {"confirmation": "yes"})


async def scenario_square_sports_court():
    banner("Scenario 4 — a public-square address opens the sports-court question")
    conversation = Conversation(
        runtime(lambda r: httpx.Response(201, json={"protocol_id": "RLU-2026-000484"}))
    )
    await conversation.say("the public-square lights are out", {})
    await conversation.say(
        "[completes the form]",
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "alternating",
            "location": "Public square",
        },
    )
    await conversation.say("Central Square", {"address": "Central Square"})
    await conversation.say("yes", {"confirmation": "yes"})
    await conversation.say("yes, it is near the sports court", {"near_sports_court": "yes"})
    await conversation.say(
        "near the volleyball net", {"reference_point": "near the volleyball net"}
    )
    await conversation.say("anonymous", {"identification_method": "anonymous"})
    await conversation.say("open it", {"confirmation": "yes"})


async def scenario_ticketing_retry():
    banner("Scenario 5 — ticketing outage (503 → retryable), followed by recovery")
    calls = {"n": 0}

    def flaky(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="ticketing system unavailable")
        return httpx.Response(201, json={"protocol_id": "RLU-2026-000485"})

    conversation = Conversation(runtime(flaky))
    await conversation.say("streetlight is not working", {})
    await conversation.say(
        "[completes the form]",
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "single",
            "location": "Street",
        },
    )
    await conversation.say("Maple Street, 40", {"address": "Maple Street, 40"})
    await conversation.say("yes", {"confirmation": "yes"})
    await conversation.say("near the bus stop", {"reference_point": "near the bus stop"})
    await conversation.say("anonymous", {"identification_method": "anonymous"})
    await conversation.say("open it", {"confirmation": "yes"})  # first open -> 503 retryable
    await conversation.say(
        "try again, please", {"confirmation": "yes"}
    )  # retry -> success, state preserved
    print(
        f"  {DIM}(ticketing calls: {calls['n']}; state preserved between failure and retry){RESET}\n"
    )


async def scenario_idempotency():
    banner("Scenario 6 — idempotency: duplicate delivery does not open two requests")
    calls = {"n": 0}

    def counting(_request):
        calls["n"] += 1
        return httpx.Response(201, json={"protocol_id": "RLU-2026-000486"})

    rt = runtime(counting)  # one runtime/registry => shared replay cache
    answers = [
        ("streetlight is not working", {}),
        (
            "[form]",
            {
                "_source": "whatsapp_flow",
                "defect_type": "Not working",
                "qty_pattern": "single",
                "location": "Street",
            },
        ),
        ("Elm Street, 5", {"address": "Elm Street, 5"}),
        ("yes", {"confirmation": "yes"}),
        ("near the school", {"reference_point": "near the school"}),
        ("anonymous", {"identification_method": "anonymous"}),
        ("open it", {"confirmation": "yes"}),
    ]
    print(f"  {DIM}— first conversation —{RESET}")
    conversation_one = Conversation(rt, user="5521900000001")
    for _, payload in answers:
        conversation_one.state = await rt.execute(conversation_one.state, payload)
    print(
        f"  👤 (complete conversation) → {GREEN}protocol_id {conversation_one.state.data['protocol_id']}{RESET}"
    )

    print(f"  {DIM}— same user, same answers (duplicate delivery) —{RESET}")
    conversation_two = Conversation(rt, user="5521900000001")
    for _, payload in answers:
        conversation_two.state = await rt.execute(conversation_two.state, payload)
    print(
        f"  👤 (complete conversation) → {GREEN}protocol_id {conversation_two.state.data['protocol_id']}{RESET}"
    )
    print(
        f"  {BOLD}→ actual ticketing calls: {calls['n']} (idempotency reuses the protocol ID){RESET}\n"
    )


async def main():
    print(
        f"{BOLD}flowspec2 · end-to-end simulations — streetlight_repair over HTTP backends (MockTransport){RESET}"
    )
    await scenario_flow_happy()
    await scenario_correction()
    await scenario_govbr()
    await scenario_square_sports_court()
    await scenario_ticketing_retry()
    await scenario_idempotency()
    print(f"{GREEN}{BOLD}✓ all simulations completed{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
