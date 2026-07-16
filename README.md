<!-- section:toc -->

Table of Contents:

- Install: 63 <!-- section:install -->
- LLM-driven (the engine side): 111 <!-- section:llm-driven -->
- Quickstart: 169 <!-- section:quickstart -->
- Real backends: 194 <!-- section:real-backends -->
- Error correlation: 217 <!-- section:error-correlation -->
- CLI: 234 <!-- section:cli -->
- What it compiles: 283 <!-- section:what-it-compiles -->
- Example: reparo de luminária: 309 <!-- section:example -->
    - The flowspec/2 document: 316 <!-- section:example-document -->
    - Compiled LangGraph: 954 <!-- section:example-compiled-langgraph -->
- Layout: 1073 <!-- section:layout -->
- Public contracts: 1127 <!-- section:public-contracts -->
- Governance: 1147 <!-- section:governance -->
- Status: 1161 <!-- section:status -->

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

`examples/simulate.py` prints six turn-by-turn `reparo_luminaria` conversations
over deterministic HTTP backends via `MockTransport`: WhatsApp Flow, address
correction, gov.br authentication, the praça→quadra branch, retry after an SGRC
503, and duplicate submission replay within one registry. Durable cross-process
exactly-once behavior remains the host's responsibility.

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

`rt.as_tool()` returns a `multi_step_service`-style callable with the contract
`(service_name, user_id, payload) -> dict`.

<!-- /section:quickstart -->
<!-- section:real-backends -->

## Real backends

`geocode`, `cpf_lookup`, `get_user_info`, and `sgrc_open_ticket` default to
in-memory fakes. `make_registry` replaces only tools with configured URLs, so
partial configurations retain the other fakes and the terminal replay cache:

```python
from flowspec2 import FlowRuntime
from flowspec2.backends import BackendConfig, make_registry

cfg = BackendConfig.from_env()      # FLOWSPEC2_GEOCODE_URL / _CPF_LOOKUP_URL / _GOVBR_ENRICH_URL / _SGRC_URL / _API_KEY
rt = FlowRuntime(doc, tools=make_registry(cfg))
```

Install HTTP support with `uv sync --extra http`. The SGRC adapter maps **2xx →
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
flowspec2 validate examples/reparo_luminaria.flow.json   # structural + semantic + profile checks
flowspec2 check examples/reparo_luminaria.flow.json --json  # checks + compile, aggregate JSON diagnostics
flowspec2 normalize examples/reparo_luminaria.flow.json  # canonical defaults and stable node ids
flowspec2 ir examples/reparo_luminaria.flow.json         # canonical contracts; subflows stay abstracted
flowspec2 graph    examples/reparo_luminaria.flow.json   # list compiled node ids
flowspec2 mermaid  examples/reparo_luminaria.flow.json   # fully expanded compiled graph as Mermaid
flowspec2 rasa-export path/to/portable.flow.json --output-dir build/rasa --allow-lossy
flowspec2 rasa-import build/rasa/flows.yml --domain build/rasa/domain.yml --flow collect_contact --output build/collect_contact.flow.json --allow-lossy
flowspec2 open-workflow-export examples/reparo_luminaria.flow.json --output build/reparo_luminaria.workflow.yaml
flowspec2 open-workflow-import build/reparo_luminaria.workflow.yaml --output build/reparo_luminaria.flow.json
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
| `domains.<X>` | a Pydantic `@field_validator(mode="before")` + `model_json_schema()` (→ constrained-decoding `payload_schema`) + interactive option titles/rows; boolean domains render canonical `true`/`false` IDs as “Sim”/“Não” |
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

## Example: reparo de luminária

The complete source document and generated graph for a guided public-lighting
repair service.

<!-- section:example-document -->

### The flowspec/2 document

<details>
<summary><code>examples/reparo_luminaria.flow.json</code> (click to expand)</summary>

