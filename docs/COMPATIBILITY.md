<!-- section:toc -->

Table of Contents:

- Conversion policy: 25 <!-- section:conversion-policy -->
- Python API: 48 <!-- section:python-api -->
- CLI: 83 <!-- section:cli -->
- Rasa CALM portable profile: 100 <!-- section:rasa-calm-profile -->
- Open Workflow conversational profile: 166 <!-- section:open-workflow-profile -->

<!-- /section:toc -->

# Compatibility profiles

flowspec2 remains the canonical executable document. Compatibility adapters are
bounded projections: they either preserve every rail in the declared profile or
return a complete diagnostic report explaining why they cannot.

The architectural boundary is recorded in
[ADR 0001](adr/0001-interoperability-boundaries.md), and the systems considered
before selecting these adapters are listed in [PRIOR_ART.md](PRIOR_ART.md).

<!-- section:conversion-policy -->

## Conversion policy

Every conversion returns a frozen `ConversionOutcome` containing the artifact
and a `CompatibilityReport`. Each diagnostic has a stable code, severity,
source path, and message.

- Errors always block conversion.
- Rasa warnings represent a documented loss and also block by default, so a
  lossy artifact is never produced accidentally.
- Rasa's `allow_lossy=True` or `--allow-lossy` permits documented warnings only.
  It never overrides an error.
- An Open Workflow warning can describe a native projection that was omitted
  while the complete value remains preserved in profile metadata. Such a
  warning is displayed but does not make the profile round trip lossy.
- Converters aggregate all findings before raising `CompatibilityError`.
- YAML input uses the YAML 1.2 core scalar rules, safe loading, and duplicate-key
  rejection.
- CLI diagnostics include a Snowflake `log_id` that matches their structured log
  record.

<!-- /section:conversion-policy -->
<!-- section:python-api -->

## Python API

```python
from flowspec2.compat import export_rasa, import_rasa
from flowspec2.compat import export_open_workflow, import_open_workflow
from flowspec2.compat import official_open_workflow_schema
from flowspec2.compat import validate_official_open_workflow

rasa_outcome = export_rasa(flow_document, allow_lossy=True)
rasa_flows = rasa_outcome.artifact.flows
rasa_domain = rasa_outcome.artifact.domain

flow_outcome = import_rasa(
    rasa_flows,
    rasa_domain,
    flow_id="collect_contact",
    flow_version="1.0.0",
    allow_lossy=True,
)

workflow_outcome = export_open_workflow(flow_document)
validate_official_open_workflow(workflow_outcome.artifact)
round_trip_outcome = import_open_workflow(workflow_outcome.artifact)

official_schema = official_open_workflow_schema()
```

Diagnostic tuples are immutable. `RasaBundle` returns defensive document copies;
mapping artifacts returned by the other conversions are fresh deep-owned values,
so callers can edit them without mutating the input document or a converter
cache.

<!-- /section:python-api -->
<!-- section:cli -->

## CLI

```bash
flowspec2 rasa-export path/to/portable.flow.json --output-dir build/rasa --allow-lossy
flowspec2 rasa-import build/rasa/flows.yml --domain build/rasa/domain.yml --flow collect_contact --version 1.0.0 --output build/collect_contact.flow.json --allow-lossy
flowspec2 open-workflow-export examples/reparo_luminaria.flow.json --output build/reparo_luminaria.workflow.yaml
flowspec2 open-workflow-import build/reparo_luminaria.workflow.yaml --output build/reparo_luminaria.flow.json
```

The CLI refuses to overwrite an existing file or Rasa output directory. It
writes only after parsing, validation, conversion, and policy enforcement have
all succeeded. Diagnostics are emitted on standard error with their correlated
Snowflake log IDs.

<!-- /section:cli -->
<!-- section:rasa-calm-profile -->

## Rasa CALM portable profile

