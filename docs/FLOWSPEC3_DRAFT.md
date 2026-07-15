<!-- section:toc -->

Table of Contents:

- Status and purpose: 22 <!-- section:status-purpose -->
- Preview shape: 44 <!-- section:preview-shape -->
- Migration contract: 88 <!-- section:migration-contract -->
- Lowering contract: 120 <!-- section:lowering-contract -->
- Loss accounting: 159 <!-- section:loss-accounting -->
- Non-goals: 198 <!-- section:non-goals -->
- Promotion boundary: 212 <!-- section:promotion-boundary -->

<!-- /section:toc -->

# flowspec/3-draft preview

The preview tests a more local and AI-oriented authoring surface without
changing the stable `flowspec/2` compiler or runtime.

<!-- section:status-purpose -->

## Status and purpose

`flowspec/3-draft` is experimental, non-executable, and excluded from
`FlowRuntime`. Its structural schema is closed except for the JSON Schema under
`route.entry_args_schema`, versioned subflow configuration under `use.with`,
and source fragments deliberately preserved under `v2_passthrough`. Subflow
configuration belongs to the selected runtime profile. The schema and migrator
exist so proposed authoring changes can be measured against stable v2 sources
before a format decision is made.

`v2_passthrough` is not an authoring extension point. Normal preview validation
rejects it; only the explicit v2-migration validation mode accepts the
loss-accounting artifact emitted by the migrator.

The preview explores locality: a reader sees a slot's domain, presentation,
collection contract, and routing references at the step that uses them. This is
a hypothesis to evaluate, not a claim that inline structure is universally
shorter or easier for models.

<!-- /section:status-purpose -->
<!-- section:preview-shape -->

## Preview shape

The closed Draft 2020-12 schema keeps service identity, route metadata, shared
configuration, and an ordered `steps` array. Its step union includes:

- `collect` with an inline domain and slot contract;
- `use` with versioned subflow configuration;
- `confirm` with typed correction and target references;
- `derive` with typed source references and a closed lookup;
- `submit` with typed input and result references;
- `await` with localized suspension, resume mapping, and recovery.

Categorical and boolean options separate canonical `value`, citizen-facing
`label`, input `aliases`, and optional description. References are single-key
objects. `$slot` addresses an authored or profile-owned slot, while `$derive`
addresses a derived state value; `$token` and `$result` are confined to their
external-call boundaries. Predicates accept only state/configuration
namespaces, never result or resume-token namespaces. Terminal result paths are
object-only; enrichment result and token paths may address array members.
Ambiguous reference objects are structurally invalid.

Categorical `options` contain only renderable strings. Canonical null input and
its aliases are represented separately by `accepts_null` and `null_aliases`, so
null acceptance cannot accidentally become a blank UI option. Numeric domains
preserve inclusive bounds. Interactive UI is a discriminated union: choice,
Meta Flow, and out-of-band CTA variants expose only fields used by that kind.
Collected slots persist in durable `data` or `internal` state. `$payload`
remains available as a read-only predicate namespace, but it is not a slot
persistence target because ephemeral values cannot survive a conversational
pause.

`check_v3_preview` aggregates structural or semantic diagnostics. The semantic
pass links step, slot, and derive references; checks producer order, unique
writers and flow-level constructs; validates forward confirmation routing,
await bindings, numeric ranges, option/alias ambiguity, conditional options,
and conflicting state writes. `validate_v3_preview` raises on either class of
violation.

The authoritative preview schema is
[`flowspec-3-draft.schema.json`](../src/flowspec2/experimental/flowspec-3-draft.schema.json).

<!-- /section:preview-shape -->
<!-- section:migration-contract -->

## Migration contract

`migrate_v2_to_v3_preview` accepts a structurally valid, semantically linked v2
mapping and returns a fresh preview mapping.
`migrate_v2_to_v3_preview_report` additionally returns an immutable report with
the migrated document, typed loss entries for original JSON Pointer locations
that lack native preview syntax, and neutral compact-byte measurements.

```python
from flowspec2 import load_flow
from flowspec2.experimental import migrate_v2_to_v3_preview_report

source = load_flow("examples/reparo_luminaria.flow.json")
report = migrate_v2_to_v3_preview_report(source)

preview = report.preview_document
loss_entries = report.loss_entries
measurements = report.compact_bytes.as_dict()
```

The migrator validates and semantically links the v2 input, performs no
external I/O or writes, does not mutate the input, emits canonical deterministic
output, and validates the resulting preview. Preview validation lazily reads
the packaged schema asset. A caller receives a fresh document each time it
reads `preview_document`.

Migration remains analytical. A preview document is not accepted by the stable
compiler or runtime.

<!-- /section:migration-contract -->
<!-- section:lowering-contract -->

