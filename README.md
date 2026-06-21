# flowspec2

**A JSON conversational-flow format that compiles to a [LangGraph](https://langchain-ai.github.io/langgraph/) `StateGraph` at runtime.**

You write one self-contained JSON document per service flow. A running agent loads it and `flowspec2` compiles it into an executable `StateGraph[ServiceState]` — the flow becomes immediately callable as a tool/subgraph. The design comes from the Prefeitura do Rio WhatsApp bot's `multi_step_service` framework; this repo is a clean, self-contained, dependency-light reimplementation of the *format* and its *runtime compiler*.

> **One idea — the boundary is the closed value-domain.** The JSON pins the **rails** (states, value-domains, transitions, guards, tool bindings, interrupts, idempotency, guardrails); the LLM reasons and acts **freely within** them (which flow to enter, extracting the closed token from free text/voice/photo, phrasing, side actions). The LLM structurally *cannot* invent a transition, skip a required slot, or widen a value-domain.

## Install

```bash
uv sync --extra dev          # creates .venv with langgraph + pydantic + jsonschema + pytest
uv run pytest                # run the suite (drives the luminária flow turn-by-turn)
uv run python examples/simulate.py   # 6 real citizen conversations over the HTTP backends
```

`examples/simulate.py` prints turn-by-turn transcripts of the reparo_luminaria flow as realistic conversations over the real HTTP backends (deterministic geocoder + SGRC via `MockTransport`): the production WhatsApp-Flow path, an address correction, gov.br auth, the praça→quadra branch, an SGRC outage (503 → retryable → recovers), and a duplicate submission that fires the side effect exactly once.

## LLM-driven (the engine side)

The simulations above feed already-extracted tokens. To exercise the *non-deterministic execution* side — a real LLM doing the work the format leaves open — there's a Gemini driver (`flowspec2.llm.GeminiAgent`, model `gemini-2.5-flash`, the model the production bot uses):

```bash
uv sync --extra llm                       # google-genai
export GEMINI_API_KEY=...
uv run python examples/llm_bot.py         # the citizen speaks free text; the LLM routes + extracts
```

The LLM does exactly two non-deterministic jobs, the rails hold everything else:
- **route** — decide whether the citizen's free-text opener enters the flow (from `route.description`);
- **extract** — read the messy message and the node's `payload_schema` / interactive options, and produce the **closed token** for the slot.

flowspec2's validators then enforce the rail: an out-of-domain extraction is rejected and the node re-asks. The transcript shows the boundary per turn: `👤 free text → 🧠 LLM extraction → 🤖 the rail's next state`. The driver is a thin protocol (`route` + `extract`), so any provider can implement it. The gated integration tests (`FLOWSPEC2_RUN_LLM_TESTS=1`) run it against a live Gemini; the default suite stays offline.

## Quickstart

```python
import asyncio
from flowspec2 import FlowRuntime, load_flow

flow = load_flow("examples/reparo_luminaria.flow.json")   # validates against the JSON Schema
rt = FlowRuntime(flow)                                     # compiles → StateGraph[ServiceState]

async def main():
    state = rt.new_state(user_id="5521999999999")
    # turn 1 — the agent extracted the closed token "Apagada" from the citizen's free text
    state = await rt.execute(state, {"luminaria_defeito": "apagada"})
    print(state.agent_response.description)                # the next question (e.g. quantidade)
    print(state.agent_response.payload_schema)             # the JSON Schema handed to constrained decoding

asyncio.run(main())
```

`rt.as_tool()` returns a `multi_step_service`-style callable `(service_name, user_id, payload) -> dict` an outer agent dispatches to.

## Real backends

Tools (`geocode`, `cpf_lookup`, `get_user_info`, `sgrc_open_ticket`) default to in-memory fakes so the suite runs offline. Swap in real HTTP backends — `make_registry` overlays them onto the fakes per configured URL (partial config falls back per-tool, and the terminal's idempotency replay cache is preserved):

```python
from flowspec2 import FlowRuntime
from flowspec2.backends import BackendConfig, make_registry

cfg = BackendConfig.from_env()      # FLOWSPEC2_GEOCODE_URL / _CPF_LOOKUP_URL / _GOVBR_ENRICH_URL / _SGRC_URL / _API_KEY
rt = FlowRuntime(doc, tools=make_registry(cfg))
```

Install the HTTP extra with `uv sync --extra http`. The SGRC adapter maps HTTP semantics onto the terminal outcome trichotomy: **2xx → `success`**, **5xx / timeout / connection error → `retryable`** (the terminal node preserves state and re-fires next turn), **4xx → `fatal`** (resets). Backends are injectable (`transport=`) so they're tested offline with `httpx.MockTransport` — no network. Point each URL at a real Prefeitura endpoint (or a thin adapter conforming to the contracts in `backends/http.py`).

## CLI

```bash
flowspec2 validate examples/reparo_luminaria.flow.json   # JSON-Schema validate
flowspec2 graph    examples/reparo_luminaria.flow.json   # list compiled node ids
flowspec2 mermaid  examples/reparo_luminaria.flow.json   # export the compiled graph as mermaid
```

## What it compiles

| flowspec2 construct | LangGraph primitive |
|---|---|
| document | one `StateGraph[ServiceState]` compiled per call |
| `domains.<X>` | a Pydantic `@field_validator(mode="before")` + `model_json_schema()` (→ constrained-decoding `payload_schema`) + interactive option titles/rows |
| `path` `slot`/`confirm`/`derive`/`terminal`/`use` step | `add_node` with the canonical collect/confirm/derive/terminal template; subflow splice |
| `path` order | synthesized `add_conditional_edges` routers (pause = `agent_response` set → `END`) |
| `ask_when` / `overrides.gates` | guard predicate compiled into the node + path map (skip-by-vacuity) |
| `confirm.correctable[]` | non-linear back-edges; clear-cascade derived from `requires[]` + `derive.from` |
| `terminal.outcomes` | success / retryable / fatal trichotomy + `_reset_on_next_call` |
| `auto_flow` | pre-graph short-circuit (send WhatsApp Flow, return `flow_sent` without entering the graph) |
| `capabilities.await_external` | suspend/resume on an external signal (gov.br OAuth) with abort/resend/switch recovery |
| `predicate` grammar | pure boolean function over `ServiceState` compiled into routers/early-returns |

Full mapping + rationale + rejected alternatives: [`docs/DESIGN.md`](docs/DESIGN.md). Field-by-field reference: [`docs/SPEC.md`](docs/SPEC.md).

## Layout

```
src/flowspec2/
  models.py        ServiceState · AgentResponse · ServiceMetadata (the contracts)
  domains.py       domain → Pydantic validator + normalize strategies
  predicates.py    the frozen predicate grammar evaluator
  nodes.py         collect / confirm / derive / terminal node templates
  compiler.py      flowspec doc → StateGraph[ServiceState]
  runtime.py       FlowRuntime: validate · compile · execute · as_tool (+ auto_flow short-circuit)
  schema.py        load + JSON-Schema validation
  interactive.py   buttons / list / flow envelope builders (Meta limits)
  tools.py         ToolRegistry + injectable fake backends + idempotency replay
  backends/        BackendConfig + make_registry + httpx HTTP tools (real integrations)
  subflows/        address@1 · identification@2 (reusable, versioned)
  flowspec-2.schema.json
examples/          reparo_luminaria.flow.json · reparo_buraco.flow.json
tests/             schema · domains · predicates · luminária E2E · authorability
```

## Status

Reference runtime for the flowspec/2 format. Subflows ship with in-memory fake backends (geocode, CPF lookup, gov.br token, SGRC ticket) so the suite runs offline; each backend is an injectable protocol that maps to the real production integration. Not affiliated with or deployed by the Prefeitura do Rio — this is a clean-room reimplementation of a format design.

MIT licensed.
