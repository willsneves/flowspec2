<!-- section:toc -->

Table of Contents:

- Files: 28 <!-- section:files -->
- Authoring-to-execution pipeline: 82 <!-- section:authoring-execution-pipeline -->
- The one idea: the boundary is the closed value-domain: 161 <!-- section:closed-value-domain -->
- Top-level shape (the two tiers): 178 <!-- section:top-level-shape -->
- Mapping table — every construct → its LangGraph primitive: 199 <!-- section:mapping-table -->
- Rationale (1 page): 259 <!-- section:rationale -->
    - Rejected alternatives: 279 <!-- section:rationale-rejected-alternatives -->
- How this was produced: 295 <!-- section:production-method -->

<!-- /section:toc -->

# flowspec/2 — a JSON conversational-flow format compiled to LangGraph

> **Status:** implemented reference format and runtime compiler.

A single self-contained JSON document describes one citizen-facing service
flow. Project tooling checks and normalizes the authored source against a named
runtime profile, produces a canonical intermediate representation, and compiles
the executable document into a `StateGraph[ServiceState]`. `FlowRuntime` reuses
that graph as a callable tool.

<!-- section:files -->

## Files
| File | What |
|---|---|
| [`../src/flowspec2/flowspec-2.schema.json`](../src/flowspec2/flowspec-2.schema.json) | The authoritative JSON Schema (Draft 2020-12). |
| [`../examples/reparo_luminaria.flow.json`](../examples/reparo_luminaria.flow.json) | The luminária flow fully expressed in the format. |
| [`../examples/reparo_buraco.flow.json`](../examples/reparo_buraco.flow.json) | A second service that exercises authorability. |
| [`../src/flowspec2/clock.py`](../src/flowspec2/clock.py) | Injectable UTC clock contract and real boundary adapter. |
| [`../src/flowspec2/observability.py`](../src/flowspec2/observability.py) | Snowflake correlation IDs and structured event logging. |
| [`../src/flowspec2/checker.py`](../src/flowspec2/checker.py) | Aggregate structural, semantic, profile, and compilation checks. |
| [`../src/flowspec2/cli.py`](../src/flowspec2/cli.py) | Command handlers, I/O boundaries, and error reporting. |
| [`../src/flowspec2/cli_parser.py`](../src/flowspec2/cli_parser.py) | Declarative argument grammar with injected handlers. |
| [`../src/flowspec2/compiler.py`](../src/flowspec2/compiler.py) | Executable node assembly, ordering, and StateGraph wiring. |
| [`../src/flowspec2/compiler_contracts.py`](../src/flowspec2/compiler_contracts.py) | Stable facade for compiler-contract validation entry points. |
| [`../src/flowspec2/compiler_schema_relations.py`](../src/flowspec2/compiler_schema_relations.py) | JSON Schema subset, property, path, and required-presence proofs. |
| [`../src/flowspec2/compiler_value_contracts.py`](../src/flowspec2/compiler_value_contracts.py) | Entry, slot, derive, exposed-state, and terminal value contracts. |
| [`../src/flowspec2/compiler_tool_contracts.py`](../src/flowspec2/compiler_tool_contracts.py) | Tool inputs, outputs, effects, and terminal lifecycle contracts. |
| [`../src/flowspec2/compiler_resume_contracts.py`](../src/flowspec2/compiler_resume_contracts.py) | External suspension, resume-token, enrichment, and recovery contracts. |
| [`../src/flowspec2/semantics.py`](../src/flowspec2/semantics.py) | Semantic orchestration and runtime-profile linking. |
| [`../src/flowspec2/semantic_source_contracts.py`](../src/flowspec2/semantic_source_contracts.py) | Stable facade for source-owned semantic contract groups. |
| [`../src/flowspec2/semantic_schema_contracts.py`](../src/flowspec2/semantic_schema_contracts.py) | Shared semantic JSON Schema type, value, and property relations. |
| [`../src/flowspec2/semantic_path_contracts.py`](../src/flowspec2/semantic_path_contracts.py) | Path identity, domain, normalization, slot, and dependency contracts. |
| [`../src/flowspec2/semantic_derive_contracts.py`](../src/flowspec2/semantic_derive_contracts.py) | Derived-value, placement, totality, and execution-order contracts. |
| [`../src/flowspec2/semantic_predicate_contracts.py`](../src/flowspec2/semantic_predicate_contracts.py) | Predicate namespace, reference, literal, and type contracts. |
| [`../src/flowspec2/semantic_state_contracts.py`](../src/flowspec2/semantic_state_contracts.py) | Entry schema, state-writer ownership, and rail-reference contracts. |
| [`../src/flowspec2/ir.py`](../src/flowspec2/ir.py) | Canonical normalization and immutable compiler contracts. |
| [`../src/flowspec2/profiles.py`](../src/flowspec2/profiles.py) | Named executable tool, subflow, domain, and capability catalogs. |
| [`../src/flowspec2/schema_contracts.py`](../src/flowspec2/schema_contracts.py) | Closed local-reference and external-resume JSON Schema contracts. |
| [`../src/flowspec2/authoring/benchmark.py`](../src/flowspec2/authoring/benchmark.py) | Provider-neutral AI-authoring and correction benchmark. |
| [`../src/flowspec2/authoring/corpus.py`](../src/flowspec2/authoring/corpus.py) | Packaged reference-corpus manifest, integrity checks, and content identity. |
| [`../src/flowspec2/authoring/gemini.py`](../src/flowspec2/authoring/gemini.py) | Explicit-network Gemini source transport over the closed projection. |
| [`../src/flowspec2/authoring/evidence.py`](../src/flowspec2/authoring/evidence.py) | Exact captures and content-addressed real-model evidence. |
| [`../src/flowspec2/authoring/evidence_verification.py`](../src/flowspec2/authoring/evidence_verification.py) | Closed-schema verification and deterministic offline replay. |
| [`../src/flowspec2/authoring/evidence_signature.py`](../src/flowspec2/authoring/evidence_signature.py) | Detached Ed25519 signing and authentication over verified evidence. |
| [`../src/flowspec2/authoring/authoring-evidence.schema.json`](../src/flowspec2/authoring/authoring-evidence.schema.json) | Authoritative evidence-envelope schema. |
| [`../src/flowspec2/authoring/authoring-evidence-signature.schema.json`](../src/flowspec2/authoring/authoring-evidence-signature.schema.json) | Authoritative detached-signature schema. |
| [`../src/flowspec2/compat/rasa_export.py`](../src/flowspec2/compat/rasa_export.py) | FlowSpec2-to-Rasa conversion and loss diagnostics. |
| [`../src/flowspec2/compat/rasa_import.py`](../src/flowspec2/compat/rasa_import.py) | Rasa-to-FlowSpec2 conversion and loss diagnostics. |
| [`AUTHORING_BENCHMARK.md`](AUTHORING_BENCHMARK.md) | Evaluation protocol and real-model evidence requirements. |
| [`FLOWSPEC3_DRAFT.md`](FLOWSPEC3_DRAFT.md) | Non-executable source preview, migration, and loss accounting. |
| [`adr/0002-format-authoring-execution-boundary.md`](adr/0002-format-authoring-execution-boundary.md) | Decision record for the source/link/profile/IR boundary. |
| [`adr/0003-benchmark-author-trust-boundary.md`](adr/0003-benchmark-author-trust-boundary.md) | Decision record for evaluator-private oracles and effective model identity. |
| [`adr/0004-evidence-authenticity.md`](adr/0004-evidence-authenticity.md) | Decision record for detached evidence signatures and external trust roots. |
| [`adr/0012-public-contract-namespace.md`](adr/0012-public-contract-namespace.md) | Decision record for project-owned schema and profile identifiers. |
| [`adr/0013-stable-package-release-boundary.md`](adr/0013-stable-package-release-boundary.md) | Decision record for the stable package and experimental preview boundary. |

