<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 43 <!-- section:decision -->
- Consequences: 73 <!-- section:consequences -->
    - What becomes easier: 77 <!-- section:consequences-easier -->
    - What becomes harder: 91 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0006 — Authoring presentation review

**Status:** Accepted
**Date:** 2026-07-14

<!-- section:context -->

## Context

[ADR 0005](0005-authoring-acceptance-semantics.md) excludes route prose and
non-verbatim prompt wording from exact authoring acceptance. Those fields are
author-controlled presentation choices: comparing them with fixture text would
grade imitation instead of executable semantics, while ignoring their quality
entirely would allow unclear, misleading, or over-broad citizen-facing text to
support a format-promotion claim.

A deterministic text oracle cannot express clarity without prescribing one
wording. A model-based judge would instead measure the candidate text together
with the judge model, its prompt, and its changing behavior. Behavioral routing
and extraction traces are useful deployment observations, but they also depend
on the selected model, service catalog, and simulated citizen. They cannot be
replayed offline as properties of the authored source alone.

The project therefore needs an explicit review boundary that acknowledges
human judgment, exposes a stable rubric, binds every decision to the exact
candidate source, and remains separate from deterministic semantic success.

<!-- /section:context -->
<!-- section:decision -->

## Decision

Authoring presentation quality uses a separate, content-addressed review
artifact. The artifact binds to a verified authoring-evidence digest and covers
the route description and every author-owned non-verbatim prompt from each
final semantically successful capture. Each subject is identified by its case,
repair round, source digest, JSON Pointer, and text digest.

A fixed public rubric evaluates scope, standalone meaning, requested-action
clarity, input-contract alignment, interaction context, and claim fidelity as
applicable to each subject kind. Every criterion carries an explicit decision
and rationale. Overall success is derived mechanically; reviewers cannot enter
an independent aggregate result. Missing, extra, duplicate, or stale subjects
invalidate the review.

The review artifact never contains fixture source or reference presentation
text. A reviewer packet may expose only the candidate text, public task,
acceptance contract, and relevant public interaction context. Detached Ed25519
authentication uses a caller-supplied trusted public key, following the trust
model of [ADR 0004](0004-evidence-authenticity.md).

Presentation review begins report-only. It becomes a blocking format-promotion
requirement after controlled reviews demonstrate that the rubric is complete,
reviewers can apply it without unresolved interpretation differences, and the
subject extractor remains stable across representative successful evidence.
Deterministic benchmark success remains unchanged in either mode.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Format promotion can account for citizen-facing clarity without prescribing
  reference wording or weakening semantic acceptance.
- Every judgment is attributable to an exact source and can be authenticated
  independently of model-generated evidence.
- A failed presentation review can trigger a new authoring run without
  rewriting or mutating the original benchmark evidence.
- Deployment-specific behavioral traces can evolve as separate evidence
  without changing the deterministic authoring protocol.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Format-promotion evidence now has a human-review lifecycle in addition to
  deterministic replay.
- Reviewer identity and trusted-key distribution remain operator
  responsibilities outside the artifact.
- Rubric changes require a new version and make earlier presentation decisions
  non-comparable unless their rubric identity is named.
- Human agreement is observable rather than guaranteed; unresolved rubric
  ambiguity prevents promotion to blocking enforcement.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
