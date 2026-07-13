<!-- section:toc -->

Table of Contents:

- Unreleased: 20 <!-- section:unreleased -->
- Version 0.2.0 — 2026-07-13: 27 <!-- section:version-0-2-0 -->
    - Added: 31 <!-- section:version-0-2-0-added -->
    - Changed: 48 <!-- section:version-0-2-0-changed -->
    - Security: 59 <!-- section:version-0-2-0-security -->

<!-- /section:toc -->

# Changelog

All notable project changes are recorded here. Versioning follows
[VERSIONING.md](docs/VERSIONING.md).

<!-- section:unreleased -->

## Unreleased

No changes yet.

<!-- /section:unreleased -->
<!-- section:version-0-2-0 -->

## Version 0.2.0 — 2026-07-13

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

<!-- /section:version-0-2-0-changed -->
<!-- section:version-0-2-0-security -->

### Security

- Required explicit network consent for live-model evaluation.
- Excluded credentials and nondeterministic operational metadata from evidence.
- Pinned CI actions and toolchains to immutable versions with read-only
  repository permissions.

<!-- /section:version-0-2-0-security -->
<!-- /section:version-0-2-0 -->