The project test suite checks the schema itself, validates both example
documents, and rejects adversarial mutations such as unknown keys, ambiguous
steps, malformed predicates, and invalid versions.

---

<!-- /section:files -->
<!-- section:authoring-execution-pipeline -->

## Authoring-to-execution pipeline

```text
FlowSpec source
    → closed structural validation
    → aggregate semantic linking + runtime profile
    → canonical FlowIR
    → LangGraph compiler
    → FlowRuntime
```

Source is the only human- or AI-authored artifact. Structural validation closes
each step kind to fields the compiler consumes. The semantic pass resolves
domains, slots, gates, predicates, dependencies, derive anchors, corrections,
entry schemas, terminal bindings, subflow declarations, and host capabilities.
It emits stable codes and JSON Pointer locations for every deterministic issue
it can prove in one pass.

Semantic orchestration consumes explicit source-contract groups for path and
slot linking, derived values, state ownership, rail references, and predicates.
Compilation separately composes schema relations, FlowSpec value contracts,
tool lifecycle contracts, and external-resume contracts before graph assembly.
The facade modules preserve stable internal entry points while preventing
directional modules from reaching across private implementation details.

The runtime profile makes “valid” deployment-specific. Its tool definitions
carry version, input/output JSON Schemas, and effect metadata. Its subflow
definitions carry configuration JSON Schema, exposed slots, and required
capabilities. A structurally plausible flow is not executable when its profile
lacks one of those contracts.

