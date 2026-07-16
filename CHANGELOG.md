<!-- section:toc -->

Table of Contents:

- Unreleased: 25 <!-- section:unreleased -->
    - Security: 29 <!-- section:unreleased-security -->
- Version 1.0.0 — prepared 2026-07-15, not published: 39 <!-- section:version-1-0-0 -->
    - Added: 43 <!-- section:version-1-0-0-added -->
    - Changed: 68 <!-- section:version-1-0-0-changed -->
    - Security: 112 <!-- section:version-1-0-0-security -->
- Version 0.2.0 — prepared 2026-07-13, not published: 125 <!-- section:version-0-2-0 -->
    - Added: 129 <!-- section:version-0-2-0-added -->
    - Changed: 146 <!-- section:version-0-2-0-changed -->
    - Security: 161 <!-- section:version-0-2-0-security -->

<!-- /section:toc -->

# Changelog

All notable project changes are recorded here. Versioning follows
[VERSIONING.md](docs/VERSIONING.md).

<!-- section:unreleased -->

## Unreleased

<!-- section:unreleased-security -->

### Security

- Restricted source-distribution discovery to an explicit public allowlist and
  made package verification reject regular files outside that boundary.

<!-- /section:unreleased-security -->

<!-- /section:unreleased -->
<!-- section:version-1-0-0 -->

## Version 1.0.0 — prepared 2026-07-15, not published

<!-- section:version-1-0-0-added -->

### Added

- Public contribution, conduct, citation, issue, and pull-request contracts for
  the stable open-source release.
- Deterministic GitHub Pages publication that resolves every project-owned
  schema and compatibility-profile identifier from canonical package sources.
- Optional persisted `await_external.max_resends` enforcement with a host-facing
  remaining-budget contract, atomic exhaustion rejection, and backward-compatible
  host-owned limiting when omitted.
- Canonical detached Ed25519 authentication for fully replayed authoring
  evidence, with Python and CLI signing and verification interfaces.
- Content-addressed human presentation review for candidate route and prompt
  prose, with a fixed public rubric, source-bound subjects, offline
  verification, and detached reviewer authentication.
- Canonical report-only operational evidence for model-specific routing and
  extraction probes, bound to verified final sources, exact request contracts,
  raw responses, and deterministic offline replay. Authored/counterfactual probe
  pairs isolate the influence of trigger examples and extraction guidance.
- Deterministic analytical lowering for the supported v3 preview subset, with
  full v2 profile and compilation checks, policy-bound loss rehydration, stable
  rejection diagnostics, and an exact preview fixed-point proof.

<!-- /section:version-1-0-0-added -->
<!-- section:version-1-0-0-changed -->

### Changed

- Moved project-owned schema and compatibility-profile identifiers to the
  `wllsena.github.io/flowspec2` namespace, replaced operational-looking example
  hosts with reserved domains, and removed internal editorial annotations.
- Adopted PEP 639 license metadata and documented the stable package boundary
  around `flowspec/2`, with `flowspec/3-draft` remaining experimental.
- Removed repository-local harness doctrine and model integrations that depended
  on non-public sibling tooling; the public package now exposes only its
  self-contained transport and provider-neutral evidence contracts.
- Documented the distinction between source-linked canonical IR and fully
  expanded executable topology, including controlled execution cycles versus
  forbidden dependency cycles and arbitrary author-defined loops.
- Replaced the v3 preview's untyped passthrough map with ordered, immutable,
  policy-classified migration loss entries; authored previews reject the
  migration-only artifact, and localized awaits now account for resume and
  timeout lifecycle rails. Loss categories now bind their allowed source paths
  so category relabeling cannot authorize unrelated rehydration.
- Separated graph assembly from schema relations, FlowSpec value contracts,
  tool lifecycle validation, and external-resume validation.
- Separated semantic orchestration from path, derive, predicate, state, and
  schema contract groups while preserving diagnostic identity.
- Separated Rasa import from export conversion.
- Replaced hidden complete-source authoring equality with a versioned public
  semantic acceptance contract, aggregate missing/mismatch/unexpected feedback,
  and explicit variable presentation paths. Authoring case, corpus, provider
  prompt, and evidence-envelope identities advanced together.
- Expanded the conformance kit with gated-derivation traces for open and closed
  guards, ignored gated input, ordered lookup, and declared fallback behavior.
- Reused a compiled Draft 2020-12 validator while preserving best-match error
  diagnostics, and made routing catalogs include non-exclusive trigger examples.
- Advanced authoring acceptance, case, corpus, and provider-prompt contracts so
  trigger examples and extraction hints are presence-checked but graded only by
  report-only operational evidence.
- Added manually dispatched CI and build-once release verification that uses a
  pinned build backend, validates archive safety and installability, and
  atomically preserves checksummed package artifacts for publication.
- Added a tag-triggered release workflow that publishes the one verified
  artifact set to PyPI through Trusted Publishing with attestations, then
  attaches the same distributions and checksum manifest to a GitHub Release.

<!-- /section:version-1-0-0-changed -->
<!-- section:version-1-0-0-security -->

### Security

- Removed source, documentation, tests, packaging exceptions, and local harness
  artifacts that referenced non-public development infrastructure.
- Pinned the maintained `cryptography` implementation for Ed25519 operations;
  private keys remain outside evidence and cannot be loaded from environment
  files.

<!-- /section:version-1-0-0-security -->

<!-- /section:version-1-0-0 -->
<!-- section:version-0-2-0 -->

## Version 0.2.0 — prepared 2026-07-13, not published

<!-- section:version-0-2-0-added -->

### Added

- Closed structural, semantic, runtime-profile, and compilation diagnostics.
- Canonical FlowIR contracts, digests, state compatibility, and declarative
  active-state migration.
- Typed tool, subflow, external-wait, observability, and HTTP backend contracts.
- Rasa CALM and Open Workflow interoperability profiles with explicit loss
  accounting.
- Provider-neutral AI-authoring benchmark, packaged reference corpus,
  conformance kit, Gemini transport, and closed structured-output projection.
- Content-addressed authoring evidence with provider-reported effective model
  versions and deterministic offline replay verification.
- Locked cross-version CI and installed-package smoke tests.

<!-- /section:version-0-2-0-added -->
<!-- section:version-0-2-0-changed -->

### Changed

- Isolated evaluator-private benchmark oracles from author-facing requests.
- Advanced the authoring evidence envelope to
  `flowspec2/authoring-benchmark-evidence@2`.
- Bound evidence to the configured correction protocol as well as exact
  captures and execution contracts.
- Separated the declarative CLI grammar from command handlers and I/O
  boundaries.
- Extracted closed local-reference and external-resume JSON Schema validation
  from graph compilation.

<!-- /section:version-0-2-0-changed -->
<!-- section:version-0-2-0-security -->

### Security

- Required explicit network consent for live-model evaluation.
- Excluded credentials and nondeterministic operational metadata from evidence.
- Pinned CI actions and toolchains to immutable versions with read-only
  repository permissions.

<!-- /section:version-0-2-0-security -->
<!-- /section:version-0-2-0 -->