[Rasa CALM flows](https://rasa.com/docs/reference/primitives/flows/) are the
closest conversational peer. The adapter deliberately covers the shared linear
collection core rather than claiming equivalence with every Rasa or flowspec2
construct.

The public profile identifier is `rasa-calm/1`. It targets Rasa Pro `3.11.0` or
newer because the profile relies on flow-level `persisted_slots`, introduced in
that release according to the official
[Rasa Pro changelog](https://rasa.com/docs/reference/changelogs/rasa-pro-changelog/).
Generated domain documents declare version `3.1`.

| flowspec2 | Rasa CALM |
|---|---|
| `flow` and `route.description` | flow id and `description` in `flows.yml` |
| linear `path` slot | `collect` step |
| categorical domain | categorical domain slot and values |
| ordinary boolean slot | bool domain slot and `collect` |
| free-text domain | text domain slot |
| slot prompt | `utter_ask_<flow>_<slot>` response |
| button options | response buttons whose payload preserves the closed token |
| LLM extraction boundary | slot mapping `type: from_llm` |

The portable profile requires a linear collection path, locally declared
slots/domains, unique non-empty explicit step ids, every collected slot in
`persisted_slots`, and `run_pattern_completed: false`. The last constraint
prevents Rasa's default completion pattern from adding behavior that flowspec2
does not define. Each collect uses one non-empty, unconditional `utter_*`
response. Static buttons must cover the domain in declared order and use one
`/SetSlots(slot=value)` assignment. Curly braces are rejected because Rasa
interprets them as response interpolation.

flowspec2 `confirm` and Rasa `ask_before_filling: true` are hard errors, not a
mapping: their prefill, clearing, retry, and exhaustion semantics differ.
Control-flow gates, derived values, correction back-edges, subflow splicing,
external waits, WhatsApp Flow behavior, and non-portable validation contracts
also block conversion. One final custom action can be projected as a terminal
only with an explicit adapter warning and conservative imported lifecycle
defaults.

Rasa `from_llm` collection can fill future slots and correct an already-filled
slot outside the active step. Its default collect processing also accepts
interruption and repair commands during collection, as documented under
[suppressing interruptions](https://rasa.com/docs/reference/primitives/flow-steps/#suppressing-interruptions).
flowspec2 collection is sequential and correction and transition routing are
explicit, so both Rasa import and export report
`RASA_LLM_SLOT_SEMANTICS_UNSUPPORTED`. Export also reports
`RASA_FLOW_VERSION_UNSUPPORTED` because `flows.yml` has no canonical flowspec2
version field. These warnings make `allow_lossy=True` or `--allow-lossy`
necessary for the collection profile; the opt-in never permits a hard error.

Import accepts one selected flow plus its domain document. It supports the
portable `collect` representation and rejects arbitrary branching, calls,
links, and other Rasa steps instead of guessing at their semantics. Imported
documents are validated against both the flowspec2 schema and compiler before
return.

After export, validate the generated project files in the target Rasa
environment with the official `rasa data validate flows` command. Rasa is not a
flowspec2 dependency, so this external validation is intentionally separate from
the offline project suite.

<!-- /section:rasa-calm-profile -->
<!-- section:open-workflow-profile -->

## Open Workflow conversational profile

The profile targets [Open Workflow Specification](https://serverlessworkflow.io/)
DSL `1.0.3` and declares this stable identifier:

```text
https://wllsena.github.io/flowspec2/profiles/open-workflow-conversation-1
```

The exported workflow stores every non-path flowspec2 section in profile
metadata and exposes each path entry as an ordered custom
`call: flowspec2.<kind>` task. The task `with.step` contains the complete path
step and `metadata.path_index` records its position, allowing generic workflow
tooling to inspect sequence while a profile-aware host retains the complete
conversational contract.

This is a lossless envelope, not a claim that every Open Workflow runtime can
execute a conversation. A runtime needs handlers for the `flowspec2.*` custom
calls. Import and export validate against both the verified official workflow
schema and the narrower bundled profile schema. Import accepts only documents
that declare the exact profile, retain the canonical native `input` projection
when the preserved entry schema is valid, remain consistent with the embedded
flowspec2 document, and pass the flowspec2 compiler. Arbitrary Open Workflow
documents are rejected rather than inferred. A document may therefore be valid
Open Workflow while remaining outside the conversational profile.

The exact upstream schema is vendored as
[`open-workflow-1.0.3.workflow.yaml`](../src/flowspec2/compat/schemas/vendor/open-workflow-1.0.3.workflow.yaml)
from the official specification commit recorded in
[`open-workflow-1.0.3.provenance.json`](../src/flowspec2/compat/schemas/vendor/open-workflow-1.0.3.provenance.json).
The manifest pins the schema and license SHA-256 digests; the loader verifies
both before caching the schema. The upstream Apache license is packaged beside
them as
[`open-workflow-1.0.3.LICENSE`](../src/flowspec2/compat/schemas/vendor/open-workflow-1.0.3.LICENSE).
All schema references are local fragments, so validation remains offline.

JSON, YAML, and YML files use the same in-memory mapping API. Open Workflow
export does not need `allow_lossy`: structural incompatibility is an error and a
valid profile round trip preserves the original flowspec2 document. If a
preserved `entry_args_schema` is not itself a valid JSON Schema, export omits the
native Open Workflow input projection, keeps the original value in metadata,
and reports `open_workflow.entry_schema_omitted`.

<!-- /section:open-workflow-profile -->