`FlowIR` materializes effective defaults and stable node identifiers, indexes
reads/writes/references/transitions, generates the partitioned state schema, and
computes canonical source and normalized digests. Its explicit
`flowspec2/ir@1` identity participates in the execution digest, so a future IR
contract cannot be mistaken for the current representation. JSON Schema
`default` values remain annotations at the source boundary; normalization, not
validation, applies the runtime defaults to a private copy. This keeps concise
source and explicit execution contracts from competing for the same
representation.

The `flowspec/3-draft` preview tests a more local authoring syntax, including
typed references and stable option values separate from labels and aliases. It
does not compile. The provider-neutral authoring benchmark compares adapters on
the same scenario requirements, forbidden constructs, aggregate diagnostics,
correction behavior, source size, and exact profile checks. Only real-model
evidence can justify promotion to a stable source format. The operational
contracts are documented in [AUTHORING_BENCHMARK.md](AUTHORING_BENCHMARK.md) and
[FLOWSPEC3_DRAFT.md](FLOWSPEC3_DRAFT.md).

The reference authoring and CTK cases are package resources pinned by a closed
manifest, rather than test-only fixtures. A live model receives a
source-answer-free task, a complete versioned public acceptance contract, the
normative schema, and the same complete `FlowProfile` contract whose digest
binds IR and restored state. Every graded semantic leaf and source-policy rule
is present in the author-facing object graph; only the private fixture source is
absent. The resulting evidence envelope binds exact captured
sources, the requested model, and any provider-reported effective model version
to every report attempt and records only deterministic, non-secret provenance.
This keeps provider operation at the edge while making later comparison and
offline review independently verifiable. Offline verification matches the installed package,
corpus, profile, adapter, and correction protocol before replaying every exact
capture and requiring complete report equality. Its content digest proves
integrity rather than author identity. Optional detached signing authenticates
that verified digest against a caller-managed Ed25519 trust root without
coupling evidence identity to key rotation.

Candidate route descriptions and non-verbatim prompt prose cross a separate
presentation-review boundary. A deterministic extractor selects only
author-owned text from final successful captures and binds each subject to its
source and evidence identities. Human decisions use a fixed public rubric and
are stored in a content-addressed artifact; fixture wording never becomes a
quality oracle. Detached reviewer authentication and a caller-managed trust
root keep subjective review attributable without changing semantic benchmark
success.

<!-- /section:authoring-execution-pipeline -->
<!-- section:closed-value-domain -->

## The one idea: the boundary is the closed value-domain

> **Deterministic structure, non-deterministic execution.** The JSON pins the rails; the LangGraph agent reasons and acts freely *within* them. The boundary is **positional — there is no `isRail` tag** (an annotation can be mislabelled). It is marked three ways at once:

- **By field** — inside any slot/step, only `prompt.text`, `prompt.extract_hint`, and `route.{description,trigger_phrases}` are LLM-editable. Every sibling (`domain`, `normalize`, `required`, `requires`, `ask_when`, transitions, tool bindings) is a rail.
- **By location** — `path` / `uses` / `derive` / `confirm` / `terminal` / `overrides` / `auto_flow` are rails (states, value-domains, transitions, guards, idempotency, reset). `capabilities` is the explicitly **fenced LLM-freedom zone** for side actions the LLM *may* invoke (media, TTS, handoff, reset), bounded by the Mule outbound whitelist and never wired as states. `capabilities.await_external` is the named deterministic exception: it binds to a path or subflow state.
- **By construct** — the closed `domain` array **is** the boundary. The LLM extracts a free-text/voice/photo candidate (its job); the generated `@field_validator(mode="before")` rejects anything outside `values` (the rail). At each pause the compiler ships `payload_schema = domain.model_json_schema()` to constrained decoding, forcing extraction onto a closed token at exactly one place.

