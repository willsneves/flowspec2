# flowspec/2 — field reference

This is the field-by-field reference for the format. The authoritative machine
contract is [`../src/flowspec2/flowspec-2.schema.json`](../src/flowspec2/flowspec-2.schema.json)
(JSON Schema Draft 2020-12); the design rationale + construct→LangGraph mapping
is in [`DESIGN.md`](DESIGN.md). Every field is labelled **RAIL** (deterministic
structure, pinned) or **LLM** (non-deterministic execution, the agent's
discretion). The boundary is positional — there is no `isRail` tag.

## Top level

| Key | Req | Rail/LLM | Meaning |
|---|---|---|---|
| `schema` | ✓ | RAIL | const `"flowspec/2"`. |
| `flow` | ✓ | RAIL | stable id == `service_name`; StateManager + checkpointer + registry key. `^[a-z][a-z0-9_]*$`. |
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
| `capabilities` | | LLM | the fenced zone of side actions the LLM *may* invoke. |

## `route` (LLM)
- `description` (✓) — the routing hook the LLM reads to decide to enter the flow.
- `trigger_phrases` — a versionable seed, not the sole signal.
- `entry_args_schema` — the closed set of initial slots the LLM may pass on entry.

## `domains.<X>` (RAIL — the spine)
Declaring a domain once materializes four artifacts: a Pydantic before-validator,
the `payload_schema` enum (constrained decoding), button titles, and list rows.
- `type` — `categorical` (default) | `bool` | `free_text` | `cpf` | `email` | `name`.
- `values` — closed token set (categorical). `null` is an allowed member. Order = elicitation/button order.
- `rows[]` — `{value, description}` subtitles for two-column lists.
- `normalize` — `accent_fold`, `number_words`, `affirmation`, `emoji_veto`, `synonyms{}` (free-text → token, **enforced**, the only LLM-facing hint).

## `slots.<s>` (RAIL)
- `domain` (✓) — references `domains{}`.
- `persist` — `data` (persist+visible, default) | `internal` (persist+hidden) | `payload` (ephemeral).
- `required`, `nullable`.
- `requires[]` — precedence + dependency edges; the correction clear-cascade is **derived** from these.
- `prefill_sources[]` — channels that may pre-fill (e.g. `whatsapp_flow`).
- `max_attempts`, `on_exhaust` (`reask`|`skip`|`default`|`handoff`|`END`), `default`.
- `fill_only_when_asked` — sensitive-slot guard.

## `path[]` steps (RAIL)
A step is exactly one of (`oneOf`): `slot` | `confirm` | `derive` | `terminal` | `use`.
- `step` — stable node id (defaults to the slot/construct name).
- `prompt` — `{text, verbatim?, extract_hint?}`. `text`/`extract_hint` are **LLM**; everything else is RAIL.
- `interactive` — buttons/list/flow/cta UI model (see below).
- `ask_when` / `skip_when` — inline gate / skip predicates.
- `on_reject.end` — for confirm/summary: end with a message on decline.
- `correctable: true` — marks the confirm step as the correction hub (binds `confirm{}`).

## `interactive` (RAIL)
- `kind` (✓) — `buttons` | `list` | `flow` | `cta_url`.
- `field` (✓) — the payload key the tap fills (binds affordance to slot; **note:** may differ from the slot name, e.g. `confirmacao`).
- `from_domain` — domain whose `values` (and `rows`) materialize the options.
- `options_when[]` — per-option conditional visibility (`{value, gate}`).
- `out_of_band`, `next_step` — push the send direct to Meta (CTA login).
- `gate` — env flag enabling the path-B send; falls back to `prompt.text` when off.
- `meta_flow_ref`, `prefill_from` — for `kind:flow`.

## `predicate` grammar (RAIL)
Single-key object; namespaces `slots.` / `internal.` / `payload.` / `config.` / `address.`.
`{"in":[ref,[...]]}`, `{"eq":[a,b]}`, `{"ne":[a,b]}`, `{"is_present":ref}`, `{"and":[...]}`, `{"or":[...]}`, `{"not":<pred>}`.

## `derive[]` (RAIL)
- `writes` (✓), `from[]` (✓, ordered source slots), `lookup` (✓, `"|".join(values)` → value), `after` (step id to run after), `default` (literal or the single `$from[i]` sigil).

## `confirm` (RAIL — correction hub)
- `step`, `slot` (bool), `on_confirm` (target on Sim), `prompt`, `interactive`.
- `correctable[]` (✓) — the directly-correctable slots; the LLM names which one, the compiler routes back and clears it + transitive dependents.

## `terminal` (RAIL)
- `step`, `tool` (✓), `idempotent` (sha256 replay), `input[]` (`{param, slot}`), `outputs{}` (`statekey` ← `result.path`).
- `outcomes` (✓) — `success` (`set`, `reset_next`) | `retryable` (`preserve_state`) | `fatal` (`reset_next`).
- `empty_payload` — `never_saved` (reset|ignore) / `in_progress` (reset|ignore).

## `auto_flow` (RAIL)
- `meta_flow_ref` (✓), `send_when` (✓, predicate), `prefill_from[]`, `resume_at`, `on_submit_source` (`whatsapp_flow`), `alias_map` (one Flow field → many slots; direct `{slot:"$value"}` or value-keyed `{flow_value:{slot:val}}`).

## `capabilities` (LLM — fenced)
- `media_in` (`analyze[]`, `route_by`), `media_out[]` (types + 24h-window `template`), `tts`, `location_in`, `handoff` (`target`, `when`), `session_reset`, `await_external` (`kind`, `resume_on`, `on_resume.set/enrich`, `timeout`, `recovery.{abort,resend,switch}`).
