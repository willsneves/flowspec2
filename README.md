<!-- section:toc -->

Table of Contents:

- Install: 81 <!-- section:install -->
- LLM boundary and evidence: 130 <!-- section:llm-driven -->
- Quickstart: 213 <!-- section:quickstart -->
- Real backends: 251 <!-- section:real-backends -->
- Error correlation: 287 <!-- section:error-correlation -->
- CLI: 306 <!-- section:cli -->
- What it compiles: 367 <!-- section:what-it-compiles -->
- Example: streetlight repair: 404 <!-- section:example -->
- Layout: 423 <!-- section:layout -->
- Public contracts: 481 <!-- section:public-contracts -->
- Governance: 510 <!-- section:governance -->
- Status: 528 <!-- section:status -->

<!-- /section:toc -->

# flowspec2

`flowspec2` turns one self-contained JSON conversation contract into a reusable
[LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)
`StateGraph`. A flow declares the values it accepts, the order in which it
collects them, the transitions it permits, the tools and subflows it can call,
and how it pauses, resumes, retries, and recovers.

The format is designed for bounded service conversations: deterministic rails
own control flow and effects, while an LLM may route a request, extract a value
from natural language, and phrase a response only within the document's closed
contracts. A model cannot invent a transition, widen a value domain, or skip a
required step that the flow did not authorize.

