<!-- section:toc -->

Table of Contents:

- Files: 27 <!-- section:files -->
- The one idea: the boundary is the closed value-domain: 45 <!-- section:closed-value-domain -->
- Top-level shape (the two tiers): 62 <!-- section:top-level-shape -->
- Mapping table — every construct → its LangGraph primitive: 83 <!-- section:mapping-table -->
- Rationale (1 page): 135 <!-- section:rationale -->
    - Rejected alternatives: 147 <!-- section:rationale-rejected-alternatives -->
- Open nits (non-blocking; for a v2.1 of the spec): 163 <!-- section:open-nits -->
- How this was produced: 172 <!-- section:production-method -->

<!-- /section:toc -->

# flowspec/2 — a JSON conversational-flow format compiled to LangGraph

> **Status:** implemented reference format and runtime compiler.

A single self-contained JSON document describes one citizen-facing service
flow. `FlowRuntime` validates and compiles it into a
`StateGraph[ServiceState]` when the runtime instance is created, then exposes the
compiled flow as a reusable callable tool.

<!-- section:files -->

## Files
| File | What |
|---|---|
| [`../src/flowspec2/flowspec-2.schema.json`](../src/flowspec2/flowspec-2.schema.json) | The authoritative JSON Schema (Draft 2020-12). |
| [`../examples/reparo_luminaria.flow.json`](../examples/reparo_luminaria.flow.json) | The luminária flow fully expressed in the format. |
| [`../examples/reparo_buraco.flow.json`](../examples/reparo_buraco.flow.json) | A second service that exercises authorability. |
| [`../src/flowspec2/clock.py`](../src/flowspec2/clock.py) | Injectable UTC clock contract and real boundary adapter. |
| [`../src/flowspec2/observability.py`](../src/flowspec2/observability.py) | Snowflake correlation IDs and structured event logging. |

The project test suite checks the schema itself, validates both example
documents, and rejects adversarial mutations such as unknown keys, ambiguous
steps, malformed predicates, and invalid versions.

---

<!-- /section:files -->
<!-- section:closed-value-domain -->

## The one idea: the boundary is the closed value-domain

> **Deterministic structure, non-deterministic execution.** The JSON pins the rails; the LangGraph agent reasons and acts freely *within* them. The boundary is **positional — there is no `isRail` tag** (an annotation can be mislabelled). It is marked three ways at once:

- **By field** — inside any slot/step, only `prompt.text`, `prompt.extract_hint`, and `route.{description,trigger_phrases}` are LLM-editable. Every sibling (`domain`, `normalize`, `required`, `requires`, `ask_when`, transitions, tool bindings) is a rail.
- **By location** — `path` / `uses` / `derive` / `confirm` / `terminal` / `overrides` / `auto_flow` are rails (states, value-domains, transitions, guards, idempotency, reset). `capabilities` is the explicitly **fenced LLM-freedom zone** for side actions the LLM *may* invoke (media, TTS, handoff, reset), bounded by the Mule outbound whitelist and never wired as states. `capabilities.await_external` is the named deterministic exception: it binds to a path or subflow state.
- **By construct** — the closed `domain` array **is** the boundary. The LLM extracts a free-text/voice/photo candidate (its job); the generated `@field_validator(mode="before")` rejects anything outside `values` (the rail). At each pause the compiler ships `payload_schema = domain.model_json_schema()` to constrained decoding, forcing extraction onto a closed token at exactly one place.

Three impossibilities are therefore **structural, not policed**: the LLM cannot invent a transition (synthesized routers only return declared targets), cannot skip a required ungated slot (the node early-returns until its domain-validated slot is filled), and cannot widen a value-domain (the before-validator raises).

What the LLM *is* free to do: choose which flow to enter (`route.description`), extract the token from free text/voice/photo, phrase prompts (unless `verbatim:true`), and choose/order the non-deterministic `capabilities` side-calls.

---

<!-- /section:closed-value-domain -->
<!-- section:top-level-shape -->

## Top-level shape (the two tiers)

