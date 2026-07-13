<!-- section:toc -->

Table of Contents:

- Top level: 42 <!-- section:top-level -->
- Validation, normalization, and runtime profiles: 67 <!-- section:validation-normalization-profile -->
- `route` (LLM): 106 <!-- section:route -->
- `domains.<X>` (RAIL — the spine): 116 <!-- section:domains -->
- `slots.<s>` (RAIL): 144 <!-- section:slots -->
- `path[]` steps (RAIL): 171 <!-- section:path-steps -->
- `uses[]` subflows (RAIL): 195 <!-- section:subflows -->
- `interactive` (RAIL): 245 <!-- section:interactive -->
- `predicate` grammar (RAIL): 270 <!-- section:predicate-grammar -->
- `derive[]` (RAIL): 280 <!-- section:derive -->
- `confirm` (RAIL — correction hub): 299 <!-- section:confirm -->
- `terminal` (RAIL): 315 <!-- section:terminal -->
- `auto_flow` (RAIL): 341 <!-- section:auto-flow -->
- `capabilities` (LLM — fenced): 362 <!-- section:capabilities -->
- Runtime response contract: 391 <!-- section:runtime-response-contract -->
- AI authoring benchmark: 422 <!-- section:ai-authoring-benchmark -->

<!-- /section:toc -->

# flowspec/2 — field reference

This is the field-by-field reference for the format. The authoritative machine
contract is [`../src/flowspec2/flowspec-2.schema.json`](../src/flowspec2/flowspec-2.schema.json)
(JSON Schema Draft 2020-12); the design rationale + construct→LangGraph mapping
is in [`DESIGN.md`](DESIGN.md). Every field is labelled **RAIL** (deterministic
structure, pinned) or **LLM** (non-deterministic execution, the agent's
discretion). The boundary is positional — there is no `isRail` tag.

Source conformance is layered: the JSON Schema proves local shape; semantic
linking proves cross-references and dependency invariants; a named runtime
profile proves that tools, subflows, domain kinds, and host capabilities are
installed; compilation proves that the linked document constructs an executable
graph. `flowspec2 check --json` is the aggregate machine interface for those
layers.

<!-- section:top-level -->

## Top level

| Key | Req | Rail/LLM | Meaning |
|---|---|---|---|
| `schema` | ✓ | RAIL | const `"flowspec/2"`. |
| `flow` | ✓ | RAIL | stable id == `service_name`; host/runtime registry key. `^[a-z][a-z0-9_]*$`. |
| `version` | ✓ | RAIL | semver of this document (`x.y.z`). |
| `service` | | RAIL | service-identity metadata (SGRC/1746 ids + `slugs`); seeded into state, never a node. `service.slugs` lists the catalog slug(s) this flow serves so a host can auto-expose the flow by scanning JSONs — add a flow by dropping its JSON, no host code change. |
| `route` | ✓ | LLM | the entry hook the outer agent uses to pick this flow. |
| `config` | | RAIL | shared guardrails (`address_required`, `identification_required`, `max_attempts`). Slot-specific requirements belong to `slots.<s>.required`. |
| `entry` | | RAIL | best-effort, non-blocking init tool (e.g. knowledge load). |
| `domains` | ✓ | RAIL | the closed value-domain registry (the spine). |
| `slots` | | RAIL | slot → domain bindings + collection guardrails. |
| `path` | ✓ | RAIL | the flat ordered happy path. |
| `uses` | | RAIL | versioned subflow refs (`name@major`) + `with{}` config. |
| `derive` | | RAIL | computed values (lookup table only). |
| `confirm` | | RAIL | the confirmation + correction hub. |
| `terminal` | | RAIL | the single fulfillment action + lifecycle. |
| `overrides.gates` | | RAIL | multi-slot gate predicates keyed by step id. |
| `auto_flow` | | RAIL | pre-graph WhatsApp Flow short-circuit. |
| `capabilities` | | LLM/RAIL | fenced LLM side actions plus deterministic `await_external`. |

