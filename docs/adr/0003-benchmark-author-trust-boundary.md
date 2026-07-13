<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 37 <!-- section:decision -->
- Consequences: 61 <!-- section:consequences -->
    - What becomes easier: 65 <!-- section:consequences-easier -->
    - What becomes harder: 79 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0003 — Benchmark author trust boundary

**Status:** Accepted
**Date:** 2026-07-13

<!-- section:context -->

## Context

The AI-authoring benchmark keeps reference flows, required constructs, and
forbidden constructs beside the task prompt in each evaluator case. A transport
must receive the prompt and repair diagnostics without gaining accidental access
to those evaluation oracles. Omitting oracle text while passing the complete
case object still leaves answers reachable through ordinary object inspection
and makes the benchmark depend on transport restraint rather than structure.

Provider model names are also commonly aliases. Recording only the configured
model can make two runs appear comparable even when the provider routes them to
different effective versions. Evidence needs the identity returned with each
response while preserving the requested configuration separately.

<!-- /section:context -->
<!-- section:decision -->

## Decision

`AuthoringBenchmarkCase` remains evaluator-private. Before invoking an author,
the harness projects it to a closed `AuthoringTask` containing only the stable
task identifier and prompt. `AuthoringRequest` carries that projection, the
source-format and runtime-profile contracts, prior source, repair diagnostics,
and correction round. It never carries the case, reference flow, evaluator
assertions, or feature tags.

Authors return `AuthoredSource`, which binds the exact source to an optional
provider-reported effective model version. The Gemini transport requires the
provider response to supply a non-empty effective version and fails closed when
it is unavailable. The evidence envelope records that version for every Gemini
capture while retaining the configured model alias in provider provenance.

The evidence contract advances to
`flowspec2/authoring-benchmark-evidence@2`. Artifacts using the prior shape are
not silently interpreted as the new contract. Oracle-reachability and missing
effective-version failures are blocking immediately because either condition
invalidates the provenance of a benchmark run.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Reviewers can audit the author-facing request type instead of proving that
  every transport ignores evaluator-only fields.
- New author transports share one typed response contract for exact source and
  effective model identity.
- Evidence distinguishes requested aliases from the model version that actually
  generated each attempt.
- Accidental oracle exposure and incomplete Gemini provenance fail before an
  evidence artifact is accepted.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Author integrations must return `AuthoredSource` instead of a bare string.
- Provider adapters must extract effective-version metadata from their response
  contract and cannot substitute the requested alias.
- Evidence consumers must explicitly recognize the new envelope format.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