## Lowering contract

`lower_v3_preview_to_v2` deterministically converts the supported preview
subset into a fresh v2 mapping. `lower_v3_preview_to_v2_report` additionally
returns the rehydrated source paths and the complete v2 check report.

```python
from flowspec2.experimental import lower_v3_preview_to_v2_report

lowering = lower_v3_preview_to_v2_report(preview)
source = lowering.source_document
rehydrated_paths = lowering.rehydrated_paths
flow_check = lowering.flow_check
```

Before returning, the lowerer validates the preview in the appropriate
authoring or migration mode, rejects every loss entry whose policy handling is
`reject`, rehydrates only `compatibility_only` entries at their policy-bound
source paths, and runs the complete v2 structural, semantic, selected-profile,
and compilation checks. It then migrates the reconstructed v2 source back to
the preview and requires exact document equality. A caller receives a fresh
document each time it reads `source_document`.

The proof is a preview fixed point: `v3 preview -> v2 -> v3 preview`. It does not
claim reconstruction of the original v2 spelling, declaration ownership,
domain sharing, or canonical IR identity. The lowerer synthesizes deterministic
domain names because those source-level choices are intentionally absent from
the preview surface.

Valid preview features without an exact v2 representation fail with stable
lowering diagnostics. This includes custom option labels, flow UI, composite or
ambiguous call bindings, non-string derive outputs, nested configuration or
payload references, non-canonical aliases, and previews that cannot declare a
v2 domain. The supported boundary and proof obligation are recorded in
[ADR 0009](adr/0009-v3-preview-lowering-boundary.md).

<!-- /section:lowering-contract -->
<!-- section:loss-accounting -->

## Loss accounting

Source fragments without safe native preview semantics are copied into ordered
`v2_passthrough.entries`. Each entry carries its original RFC 6901 JSON Pointer,
a closed category, the policy disposition, and an exact source fragment. The
immutable migration report exposes the same typed entries and returns a fresh
fragment copy to callers. Source paths are ordered and unique, so omission and
repeated classification cannot be hidden by map overwrite. The packaged
[`v3-preview-loss-policy.json`](../src/flowspec2/experimental/v3-preview-loss-policy.json)
is authoritative for category disposition, lowerer handling, and the allowed
source-path pattern for each category.

`compatibility_only` entries are eligible for deterministic rehydration;
`excluded` entries require rejection. Rehydration does not establish trust or
equivalence by itself. The lowerer validates, semantically links, profile-checks,
and compiles the reconstructed v2 source before accepting it. The loss-policy
decision is recorded in [ADR 0008](adr/0008-v3-preview-loss-accounting.md).

The migrator localizes an external wait only when the v2 path explicitly owns
its marker. A capability supplied implicitly by a subflow remains passthrough
because v2 does not declare enough ownership information to infer the correct
local step safely. Entry initialization, automatic WhatsApp Flow behavior, and
other source fragments also remain passthrough until the preview gives them an
explicit native contract. Legacy bare-name await enrichment and ignored
interactive environment gates are classified as excluded rather than promoted
into native v3 fields. The migrator recognizes the removed interactive gate
only to preserve its original pointer, rejected category, and value in loss
accounting. Localized awaits also account for the v2 resume lifecycle contract
and timeout duration until native preview fields can represent them. A v2 slot
explicitly persisted to ephemeral payload state is rejected as unmigratable
rather than copied into a contract that cannot preserve it.

Byte deltas are measurements, not a success criterion. Any comparison must
inspect loss entries and semantic equivalence before interpreting source
size.

<!-- /section:loss-accounting -->
<!-- section:non-goals -->

## Non-goals

The preview does not:

- replace or loosen the stable v2 schema;
- register a compiler, runtime, CLI execution path, or compatibility profile;
- infer missing subflow ownership or silently discard source behavior;
- introduce scripts, arbitrary expressions, loops, parallel branches, or
  manual graph transitions;
- claim improved model performance from fixture migration or byte size.

<!-- /section:non-goals -->
<!-- section:promotion-boundary -->

## Promotion boundary

Promotion requires the evidence and provenance described in
[AUTHORING_BENCHMARK.md](AUTHORING_BENCHMARK.md), an explicit decision for every
passthrough category, broader lowering coverage with an appropriate semantic
equivalence argument, and the same or stronger profile, tool, subflow,
lifecycle, and runtime guarantees as v2. The analytical lowerer proves only its
documented preview fixed point over the supported subset; it is evidence for
promotion, not promotion itself.

If those conditions are met, stabilization is a new format decision with its
own compatibility and migration policy. The preview identifier must never be
silently reinterpreted as a stable version.

<!-- /section:promotion-boundary -->