<!-- /section:top-level -->
<!-- section:validation-normalization-profile -->

## Validation, normalization, and runtime profiles

- Structural diagnostics aggregate every Draft 2020-12 violation. Each `path`
  kind is a closed object union and may contain only fields consumed by that
  kind; the terminal and external-wait markers are the constant `true`.
- Semantic diagnostics link step IDs, domains, native and exposed slots,
  subflow declarations, gates, predicate partitions, derive dependencies,
  correction targets, terminal bindings, embedded entry schemas, and
  capabilities. Findings have stable codes and RFC 6901 JSON Pointer paths.
- A `FlowProfile` names the executable catalog. `ToolDefinition` supplies a
  versioned input schema, output schema, description, and effect contract.
  `SubflowDefinition` supplies a versioned configuration schema, exact exposed
  slot schemas, owned state keys and partitions, node identifiers, required
  tools, and required capabilities. The reference profile rejects legacy open
  tool and subflow contracts; compatibility profiles must opt into them
  explicitly. Unknown or incomplete contracts are errors, not late runtime
  lookups.
- `normalize_flow` applies effective defaults and stable native node IDs to a
  private copy. JSON Schema validation alone never applies `default`
  annotations.
- `build_flow_ir` adds immutable slot/node/reference/transition contracts,
  read/write sets, a generated partitioned state schema, required capabilities,
  resolved dependency contracts, and canonical source, normalized, profile,
  dependency, and IR SHA-256 digests. The IR carries the explicit
  `flowspec2/ir@1` identity, which participates in its execution digest.
  `to_document()` always returns a fresh compiler input.

Embedded tool, subflow, entry, and resume schemas use Draft 2020-12 with format
assertion. Only safe local references are accepted; unresolved, cyclic, remote,
dynamic, nested-resource, and provably impossible object contracts are rejected
before execution.

The stable authoring contract is still `flowspec/2`; authors never edit IR.
`flowspec/3-draft` is an isolated source experiment and is not accepted by
`FlowRuntime`.

<!-- /section:validation-normalization-profile -->
<!-- section:route -->

## `route` (LLM)
- `description` (✓) — the routing hook the LLM reads to decide to enter the flow.
- `trigger_phrases` — a versionable seed, not the sole signal.
- `entry_args_schema` — a valid Draft 2020-12 object schema with
  `additionalProperties:false`; every property must resolve to a declared or
  profile-exposed slot.

<!-- /section:route -->
<!-- section:domains -->

## `domains.<X>` (RAIL — the spine)
Declaring a domain materializes its Pydantic before-validator, constrained-decoding
`payload_schema`, button titles, and list rows from one contract.
- `type` — `categorical` (default) | `bool` | `free_text` | `cpf` | `email` |
  `name` | `integer` | `number`.
- Each type is a closed object branch. Fields that its validator would not
  consume are structural errors rather than ignored annotations.
- `values` — closed token set (categorical). `null` is an allowed member. Order = elicitation/button order.
- `rows[]` — `{value, description}` subtitles for two-column lists.
- A `bool` domain exposes ordered interactive tokens `true` and `false`, with
  citizen-facing titles “Sim” and “Não”; button/list IDs remain the lowercase
  canonical tokens accepted by the validator.
- Categorical `normalize` supports `accent_fold`, positional `number_words`, and
  `synonyms{}` from free text to a canonical token.
- Boolean `normalize` supports `accent_fold`, `affirmation`, `emoji_veto`, and
  boolean-valued `synonyms{}`. Emoji veto requires affirmation parsing.
- `optional` belongs only to `free_text` and accepts an empty string.
- `minimum`, `maximum` — inclusive bounds for `integer` and `number`. Integers
  accept only exact integral input. Numbers reject booleans and non-finite
  values. An inverted range is a semantic error.
- `accent_fold:false` preserves accents in canonical values, aliases, and input.
  `emoji_veto:true` makes a negative emoji authoritative for boolean parsing;
  when false, recognized words take precedence in mixed text while isolated
  emojis remain supported.