Three impossibilities are therefore **structural, not policed**: the LLM cannot invent a transition (synthesized routers only return declared targets), cannot skip a required ungated slot unless the author explicitly declares an exhaustion transition, and cannot widen a value-domain (the before-validator raises).

What the LLM *is* free to do: choose which flow to enter (`route.description`, with `route.trigger_phrases` as non-exclusive examples), extract the token from free text/voice/photo, phrase prompts (unless `verbatim:true`), and choose/order the non-deterministic `capabilities` side-calls.

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

The `domains` registry is the spine: one declaration materializes the Pydantic validator, the `payload_schema` handed to constrained decoding, and the interactive presentation that must never drift. Categorical domains derive presentation from `values`/`rows`; boolean domains expose canonical `true`/`false` tokens titled “Sim”/“Não”. The recurring production bug (a button tap that doesn't match the recognizer token) becomes structurally impossible. Adding a categorical value is a one-line append. Corrections are **derived**: the author declares only which slots are directly `correctable`; the clear-cascade is computed from each slot's `requires[]` + `derive.from` (no hand-maintained `clears[]` — the stale-state footgun killer). Escape hatches are weak on purpose: `derive` is lookup-table-only with a single `$from[i]` sigil; predicates are a frozen object grammar (`in|eq|ne|is_present|and|or|not`) over five read-only namespaces (`slots.`/`internal.`/`payload.`/`config.`/`address.`) — no string sugar, no arbitrary computation.

---

<!-- /section:top-level-shape -->
<!-- section:mapping-table -->

## Mapping table — every construct → its LangGraph primitive

| flowspec/2 construct | LangGraph / runtime primitive | Notes |
|---|---|---|
| source document | structural validator → semantic linker → named profile → canonical `FlowIR` | compiler input is explicit without making authored JSON verbose. |
| `ToolDefinition` | immutable versioned callable metadata with input/output JSON Schemas, effects, and an explicit legacy marker | the reference profile rejects open legacy contracts; entry, terminal, and external-wait enrichments reject absent, unsafe, or inconsistent contracts. |
| `SubflowDefinition` | immutable configuration schema, exact exposed-slot schemas, owned state keys/partitions, node IDs, required tool versions, and capabilities | declarations, anchors, transitive dependencies, and ownership collisions are linked before a builder can splice nodes; resolved manifests participate in the dependency digest. |
| `flow` (id) + `version` | stable service identity plus document semver; the reference `as_tool()` adapter keys its in-memory state by user id | A production host may replace the reference state store. |
| top-level document | one `StateGraph[ServiceState]` compiled when a `FlowRuntime` is created and reused by that instance | Flow = callable subgraph/tool an outer agent dispatches to. |
| `route.description` / `trigger_phrases` | the LLM router-in: top `create_react_agent` agent node picks `multi_step_service(service_name=…)` | The ONE LLM hook for entry. `entry_args_schema` = closed initial-slot set. |
| `entry.tool` (blocking:false) | no-pause node at `set_entry_point`; `@handle_errors` swallows failure; unconditional edge to first step | generic `initialize`, decoupled from `service` identity. |
| `service{}` | constants seeded into `state.internal`/`data` at compile time | never a node. |
| `domains.<X>` | Pydantic model with `@field_validator(mode="before")`; `model_json_schema()` → `AgentResponse.payload_schema`; shared domain options → interactive presentation | categorical values/rows and canonical boolean options remain aligned with validation. |
| `domains.<X>.values` | the enum shipped to constrained decoding at each pause; `ValueError` on out-of-domain | the rail the LLM cannot widen. |
| `domains.<X>` `integer` / `number` | deterministic scalar coercion with optional inclusive bounds and matching Pydantic/IR JSON Schema | booleans and non-finite numbers are rejected rather than coerced accidentally. |
| `domains.<X>.rows[].description` | `send_whatsapp_list` `sections[].rows[].{title,description}` | two-column list subtitles. |
| `slots.<s>.persist` | shared slot accessors select the `ServiceState` partition used by collection, declared-slot derives, confirmations, corrections, and terminal bindings | `data`=persisted+visible, `internal`=persisted+hidden, `payload`=ephemeral. |
| `slots.<s>` + `path.slot` step | `add_node(step, collect_template)`: early-return-if-persisted-or-skipped → prompt/source ownership guard → optional `null` skip or domain validation → ADVANCE (`agent_response=None`) / RE-ASK (`error_message`, ++attempts) / PAUSE | Values use `persist`; optional no-value completion and prior-prompt ownership use persistent internal markers, cleared or rotated by the runtime. `on_exhaust:default` is domain-validated at compile time. |
| `path` order (implicit fall-through) | synthesized `add_conditional_edges(step, _route_after, {next:next, END:END})`; router → `END` iff `agent_response` set | authors never write routers or `set_entry_point`. |
| `step.ask_when` / `overrides.gates[step]` | guard predicate compiled into the node's leading early-return + path_map; `gate==false` ⇒ slot satisfied-by-vacuity | gated quantidade/intercaladas/quadra. |
| `step.skip_when` | leading early-return that advances without asking | summary skip on `payload._source==whatsapp_flow`. |
| `confirm` step + domain-backed `interactive` | `confirm_template` sets `AgentResponse.interactive` with buttons or a list built from the confirmation domain | the runtime consumes either the declared UI field or canonical slot and rejects conflicting aliases. |
| `interactive` (buttons/list) **path B** | `AgentResponse.interactive` → wrapper → Mule `canonicalToolReturn`; the enclosing path step compiles `interactive.field → slot/confirm` and the node consumes either the UI alias or canonical slot key | categorical domains reuse `values`/`rows`; boolean domains emit `true`/`false` IDs titled “Sim”/“Não”; conflicting aliases fail compilation or produce a correlated runtime error. |
| `interactive kind:cta_url` / `out_of_band:true` | external-wait CTA push whose field and optional next step are linked to the wait contract | gov.br login button; WhatsApp forms belong to top-level `auto_flow`, not this union. |
| `interactive.options_when` | conditional option rendering (e.g. append "Sem me identificar" iff `not identification_required`) | per-option visibility is covered by an executable format fixture. |
| capabilities tools (LLM **path A**) | `send_whatsapp_buttons/list/build_whatsapp_flow_envelope` marked `return_direct=True` → `route_tool_responses` → `END` | the two send paths pinned separately (A return_direct, B interactive_sent). |
| `derive[]` + `derive` step | no-prompt node: canonical JSON components form the lookup key, otherwise the declared literal or `$from[i]` supplies the fallback; `agent_response=None` → advance | booleans and null are `true`/`false`/`null`; a single-source token may contain `|`, while multi-source domains are restricted to keep encoding injective. |
| `uses[].ref` + `use` step | splice mixin subgraph at the anchor; `SubflowBuild.node_for_slot` exposes collectable slots upward | after every splice, the compiler checks path, dependency, derive, correction, and terminal references without an author-maintained exposure echo. |
| `confirm.correctable[]` | the extraction schema exposes the exact canonical slot enum; the confirm node stores the selected ID in private state → `Command(update=cleared, goto=<slot node>)` | fuzzy labels and arbitrary text are invalid; private routing state cannot collide with authored data. |
| clear-cascade (DERIVED from `requires[]`) | `_clear_corrected_field`: pop slot from persistent partitions + transitive `requires[]`-dependents + `derive.from` readers | author declares only the correctable set; current-turn correction payload remains available for validation. |
| `terminal` (idempotent, outcomes) | terminal service-call node → typed `tool` with input mapping; edge → `END` | compiler proves input/output compatibility and the status trichotomy; SHA-256 replay is registry- and process-local, so durable exactly-once requires host storage. |
| `terminal.outcomes.success` | `ticket_opened()`: set `outputs`, `_reset_on_next_call=True` | reset-on-next-call. |
| `terminal.outcomes.retryable` | `ticket_failed(reset_workflow=False)`: preserve state, re-enter | real retry. |
| `terminal.outcomes.fatal` | `ticket_failed(reset_workflow=True)`: `_reset_on_next_call=True` | non-retryable reset. |
| `terminal.empty_payload` | `base_workflow.execute`: never_saved→wipe; in_progress→ignore | empty-payload semantics. |
| graph end, `agent_response is None` | `status='completed'` | terminal/complete. |
| `AgentResponse.error_message` / `log_id` | runtime boundary emits a structured event and propagates its decimal Snowflake identifier through `AgentResponse` and `as_tool()` | injected clock/worker generator; internal warnings use the same event contract. |
| `ServiceMetadata` timestamps | `FlowRuntime` samples its injected UTC clock when state is created or saved | the real UTC clock is a boundary adapter; deterministic tests pass a fake clock. |
| `ServiceMetadata` provenance | flow version plus IR, dependency, and profile digests are persisted and checked before execution; a pending external wait also pins its typed resume contract, correlation, and deadline | restored state with any contract drift fails closed before a node or effect runs. |
| active state migration | exact source/target contracts plus declarative partition copies/defaults/drops | source and target schemas are validated atomically and a canonical loss report binds the transformation; direct restore never migrates. |
| `auto_flow.send_when` | tool-layer ABOVE the graph: true + entry slot empty → send Flow → `{status:flow_sent}` WITHOUT `ainvoke` | pre-graph short-circuit. |
| `auto_flow.prefill_from` | `flow_token = encode_prefill_token(...)` (`v1:base64url(json)`) | Meta `${data.X}` prefill. |
| `auto_flow.alias_map` | `_normalize_payload_aliases` on nfm_reply (value-keyed fan-out) | one Flow field → many slots. |
| `auto_flow.resume_at` | re-injected `multi_step_service(_source=whatsapp_flow)` rediscovers position | also derives summary-skip. |
| `auto_flow.recovery` | persist pending marker, absolute UTC deadline, and resend state; accept only the closed cancel/resend/fallback/timeout event payload | compiler proves resume/fallback prefix safety; resend exhaustion and elapsed deadlines execute the declared timeout action. |
| `capabilities.media_in.analyze` | `register_inbound_media` + `analyze_inbound_image/audio/video`; `workflow_sugerido` → top-agent route | inbound media. |
| `capabilities.media_out` (+ template) | `send_whatsapp_media(type,…)` (LLM may call); template = 24h-window Meta template | bounded by Mule whitelist. |
| `capabilities.tts` | `generate_audio_response` | TTS. |
| `capabilities.location_in` | `message_type==location` → `media.latitude/longitude`; `reverse_geocode_address` | location in. |
| `capabilities.handoff` | LLM emits Central-1746 fallback / handoff | to human. |
| `capabilities.session_reset` | `reset_session_state` / `_reset_on_next_call` | session reset. |
| `capabilities.await_external` | suspend node: emit a typed out-of-band resume contract and absolute timeout deadline, resume on a host-delivered `resume_on` signal, apply bounded token/result mappings atomically, and route host-delivered abort/resend/switch/timeout events | optional `max_resends` persists and exposes the remaining budget; exhausted resend and early timeout events are rejected atomically; omission preserves host-owned resend limiting; recovery ending at `END` resets on the next call. |
| `predicate` object grammar | pure boolean fn over `ServiceState` (frozen namespaces) compiled into routers/early-returns | no arbitrary Python; verifiable/diffable. |
| `GeminiAgent` route/extract call | provider response JSON Schema derived from the active catalog or payload schema, followed by local Draft validation | invalid provider JSON never reaches the rail, and constrained decoding does not replace local validation. |

---

<!-- /section:mapping-table -->
<!-- section:rationale -->

## Rationale (1 page)

The design rests on one decision: the deterministic/non-deterministic boundary is positional and falls on the closed value-domain. Everything else follows.

**The boundary, precisely.** The JSON pins the rails: the set of states (`path` steps + spliced `uses` nodes + synthesized derive/terminal nodes), the entry point (`entry` then the first `path` step), the required slots and their closed value-domains (`domains` + `slots`), the normalization rules (`domains.*.normalize`, deterministically enforced even though authored as hints), the allowed transitions and guards (linear fall-through + `ask_when`/`overrides.gates` + `confirm.correctable` back-edges + registered subflow routes + typed `await_external` recovery, all over closed contracts), which tool each state calls (`entry.tool`, `terminal.tool`, subflow bindings), where the flow pauses (the `agent_response is not None` convention, synthesized per step kind — never authored), and the idempotency/reset/guardrail behavior (`terminal.outcomes` trichotomy, `empty_payload`, `config` booleans, `slots.*.persist/max_attempts/on_exhaust/fill_only_when_asked`). It leaves the LLM free for exactly: which flow to enter, extracting the closed token from free text/voice/photo, phrasing, and choosing/ordering the `capabilities`.

**Why two tiers.** Most service conversations are a linear sequence of slot
collections; branches, gates, back-edges, and derivations are the exceptional
part. Forcing explicit `transitions[]` on every node adds graph mechanics without
adding useful conversational meaning and makes diffs harder to review. The flat
`path` keeps the common case readable, while derived values, multi-slot gates,
non-linear corrections, registered subflow routes, and typed external recovery
live in bounded escape hatches. The compiled graph may therefore be cyclic even
though the source has no general loop construct; dependency graphs remain
acyclic and `END` may be a turn boundary rather than conversational completion.

**Why the domain registry is the spine.** Declaring a domain materializes validation, constrained-decoding schema, and interactive presentation from one contract; the recurring "button tap ≠ recognizer token" bug becomes structurally impossible. A slot's `required` flag is the sole requirement source for native fields such as `ponto_referencia`; subflow configuration only governs subflow-owned fields. **Why corrections are derived.** A hand-declared `clears[]` is a second dependency list duplicating `requires[]`/`derive.from`, reintroducing the stale-state footgun. **Why the escape hatches are weak on purpose.** `derive` is a lookup table + one `$from[i]` sigil (exactly `_classifica_defeito`, nothing more); the predicate grammar is a frozen object form over five namespaces (no string sugar → always compilable). Provider plumbing (flow_token base64, sha256 recipe, Mule whitelist, JWT/SCRT, Object Store TTLs) stays in the compiler/library; the author declares `idempotent:true` and `media_out:["location"]` and the compiler cross-checks the whitelist. The luminária instance proves both sufficiency (every grounding feature) and minimality (no field unused by it).

<!-- section:rationale-rejected-alternatives -->

### Rejected alternatives
1. **Explicit `transitions[]` per state (XState/SCXML/ASL-faithful).** Adds repeated graph syntax without useful conversational power, plus string-sugar guards that are not deterministically compilable. flowspec/2 keeps the direct LangGraph mapping but **synthesizes** edges from the flat path + dependency DAG, and freezes the predicate grammar to the object form.
2. **Hand-declared `clears[]` correction cascade.** A second dependency list duplicating `requires[]`/`derive.from` → stale-state footgun. flowspec/2 **derives** the cascade.
3. **A general expression/derive language (`$`/`=field`/named functions; Power Fx / DF CX `$sys.func.*`).** Unverifiable, undiffable, re-opens the "and then a dev writes Python" hole. Capped at a lookup table + one sigil; predicates frozen.
4. **Pure contract/DAG for *every* flow (LLM plans collection order even for linear cases).** Elegant but makes the simple case read as a contract, not a sequence — hurting authorability. flowspec/2 keeps the flat `path` for the common case and grafts DAG-derived correctness only into the hard-case tier.
5. **Model on `langgraph.json` / persist a `current_step` pointer.** `langgraph.json` points at hand-authored Python and describes no graph structure. A persisted pointer is a second source of truth that conflicts with correction back-edges, reset, and Flow prefill — the verified system rediscovers position from filled slots, which flowspec/2 preserves.
6. **Native `interrupt()` + `Command(resume=)` + a flow-level checkpointer.** The reference runtime keeps state outside the compiled graph and reuses one compiled graph per runtime instance; pause is the `agent_response`-is-set convention. Designing around `interrupt()` would introduce a second persistence model.
7. **Per-field `isRail:true` annotations.** An annotation can be mislabelled and adds noise. The boundary is made positional and self-documenting instead.
8. **gov.br / WhatsApp Flow as ordinary collect slots; media/TTS/handoff as path states.** Both suspend on an *external* signal (OAuth token / nfm_reply), not the next text turn — modeling them as collect slots waits on the wrong channel. Media/TTS/handoff are non-deterministic LLM side-calls bounded by the whitelist; forcing them into the state machine over-constrains the LLM. flowspec/2 makes gov.br a `await_external` capability, `auto_flow` a pre-graph guard, and keeps media/TTS/handoff as `capabilities`.

---

<!-- /section:rationale-rejected-alternatives -->
<!-- /section:rationale -->
<!-- section:production-method -->

## How this was produced

The format combines a prior-art sweep, a code-grounded runtime feature
inventory, competing design approaches, adversarial review, schema mutations,
and executable scenario verification. The current authoring work adds an
aggregate linker, typed runtime profiles, canonical IR, and a provider-neutral
benchmark so future syntax decisions can be based on measured task completion
and correction behavior instead of source aesthetics alone.

<!-- /section:production-method -->
