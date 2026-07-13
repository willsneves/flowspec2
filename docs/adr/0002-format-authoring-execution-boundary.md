<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 44 <!-- section:decision -->
- Consequences: 96 <!-- section:consequences -->
    - What becomes easier: 100 <!-- section:consequences-easier -->
    - What becomes harder: 120 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0002 — Format authoring and execution boundary

**Status:** Accepted
**Date:** 2026-07-13

<!-- section:context -->

## Context

FlowSpec2 source documents are authored by humans and language models, while the
runtime needs explicit defaults, resolved references, typed integration
contracts, stable node identities, and reproducible state access. Treating the
same JSON object as both concise source and compiler representation forced
defaults into scattered runtime code and allowed structurally valid documents to
reach compilation with ignored gates, unresolved anchors, invalid embedded
schemas, or host capabilities that were not installed.

JSON Schema remains necessary for shape validation, but it cannot prove
cross-document references, dependency order, subflow exposure, tool result paths,
or executability against a particular host. Compiling as the first semantic check
also reports one failure at a time, which is inefficient for AI correction loops.

The alternatives were to make source fully explicit and verbose, keep compiler
defaults and first-error validation, introduce a separate authoring format
immediately, or place a deterministic normalization and linking boundary between
source and execution. A new stable syntax without comparative model evidence
would optimize for assumptions about AI authoring rather than measured outcomes.

<!-- /section:context -->
<!-- section:decision -->

## Decision

FlowSpec source remains the only human- and AI-authored artifact. Before
execution, it passes through closed structural validation, semantic linking, and
a named runtime profile, then becomes a canonical intermediate representation.
The representation materializes effective defaults and node identifiers and
indexes state contracts, reads, writes, references, transitions, required
capabilities, generated state schema, and stable content digests. It carries a
versioned IR-format identity that participates in the execution digest.

A runtime profile is the executability boundary. It names the domain kinds, host
capabilities, versioned subflows, and versioned tools available to a flow. Tool
contracts include input and output JSON Schemas plus effect metadata; subflow
contracts include configuration schema, exact exposed slot schemas, persistent
state ownership, stable node identifiers, transitive tool versions, and required
capabilities. External waits add a versioned token schema, correlation identity,
duplicate/late-delivery policy, and a persisted absolute timeout deadline.
Unknown, legacy-without-opt-in, unsafe, or inconsistent bindings are rejected
before graph construction.

The checker emits every deterministic structural, semantic, and profile finding
with stable codes and JSON Pointer locations. Compilation runs only after those
checks are clean. Source normalization and inspection are read-only CLI surfaces,
and the original document is never mutated.

Authoring evaluation uses a versioned corpus distributed with the package and
pinned by an integrity manifest. A provider receives the normative source schema
and exact runtime-profile contract but never the reference answer or evaluation
oracle. External runs require explicit network opt-in and produce one canonical,
content-addressed evidence envelope binding exact emitted sources to the report,
corpus, profile, model configuration, prompt identity, package version, and
operator-supplied repository revision. Credentials and nondeterministic
operational metadata are excluded.

Persisted state is bound to the exact flow revision plus IR format, IR,
dependency, and profile digests. Direct restore fails closed on drift. Any
active-state migration is a separate declarative operation with pinned source
and target provenance, closed partition-member transformations, target-schema
validation, and a verifiable loss report; runtime execution never infers a
migration.

Referential errors and closed step contracts are blocking immediately because
they have deterministic answers, no external side effects, and correspond to
behavior that was already invalid or silently ignored. The alternate
`flowspec/3-draft` source is report-only and cannot execute. It may become a
stable format only after the provider-neutral authoring benchmark is run with
real target models and shows better valid-task completion and correction behavior
without semantic loss across the required scenario corpus.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- AI correction loops receive all actionable errors in one deterministic report
  rather than discovering references through repeated compiler failures.
- Runtime behavior no longer depends on implicit JSON object order or scattered
  default reads; canonical digests support caching, provenance, and reproducible
  comparisons.
- A flow can be checked against a deployment profile before graph construction,
  including exact tool, subflow, domain, and host-capability availability.
- Tool inputs, tool outputs, subflow configuration, state access, and generated
  state schemas are inspectable contracts rather than conventions hidden in
  runtime functions.
- External resume payloads, replay decisions, state migration, and restored-state
  compatibility become deterministic, auditable boundary operations.
- Alternate source formats can be compared through one benchmark and lower to
  the same executable contract without weakening the stable runtime.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Every new domain kind, tool, subflow, or host feature must ship profile metadata
  and linking coverage in addition to runtime code.
- Source-schema, normalizer, linker, IR, compiler, and documentation changes must
  remain synchronized; a field is incomplete until every relevant layer consumes
  or rejects it.
- Compatibility adapters must construct explicit conservative profiles for
  external actions instead of relying on arbitrary callable names.
- Consumers that bypass the checker can still create structurally shaped objects,
  so public tooling and integrations must use the check-before-compile contract.
- The experimental concise syntax incurs maintenance cost while evidence is
  gathered, and it cannot be advertised as a replacement merely because a fixture
  is shorter.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