<!-- /section:domains -->
<!-- section:slots -->

## `slots.<s>` (RAIL)
- `domain` (✓) — references `domains{}`.
- `persist` — selects the slot's runtime partition: `data` (persisted and
  agent-visible, default), `internal` (persisted and hidden), or `payload`
  (current graph invocation only). Native collection, declared-slot derives,
  confirmations, correction cascades, and terminal input bindings resolve the
  same partition.
- `required` — when false, the payload schema advertises `null` as an explicit
  skip and the runtime advances without storing a value. When true, the
  collector requires a domain-valid value unless `nullable` is also true.
- `nullable` — makes `null` a domain-valid stored value; it is distinct from an
  optional skip, which stores no slot value.
- `requires[]` — precedence + dependency edges; the correction clear-cascade is **derived** from these.
- `prefill_sources[]` — channels allowed to bypass `fill_only_when_asked`
  (e.g. `whatsapp_flow`).
- `max_attempts`, `on_exhaust` (`reask`|`skip`|`default`|`handoff`|`END`),
  `default`. `skip` stores no value. `default` is required for the matching
  exhaustion mode and is validated and normalized against the slot domain when
  the graph compiles; `null` is valid only for a nullable slot.
- `fill_only_when_asked` — accepts the slot only when the preceding pause asked
  for that same slot or `_source` belongs to `prefill_sources[]`; values bundled
  into a turn answering another slot are ignored and the sensitive slot is
  asked explicitly.

<!-- /section:slots -->
<!-- section:path-steps -->

## `path[]` steps (RAIL)
A step is exactly one closed `oneOf` branch. Fields from another branch are a
structural error rather than ignored metadata:

- Collection — `slot` (required), optional stable `step`, `prompt`,
  `interactive`, `ask_when`, and `skip_when`.
- Confirmation — `confirm` (required), optional stable `step`, `prompt`,
  `interactive`, `ask_when`, `skip_when`, `on_reject.end`, and
  `correctable:true` for the correction hub.
- Derivation — `derive` (required) references a top-level derive target;
  optional `step` overrides its generated node ID.
- Terminal — exactly `{\"terminal\": true}` and binds the top-level terminal
  contract at that position.
- Subflow — exactly `{\"use\": \"name@major\"}` and binds the matching
  declaration in `uses[]`.
- External wait — `await_external:true` (required), optional stable `step`,
  `prompt`, and `interactive`; it binds the singular external-wait capability.

`prompt` is `{text, verbatim?, extract_hint?}`. `text` and `extract_hint` are
**LLM** fields; the surrounding behavior remains RAIL.

<!-- /section:path-steps -->
<!-- section:subflows -->

## `uses[]` subflows (RAIL)

`uses[].with` configures a versioned subflow at its matching `path[].use`
anchor. `address@1` resolves `required` from `config.address_required` when the
local value is omitted, applies `max_attempts`, and dispatches `on_exhaust` as
follows:

- `reask` resets the attempt budget and asks again.
- `skip` advances without storing `address`.
- `default` advances with an explicit `address: null`.
- `handoff` pauses with the Central 1746 handoff response.
- `END` completes with a correlated warning.

An optional address accepts an explicit `null` payload as a deterministic skip;
a required address counts the same payload as a failed attempt.
`needs_confirmation` still controls whether a successfully geocoded address
passes through the confirmation node.

`identification@2` applies the same exhaustion policy to method selection and
the CPF, e-mail, and name collectors. `reask` resets the current stage's
attempt budget; `skip` resolves identification/CPF exhaustion to anonymous and
omits an exhausted optional contact field; `default` chooses the first eligible
configured method during method selection, then uses anonymous/no-contact
fallbacks for value collection; `handoff` pauses for Central 1746; `END`
completes with a correlated warning. `required` controls voluntary refusal,
while the explicitly configured exhaustion route remains authoritative.

Requirements for ordinary fields, including `ponto_referencia`, have one
source of truth: their native `slots.<s>.required` declaration. Subflow
configuration controls only fields owned by that subflow.