```json
{
  "schema": "flowspec/2",
  "flow": "reparo_luminaria",
  "version": "1.8.0",
  "service": {
    "id": "18131",
    "codigo_servico_1746": "18131",
    "knowledge_service_id": "46f2d094-030c-449f-8f9d-5437a7243a43",
    "identificacao_obrigatoria_1746": false
  },
  "route": {
    "description": "Reparo de iluminação pública: luminária apagada, piscando, acesa de dia, pendurada, danificada ou com ruído; poste sem luz na rua, calçada, praça ou parque.",
    "trigger_phrases": [
      "luz da rua apagada",
      "poste sem luz",
      "luminária piscando",
      "iluminação pública queimada"
    ],
    "entry_args_schema": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "luminaria_defeito": {
          "enum": [
            "Apagada",
            "Piscando",
            "Acesa de dia",
            "Pendurada",
            "Danificada",
            "Com ruído"
          ]
        }
      }
    }
  },
  "config": {
    "address_required": true,
    "identification_required": false,
    "max_attempts": 3
  },
  "entry": {
    "tool": "hub_search",
    "blocking": false,
    "writes": "service_info"
  },
  "domains": {
    "LuminariaDefeito": {
      "type": "categorical",
      "values": [
        "Apagada",
        "Piscando",
        "Acesa de dia",
        "Pendurada",
        "Danificada",
        "Com ruído"
      ],
      "rows": [
        {
          "value": "Apagada",
          "description": "A luminária não acende / está sem luz"
        },
        {
          "value": "Piscando",
          "description": "A luz fica piscando"
        },
        {
          "value": "Acesa de dia",
          "description": "Fica acesa durante o dia"
        },
        {
          "value": "Pendurada",
          "description": "A luminária está pendurada/solta"
        },
        {
          "value": "Danificada",
          "description": "Estrutura quebrada ou danificada"
        },
        {
          "value": "Com ruído",
          "description": "Faz barulho/zumbido"
        }
      ],
      "normalize": {
        "accent_fold": true,
        "number_words": true,
        "synonyms": {
          "sem luz": "Apagada",
          "queimada": "Apagada",
          "nao acende": "Apagada",
          "pisca": "Piscando",
          "acesa durante o dia": "Acesa de dia",
          "ruido": "Com ruído"
        }
      }
    },
    "Quantidade": {
      "type": "categorical",
      "values": [
        "uma",
        "grupo"
      ],
      "normalize": {
        "number_words": true,
        "synonyms": {
          "varias": "grupo",
          "todas": "grupo",
          "so uma": "uma"
        }
      }
    },
    "BlocoIntercaladas": {
      "type": "categorical",
      "values": [
        "bloco",
        "intercaladas"
      ],
      "normalize": {
        "synonyms": {
          "juntas": "bloco",
          "sequencia": "bloco",
          "intervaladas": "intercaladas",
          "alternadas": "intercaladas"
        }
      }
    },
    "LuminariaLocalizacao": {
      "type": "categorical",
      "values": [
        "Calçada",
        "Fachada",
        "Monumento",
        "Parque",
        "Praça",
        "Quadra de esportes",
        "Rua",
        null
      ],
      "normalize": {
        "accent_fold": true,
        "synonyms": {
          "calcada": "Calçada",
          "praca": "Praça",
          "quadra": "Quadra de esportes"
        }
      }
    },
    "PontoReferencia": {
      "type": "free_text",
      "optional": true
    },
    "SimNao": {
      "type": "bool",
      "normalize": {
        "affirmation": true,
        "emoji_veto": true
      }
    }
  },
  "slots": {
    "service_confirmed": {
      "domain": "SimNao",
      "persist": "data"
    },
    "luminaria_defeito": {
      "domain": "LuminariaDefeito",
      "persist": "data",
      "required": true,
      "fill_only_when_asked": true,
      "prefill_sources": [
        "whatsapp_flow"
      ],
      "max_attempts": 3,
      "on_exhaust": "reask"
    },
    "luminaria_quantidade": {
      "domain": "Quantidade",
      "persist": "data",
      "required": true,
      "requires": [
        "luminaria_defeito"
      ]
    },
    "luminaria_intercaladas_bloco": {
      "domain": "BlocoIntercaladas",
      "persist": "data",
      "required": true,
      "requires": [
        "luminaria_quantidade"
      ]
    },
    "luminaria_localizacao": {
      "domain": "LuminariaLocalizacao",
      "persist": "data",
      "required": false,
      "fill_only_when_asked": true,
      "prefill_sources": [
        "whatsapp_flow"
      ]
    },
    "reparo_luminaria_quadra_esportes": {
      "domain": "SimNao",
      "persist": "data",
      "required": true,
      "requires": [
        "address"
      ]
    },
    "ponto_referencia": {
      "domain": "PontoReferencia",
      "persist": "data",
      "required": true,
      "requires": [
        "address"
      ]
    },
    "ticket_data_confirmed": {
      "domain": "SimNao",
      "persist": "data"
    }
  },
  "path": [
    {
      "step": "show_service_summary",
      "confirm": "service_confirmed",
      "prompt": {
        "text": "Você quer abrir um chamado de reparo de luminária?"
      },
      "interactive": {
        "kind": "buttons",
        "field": "confirmacao_servico",
        "from_domain": "SimNao"
      },
      "skip_when": {
        "eq": [
          "payload._source",
          "whatsapp_flow"
        ]
      },
      "on_reject": {
        "end": "Entendi, não vou abrir o chamado. Se mudar de ideia é só me chamar."
      }
    },
    {
      "step": "collect_defeito",
      "slot": "luminaria_defeito",
      "prompt": {
        "text": "Qual o problema na luminária?",
        "extract_hint": "one closed LuminariaDefeito token; map numbers/synonyms/accents and infer from a photo if sent"
      },
      "interactive": {
        "kind": "list",
        "field": "luminaria_defeito",
        "from_domain": "LuminariaDefeito"
      }
    },
    {
      "step": "collect_quantidade",
      "slot": "luminaria_quantidade",
      "prompt": {
        "text": "É uma luminária só ou um grupo de luminárias?"
      },
      "ask_when": {
        "in": [
          "slots.luminaria_defeito",
          [
            "Apagada",
            "Piscando",
            "Acesa de dia"
          ]
        ]
      }
    },
    {
      "step": "collect_intercaladas",
      "slot": "luminaria_intercaladas_bloco",
      "prompt": {
        "text": "As apagadas estão em bloco (todas juntas) ou intercaladas?"
      },
      "ask_when": {
        "eq": [
          "slots.luminaria_quantidade",
          "grupo"
        ]
      }
    },
    {
      "step": "collect_localizacao",
      "slot": "luminaria_localizacao",
      "prompt": {
        "text": "Onde fica a luminária? (calçada, fachada, praça, parque, rua...)"
      }
    },
    {
      "use": "address@1"
    },
    {
      "step": "collect_quadra_esportes",
      "confirm": "reparo_luminaria_quadra_esportes",
      "prompt": {
        "text": "O reparo é numa quadra de esportes?"
      },
      "interactive": {
        "kind": "buttons",
        "field": "reparo_luminaria_quadra_esportes",
        "from_domain": "SimNao"
      }
    },
    {
      "step": "collect_reference_point",
      "slot": "ponto_referencia",
      "prompt": {
        "text": "Tem um ponto de referência perto da luminária?"
      }
    },
    {
      "use": "identification@2"
    },
    {
      "step": "confirm_ticket_data",
      "confirm": "ticket_data_confirmed",
      "correctable": true,
      "prompt": {
        "text": "Confirma os dados do chamado?"
      },
      "interactive": {
        "kind": "buttons",
        "field": "confirmacao",
        "from_domain": "SimNao"
      }
    },
    {
      "terminal": true
    }
  ],
  "uses": [
    {
      "ref": "address@1",
      "with": {
        "required": true,
        "needs_confirmation": true,
        "max_attempts": 3
      }
    },
    {
      "ref": "identification@2",
      "with": {
        "required": false,
        "methods": [
          "cpf",
          "govbr",
          "anonimo"
        ],
        "max_attempts": 3,
        "on_exhaust": "default"
      }
    }
  ],
  "derive": [
    {
      "writes": "luminaria_defeito_classificado",
      "from": [
        "luminaria_defeito",
        "luminaria_quantidade",
        "luminaria_intercaladas_bloco"
      ],
      "after": "collect_localizacao",
      "lookup": {
        "Apagada|uma|null": "Apagada",
        "Apagada|grupo|bloco": "Bloco ou grupo de luminárias apagadas",
        "Apagada|grupo|intercaladas": "Várias luminárias intercaladas apagadas",
        "Piscando|uma|null": "Piscando",
        "Piscando|grupo|bloco": "Bloco ou grupo de luminárias piscando",
        "Piscando|grupo|intercaladas": "Bloco ou grupo de luminárias piscando",
        "Acesa de dia|uma|null": "Acesa durante o dia",
        "Acesa de dia|grupo|bloco": "Bloco ou grupo de luminárias acesas de dia",
        "Acesa de dia|grupo|intercaladas": "Várias luminárias intercaladas acesas de dia",
        "Pendurada|null|null": "Pendurada",
        "Danificada|null|null": "Danificada",
        "Com ruído|null|null": "Com ruído"
      },
      "default": "$from[0]"
    }
  ],
  "confirm": {
    "step": "confirm_ticket_data",
    "slot": "ticket_data_confirmed",
    "on_confirm": "open_ticket",
    "prompt": {
      "text": "Confirma os dados do chamado?"
    },
    "interactive": {
      "kind": "buttons",
      "field": "confirmacao",
      "from_domain": "SimNao"
    },
    "correctable": [
      "luminaria_defeito",
      "luminaria_localizacao",
      "address",
      "ponto_referencia",
      "cpf",
      "email",
      "name"
    ]
  },
  "terminal": {
    "step": "open_ticket",
    "tool": "sgrc_open_ticket",
    "idempotent": true,
    "input": [
      {
        "param": "defeitoLuminaria",
        "slot": "luminaria_defeito_classificado"
      },
      {
        "param": "endereco",
        "slot": "address"
      },
      {
        "param": "pontoReferencia",
        "slot": "ponto_referencia"
      },
      {
        "param": "dentroQuadraEsporte",
        "slot": "reparo_luminaria_quadra_esportes"
      },
      {
        "param": "solicitante",
        "slot": "cpf"
      }
    ],
    "outputs": {
      "protocol_id": "result.protocolo"
    },
    "outcomes": {
      "success": {
        "set": {
          "ticket_created": true
        },
        "reset_next": true
      },
      "retryable": {
        "preserve_state": true
      },
      "fatal": {
        "reset_next": true
      }
    },
    "empty_payload": {
      "never_saved": "reset",
      "in_progress": "ignore"
    }
  },
  "overrides": {
    "gates": {
      "collect_quadra_esportes": {
        "or": [
          {
            "eq": [
              "address.kind",
              "praca"
            ]
          },
          {
            "eq": [
              "slots.luminaria_localizacao",
              "Praça"
            ]
          }
        ]
      }
    }
  },
  "auto_flow": {
    "meta_flow_ref": "flow_luminaria_id",
    "send_when": {
      "not": {
        "is_present": "slots.luminaria_defeito"
      }
    },
    "prefill_from": [
      "luminaria_defeito",
      "luminaria_localizacao"
    ],
    "resume_at": "collect_defeito",
    "on_submit_source": "whatsapp_flow",
    "recovery": {
      "fallback_at": "show_service_summary",
      "cancel": "END",
      "timeout": "fallback",
      "max_resends": 2,
      "timeout_seconds": 900
    },
    "alias_map": {
      "defect_type": {
        "luminaria_defeito": "$value"
      },
      "location": {
        "luminaria_localizacao": "$value"
      },
      "qty_pattern": {
        "uma": {
          "luminaria_quantidade": "uma"
        },
        "bloco": {
          "luminaria_quantidade": "grupo",
          "luminaria_intercaladas_bloco": "bloco"
        },
        "intercaladas": {
          "luminaria_quantidade": "grupo",
          "luminaria_intercaladas_bloco": "intercaladas"
        }
      },
      "is_quadra_esportes": {
        "sim": {
          "luminaria_localizacao": "Quadra de esportes"
        }
      }
    }
  },
  "capabilities": {
    "media_in": {
      "analyze": [
        "image",
        "audio",
        "video"
      ],
      "route_by": "workflow_sugerido"
    },
    "media_out": [
      "image",
      "location"
    ],
    "tts": true,
    "location_in": true,
    "handoff": {
      "target": "1746",
      "when": "llm_decides"
    },
    "session_reset": true,
    "await_external": {
      "kind": "cta_url",
      "step": "authenticate_govbr",
      "resume_on": "govbr_token",
      "resume": {
        "version": "1",
        "schema": {
          "$schema": "https://json-schema.org/draft/2020-12/schema",
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "cpf": {
              "type": "string",
              "pattern": "^[0-9]{11}$"
            },
            "nome": {
              "type": "string",
              "minLength": 2
            },
            "email": {
              "type": "string",
              "format": "email"
            }
          },
          "required": [
            "cpf"
          ]
        },
        "correlation": "$token.cpf",
        "duplicate": "ignore",
        "late": "reject"
      },
      "prompt": {
        "text": "Para se identificar pelo gov.br, toque no botão de login que enviei. 🔐",
        "verbatim": true
      },
      "interactive": {
        "kind": "cta_url",
        "field": "govbr_token",
        "out_of_band": true,
        "next_step": "authenticate_govbr"
      },
      "on_resume": {
        "set": {
          "cpf": "$token.cpf",
          "name": "$token.nome",
          "email": "$token.email",
          "govbr_authenticated": true,
          "cadastro_verificado": true
        },
        "enrich": {
          "tool": "get_user_info",
          "optional": true,
          "input": {
            "cpf": "$token.cpf"
          },
          "set": {
            "phone": "$result.phones.0"
          }
        }
      },
      "timeout": {
        "goto": "collect_cpf",
        "set": {
          "identification_method": "cpf"
        }
      },
      "timeout_seconds": 900,
      "recovery": {
        "abort": {
          "goto": "select_identification_method"
        },
        "resend": {
          "goto": "authenticate_govbr"
        },
        "switch": {
          "goto": "collect_cpf",
          "set": {
            "identification_method": "cpf"
          }
        }
      }
    }
  }
}
```

