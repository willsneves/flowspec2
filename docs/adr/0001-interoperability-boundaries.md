<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 38 <!-- section:decision -->
- Consequences: 66 <!-- section:consequences -->
    - What becomes easier: 70 <!-- section:consequences-easier -->
    - What becomes harder: 85 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0001 — Interoperability boundaries

**Status:** Accepted
**Date:** 2026-07-13

<!-- section:context -->

## Context

flowspec2 is a conversational workflow language whose closed domains, synthesized
routers, correction cascades, terminal lifecycle, and channel-aware suspension
semantics are enforced by its runtime compiler. Rasa CALM and Open Workflow
Specification documents overlap with parts of that model, but neither format has
an equivalent for every flowspec2 rail.

The available choices were to replace flowspec2 with a broader format, claim a
best-effort conversion that silently drops unsupported behavior, remain isolated,
or keep flowspec2 canonical behind explicitly bounded interoperability profiles.
Replacement would weaken the conversational contracts that motivated the project,
while best-effort conversion would make a valid artifact appear behaviorally
equivalent when it is not.

<!-- /section:context -->
<!-- section:decision -->

## Decision

flowspec2 remains the canonical executable format. Interoperability is provided by
separate, versioned profiles with aggregate, machine-readable diagnostics and no
silent semantic loss.

The Rasa CALM profile covers the shared deterministic slot-collection core. Its
converter is strict by default; callers may explicitly allow only the profile's
documented metadata, lifecycle, presentation, and host-adapter differences.
Control-flow and validation incompatibilities always block serialization. The
profile disables Rasa's completion pattern and persists collected slots. Its
unavoidable CALM opportunistic-fill, correction, interruption, and repair
differences are a named warning that requires the same explicit opt-in. The
Open Workflow conversational profile uses Open Workflow
Specification 1.0.3 document/task metadata and custom `flowspec2.*` calls to
preserve flowspec2 semantics. Only documents declaring that profile can be
imported; arbitrary Open Workflow documents are not inferred into flowspec2.
Both directions validate against a pinned copy of the official schema and the
narrower local profile schema. The upstream schema, license, source commit, and
content digests ship with the package so validation is reproducible offline.

The strict gate is enabled immediately rather than run report-only because these
adapters are new, produce no external side effects, aggregate every diagnostic in
one pass, and expose an explicit opt-in for the losses that are safe to permit.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Native flows retain their closed-domain, correction, suspension, and lifecycle
  guarantees without being constrained by the least expressive target format.
- Rasa projects can exchange the common linear collection subset with explicit
  evidence about every construct that did not transfer.
- Open Workflow tooling can store, inspect, and transport the versioned
  conversational profile without pretending that a generic runtime understands
  flowspec2 custom calls.
- Future adapters can reuse one diagnostic contract and the same strictness
  policy.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Consumers must understand the difference between core flowspec2, the Rasa
  portable subset, and the Open Workflow conversational profile.
- Full municipal-service flows cannot be exported to Rasa without adapter code or
  redesign when they use corrections, derived values, external waits, or terminal
  lifecycle guarantees.
- Generic Open Workflow runtimes require profile-aware handlers for
  `flowspec2.*` calls; schema validity alone does not make the workflow executable.
- Changes to either upstream format require an explicit profile review instead of
  inheriting compatibility automatically.
- Updating the Open Workflow schema requires refreshing the vendored artifact,
  license, provenance manifest, integrity tests, and profile review together.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