Each subflow returns its exposed `slot → collector node` mapping to the
compiler. After every `use` is expanded, the compiler validates native path
slots, `requires[]`, derive sources, `confirm.correctable[]`, and terminal input
bindings against declared, derived, or subflow-exposed slots. An unresolved
reference fails compilation with its JSON location.

Every registered subflow also exposes an immutable `SubflowDefinition` manifest:
the exact `name@major` reference, a closed configuration JSON Schema, exact
schemas for exposed slots, every owned persistent state key and partition,
stable node identifiers, required tool versions, and runtime capabilities. A
use declaration must be unique, must have a matching path anchor, must not
collide with another state owner, and must satisfy that manifest before the
subflow builder runs. These resolved contracts participate in the dependency
digest. A legacy manifest is executable only under a profile that explicitly
permits legacy contracts.

<!-- /section:subflows -->
<!-- section:interactive -->

## `interactive` (RAIL)
- `kind` (✓) — `buttons` | `list` | `cta_url`. Collection and confirmation
  steps accept only the domain-backed choice variants; `cta_url` is reserved
  for an external wait and requires `out_of_band:true`.
- `field` (✓) — the payload key emitted by the UI. The enclosing path `slot` or
  `confirm` is its state target, so names may differ (e.g. `confirmacao` →
  `ticket_data_confirmed`). Native collectors accept either the interactive
  field or canonical slot key and reject conflicting values when both are
  present. One field cannot bind different state slots in the same document.
- `from_domain` — domain whose categorical `values`/`rows` or canonical boolean
  options materialize the UI; for a path collection or confirmation it must
  match the target slot's domain.
- `options_when[]` — per-option conditional visibility (`{value, gate}`),
  evaluated by the compiled node before it emits buttons or list rows. Each
  configured categorical or boolean value must be unique and belong to
  `from_domain`.
- `next_step` — optional exact wait-step identity carried by an out-of-band CTA.
  It must equal the bound external-wait step.

WhatsApp Flow forms are declared only through top-level `auto_flow`; they are
not an interactive variant and cannot inherit choice-only fields.

<!-- /section:interactive -->
<!-- section:predicate-grammar -->

## `predicate` grammar (RAIL)
Single-key object; namespaces `slots.` / `internal.` / `payload.` / `config.` / `address.`.
`{"in":[ref,[...]]}`, `{"eq":[a,b]}`, `{"ne":[a,b]}`, `{"is_present":ref}`, `{"and":[...]}`, `{"or":[...]}`, `{"not":<pred>}`.
Predicate namespaces are literal state partitions: `slots.` addresses public
`data`, while a slot declared with `persist:internal` is addressed through
`internal.` and a `persist:payload` slot through `payload.`.

<!-- /section:predicate-grammar -->
<!-- section:derive -->

## `derive[]` (RAIL)
- `writes` (✓), `from[]` (✓, ordered source slots), `lookup` (✓, canonical
  source tuple → value), `after` (step id to run after), `default` (literal or
  the single `$from[i]` sigil).
- Every source and anchor must resolve, lookup keys must match the source arity,
  and `$from[i]` must address an existing source. Duplicate writes, write/source
  collisions, and dependency cycles are semantic errors.
- Canonical components use JSON spellings for booleans and null: `true`,
  `false`, and `null`. Multiple sources are joined with `|` and therefore must
  be closed categorical or boolean domains whose authored tokens do not contain
  that delimiter. A single-source key remains the complete token, including an
  authored `|`.
- Derives that share an anchor preserve declaration order. A later derive that
  reads an earlier derived value is inserted after its producer, so source order
  remains the deterministic tie-breaker.

<!-- /section:derive -->
<!-- section:confirm -->

## `confirm` (RAIL — correction hub)
- `step`, `slot` (bool), `on_confirm` (target on Sim), `prompt`, `interactive`.
- `correctable[]` (✓) — the directly-correctable canonical slot identifiers.
  The response schema exposes this exact enum; the LLM returns one identifier,
  and fuzzy labels or arbitrary text are rejected. The runtime stores the
  request in private state, routes to the exact collector, and clears that slot
  plus transitive dependents.
