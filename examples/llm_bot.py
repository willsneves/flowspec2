"""LLM-driven simulation — the bot's *intelligence* doing the non-deterministic work.

Here the citizen speaks in messy free text. A real LLM (Gemini 2.5 Flash) does
the non-deterministic part:
routing into the flow and extracting the closed token for each slot from the
node's payload_schema / interactive options. flowspec2 holds the rails — the
validators reject anything out of domain. The transcript shows the boundary:

    👤 free text   →   🧠 LLM extraction (closed token)   →   🤖 the rail's next state

Uses the conversational variant of streetlight_repair (no auto_flow / no summary)
so the LLM extracts EVERY slot from natural language. Needs GEMINI_API_KEY.

Run:  uv run python examples/llm_bot.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

from flowspec2 import FlowRuntime, load_flow
from flowspec2.llm import GeminiAgent

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAG = "\033[35m"

BASE = load_flow(str(Path(__file__).with_name("streetlight_repair.flow.json")))


def conversational(doc: dict) -> dict:
    """Strip auto_flow + the summary step so the LLM drives every slot in text."""
    d = copy.deepcopy(doc)
    d.pop("auto_flow", None)
    d["path"] = [s for s in d["path"] if s.get("confirm") != "service_confirmed"]
    return d


def _render(state) -> None:
    ar = state.agent_response
    iv = ar.interactive or {}
    print(f"  {CYAN}🤖{RESET} {ar.description}")
    if iv.get("out_of_band_sent"):
        print(f"     {DIM}↳ [sent the gov.br login button]{RESET}")
    elif iv.get("buttons"):
        print(f"     {DIM}↳ buttons: {' · '.join(b['title'] for b in iv['buttons'])}{RESET}")
    elif iv.get("sections"):
        print(
            f"     {DIM}↳ list: {' · '.join(r['title'] for s in iv['sections'] for r in s['rows'])}{RESET}"
        )
    if state.status == "completed" and state.data.get("protocol_id"):
        print(f"     {GREEN}✅ request opened — protocol_id {state.data['protocol_id']}{RESET}")
    elif state.status == "error":
        print(f"     {YELLOW}⚠️  {ar.error_message}{RESET}")


def banner(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}\n")


async def converse(agent: GeminiAgent, title: str, opener: str, turns: list) -> None:
    banner(title)
    rt = FlowRuntime(conversational(BASE))
    state = rt.new_state("5521999990000")

    print(f"  👤 {opener}")
    service = await asyncio.to_thread(agent.route, opener, [rt.doc])
    print(f"  {MAG}🧭 routed to: {service}{RESET}")
    if service != rt.flow:
        print(
            f"  {CYAN}🤖{RESET} Sorry, I cannot help with that yet. I can register public-lighting problems. 🙏\n"
        )
        return
    state = await rt.execute(state, {})
    _render(state)
    print()

    for turn in turns:
        if state.status == "completed":
            break
        if isinstance(turn, dict):  # an external signal (e.g. gov.br auth completion)
            print(f"  {DIM}📨 [external signal: {', '.join(turn)}]{RESET}")
            payload = turn
        else:
            print(f"  👤 {turn}")
            payload = await asyncio.to_thread(agent.extract, turn, state.agent_response)
            print(f"  {MAG}🧠 extracted: {json.dumps(payload, ensure_ascii=False)}{RESET}")
        state = await rt.execute(state, payload)
        _render(state)
        print()


async def main() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is missing; set it to run the LLM bot.")
        sys.exit(1)
    agent = GeminiAgent()
    print(f"{BOLD}flowspec2 · LLM-driven bot (Gemini) — streetlight_repair{RESET}")
    print(
        f"{DIM}The user speaks freely; the LLM routes and extracts a closed token; flowspec2 validates the rail.{RESET}"
    )

    await converse(
        agent,
        "Conversation 1 — free text, happy path (anonymous)",
        "hello! the lighting on my street has a problem that I want to report",
        [
            "the light has been out for days and the street is dark at night 😞",
            "the whole block has several lights out",
            "some turn on and others do not",
            "it is on the street",
            "Orange Tree Street, 300, Downtown",
            "yes, confirm it 👍",
            "there is a market directly across from it",
            "no identification, I prefer to remain anonymous",
            "yes, open it, thank you",
        ],
    )

    await converse(
        agent,
        "Conversation 2 — user requests a correction in natural language",
        "I need to report a streetlight problem",
        [
            "the streetlight keeps flickering",
            "it is only this one streetlight",
            "it is on the street",
            "Oak Street, 200, Downtown",
            "yes",
            "near the pharmacy",
            "anonymous is fine",
            "wait, I think I gave the wrong address",  # -> address correction
            "it is actually on Pine Street, 150",
            "now it is correct",
            "beside the newsstand",
            "open it",
        ],
    )

    await converse(
        agent,
        "Conversation 3 — identification through gov.br",
        "I want to open a request for a damaged streetlight",
        [
            "it is broken and hanging from the pole",
            "it is on the street",
            "Ocean Avenue, 1700, Downtown",
            "yes",
            "beside the kiosk",
            "I want to identify through gov.br",
            {
                "govbr_token": {
                    "brazilian_tax_id": "52998224725",
                    "name": "Jane Smith",
                    "email": "jane@example.com",
                }
            },
            "confirm it",
        ],
    )

    await converse(
        agent,
        "Conversation 4 — public square opens the sports-court question",
        "the lights in the public square are out",
        [
            "all of them are out",
            "there are several streetlights",
            "some turn on and others do not",
            "it is in a public square",
            "Central Square, Downtown",
            "yes",
            "yes, it is near the sports court",
            "near the volleyball net",
            "anonymous",
            "open it",
        ],
    )

    await converse(
        agent,
        "Conversation 5 — routing rejects an out-of-scope service",
        "hello, I want to pay an overdue property-tax bill",
        [],
    )

    print(f"{GREEN}{BOLD}✓ LLM simulations completed{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
