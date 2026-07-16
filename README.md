<!-- section:toc -->

Table of Contents:

- Install: 61 <!-- section:install -->
- LLM-driven (the engine side): 110 <!-- section:llm-driven -->
- Quickstart: 178 <!-- section:quickstart -->
- Real backends: 203 <!-- section:real-backends -->
- Error correlation: 226 <!-- section:error-correlation -->
- CLI: 243 <!-- section:cli -->
- What it compiles: 292 <!-- section:what-it-compiles -->
- Example: streetlight repair: 318 <!-- section:example -->
- Layout: 336 <!-- section:layout -->
- Public contracts: 390 <!-- section:public-contracts -->
- Governance: 410 <!-- section:governance -->
- Status: 425 <!-- section:status -->

<!-- /section:toc -->

# flowspec2

**A self-contained JSON format for bounded conversational flows, compiled at
runtime into a [LangGraph](https://langchain-ai.github.io/langgraph/)
`StateGraph`.** Each service is one document and becomes a callable tool or
subgraph.

> **Core idea: closed value domains separate rails from model freedom.** The
> document fixes states, accepted values, transitions, guards, tool bindings,
> pauses, idempotency, and recovery. The LLM chooses the flow, extracts a closed
> token from natural input, phrases prompts, and selects permitted side actions.
> It cannot invent transitions, widen a domain, or bypass a required slot unless
> the author declared that route.

`flowspec/2` is stable and executable. `flowspec/3-draft` is an isolated,
non-executable authoring experiment with typed loss accounting and analytical
lowering to compile-checked v2; its supported fixed point is documented in the
[preview](docs/FLOWSPEC3_DRAFT.md) and
[lowering decision](docs/adr/0009-v3-preview-lowering-boundary.md).

Start with the [field reference](docs/SPEC.md), [design](docs/DESIGN.md),
[prior-art comparison](docs/PRIOR_ART.md), and
[compatibility profiles](docs/COMPATIBILITY.md). The
[AI authoring benchmark](docs/AUTHORING_BENCHMARK.md) separates deterministic
format conformance from real-model evidence. Releases and security are covered
by the [changelog](CHANGELOG.md), [versioning policy](docs/VERSIONING.md), and
[private vulnerability process](SECURITY.md).

Key decisions cover [interoperability](docs/adr/0001-interoperability-boundaries.md),
[source/profile/IR separation](docs/adr/0002-format-authoring-execution-boundary.md),
[evidence authenticity](docs/adr/0004-evidence-authenticity.md),
[public acceptance semantics](docs/adr/0005-authoring-acceptance-semantics.md),
[presentation review](docs/adr/0006-authoring-presentation-review.md),
[report-only operational evidence](docs/adr/0007-operational-llm-evidence-boundary.md),
[external-wait resend limits](docs/adr/0010-external-wait-resend-policy.md),
[public model transports](docs/adr/0011-public-model-transport-scope.md),
[contract namespaces](docs/adr/0012-public-contract-namespace.md), and the
[stable package boundary](docs/adr/0013-stable-package-release-boundary.md).

<!-- section:install -->

## Install

```bash
uv sync                      # install the runtime package
make ci                      # locked lint, format check, type checks, and offline tests
make package-check           # build, inspect, and smoke-test the wheel and source distribution
uv run python examples/simulate.py   # 6 real citizen conversations over the HTTP backends
```

Release artifacts are built once from a clean checkout of the matching
`vX.Y.Z` tag. The build verifies metadata, packaged contracts, locked
dependencies, and installed behavior; writes `SHA256SUMS`; and refuses an
existing destination. An explicit sdist allowlist prevents unrelated local
files from entering or breaking the build. The check verifies the allowed
archive contents, exact artifact set, integrity, and current `pyproject.toml`
version without rebuilding:

```bash
make release-build RELEASE_ARTIFACTS=build/release
make release-check RELEASE_ARTIFACTS=build/release
```

An immutable tag publishes the verified wheel and source distribution to PyPI
through OpenID Connect Trusted Publishing with provenance attestations, then
attaches the same files and `SHA256SUMS` to the GitHub Release. Before the first
tag, the `pypi` environment must require maintainer approval and be registered
as the project's Trusted Publisher.

Tooling is pinned in `pyproject.toml` and `uv.lock`. `make lint`,
`make typecheck`, and `make test` run independently; `make format` applies Ruff.
All targets use the locked `dev` extra without loading environment files.
Pyright runs through its packaged distribution and a signed, digest-pinned
Distroless Node image with no shell, package manager, network, capabilities,
writable root, or writable project mount. Docker is the only extra prerequisite
for `make typecheck` and `make ci`. GitHub Actions runs the same gate across
supported Python minors plus a separate package check, with read-only
permissions, manual dispatch, no secrets or live-model tests, and immutable
Action and uv pins.

`examples/simulate.py` prints six turn-by-turn `streetlight_repair` conversations
over deterministic HTTP backends via `MockTransport`: WhatsApp Flow, address
correction, gov.br authentication, the public-square-to-sports-court branch,
retry after a ticketing-system 503, and duplicate submission replay within one
registry. Durable cross-process exactly-once behavior remains the host's
responsibility.

<!-- /section:install -->
<!-- section:llm-driven -->

## LLM-driven (the engine side)

The simulations use pre-extracted tokens. `GeminiAgent` exercises the
non-deterministic side with free-text input:

```bash
uv sync --extra llm                       # google-genai
export GEMINI_API_KEY=...
uv run python examples/llm_bot.py         # the citizen speaks free text; the LLM routes + extracts
```

The LLM has two jobs:

- **Route:** choose a flow from `route.description`; `trigger_phrases` are
  non-exclusive examples.
- **Extract:** map natural input to the closed token defined by the active
  `payload_schema` and interactive options.

Closed response schemas constrain both jobs, and local Draft 2020-12 validation
still runs before runtime execution. Invalid values are rejected and re-asked.
Live-model tests require explicit opt-in; the default suite is offline.

The provider-neutral agent accepts deployment-specific system prompts without
changing routing or extraction schemas:

```python
agent = GeminiAgent(
    route_system_prompt="Route requests for Acme services. Return only JSON.",
    extraction_system_prompt="Extract Acme workflow fields. Return only JSON.",
)
```

Authoring evaluation is separate. `GeminiAuthor` receives the packaged corpus,
complete runtime profile, public acceptance contract, prior source, and repair
diagnostics—but never the private reference source—and returns exact source
through the closed authoring projection. The public contract enumerates every
graded observation, required and forbidden construct, and variable presentation
path. With explicit network consent, the run writes a content-addressed envelope
binding exact captures and the report to corpus/profile digests, model
configuration and effective identity, prompt identity, package version,
correction protocol, and repository revision:

```bash
flowspec2 authoring-benchmark-gemini --allow-network --repository-revision <revision> --output authoring-evidence.json
flowspec2 authoring-evidence-verify authoring-evidence.json --repository-revision <revision>
flowspec2 authoring-evidence-sign authoring-evidence.json --private-key authoring-private-key.pem --repository-revision <revision> --output authoring-evidence.signature.json
flowspec2 authoring-evidence-signature-verify authoring-evidence.json --signature authoring-evidence.signature.json --public-key authoring-public-key.pem --repository-revision <revision>
flowspec2 operational-benchmark-gemini authoring-evidence.json --allow-network --repository-revision <revision> --output operational-evidence.json
flowspec2 operational-evidence-verify operational-evidence.json --authoring-evidence authoring-evidence.json --repository-revision <revision>
```

Commands never overwrite artifacts, emit partial output after provider failure,
or record or print the API key. Configured credentials alone do not authorize
network use. Verification is offline and replays every exact capture against
the closed package, corpus, profile, and correction contracts. The content
digest proves integrity, not identity; optional detached Ed25519 signatures use
an explicit caller-trusted public key, while private keys come only from
explicit PEM paths and are never serialized or loaded from environment files.

Operational routing/extraction evidence remains `report_only`. It preserves
exact requests and raw responses; paired authored/counterfactual probes isolate
the effect of trigger examples and extraction hints. Completed mismatches remain
successful report-only artifacts with `all_matched:false`, without changing
deterministic conformance or presentation-review eligibility.

<!-- /section:llm-driven -->
<!-- section:quickstart -->

## Quickstart

```python
import asyncio
from flowspec2 import FlowRuntime, load_flow

flow = load_flow("examples/streetlight_repair.flow.json")  # validates against the JSON Schema
rt = FlowRuntime(flow)                                     # compiles → StateGraph[ServiceState]

async def main():
    state = rt.new_state(user_id="5521999999999")
    # turn 1 — the agent extracted the closed token "Not working" from free text
    state = await rt.execute(state, {"streetlight_issue": "Not working"})
    print(state.agent_response.description)                # the next question (e.g. streetlight count)
    print(state.agent_response.payload_schema)             # the JSON Schema handed to constrained decoding

asyncio.run(main())
```

`rt.as_tool()` returns a `multi_step_service`-style callable with the contract
`(service_name, user_id, payload) -> dict`.

<!-- /section:quickstart -->
<!-- section:real-backends -->

## Real backends

`geocode`, `brazilian_tax_id_lookup`, `get_user_info`, and `open_service_request` default to
in-memory fakes. `make_registry` replaces only tools with configured URLs, so
partial configurations retain the other fakes and the terminal replay cache:

```python
from flowspec2 import FlowRuntime
from flowspec2.backends import BackendConfig, make_registry

cfg = BackendConfig.from_env()  # GEOCODE / BRAZILIAN_TAX_ID_LOOKUP / GOVBR_ENRICH / TICKETING URLs
rt = FlowRuntime(doc, tools=make_registry(cfg))
```

Install HTTP support with `uv sync --extra http`. The ticketing adapter maps **2xx →
`success`**, **5xx/timeout/connection error → `retryable`** with preserved state,
and **4xx → `fatal`** with reset. Backends accept `transport=` for offline
`httpx.MockTransport` tests. Each URL must target an authorized service or an
adapter implementing `backends/http.py`.

<!-- /section:real-backends -->
<!-- section:error-correlation -->

## Error correlation

Every caller-visible warning or error carries a decimal Snowflake `log_id` that
matches its structured log record. `AgentResponse` preserves it,
`FlowRuntime.as_tool()` returns it with `error_message`, and the CLI renders
`[log_id=…]`; internal best-effort warnings follow the same contract.

`FlowRuntime` accepts injectable Snowflake generators and UTC clocks. The
default generator derives a best-effort process-local worker identity;
concurrent processes need distinct `FLOWSPEC2_SNOWFLAKE_WORKER_ID` values for
distributed uniqueness. Invalid configuration fails before emission. Locked
logical time preserves ordering across clock rollback and sequence saturation.
The metadata clock is independently injectable for deterministic tests.

<!-- /section:error-correlation -->
<!-- section:cli -->

## CLI

```bash
flowspec2 validate examples/streetlight_repair.flow.json   # structural + semantic + profile checks
flowspec2 check examples/streetlight_repair.flow.json --json  # checks + compile, aggregate JSON diagnostics
flowspec2 normalize examples/streetlight_repair.flow.json  # canonical defaults and stable node ids
flowspec2 ir examples/streetlight_repair.flow.json         # canonical contracts; subflows stay abstracted
flowspec2 graph examples/streetlight_repair.flow.json      # list compiled node ids
flowspec2 mermaid examples/streetlight_repair.flow.json    # fully expanded compiled graph as Mermaid
flowspec2 rasa-export path/to/portable.flow.json --output-dir build/rasa --allow-lossy
flowspec2 rasa-import build/rasa/flows.yml --domain build/rasa/domain.yml --flow collect_contact --output build/collect_contact.flow.json --allow-lossy
flowspec2 open-workflow-export examples/streetlight_repair.flow.json --output build/streetlight_repair.workflow.yaml
flowspec2 open-workflow-import build/streetlight_repair.workflow.yaml --output build/streetlight_repair.flow.json
flowspec2 authoring-benchmark-gemini --allow-network --repository-revision <revision> --output build/authoring-evidence.json
flowspec2 authoring-evidence-verify build/authoring-evidence.json --repository-revision <revision>
flowspec2 authoring-evidence-sign build/authoring-evidence.json --private-key authoring-private-key.pem --repository-revision <revision> --output build/authoring-evidence.signature.json
flowspec2 authoring-evidence-signature-verify build/authoring-evidence.json --signature build/authoring-evidence.signature.json --public-key authoring-public-key.pem --repository-revision <revision> --json
flowspec2 authoring-presentation-review-init build/authoring-evidence.json --repository-revision <revision> --output build/presentation-review.draft.json
flowspec2 authoring-presentation-review-finalize build/authoring-evidence.json --draft build/presentation-review.draft.json --repository-revision <revision> --output build/presentation-review.json
flowspec2 authoring-presentation-review-verify build/authoring-evidence.json --review build/presentation-review.json --repository-revision <revision> --json
flowspec2 authoring-presentation-review-sign build/authoring-evidence.json --review build/presentation-review.json --private-key reviewer-private-key.pem --repository-revision <revision> --output build/presentation-review.signature.json
flowspec2 authoring-presentation-review-signature-verify build/authoring-evidence.json --review build/presentation-review.json --signature build/presentation-review.signature.json --public-key reviewer-public-key.pem --repository-revision <revision> --json
flowspec2 authoring-promotion-verify build/authoring-evidence.json --review build/presentation-review.json --signature build/presentation-review.signature.json --public-key reviewer-public-key.pem --repository-revision <revision> --json
```

- `check --json` is the AI repair-loop interface. Findings have stable codes,
  severity, JSON Pointer, message, and optional related location or fix.
  Structural findings come first; safe sources then proceed through semantic,
  profile, and compilation checks. `normalize` and `ir` write only to standard
  output and never mutate source.
- `ir` shows source-linked canonical contracts and logical subflow anchors for
  digests, compatibility, and migration. `graph` and `mermaid` inspect the
  expanded executable topology, including fan-out, back-edges, and internal
  cycles.
- Rasa conversion is a strict versioned subset; `--allow-lossy` acknowledges
  reported metadata and lifecycle differences. Open Workflow uses a lossless
  profile envelope. Exact boundaries and Python APIs are in
  [COMPATIBILITY.md](docs/COMPATIBILITY.md).
- Presentation-review initialization verifies and replays evidence before
  creating a packet containing candidate prose and public context. Edit only
  `reviewer_identifier`, `decision`, and `rationale`; finalization rejects other
  changes. Verification and signing repeat evidence and subject closure
  offline. Promotion verification combines deterministic, review, and
  authentication results but remains report-only under
  [ADR 0006](docs/adr/0006-authoring-presentation-review.md).

<!-- /section:cli -->
<!-- section:what-it-compiles -->

## What it compiles

| flowspec2 construct | LangGraph primitive |
|---|---|
| source document | closed JSON Schema → aggregate semantic linker → named runtime profile → canonical `FlowIR`; source remains the only authored artifact and the IR carries the explicit `flowspec2/ir@1` identity |
| runtime profile | exact domain-kind, typed tool, complete subflow manifest, and host-capability catalog checked before graph construction; the reference profile rejects legacy open contracts |
| `ToolDefinition` / `SubflowDefinition` | immutable versioned JSON Schema contracts for calls, results, effects, configuration, exposed slot schemas, state ownership, node IDs, transitive tools, and capabilities |
| document | one `StateGraph[ServiceState]` compiled when each `FlowRuntime` instance is created, then reused across calls |
| `domains.<X>` | a Pydantic `@field_validator(mode="before")` + `model_json_schema()` (→ constrained-decoding `payload_schema`) + interactive option titles/rows; boolean domains render canonical `true`/`false` IDs as “Yes”/“No” |
| `path.slot` + `slots.<s>` | a partition-aware collection node where `required:false` advertises `null` as a persistent no-value skip, `required:true` requires a domain-valid value unless `nullable:true` stores `null`, `fill_only_when_asked` binds sensitive input to its prompt or an allowed prefill source, and `on_exhaust:default` is compiled through the domain validator |
| `path` `confirm`/`derive`/`terminal`/`use` step | `add_node` with the canonical confirm/derive/terminal template; address and identification subflows expose their collectable slots to a post-expansion referential-integrity pass |
| `interactive.field` / `options_when` | the enclosing slot or confirmation is the compiled state target for the UI payload key; conflicting field bindings fail compilation and conditional options are evaluated against runtime predicates |
| `path` order | synthesized `add_conditional_edges` routers (pause = `agent_response` set → `END`) |
| `ask_when` / `overrides.gates` | guard predicate compiled into the node + path map (skip-by-vacuity) |
| `confirm.correctable[]` | exact canonical-ID enum and private non-linear back-edge routing; clear-cascade derived from `requires[]` + `derive.from` |
| `terminal.outcomes` | typed success / retryable / fatal protocol + `_reset_on_next_call`; replay is registry-local unless the host supplies durable storage |
| `auto_flow` | pre-graph send with a closed pending-event contract, absolute deadline, bounded resend, and compiler-validated cancel/timeout/fallback recovery |
| `capabilities.await_external` | typed suspend/resume with token schema, correlation, duplicate/late policy, atomic mappings, and abort/resend/switch/timeout recovery; optional `max_resends` persists and exposes the remaining runtime-enforced resend budget, timeout materializes a persisted absolute deadline, and an `END` recovery resets on the next call |
| active state migration | exact source/target IR contracts + declarative partition copies/defaults/drops → target-schema validation and a canonical verifiable loss report; ordinary restore never guesses |
| `predicate` grammar | pure boolean function over `ServiceState` compiled into routers/early-returns |

Full mapping + rationale + rejected alternatives: [`docs/DESIGN.md`](docs/DESIGN.md). Field-by-field reference: [`docs/SPEC.md`](docs/SPEC.md).

<!-- /section:what-it-compiles -->
<!-- section:example -->

## Example: streetlight repair

The complete executable contract is in
[`examples/streetlight_repair.flow.json`](examples/streetlight_repair.flow.json).
It demonstrates closed domains, conditional collection, corrections, address and
identification subflows, typed external resume, idempotent submission, and
recovery policies. Compile it to inspect the generated graph:

```python
from flowspec2 import FlowRuntime, load_flow

runtime = FlowRuntime(load_flow("examples/streetlight_repair.flow.json"))
print(runtime.compiled.graph.get_graph().draw_mermaid())
```

<!-- /section:example -->
<!-- section:layout -->

## Layout

<details>
<summary>Source tree (click to expand)</summary>

```
src/flowspec2/
  clock.py         injectable UTC clock contract · real boundary adapter
  models.py        ServiceState · AgentResponse · ServiceMetadata (the contracts)
  domains.py       domain → Pydantic validator + normalize strategies
  predicates.py    the frozen predicate grammar evaluator
  nodes.py         collect / confirm / derive / terminal node templates
  compiler.py      graph assembly · node ordering · StateGraph wiring
  compiler_contracts.py stable compiler-contract facade
  compiler_schema_relations.py JSON Schema subset · path · presence proofs
  compiler_value_contracts.py entry · slot · derive · terminal value contracts
  compiler_tool_contracts.py tool inputs · outputs · effects · terminal protocol
  compiler_resume_contracts.py suspension · resume token · recovery contracts
  checker.py       aggregate structural · semantic · profile · compilation diagnostics
  diagnostics.py   immutable machine-readable findings and reports
  semantics.py     semantic orchestration · runtime-profile linking
  semantic_source_contracts.py stable source-contract facade
  semantic_schema_contracts.py reusable semantic JSON Schema relations
  semantic_path_contracts.py path · domain · slot contracts
  semantic_derive_contracts.py derive values · placement · execution order
  semantic_predicate_contracts.py predicate references · literal compatibility
  semantic_state_contracts.py entry · state writers · rail references
  semantic_support.py shared semantic diagnostics · JSON projections
  profiles.py      named runtime capability catalogs
  ir.py            canonical normalization · contracts · state schema · digests
  state_migration.py declarative active-state migration · loss report · schema proofs
  runtime.py       FlowRuntime: validate · compile · execute · as_tool (+ auto_flow short-circuit)
  cli.py           command handlers · I/O boundaries · error reporting
  cli_parser.py    declarative argument grammar · injected handlers
  observability.py Snowflake log IDs · structured event helper
  schema.py        load + JSON-Schema validation
  schema_contracts.py closed local references · external-resume schema contract
  compat/          directional Rasa import/export · Open Workflow profile + vendored schema
  authoring/       corpus · benchmark · Gemini · evidence verification/signing · projection · CTK
  experimental/    non-runtime flowspec/3-draft preview · migration · lowering
  interactive.py   buttons / list / flow envelope builders (Meta limits)
  tools.py         ToolRegistry + injectable fake backends + idempotency replay
  backends/        BackendConfig + make_registry + httpx HTTP tools (real integrations)
  subflows/        address@1 · identification@2 (reusable, versioned)
  flowspec-2.schema.json
examples/          streetlight_repair.flow.json · pothole_repair.flow.json
tests/             schema · boundaries · linker · IR · authoring · compatibility · runtime E2E
```

</details>

<!-- /section:layout -->
<!-- section:public-contracts -->

## Public contracts

Project-owned JSON Schemas resolve at their canonical `$id` under
[`wllsena.github.io/flowspec2`](https://wllsena.github.io/flowspec2/). The Open
Workflow profile identifier resolves to a page linking its schema and
compatibility contract. The site is generated from package sources:

```bash
make public-site-check
make public-site-build PUBLIC_SITE=build/public-site
```

The checker validates schemas, rejects duplicate or off-namespace identifiers,
and proves generated resources equal their canonical documents. GitHub Pages
deploys only the `main` artifact with short-lived identity and narrow
permissions, then verifies the deployed resources against source over HTTP.

<!-- /section:public-contracts -->
<!-- section:governance -->

## Governance

See [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [CITATION.cff](CITATION.cff), and
[SECURITY.md](SECURITY.md). [SUPPORT.md](SUPPORT.md) routes usage questions,
public bugs, proposals, and private security reports.

Development is substantially AI-assisted, but the repository owner remains
responsible for scope, review, verification, release, and acceptance.
Substantially AI-authored commits carry an authorship trailer, and model output
becomes evidence only through the repository's deterministic gates.

<!-- /section:governance -->
<!-- section:status -->

## Status

`flowspec/2` is stable and executable. `flowspec/3-draft` is neither: its
analytical lowerer accepts only previews that can become compile-checked v2 and
round-trip back exactly. Promotion still requires comparative real-model
evidence; fixture success is not model-quality evidence.

The suite stays offline through injectable in-memory backends. Civic-service
subflows ship with those fakes behind the same production-facing protocols. The
live runner emits reproducible evidence for promotion decisions without treating
fixture success as model-quality evidence. Civic-service examples use reserved
demonstration endpoints and imply no affiliation or deployment. Licensed under
MIT.

<!-- /section:status -->