- The confirmation slot must resolve to a declared `bool` domain. Its
  `interactive.from_domain`, when present, must name that same domain.
- The matching correctable path step is authoritative for `prompt` and
  `interactive`. The top-level block may retain them for compatibility, but a
  duplicate must be identical or compilation fails.

<!-- /section:confirm -->
<!-- section:terminal -->

## `terminal` (RAIL)
- `step`, `tool` (✓), `idempotent` (registry-local SHA-256 replay), `input[]`
  (`{param, slot}`), `outputs{}` (`statekey` ← `result.path`).
- `outcomes` (✓) — `success` (`set`, `reset_next`) | `retryable` (`preserve_state`) | `fatal` (`reset_next`).
- Each `input[].slot` must resolve after subflow expansion to a declared,
  derived, or subflow-exposed state value.
- `empty_payload` — `never_saved` (reset|ignore) / `in_progress` (reset|ignore).
- `tool` must resolve through the selected runtime profile to an immutable
  `ToolDefinition`. The compiler validates input parameter names against its
  input JSON Schema, proves compatible source schemas, and rejects missing
  required or duplicate bindings. It proves output paths exist and are required
  in every success-compatible branch, and requires the tool status contract to
  be a non-vacuous subset of `success`, `retryable`, and `fatal`. Runtime results
  are validated against the output schema before mapped writes commit.
- `retryable.preserve_state:true` leaves state ready to retry the terminal on
  the next invocation; `false` clears the flow state. `success.reset_next` and
  `fatal.reset_next` govern whether the completed state is cleared on the next
  call. `empty_payload` handles the no-payload cases before any tool side effect.
- Local replay coalesces concurrent equivalent calls and caches successful
  results only within the current `ToolRegistry`. Cross-process or durable
  exactly-once behavior requires a host replay store; `idempotent:true` alone
  never claims it.

<!-- /section:terminal -->
<!-- section:auto-flow -->

## `auto_flow` (RAIL)
- `meta_flow_ref` (✓), `send_when` (✓, predicate), `prefill_from[]`, `resume_at`,
  `on_submit_source` (`whatsapp_flow`), `alias_map` (one Flow field → many slots;
  direct `{slot:"$value"}` or value-keyed `{flow_value:{slot:val}}`), and required
  `recovery`.
- On submission, aliases are resolved atomically and the payload source is set
  before graph invocation. `resume_at` selects the declared node for that
  invocation without persisting a mutable program counter; later invocations
  rediscover position from state as usual.
- `recovery` declares a compiler-validated `fallback_at`, actions for cancellation
  and timeout (`END` or `fallback`), a resend budget, and a timeout duration.
  Sending the form persists an absolute UTC deadline and the remaining recovery
  contract. The host receives the closed event schema and deadline; the runtime
  also applies timeout deterministically on the next invocation when the
  deadline has passed. Resend exhaustion follows the timeout action. Recovery
  events reject unrelated payload fields, and fallback cannot skip an
  unsatisfied required prefix.

<!-- /section:auto-flow -->
<!-- section:capabilities -->

## `capabilities` (LLM — fenced)
- `media_in` (`analyze[]`, `route_by`), `media_out[]` (types + 24h-window
  `template`), `tts`, `location_in`, `handoff` (`target`, `when`), and
  `session_reset` remain optional LLM side actions.
- `await_external` is the deterministic exception inside this fenced block. It
  declares `kind`, optional `step`, `resume_on`, fallback `prompt`/`interactive`,
  `on_resume`, `timeout`, optional `timeout_seconds`, and
  `recovery.{abort,resend,switch}`. Normalization materializes the schema-owned
  timeout default only when a timeout transition exists.
- `on_resume.set` maps JSON scalar literals or `$token.*` references into state.
  Object-form `on_resume.enrich` declares a tool, literal/`$token.*` inputs, and
  literal/`$result.*` output mappings. All resolved writes commit atomically.
