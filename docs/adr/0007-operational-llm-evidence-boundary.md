<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 38 <!-- section:decision -->
- Consequences: 59 <!-- section:consequences -->
    - What becomes easier: 63 <!-- section:consequences-easier -->
    - What becomes harder: 76 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0007 — Operational LLM Evidence Boundary

**Status:** Accepted
**Date:** 2026-07-14

<!-- section:context -->

## Context

FlowSpec2 deliberately separates deterministic rails from model-mediated behavior. The
authoring benchmark proves that a candidate source satisfies the public structural and
semantic contract, while the presentation review records attributable human judgments about
citizen-facing prose. Neither mechanism demonstrates that a particular model configuration
routes citizen utterances with `route.trigger_phrases` or extracts closed values with
`prompt.extract_hint`.

Treating live-model outcomes as deterministic format conformance would make acceptance depend
on provider drift, sampling behavior, and model availability. Extending the presentation review
would also mix human prose judgments with executable model observations. Conversely, leaving
these fields without attributable execution evidence makes their operational value impossible
to audit.

<!-- /section:context -->
<!-- section:decision -->

## Decision

Operational routing and extraction observations use a separate, canonical, content-addressed
evidence artifact. The artifact binds a verified authoring-evidence source closure to a packaged
operational corpus, the exact model and generation configuration, the rendered request contract,
raw provider output, parsed semantic output, and a replayable report.
Each subject is evaluated through a controlled authored/counterfactual pair: operation, source,
pointer, and citizen input stay fixed while the trigger examples or extraction guidance changes,
and the pair declares distinct expected outcomes. This makes the observation evidence of field
influence rather than request inclusion alone.

Operational evidence is report-only. It does not change format validity, deterministic authoring
success, presentation-review success, or promotion eligibility. Offline verification proves
artifact integrity and deterministic replay; it does not claim that a stochastic result is a
property of the FlowSpec2 format. Promotion to a blocking gate requires a superseding ADR after
supported provider configurations show reproducible correlation with accepted operational
quality and reviewers judge the remaining model variance suitable for enforcement.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Operators can attribute routing and extraction behavior to the exact candidate source, corpus,
  request contract, provider configuration, and raw turn.
- `route.trigger_phrases` and `prompt.extract_hint` can be evaluated without weakening the
  deterministic meaning of authoring conformance.
- Provider failures, semantic failures, and artifact tampering remain distinguishable.
- Future quality gates can use accumulated evidence without changing the existing evidence or
  presentation-review formats.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- A complete quality assessment now consists of independent deterministic, human-review, and
  operational artifacts with distinct trust claims.
- Provider adapters must expose exact request and response identities instead of returning only
  parsed values.
- Operational corpus and request-contract changes require explicit versioning because historical
  model results cannot be replayed under silently changed prompts.
- Offline verification can prove faithful capture and replay but cannot prove provider execution
  authenticity without a separate external trust mechanism.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