```
{ schema:"flowspec/2", flow, version,            // identity + semver
  service{}, route{}, config{}, entry{},          // metadata, the LLM route-in hook, guardrail booleans, best-effort init tool
  domains{}, slots{},                             // the SPINE: closed value-domains (4 artifacts each) + slot bindings
  path[ {slot|confirm|derive|terminal|use|await_external} ], // TIER 1 — flat ordered happy path; edges/pauses SYNTHESIZED
  uses[], derive[], confirm{}, terminal{},        // reusable subflows, computed values, correction hub, fulfillment+lifecycle
  overrides{ gates{} }, auto_flow{},              // TIER 2 — hard cases (multi-slot gates, pre-graph WhatsApp Flow)
  capabilities{} }                                // FENCED LLM zone: media in/out, TTS, location, handoff, reset, await_external
```

**Tier 1** is the authorable happy path: a flat ordered `path` of single-purpose steps + a `domains` registry + a few `uses` subflow refs. It reads top-to-bottom with no hand-wired nodes, edges, or routers — the `PAUSE→END` and back-to-agent edges are synthesized. **Tier 2** (`overrides`, `derive`, `confirm.correctable`, `auto_flow`, `await_external`) is paid for only by flows that need it. The luminária's gates, derived lookup table, gov.br race, and non-linear corrections live in escape hatches, never in the linear read. The **step pointer is never serialized** — position is rediscovered from filled slots each turn (which is exactly what lets corrections + WhatsApp-Flow prefill compose without resume bugs).

