<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 45 <!-- section:decision -->
- Consequences: 80 <!-- section:consequences -->
    - What becomes easier: 84 <!-- section:consequences-easier -->
    - What becomes harder: 100 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0005 — Authoring acceptance semantics

**Status:** Accepted
**Date:** 2026-07-14

<!-- section:context -->

## Context

The authoring benchmark originally supplied a natural-language task while
grading successful compilations against a private complete normalized source.
That rejected unrelated behavior, but it also graded arbitrary identifiers,
prompt prose, defaults, lifecycle policies, mappings, and other values the task
did not specify. A model discovered only the first private difference per repair
round, so a bounded correction loop could fail despite producing a valid flow
that satisfied every visible requirement.

Exposing the reference source would turn the FlowSpec2 case into a copying test
and would bias candidate source adapters toward FlowSpec2 syntax. Removing the
closed comparison would accept extra slots, steps, effects, and capabilities.
Maintaining hand-written prose and a separate private oracle would retain the
same drift risk because no machine check could prove that every graded dimension
was visible to the author.

The benchmark needs a source-syntax-free contract that is complete enough to
reject additions, explicit about intentionally flexible presentation choices,
and identical at provider prompting, evaluation, evidence replay, and installed
corpus boundaries.

<!-- /section:context -->
<!-- section:decision -->

## Decision

Each author-facing task carries a versioned public semantic acceptance contract.
The packaged loader normalizes its private fixture source, replaces internal
domain names with their referenced contracts, replaces step names with semantic
positions, and projects every resulting scalar and empty container into exact
JSON Pointer observations. Required and forbidden constructs are included as
public focused constraints.
Flow identity, version labels, route prose, and non-verbatim path prompt text
are listed as explicit variable pointer paths and are excluded from exact-value
grading.

Evaluation checks source-policy violations before lowering, runs structural,
semantic, profile, and compilation checks, then compares the compiled flow only
with the public observations. Missing, mismatched, and unexpected observations
are all emitted in one deterministic diagnostic set. Focused required-construct
diagnostics suppress only overlapping generic observations, so unrelated
behavior cannot hide behind a satisfied wildcard requirement.

Provider prompts serialize the same public contract and never the fixture
source. Evidence replay reconstructs the contract from the content-addressed
installed corpus. The authoring case, corpus, Gemini prompt, Codex prompt, and
evidence-envelope identifiers advance together because earlier evidence used
different grading semantics and is not directly comparable as model-quality
evidence.

This gate is blocking immediately. It affects only a versioned, deterministic,
offline-replayable benchmark, not runtime execution or persisted user state.
The prior private grader already blocked failures; the replacement removes
hidden dimensions and has exact regression fixtures, so a report-only trial
would preserve the known unfair result without reducing false-positive risk.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- A model can inspect every value that can affect benchmark success before its
  first attempt.
- One repair response contains every observed semantic divergence instead of
  serial discovery through the correction limit.
- New normalized fields automatically become visible grading dimensions unless
  the case explicitly classifies them as variable.
- Alternative source adapters are compared through executable semantics rather
  than private FlowSpec2 source equality.
- Unrelated domains, slots, steps, effects, and capabilities remain rejectable
  through unexpected observations.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Provider prompts are larger because the complete semantic contract is public.
- Corpus and normalizer evolution can change acceptance observations and must
  advance the appropriate versioned contracts and evidence expectations.
- Semantic equivalences beyond the canonicalized domain and step identities
  still require an explicit observation rule before the benchmark treats them
  as interchangeable.
- Earlier and current authoring success rates cannot be compared without naming
  their different corpus and prompt contracts.
- Natural-language quality of route and non-verbatim prompt prose remains
  outside exact deterministic grading and needs separate trace or human review
  when it becomes a promotion criterion.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
