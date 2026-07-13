<!-- section:toc -->

Table of Contents:

- Top level: 33 <!-- section:top-level -->
- `route` (LLM): 58 <!-- section:route -->
- `domains.<X>` (RAIL — the spine): 66 <!-- section:domains -->
- `slots.<s>` (RAIL): 77 <!-- section:slots -->
- `path[]` steps (RAIL): 89 <!-- section:path-steps -->
- `uses[]` subflows (RAIL): 104 <!-- section:subflows -->
- `interactive` (RAIL): 125 <!-- section:interactive -->
- `predicate` grammar (RAIL): 137 <!-- section:predicate-grammar -->
- `derive[]` (RAIL): 144 <!-- section:derive -->
- `confirm` (RAIL — correction hub): 150 <!-- section:confirm -->
- `terminal` (RAIL): 157 <!-- section:terminal -->
- `auto_flow` (RAIL): 165 <!-- section:auto-flow -->
- `capabilities` (LLM — fenced): 171 <!-- section:capabilities -->
- Runtime response contract: 193 <!-- section:runtime-response-contract -->

<!-- /section:toc -->

# flowspec/2 — field reference

This is the field-by-field reference for the format. The authoritative machine
contract is [`../src/flowspec2/flowspec-2.schema.json`](../src/flowspec2/flowspec-2.schema.json)
(JSON Schema Draft 2020-12); the design rationale + construct→LangGraph mapping
is in [`DESIGN.md`](DESIGN.md). Every field is labelled **RAIL** (deterministic
structure, pinned) or **LLM** (non-deterministic execution, the agent's
discretion). The boundary is positional — there is no `isRail` tag.

<!-- section:top-level -->

## Top level

| Key | Req | Rail/LLM | Meaning |
|---|---|---|---|
| `schema` | ✓ | RAIL | const `"flowspec/2"`. |
| `flow` | ✓ | RAIL | stable id == `service_name`; host/runtime registry key. `^[a-z][a-z0-9_]*$`. |
| `version` | ✓ | RAIL | semver of this document (`x.y.z`). |
| `service` | | RAIL | service-identity metadata (SGRC/1746 ids + `slugs`); seeded into state, never a node. `service.slugs` lists the catalog slug(s) this flow serves so a host can auto-expose the flow by scanning JSONs — add a flow by dropping its JSON, no host code change. |
| `route` | ✓ | LLM | the entry hook the outer agent uses to pick this flow. |
| `config` | | RAIL | guardrail booleans (`address_required`, `reference_point_required`, `identification_required`, `max_attempts`). |
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
<!-- section:route -->

## `route` (LLM)
- `description` (✓) — the routing hook the LLM reads to decide to enter the flow.
- `trigger_phrases` — a versionable seed, not the sole signal.
- `entry_args_schema` — the closed set of initial slots the LLM may pass on entry.

<!-- /section:route -->
<!-- section:domains -->

## `domains.<X>` (RAIL — the spine)
Declaring a domain once materializes four artifacts: a Pydantic before-validator,
the `payload_schema` enum (constrained decoding), button titles, and list rows.
- `type` — `categorical` (default) | `bool` | `free_text` | `cpf` | `email` | `name`.
- `values` — closed token set (categorical). `null` is an allowed member. Order = elicitation/button order.
- `rows[]` — `{value, description}` subtitles for two-column lists.
- `normalize` — `accent_fold`, `number_words`, `affirmation`, `emoji_veto`, `synonyms{}` (free-text → token, **enforced**, the only LLM-facing hint).

<!-- /section:domains -->
<!-- section:slots -->

## `slots.<s>` (RAIL)
- `domain` (✓) — references `domains{}`.
- `persist` — `data` (persist+visible, default) | `internal` (persist+hidden) | `payload` (ephemeral).
- `required`, `nullable`.
- `requires[]` — precedence + dependency edges; the correction clear-cascade is **derived** from these.
- `prefill_sources[]` — channels that may pre-fill (e.g. `whatsapp_flow`).
- `max_attempts`, `on_exhaust` (`reask`|`skip`|`default`|`handoff`|`END`), `default`.
- `fill_only_when_asked` — sensitive-slot guard.

<!-- /section:slots -->
<!-- section:path-steps -->