- The enrichment tool must exist in the selected profile. Its declared input
  and output schemas are checked at the same boundaries as entry and terminal
  tools; unknown parameter or result paths are rejected before execution.
- A missing referenced path produces no write; a literal `null` writes `null`.
  Required enrichment failure commits none of the pending token/result writes.
  Optional enrichment failure is logged and commits the resolved token writes.
- A host resumes recovery with `_external_event` set to `abort`, `resend`,
  `switch`, or `timeout`. The runtime schedules no timeout itself. When timeout
  is configured, the out-of-band resume marker and persisted resume provenance
  carry the absolute UTC deadline; a timeout event received before it is
  rejected without applying transition writes.
- `resend.goto` must be the bound wait step. A recovery or timeout targeting
  `END` marks the flow completed and resets its state on the next call.

<!-- /section:capabilities -->
<!-- section:runtime-response-contract -->

## Runtime response contract

`AgentResponse` is the host-facing runtime contract, not a serialized flowspec
section. It contains `service_name`, `description`, optional `payload_schema`,
`data`, optional `interactive`, optional `error_message`, and optional `log_id`.
When a response represents a warning or error, `log_id` is a decimal Snowflake
identifier attached to the matching structured log record. The callable returned
by `FlowRuntime.as_tool()` propagates `error_message` and `log_id` when present.
`FlowRuntime(clock=...)` injects the UTC clock used to create and touch
`ServiceMetadata`; omitting it selects the real UTC boundary adapter. Persisted
metadata binds the state to the flow version plus IR, dependency, and profile
digests. Restore rejects any mismatch before a graph node or external effect can
run. External-wait metadata additionally pins the resume version, schema digest,
correlation path and accepted value, and timeout deadline. Lifecycle reset
clears this provenance before a later wait can publish a fresh contract.

`migrate_service_state()` is the only active-state upgrade boundary. Its
declarative plan pins exact source and target IR/state-schema contracts and may
copy, default, or drop whole partition members. It validates source state,
proves supported schema compatibility, validates the target atomically, and
returns a canonical loss report bound to the plan and both states. Ordinary
runtime restore never performs or infers migration.

Gemini requests use provider-native `response_json_schema` generated from the
same route and extraction contracts. Provider output is validated locally with
Draft 2020-12 before any route or slot value is accepted, so constrained
decoding is an optimization and not the trust boundary.

<!-- /section:runtime-response-contract -->
<!-- section:ai-authoring-benchmark -->

## AI authoring benchmark

The provider-neutral authoring harness evaluates a source adapter with the same
scenario corpus, runtime profile, and deterministic checker. An injected author
receives an oracle-free task projection, canonical complete profile contract,
prior source, and aggregate diagnostics; the evaluator-private case containing
the reference flow and assertions is not reachable through the request object
graph. A bounded correction protocol records every attempt without hiding
invalid source behind an adapter.

Each case combines expected validity with positive required constructs and
forbidden constructs such as arbitrary expressions, scripts, manual graph
transitions, loops, or parallel branches. This prevents a trivial valid flow
from satisfying a non-trivial scenario. Reports include compact UTF-8 source
size, a declared token proxy, source digest, diagnostic codes, correction
outcome, and profile identifier. The installed reference corpus has a closed
manifest with case integrity digests. A live provider run records the report,
exact source captures, corpus/profile identities, provider configuration,
prompt identity, package version, and repository revision in one canonical
content-addressed evidence envelope. Every Gemini capture also records the
provider-reported effective model version rather than treating the requested
model alias as the executed identity. Network use requires an explicit CLI
opt-in; a configured credential alone is never consent.

The included corpus is a deterministic infrastructure baseline, not evidence
about model quality. A format revision may become stable only after controlled
real-model runs show an authoring improvement without semantic loss or weaker
runtime guarantees. Until then, `flowspec/3-draft` remains report-only and has
no compiler or runtime registration.

<!-- /section:ai-authoring-benchmark -->