| Published contract | Current value |
|---|---|
| PyPI distribution | [`flowspec2 1.0.1`](https://pypi.org/project/flowspec2/1.0.1/) |
| Python | `>=3.11,<3.14` — Python 3.11, 3.12, and 3.13 |
| Stable source format | `flowspec/2` |
| Runtime target | LangGraph `StateGraph[ServiceState]` |
| Type and license metadata | Typed package · MIT |
| Optional integrations | HTTP backends with `http` · Gemini with `llm` |

`flowspec/2` is the stable executable format. `flowspec/3-draft` is a separate,
experimental authoring preview: it is not registered with `FlowRuntime`, is
outside the stable compatibility promise, and lowers only its documented subset
to compile-checked v2 with an exact preview fixed-point proof. See the
[preview contract](https://github.com/wllsena/flowspec2/blob/main/docs/FLOWSPEC3_DRAFT.md)
and
[lowering decision](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0009-v3-preview-lowering-boundary.md).

The documentation entry points are the
[field reference](https://github.com/wllsena/flowspec2/blob/main/docs/SPEC.md),
[compiler and runtime design](https://github.com/wllsena/flowspec2/blob/main/docs/DESIGN.md),
[compatibility profiles](https://github.com/wllsena/flowspec2/blob/main/docs/COMPATIBILITY.md),
[prior-art comparison](https://github.com/wllsena/flowspec2/blob/main/docs/PRIOR_ART.md),
and
[AI authoring benchmark](https://github.com/wllsena/flowspec2/blob/main/docs/AUTHORING_BENCHMARK.md).
Release history, compatibility policy, and private vulnerability reporting live
in the
[changelog](https://github.com/wllsena/flowspec2/blob/main/CHANGELOG.md),
[versioning policy](https://github.com/wllsena/flowspec2/blob/main/docs/VERSIONING.md),
and [security policy](https://github.com/wllsena/flowspec2/blob/main/SECURITY.md).

The accepted decisions document
[interoperability boundaries](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0001-interoperability-boundaries.md),
[source/profile/IR separation](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0002-format-authoring-execution-boundary.md),
[benchmark author trust](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0003-benchmark-author-trust-boundary.md),
[evidence authenticity](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0004-evidence-authenticity.md),
[public acceptance semantics](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0005-authoring-acceptance-semantics.md),
[presentation review](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0006-authoring-presentation-review.md),
[operational LLM evidence](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0007-operational-llm-evidence-boundary.md),
[v3 loss accounting](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0008-v3-preview-loss-accounting.md),
[external-wait resend policy](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0010-external-wait-resend-policy.md),
[public model transport scope](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0011-public-model-transport-scope.md),
[public contract namespaces](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0012-public-contract-namespace.md),
and the
[stable package boundary](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0013-stable-package-release-boundary.md).

<!-- section:install -->

## Install

Install the stable runtime and CLI directly from PyPI:

```bash
python -m pip install flowspec2
python -c "import flowspec2; print(flowspec2.__version__)"
```

The published package supports Python 3.11 through 3.13. With uv, add it to a
project or install only the command-line application in an isolated tool
environment:

```bash
uv add flowspec2
uv tool install flowspec2
flowspec2 --help
```

Optional integrations are explicit extras:

```bash
python -m pip install "flowspec2[http]"  # httpx-backed service adapters
python -m pip install "flowspec2[llm]"   # Google Gemini runtime and authoring transports
```

The base installation includes the compiler, runtime, CLI, canonical schemas,
compatibility adapters, evidence verification and signing, packaged conformance
resources, deterministic fake tools, and the experimental preview APIs. It does
not make network calls merely because an optional provider package is installed.

Release `1.0.1` was published from the immutable `v1.0.1` tag through OpenID
Connect Trusted Publishing with provenance attestations. The preserved wheel
and source distribution are available on
[PyPI](https://pypi.org/project/flowspec2/1.0.1/); the same files and
`SHA256SUMS` are attached to the
[GitHub Release](https://github.com/wllsena/flowspec2/releases/tag/v1.0.1).

Source-checkout setup, verification, and release instructions live in
[CONTRIBUTING.md](https://github.com/wllsena/flowspec2/blob/main/CONTRIBUTING.md)
and the
[versioning policy](https://github.com/wllsena/flowspec2/blob/main/docs/VERSIONING.md).
For release-exact source and documentation, use the
[`v1.0.1` tag](https://github.com/wllsena/flowspec2/tree/v1.0.1); the default
branch may contain work prepared for a later package release.

<!-- /section:install -->
<!-- section:llm-driven -->

## LLM boundary and evidence

An LLM is optional. The compiler, runtime, validation, compatibility adapters,
and evidence verification work without a model or network access. Install the
`llm` extra only when using the public Gemini runtime, authoring, or operational
probe transports:

```bash
python -m pip install "flowspec2[llm]"
```

`GeminiAgent` exercises the non-deterministic side with free-text input. The
complete demo is repository-only, so run it from a source checkout:

```bash
export GEMINI_API_KEY=...
uv run python examples/llm_bot.py
```

At runtime, the model has two bounded jobs:

- **Route:** choose a flow from `route.description`; `trigger_phrases` are
  non-exclusive examples.
- **Extract:** map natural input to the closed token defined by the active
  `payload_schema` and interactive options.

Closed response schemas constrain both jobs. Local Draft 2020-12 validation
still runs before runtime execution, and invalid values are rejected and
re-asked. The model does not own graph transitions, effects, persistence,
idempotency, or recovery. Live-model tests require explicit opt-in; the default
suite stays offline.

The provider-neutral agent accepts deployment-specific system prompts without
changing routing or extraction schemas:

```python
from flowspec2.llm import GeminiAgent

agent = GeminiAgent(
    route_system_prompt="Route requests for Acme services. Return only JSON.",
    extraction_system_prompt="Extract Acme workflow fields. Return only JSON.",
)
```

AI authoring is a separate boundary. `GeminiAuthor` receives the packaged
corpus, complete runtime profile, public acceptance contract, prior source, and
repair diagnostics—but never the evaluator's private reference source. It
returns exact model source through a closed authoring projection. The public
contract exposes every graded observation, required and forbidden construct,
and variable presentation path.

With explicit network consent, an authoring run writes a content-addressed
envelope that binds exact captures and the report to corpus/profile digests,
model configuration and provider-reported effective identity, prompt identity,
package version, correction protocol, and the supplied repository revision:

```bash
flowspec2 authoring-benchmark-gemini --allow-network --repository-revision <revision> --output authoring-evidence.json
flowspec2 authoring-evidence-verify authoring-evidence.json --repository-revision <revision>
flowspec2 authoring-evidence-sign authoring-evidence.json --private-key authoring-private-key.pem --repository-revision <revision> --output authoring-evidence.signature.json
flowspec2 authoring-evidence-signature-verify authoring-evidence.json --signature authoring-evidence.signature.json --public-key authoring-public-key.pem --repository-revision <revision>
flowspec2 operational-benchmark-gemini authoring-evidence.json --allow-network --repository-revision <revision> --output operational-evidence.json
flowspec2 operational-evidence-verify operational-evidence.json --authoring-evidence authoring-evidence.json --repository-revision <revision>
```

These commands never overwrite artifacts, emit partial output after provider
failure, or record or print the API key. Configured credentials alone do not
authorize network use. Verification is offline and replays every exact capture
against the closed package, corpus, profile, and correction contracts. A
content digest proves integrity, not identity; optional detached Ed25519
signatures use a caller-trusted public key, while private keys come only from
explicit PEM paths and are never serialized or loaded from environment files.

Operational routing/extraction evidence remains `report_only`. It preserves
exact requests and raw responses; paired authored/counterfactual probes isolate
the effect of trigger examples and extraction hints. A completed mismatch still
produces an attributable artifact with `all_matched:false`; it does not change
deterministic conformance, presentation-review success, or promotion
eligibility.

<!-- /section:llm-driven -->
<!-- section:quickstart -->

## Quickstart

```python
import asyncio
from flowspec2 import FlowRuntime

flow = {
    "schema": "flowspec/2",
    "flow": "support_request",
    "version": "1.0.0",
    "route": {"description": "Collect one support request."},
    "domains": {"Request": {"type": "free_text"}},
    "slots": {"request": {"domain": "Request", "required": True}},
    "path": [{"slot": "request", "prompt": {"text": "How can we help?"}}],
}

runtime = FlowRuntime(flow)
state = asyncio.run(runtime.execute(runtime.new_state("example-user"), {}))
print(state.agent_response.description)  # How can we help?

state = asyncio.run(runtime.execute(state, {"request": "Reset my access"}))
print(state.status)  # completed
```

`FlowRuntime` closes structural, semantic, profile, and compilation checks
before it accepts turns. The first empty payload reaches the collection node and
returns its prompt; the second payload is validated against the `Request`
domain and completes the flow.

`runtime.as_tool()` exposes the same runtime as an async
`(service_name, user_id, payload) -> dict` callable. Its reference state store
and per-user serialization are in-memory and scoped to one runtime and event
loop. Multi-process hosts must provide transactional shared persistence and
distributed per-user serialization around this adapter.

<!-- /section:quickstart -->
<!-- section:real-backends -->

## Real backends

The reference profile provides deterministic in-memory implementations of
`geocode`, `brazilian_tax_id_lookup`, `get_user_info`, and
`open_service_request`. Install the `http` extra and pass a registry to replace
only the tools whose service URLs are configured; unconfigured tools remain
fakes and the registry-local terminal replay cache stays intact:

```python
from flowspec2 import FlowRuntime
from flowspec2.backends import BackendConfig, make_registry

backend_config = BackendConfig.from_env()
runtime = FlowRuntime(flow_document, tools=make_registry(backend_config))
```

`BackendConfig.from_env()` recognizes these variables:

| Variable | Purpose |
|---|---|
| `FLOWSPEC2_GEOCODE_URL` | geocoding endpoint |
| `FLOWSPEC2_BRAZILIAN_TAX_ID_LOOKUP_URL` | Brazilian tax-ID lookup endpoint |
| `FLOWSPEC2_GOVBR_ENRICH_URL` | gov.br enrichment endpoint used by `get_user_info` |
| `FLOWSPEC2_TICKETING_URL` | service-request endpoint |
| `FLOWSPEC2_API_KEY` | optional bearer credential sent to configured endpoints |
| `FLOWSPEC2_HTTP_TIMEOUT` | HTTP timeout override |

The ticketing adapter maps **2xx → `success`**, **5xx, timeout, or connection
error → `retryable`** with preserved state, and **4xx → `fatal`** with reset.
Backends accept `transport=` for offline `httpx.MockTransport` tests. Each URL
must name an authorized service or an adapter implementing the contracts in
`flowspec2.backends.http`; configuration does not establish authorization.

<!-- /section:real-backends -->
<!-- section:error-correlation -->

## Error correlation

Every caller-visible warning or error carries a decimal Snowflake `log_id`
matching its structured log record. `AgentResponse` preserves the identifier,
`FlowRuntime.as_tool()` returns it beside `error_message`, and the CLI renders
`[log_id=…]`. Internal best-effort warnings follow the same correlation
contract.

`FlowRuntime` accepts injected Snowflake generators and UTC clocks. The default
generator derives a best-effort process-local worker identity; concurrent
processes must receive distinct `FLOWSPEC2_SNOWFLAKE_WORKER_ID` values to claim
distributed uniqueness. Invalid worker configuration fails before an identifier
is emitted. Locked logical time preserves ordering across clock rollback and
sequence saturation, while the independently injectable metadata clock keeps
tests deterministic.

<!-- /section:error-correlation -->
<!-- section:cli -->

## CLI

Installing `flowspec2` creates the `flowspec2` executable. The paths under
`examples/` below exist only in a source checkout; replace them with your own
flow documents when using the PyPI installation.

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
flowspec2 operational-benchmark-gemini build/authoring-evidence.json --allow-network --repository-revision <revision> --output build/operational-evidence.json
flowspec2 operational-evidence-verify build/operational-evidence.json --authoring-evidence build/authoring-evidence.json --repository-revision <revision> --json
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

- `validate` aggregates structural, semantic, and profile diagnostics without
  compiling. `check` adds compilation; `check --json` is the machine-readable
  AI repair-loop interface. Findings carry a stable code, severity, JSON
  Pointer, message, and optional related location or suggested fix. Structural
  findings come first, then safe sources proceed through semantic, profile, and
  compilation checks. `normalize` and `ir` write only to standard output and
  never mutate source.
- `ir` shows source-linked canonical contracts and logical subflow anchors for
  digests, compatibility, and migration. `graph` and `mermaid` inspect the
  expanded executable topology, including fan-out, back-edges, and internal
  cycles.
- Rasa conversion implements a strict versioned portable subset;
  `--allow-lossy` acknowledges only the profile's reported metadata, lifecycle,
  presentation, and host-adapter differences. Open Workflow uses a lossless
  profile envelope whose custom calls still require a profile-aware runtime.
  Exact boundaries and Python APIs are in the
  [compatibility documentation](https://github.com/wllsena/flowspec2/blob/main/docs/COMPATIBILITY.md).
- Network-backed benchmark commands require `--allow-network`; verification,
  signing, and presentation-review commands do not call a provider.
  Presentation-review initialization verifies and replays evidence before
  creating a packet containing candidate prose and public context. Edit only
  `reviewer_identifier`, `decision`, and `rationale`; finalization rejects other
  changes. Verification and signing repeat evidence and subject closure
  offline. Promotion verification combines deterministic, review, and
  authentication results but remains report-only under
  [ADR 0006](https://github.com/wllsena/flowspec2/blob/main/docs/adr/0006-authoring-presentation-review.md).

<!-- /section:cli -->
<!-- section:what-it-compiles -->

## What it compiles

The reference implementation keeps authoring, linking, execution, and host
responsibilities separate:

```text
flowspec/2 JSON -> structural validation -> semantic linking -> runtime profile
                -> canonical FlowIR -> compiled LangGraph -> host-managed session
```

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

Full mapping, rationale, and rejected alternatives are in the
[design](https://github.com/wllsena/flowspec2/blob/main/docs/DESIGN.md). The
[field reference](https://github.com/wllsena/flowspec2/blob/main/docs/SPEC.md)
documents every source contract.

<!-- /section:what-it-compiles -->
<!-- section:example -->

## Example: streetlight repair

The complete executable contract is available in the source repository as
[`examples/streetlight_repair.flow.json`](https://github.com/wllsena/flowspec2/blob/main/examples/streetlight_repair.flow.json).
It demonstrates closed domains, conditional collection, corrections, address
and identification subflows, typed external resume, idempotent submission, and
recovery policies. Compile it from a source checkout to inspect the generated
graph:

```python
from flowspec2 import FlowRuntime, load_flow

runtime = FlowRuntime(load_flow("examples/streetlight_repair.flow.json"))
print(runtime.compiled.graph.get_graph().draw_mermaid())
```

<!-- /section:example -->
<!-- section:layout -->

## Layout

This is the repository layout used to build the PyPI distributions. Examples,
tests, and the full documentation corpus remain in the source repository; the
wheel contains the runtime package and its canonical packaged contracts.

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
[`wllsena.github.io/flowspec2`](https://wllsena.github.io/flowspec2/). This
includes the stable
[`flowspec/2` schema](https://wllsena.github.io/flowspec2/schemas/flowspec-2.json)
and the experimental
[`flowspec/3-draft` schema](https://wllsena.github.io/flowspec2/schemas/flowspec-3-draft.json).
The
[Open Workflow conversational profile](https://wllsena.github.io/flowspec2/profiles/open-workflow-conversation-1)
resolves to a human-readable resource linking its schema and compatibility
contract. These URLs are versioned public identities, not mutable aliases.

The site is generated from package sources rather than maintained as a second
editable schema copy:

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

See the
[contribution guide](https://github.com/wllsena/flowspec2/blob/main/CONTRIBUTING.md),
[code of conduct](https://github.com/wllsena/flowspec2/blob/main/CODE_OF_CONDUCT.md),
[citation metadata](https://github.com/wllsena/flowspec2/blob/main/CITATION.cff),
and [security policy](https://github.com/wllsena/flowspec2/blob/main/SECURITY.md).
[Support](https://github.com/wllsena/flowspec2/blob/main/SUPPORT.md) routes usage
questions, public bugs, proposals, and private security reports.

Development is substantially AI-assisted, but the repository owner remains
responsible for scope, review, verification, release, and acceptance.
Substantially AI-authored commits carry an authorship trailer, and model output
becomes evidence only through the repository's deterministic gates.

<!-- /section:governance -->
<!-- section:status -->

## Status

The latest verified PyPI artifact is the stable `flowspec2 1.0.1` package,
published on
[PyPI](https://pypi.org/project/flowspec2/1.0.1/) and mirrored by the immutable
[GitHub Release](https://github.com/wllsena/flowspec2/releases/tag/v1.0.1).
Repository metadata may prepare a later version before publication; a prepared
version is not a release until its verified artifacts exist on PyPI. The
[changelog](https://github.com/wllsena/flowspec2/blob/main/CHANGELOG.md) labels
that distinction explicitly.

`flowspec/2` is stable and executable. `flowspec/3-draft` is neither: its
analytical lowerer accepts only the subset that can become compile-checked v2
and round-trip back to the exact preview. Promotion still requires comparative
real-model evidence; fixture success and source-size measurements are not
model-quality evidence.

The suite stays offline through injectable in-memory backends. Civic-service
subflows ship with those fakes behind the same production-facing protocols. The
live runner emits reproducible evidence for promotion decisions without treating
fixture success as model-quality evidence. Civic-service examples use reserved
demonstration endpoints and imply no affiliation or deployment. Licensed under
MIT.

<!-- /section:status -->