## `path[]` steps (RAIL)
A step is exactly one of (`oneOf`): `slot` | `confirm` | `derive` | `terminal` | `use` | `await_external`.
- `step` — stable node id (defaults to the slot/construct name).
- `prompt` — `{text, verbatim?, extract_hint?}`. `text`/`extract_hint` are **LLM**; everything else is RAIL.
- `interactive` — buttons/list/flow/cta UI model (see below).
- `ask_when` / `skip_when` — inline gate / skip predicates.
- `on_reject.end` — for confirm/summary: end with a message on decline.
- `correctable: true` — marks the confirm step as the correction hub (binds `confirm{}`).
- `await_external: true` — inserts the capability-bound suspension at this
  stable step id; when a subflow supplies the node, `capabilities.await_external.step`
  binds to that same id.

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

<!-- /section:subflows -->
<!-- section:interactive -->

## `interactive` (RAIL)
- `kind` (✓) — `buttons` | `list` | `flow` | `cta_url`.
- `field` (✓) — the payload key the tap fills (binds affordance to slot; **note:** may differ from the slot name, e.g. `confirmacao`).
- `from_domain` — domain whose `values` (and `rows`) materialize the options.
- `options_when[]` — per-option conditional visibility (`{value, gate}`).
- `out_of_band`, `next_step` — push the send direct to Meta (CTA login).
- `gate` — env flag enabling the path-B send; falls back to `prompt.text` when off.
- `meta_flow_ref`, `prefill_from` — for `kind:flow`.

<!-- /section:interactive -->
<!-- section:predicate-grammar -->

## `predicate` grammar (RAIL)
Single-key object; namespaces `slots.` / `internal.` / `payload.` / `config.` / `address.`.
`{"in":[ref,[...]]}`, `{"eq":[a,b]}`, `{"ne":[a,b]}`, `{"is_present":ref}`, `{"and":[...]}`, `{"or":[...]}`, `{"not":<pred>}`.

<!-- /section:predicate-grammar -->
<!-- section:derive -->

## `derive[]` (RAIL)
- `writes` (✓), `from[]` (✓, ordered source slots), `lookup` (✓, `"|".join(values)` → value), `after` (step id to run after), `default` (literal or the single `$from[i]` sigil).

<!-- /section:derive -->
<!-- section:confirm -->

## `confirm` (RAIL — correction hub)
- `step`, `slot` (bool), `on_confirm` (target on Sim), `prompt`, `interactive`.
- `correctable[]` (✓) — the directly-correctable slots; the LLM names which one, the compiler routes back and clears it + transitive dependents.

<!-- /section:confirm -->
<!-- section:terminal -->

## `terminal` (RAIL)
- `step`, `tool` (✓), `idempotent` (sha256 replay), `input[]` (`{param, slot}`), `outputs{}` (`statekey` ← `result.path`).
- `outcomes` (✓) — `success` (`set`, `reset_next`) | `retryable` (`preserve_state`) | `fatal` (`reset_next`).
- `empty_payload` — `never_saved` (reset|ignore) / `in_progress` (reset|ignore).

<!-- /section:terminal -->
<!-- section:auto-flow -->

## `auto_flow` (RAIL)
- `meta_flow_ref` (✓), `send_when` (✓, predicate), `prefill_from[]`, `resume_at`, `on_submit_source` (`whatsapp_flow`), `alias_map` (one Flow field → many slots; direct `{slot:"$value"}` or value-keyed `{flow_value:{slot:val}}`).

<!-- /section:auto-flow -->
<!-- section:capabilities -->

## `capabilities` (LLM — fenced)
- `media_in` (`analyze[]`, `route_by`), `media_out[]` (types + 24h-window
  `template`), `tts`, `location_in`, `handoff` (`target`, `when`), and
  `session_reset` remain optional LLM side actions.
- `await_external` is the deterministic exception inside this fenced block. It
  declares `kind`, optional `step`, `resume_on`, fallback `prompt`/`interactive`,
  `on_resume`, `timeout`, and `recovery.{abort,resend,switch}`.
- `on_resume.set` maps JSON scalar literals or `$token.*` references into state.
  Object-form `on_resume.enrich` declares a tool, literal/`$token.*` inputs, and
  literal/`$result.*` output mappings. All resolved writes commit atomically.
- A missing referenced path produces no write; a literal `null` writes `null`.
  Required enrichment failure commits none of the pending token/result writes.
  Optional enrichment failure is logged and commits the resolved token writes.
- A host resumes recovery with `_external_event` set to `abort`, `resend`,
  `switch`, or `timeout`. The runtime schedules no timeout itself and emits an
  out-of-band marker rather than constructing a real external URL.
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
`ServiceMetadata`; omitting it selects the real UTC boundary adapter.

<!-- /section:runtime-response-contract -->