</details>

<!-- /section:example-document -->
<!-- section:example-compiled-langgraph -->

### Compiled LangGraph

`FlowRuntime` compiles the document once into a `StateGraph[ServiceState]`.

<details>
<summary>Generated LangGraph (click to expand)</summary>

```python
from flowspec2 import FlowRuntime, load_flow

rt = FlowRuntime(load_flow("examples/reparo_luminaria.flow.json"))
print(rt.compiled.graph.get_graph().draw_mermaid())
```

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	__init__(<p>__init__</p>)
	show_service_summary(show_service_summary)
	collect_defeito(collect_defeito)
	collect_quantidade(collect_quantidade)
	collect_intercaladas(collect_intercaladas)
	collect_localizacao(collect_localizacao)
	derive_luminaria_defeito_classificado(derive_luminaria_defeito_classificado)
	collect_address(collect_address)
	confirm_address(confirm_address)
	address_done(address_done)
	collect_quadra_esportes(collect_quadra_esportes)
	collect_reference_point(collect_reference_point)
	select_identification_method(select_identification_method)
	authenticate_govbr(authenticate_govbr)
	collect_cpf(collect_cpf)
	collect_email(collect_email)
	collect_name(collect_name)
	identification_done(identification_done)
	confirm_ticket_data(confirm_ticket_data)
	open_ticket(open_ticket)
	__end__([<p>__end__</p>]):::last
	__init__ -.-> __end__;
	__init__ -.-> show_service_summary;
	__start__ --> __init__;
	address_done -.-> __end__;
	address_done -.-> collect_quadra_esportes;
	authenticate_govbr -.-> __end__;
	authenticate_govbr -.-> collect_cpf;
	authenticate_govbr -.-> collect_email;
	authenticate_govbr -.-> collect_name;
	authenticate_govbr -.-> identification_done;
	collect_address -.-> __end__;
	collect_address -.-> address_done;
	collect_address -.-> confirm_address;
	collect_cpf -.-> __end__;
	collect_cpf -.-> collect_email;
	collect_cpf -.-> collect_name;
	collect_cpf -.-> identification_done;
	collect_defeito -.-> __end__;
	collect_defeito -.-> collect_quantidade;
	collect_email -.-> __end__;
	collect_email -.-> collect_name;
	collect_email -.-> identification_done;
	collect_intercaladas -.-> __end__;
	collect_intercaladas -.-> collect_localizacao;
	collect_localizacao -.-> __end__;
	collect_localizacao -.-> derive_luminaria_defeito_classificado;
	collect_name -.-> __end__;
	collect_name -.-> identification_done;
	collect_quadra_esportes -.-> __end__;
	collect_quadra_esportes -.-> collect_reference_point;
	collect_quantidade -.-> __end__;
	collect_quantidade -.-> collect_intercaladas;
	collect_reference_point -.-> __end__;
	collect_reference_point -.-> select_identification_method;
	confirm_address -.-> __end__;
	confirm_address -.-> address_done;
	confirm_address -.-> collect_address;
	confirm_ticket_data -.-> __end__;
	confirm_ticket_data -.-> collect_address;
	confirm_ticket_data -.-> collect_cpf;
	confirm_ticket_data -.-> collect_defeito;
	confirm_ticket_data -.-> collect_email;
	confirm_ticket_data -.-> collect_localizacao;
	confirm_ticket_data -.-> collect_name;
	confirm_ticket_data -.-> collect_reference_point;
	confirm_ticket_data -.-> open_ticket;
	derive_luminaria_defeito_classificado -.-> __end__;
	derive_luminaria_defeito_classificado -.-> collect_address;
	identification_done -.-> __end__;
	identification_done -.-> confirm_ticket_data;
	open_ticket -.-> __end__;
	select_identification_method -.-> __end__;
	select_identification_method -.-> authenticate_govbr;
	select_identification_method -.-> collect_cpf;
	select_identification_method -.-> identification_done;
	show_service_summary -.-> __end__;
	show_service_summary -.-> collect_defeito;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

</details>

The solid edge is `__start__ → __init__`. Dotted synthesized routers pause at
`__end__` when `agent_response` is set—one question per invocation—and otherwise
advance. The
`confirm_ticket_data` fan-out contains one correction back-edge per
`confirm.correctable[]` slot. Mermaid expands the internal nodes and cycles of
`address@1` and `identification@2`; canonical IR keeps each subflow at its
logical `use` anchor.

<!-- /section:example-compiled-langgraph -->
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
examples/          reparo_luminaria.flow.json · reparo_buraco.flow.json
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
[SECURITY.md](SECURITY.md); use the issue forms for public bugs and proposals.

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