The `domains` registry is the spine: declaring a domain once materializes four artifacts that must never drift — the Pydantic validator, the `payload_schema` enum handed to constrained decoding, the interactive button titles, and the list rows. The recurring production bug (a button tap that doesn't match the recognizer token) becomes structurally impossible. Adding a value is a one-line append. Corrections are **derived**: the author declares only which slots are directly `correctable`; the clear-cascade is computed from each slot's `requires[]` + `derive.from` (no hand-maintained `clears[]` — the stale-state footgun killer). Escape hatches are weak on purpose: `derive` is lookup-table-only with a single `$from[i]` sigil; predicates are a frozen object grammar (`in|eq|ne|is_present|and|or|not`) over five read-only namespaces (`slots.`/`internal.`/`payload.`/`config.`/`address.`) — no string sugar, no arbitrary computation.

---

<!-- /section:top-level-shape -->
<!-- section:mapping-table -->

## Mapping table — every construct → its LangGraph primitive

| flowspec/2 construct | LangGraph / runtime primitive | Notes |
|---|---|---|
| `flow` (id) + `version` | stable service identity plus document semver; the reference `as_tool()` adapter keys its in-memory state by user id | A production host may replace the reference state store. |
| top-level document | one `StateGraph[ServiceState]` compiled when a `FlowRuntime` is created and reused by that instance | Flow = callable subgraph/tool an outer agent dispatches to. |
| `route.description` / `trigger_phrases` | the LLM router-in: top `create_react_agent` agent node picks `multi_step_service(service_name=…)` | The ONE LLM hook for entry. `entry_args_schema` = closed initial-slot set. |
| `entry.tool` (blocking:false) | no-pause node at `set_entry_point`; `@handle_errors` swallows failure; unconditional edge to first step | generic `initialize`, decoupled from `service` identity. |
| `service{}` | constants seeded into `state.internal`/`data` at compile time | never a node. |
| `domains.<X>` | Pydantic model w/ `@field_validator(mode="before")` from `values`+`normalize`; `model_json_schema()` → `AgentResponse.payload_schema` | FOUR artifacts from one declaration. |
| `domains.<X>.values` | the enum shipped to constrained decoding at each pause; `ValueError` on out-of-domain | the rail the LLM cannot widen. |
| `domains.<X>.rows[].description` | `send_whatsapp_list` `sections[].rows[].{title,description}` | two-column list subtitles. |
| `slots.<s>.persist` | which `ServiceState` partition (data/internal/payload) the node reads/writes | data=persist+visible, internal=persist+hidden, payload=ephemeral. |
| `slots.<s>` + `path.slot` step | `add_node(step, collect_template)`: early-return-if-filled → validate `payload[s]` via domain → ADVANCE (`agent_response=None`) / RE-ASK (`error_message`, ++attempts) / PAUSE | pointer NOT persisted; position rediscovered from `data`. |
| `path` order (implicit fall-through) | synthesized `add_conditional_edges(step, _route_after, {next:next, END:END})`; router → `END` iff `agent_response` set | authors never write routers or `set_entry_point`. |
| `step.ask_when` / `overrides.gates[step]` | guard predicate compiled into the node's leading early-return + path_map; `gate==false` ⇒ slot satisfied-by-vacuity | gated quantidade/intercaladas/quadra. |
| `step.skip_when` | leading early-return that advances without asking | summary skip on `payload._source==whatsapp_flow`. |
| `confirm` step + `interactive.gate` | `confirm_template` sets `AgentResponse.interactive`; wrapper sends if `ENABLE_INTERACTIVE_CONFIRM` → `status=interactive_sent` (**path B**) | `route_tool_responses` ends turn on `interactive_sent`. |
| `interactive` (buttons/list) **path B** | `AgentResponse.interactive` → wrapper → Mule `canonicalToolReturn` | options from `from_domain`. |
| `interactive kind:flow` / `out_of_band:true` | path-B Flow envelope / CTA-URL push; `out_of_band_sent=True` → `interactive_sent` unconditionally | gov.br login button. |
| `interactive.options_when` | conditional option rendering (e.g. append "Sem me identificar" iff `not identification_required`) | per-option visibility. |
| capabilities tools (LLM **path A**) | `send_whatsapp_buttons/list/build_whatsapp_flow_envelope` marked `return_direct=True` → `route_tool_responses` → `END` | the two send paths pinned separately (A return_direct, B interactive_sent). |
| `derive[]` + `derive` step | no-prompt node: `data[writes]=lookup['|'.join(from)]` else `$from[i]`; `agent_response=None` → advance | `_classifica_defeito`; never asked, no domain leak. |
| `uses[].ref` (`address@1`) + `use` step | splice mixin subgraph (nodes/edges/slots) at the anchor; `with{}` → `common_config` | Address honors optional/required collection, confirmation, attempt budget, and every declared exhaustion route; mixins expose slots upward. |
| `confirm.correctable[]` | confirm node: LLM sets `data['correction_requested']=<slot>` → `Command(update=cleared, goto=<slot node>)` | non-linear back-edge. |
| clear-cascade (DERIVED from `requires[]`) | `_clear_corrected_field`: pop slot + transitive `requires[]`-dependents + `derive.from` readers | author declares only the correctable set. |
| `terminal` (idempotent, outcomes) | terminal service-call node → `tool` with `input` mapping; edge → `END` | sha256 key + replay computed by compiler. |
| `terminal.outcomes.success` | `ticket_opened()`: set `outputs`, `_reset_on_next_call=True` | reset-on-next-call. |
| `terminal.outcomes.retryable` | `ticket_failed(reset_workflow=False)`: preserve state, re-enter | real retry. |
| `terminal.outcomes.fatal` | `ticket_failed(reset_workflow=True)`: `_reset_on_next_call=True` | non-retryable reset. |
| `terminal.empty_payload` | `base_workflow.execute`: never_saved→wipe; in_progress→ignore | empty-payload semantics. |
| graph end, `agent_response is None` | `status='completed'` | terminal/complete. |
| `AgentResponse.error_message` / `log_id` | runtime boundary emits a structured event and propagates its decimal Snowflake identifier through `AgentResponse` and `as_tool()` | injected clock/worker generator; internal warnings use the same event contract. |
| `ServiceMetadata` timestamps | `FlowRuntime` samples its injected UTC clock when state is created or saved | the real UTC clock is a boundary adapter; deterministic tests pass a fake clock. |
| `auto_flow.send_when` | tool-layer ABOVE the graph: true + entry slot empty → send Flow → `{status:flow_sent}` WITHOUT `ainvoke` | pre-graph short-circuit. |
| `auto_flow.prefill_from` | `flow_token = encode_prefill_token(...)` (`v1:base64url(json)`) | Meta `${data.X}` prefill. |
| `auto_flow.alias_map` | `_normalize_payload_aliases` on nfm_reply (value-keyed fan-out) | one Flow field → many slots. |
| `auto_flow.resume_at` | re-injected `multi_step_service(_source=whatsapp_flow)` rediscovers position | also derives summary-skip. |
| `capabilities.media_in.analyze` | `register_inbound_media` + `analyze_inbound_image/audio/video`; `workflow_sugerido` → top-agent route | inbound media. |
| `capabilities.media_out` (+ template) | `send_whatsapp_media(type,…)` (LLM may call); template = 24h-window Meta template | bounded by Mule whitelist. |
| `capabilities.tts` | `generate_audio_response` | TTS. |
| `capabilities.location_in` | `message_type==location` → `media.latitude/longitude`; `reverse_geocode_address` | location in. |
| `capabilities.handoff` | LLM emits Central-1746 fallback / handoff | to human. |
| `capabilities.session_reset` | `reset_session_state` / `_reset_on_next_call` | session reset. |
| `capabilities.await_external` | suspend node: emit out-of-band, resume on a host-delivered `resume_on` signal; apply bounded token/result mappings atomically; route host-delivered abort/resend/switch/timeout events; reset next call when recovery ends at `END` | gov.br race; reusable for payment/IdP. |
| `predicate` object grammar | pure boolean fn over `ServiceState` (frozen namespaces) compiled into routers/early-returns | no arbitrary Python; verifiable/diffable. |

---

<!-- /section:mapping-table -->
<!-- section:rationale -->

## Rationale (1 page)

The design rests on one decision: the deterministic/non-deterministic boundary is positional and falls on the closed value-domain. Everything else follows.

**The boundary, precisely.** The JSON pins the rails: the set of states (`path` steps + spliced `uses` nodes + synthesized derive/terminal nodes), the entry point (`entry` then the first `path` step), the required slots and their closed value-domains (`domains` + `slots`), the normalization rules (`domains.*.normalize`, deterministically enforced even though authored as hints), the allowed transitions and guards (linear fall-through + `ask_when`/`overrides.gates` + `confirm.correctable` back-edges, all over the frozen grammar), which tool each state calls (`entry.tool`, `terminal.tool`, subflow bindings), where the flow pauses (the `agent_response is not None` convention, synthesized per step kind — never authored), and the idempotency/reset/guardrail behavior (`terminal.outcomes` trichotomy, `empty_payload`, `config` booleans, `slots.*.max_attempts/on_exhaust`). It leaves the LLM free for exactly: which flow to enter, extracting the closed token from free text/voice/photo, phrasing, and choosing/ordering the `capabilities`.

**Why two tiers.** ~80% of every flow is a linear sequence of slot collections; only branches/gates/back-edges/derivations are hard. Forcing explicit `transitions[]` on every node multiplies each state by ~10 lines for zero added expressiveness and wrecks diffs. The flat `path` with implicit fall-through collapses luminária's 14 nodes + 13 conditional-edge wirings into a readable array; the three genuinely irreducible hard cases the code proves necessary — derived values (`_classifica_defeito`), multi-slot gates (`_needs_quadra_question`), and non-linear corrections — live in `overrides`/`derive`/`confirm`, so the happy path never gets harder to read as edge cases accumulate.

**Why the domain registry is the spine.** Declaring a domain once materializes four artifacts that must never drift; the recurring "button tap ≠ recognizer token" bug becomes structurally impossible. **Why corrections are derived.** A hand-declared `clears[]` is a second dependency list duplicating `requires[]`/`derive.from`, reintroducing the stale-state footgun. **Why the escape hatches are weak on purpose.** `derive` is a lookup table + one `$from[i]` sigil (exactly `_classifica_defeito`, nothing more); the predicate grammar is a frozen object form over five namespaces (no string sugar → always compilable). Provider plumbing (flow_token base64, sha256 recipe, Mule whitelist, JWT/SCRT, Object Store TTLs) stays in the compiler/library; the author declares `idempotent:true` and `media_out:["location"]` and the compiler cross-checks the whitelist. The luminária instance proves both sufficiency (every grounding feature) and minimality (no field unused by it).

<!-- section:rationale-rejected-alternatives -->

### Rejected alternatives
1. **Explicit `transitions[]` per state (XState/SCXML/ASL-faithful).** Lowest-scored: ~10 lines/node for zero added power, plus string-sugar guards that aren't deterministically compilable. flowspec/2 keeps the 1:1 LangGraph mapping but **synthesizes** edges from the flat path + dependency DAG, and freezes the predicate grammar to the object form.
2. **Hand-declared `clears[]` correction cascade.** A second dependency list duplicating `requires[]`/`derive.from` → stale-state footgun. flowspec/2 **derives** the cascade.
3. **A general expression/derive language (`$`/`=field`/named functions; Power Fx / DF CX `$sys.func.*`).** Unverifiable, undiffable, re-opens the "and then a dev writes Python" hole. Capped at a lookup table + one sigil; predicates frozen.
4. **Pure contract/DAG for *every* flow (LLM plans collection order even for linear cases).** Elegant but makes the simple case read as a contract, not a sequence — hurting authorability (the dominant axis). flowspec/2 keeps the flat `path` for the readable 80% and grafts DAG-derived correctness only into Tier 2.
5. **Model on `langgraph.json` / persist a `current_step` pointer.** `langgraph.json` points at hand-authored Python and describes no graph structure. A persisted pointer is a second source of truth that conflicts with correction back-edges, reset, and Flow prefill — the verified system rediscovers position from filled slots, which flowspec/2 preserves.
6. **Native `interrupt()` + `Command(resume=)` + a flow-level checkpointer.** The reference runtime keeps state outside the compiled graph and reuses one compiled graph per runtime instance; pause is the `agent_response`-is-set convention. Designing around `interrupt()` would introduce a second persistence model.
7. **Per-field `isRail:true` annotations.** An annotation can be mislabelled and adds noise. The boundary is made positional and self-documenting instead.
8. **gov.br / WhatsApp Flow as ordinary collect slots; media/TTS/handoff as path states.** Both suspend on an *external* signal (OAuth token / nfm_reply), not the next text turn — modeling them as collect slots waits on the wrong channel. Media/TTS/handoff are non-deterministic LLM side-calls bounded by the whitelist; forcing them into the state machine over-constrains the LLM. flowspec/2 makes gov.br a `await_external` capability, `auto_flow` a pre-graph guard, and keeps media/TTS/handoff as `capabilities`.

---

<!-- /section:rationale-rejected-alternatives -->
<!-- /section:rationale -->
<!-- section:open-nits -->

## Open nits (non-blocking; for a v2.1 of the spec)
- **Subflow-exposed slot discovery.** `address`/`cpf`/`email`/`name` are referenced in `confirm.correctable[]` and `terminal.input` but declared nowhere in the document (they come from the subflows). Add a `uses[].exposes:[…]` echo (or require the compiler to reject unresolved slot refs) so referential integrity is checkable in-format.
- **Reference-point is modeled in three places** in the luminária example (`config.reference_point_required` + a top-level `ponto_referencia` slot + `address@1 with.reference_point_required`). The format permits either home; the canonical example should commit to one.
- **`options_when` / `fill_only_when_asked`** are in the schema but only exercised inside the identification subflow — assert-by-mechanism, not demonstrated end-to-end. Add a subflow-definition fixture that shows them firing.
- **`interactive.field` ≠ slot name** (`confirmacao` vs `ticket_data_confirmed`) is faithful to the code but a footgun; the schema description notes it is the Meta tap payload key, not the data slot.

<!-- /section:open-nits -->
<!-- section:production-method -->

## How this was produced
Multi-agent workflow (17 agents): prior-art sweep (Rasa CALM, Dialogflow CX / Lex V2, XState/SCXML/Step Functions/BPMN, agent-SDK handoffs, LangGraph-native) → code-grounded feature inventory (47 features) → 4 competing design philosophies → adversarial judges + completeness critic → synthesis → schema-validated adversarial verification. flowspec/2 = the two-tier declarative skin (highest-scored design) grafted onto the contract/DAG correctness primitives (derived corrections, gate-by-vacuity) and the constrained-decoding/lookup-only-derive discipline from the other finalists.

<!-- /section:production-method -->
