<!-- section:toc -->

Table of Contents:

- Package versions: 20 <!-- section:package-versions -->
- Format and contract versions: 35 <!-- section:contract-versions -->
- Compatibility promises: 75 <!-- section:compatibility-promises -->
- Release procedure: 91 <!-- section:release-procedure -->
- Artifact rollback: 131 <!-- section:artifact-rollback -->

<!-- /section:toc -->

# Versioning and releases

This policy separates the Python distribution version from the identifiers of
documents, intermediate representations, corpora, profiles, and evidence.

<!-- section:package-versions -->

## Package versions

The `flowspec2` distribution follows Semantic Versioning. The stable release
line begins with `1.0.0`; incompatible changes to its public package surfaces
require a major release. The decision and its experimental exclusion are
recorded in
[ADR 0013](adr/0013-stable-package-release-boundary.md).

The package version is declared in `pyproject.toml`; `flowspec2.__version__`
reads installed distribution metadata so it cannot drift from the built
artifact. Dependency locks change in the same commit as package metadata.

<!-- /section:package-versions -->
<!-- section:contract-versions -->

## Format and contract versions

Versioned identifiers such as `flowspec/2`, `flowspec2/ir@1`, corpus formats,
prompt formats, projection formats, evidence envelopes, presentation rubrics,
presentation reviews, operational probes and evidence, and detached signatures
are independent protocol contracts. An incompatible contract change receives a
new identifier. Existing identifiers are never silently reinterpreted.

Provider transports retain distinct versioned prompt identities while sharing
the same closed authoring projection. Evidence records an effective model
version only when the provider exposes one; `null` is the explicit compatible
representation for transports that expose only the requested model identity.

Authoring case and corpus contracts version their grading semantics separately.
The second contract generation replaces hidden complete-source equality with a
public semantic acceptance projection and versioned provider prompt contracts;
evidence produced against the earlier corpus is intentionally not comparable as
model-quality evidence without naming that older contract.

The next authoring contract generation classifies trigger examples and
extraction hints as operational presentation choices, requires their presence
where a case exercises them, and advances the acceptance, case, corpus, and
provider-prompt identities together.

The operational evidence, corpus, and probe contracts advance together when
single observations become paired authored/counterfactual interventions. The
new generation makes pair identity and subject mode explicit and rejects pairs
whose inputs are not controlled or whose expected outcomes are identical.

Runtime profiles and packaged corpora additionally carry canonical content
digests. A known identifier with a different digest is drift and fails closed.
Operational evidence also pins the exact authoring source closure, model request
contract, and provider configuration. It is comparable only within those
identities and remains independent of deterministic format conformance.
Draft identifiers are experimental and provide no execution or compatibility
promise unless their documentation says otherwise.

<!-- /section:contract-versions -->
<!-- section:compatibility-promises -->

## Compatibility promises

The stable `flowspec/2` source identifier remains readable and executable across
compatible package releases. A behavior change that cannot preserve its
semantics requires an explicit migration, adapter, or new format identifier.

Public Python imports, CLI commands, machine-readable diagnostics, canonical IR,
state provenance, compatibility profiles, and evidence schemas are versioned
surfaces. Changes update tests, documentation, callers, and the changelog in the
same changeset. Deprecated implementations are replaced rather than kept beside
their successor. The `flowspec/3-draft` experiment is explicitly outside these
compatibility promises until a new decision promotes a versioned successor.

<!-- /section:compatibility-promises -->
<!-- section:release-procedure -->

## Release procedure

1. Update `CHANGELOG.md`, `pyproject.toml`, and `uv.lock` together.
2. Run `make ci` and `make package-check` from a clean checkout and require the
   remote CI jobs to execute successfully on the exact release commit.
3. Commit the release preparation with an atomic conventional commit.
4. Create an immutable `vX.Y.Z` tag only after reviewing the exact commit.
5. Push the tag only after the `pypi` GitHub environment requires maintainer
   approval and PyPI identifies that environment as the project's Trusted
   Publisher.
6. The release workflow runs `make release-build` from a clean checkout of the
   tag. The target rejects an untagged, dirty, or version-mismatched checkout;
   inspects the wheel and source distribution; installs and smoke-tests them
   independently from the lock embedded in the source distribution; builds
   through the pinned backend under lock-derived hashed constraints; and
   atomically exposes the directory only after successful verification with
   `SHA256SUMS` written.
7. Later jobs download that single artifact set and run `make release-check` to
   verify its checksums, closed file set, versions, installability, and package
   contracts without rebuilding it.
8. After environment approval, publish the wheel and source distribution to
   PyPI through OpenID Connect Trusted Publishing with provenance attestations.
   No long-lived package-index credential is stored by GitHub.
9. Publish the same wheel and source distribution plus `SHA256SUMS` in a GitHub
   Release attached to the immutable tag.
10. Download the published files, verify their checksums, and confirm the
    installed package version, CLI entry point, and packaged contracts.

Tagging and publishing are explicit external actions and are never implied by a
version bump or release-preparation commit. PyPI publication requires explicit
Trusted Publisher configuration; the GitHub Release is created only after PyPI
accepts the preserved distributions.

`SHA256SUMS` provides byte integrity for the wheel and source distribution. It
is not a package signature and does not establish authenticity independently of
the GitHub Release and immutable tag that distribute it.

<!-- /section:release-procedure -->
<!-- section:artifact-rollback -->

## Artifact rollback

Published artifacts are immutable. A defective release is withdrawn or marked
as affected according to the registry's supported mechanism, then corrected in
a new version. Tags and existing files are never moved or overwritten.

If a contract regression affects persisted state or evidence, the advisory must
name the affected identifiers and digests, the safe replacement version, and any
required migration or re-verification procedure.

<!-- /section:artifact-rollback -->
