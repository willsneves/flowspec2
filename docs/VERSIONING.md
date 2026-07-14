<!-- section:toc -->

Table of Contents:

- Package versions: 20 <!-- section:package-versions -->
- Format and contract versions: 35 <!-- section:contract-versions -->
- Compatibility promises: 62 <!-- section:compatibility-promises -->
- Release procedure: 77 <!-- section:release-procedure -->
- Artifact rollback: 95 <!-- section:artifact-rollback -->

<!-- /section:toc -->

# Versioning and releases

This policy separates the Python distribution version from the identifiers of
documents, intermediate representations, corpora, profiles, and evidence.

<!-- section:package-versions -->

## Package versions

The `flowspec2` distribution follows Semantic Versioning. Before the stable
release line, a minor release may contain an intentional public-API break and a
patch release remains backward compatible within its minor line. After the
stable release line begins, incompatible public-API changes require a major
release.

The package version is declared in `pyproject.toml`; `flowspec2.__version__`
reads installed distribution metadata so it cannot drift from the built
artifact. Dependency locks change in the same commit as package metadata.

<!-- /section:package-versions -->
<!-- section:contract-versions -->

## Format and contract versions

Versioned identifiers such as `flowspec/2`, `flowspec2/ir@1`, corpus formats,
prompt formats, projection formats, evidence envelopes, presentation rubrics,
presentation reviews, and detached signatures are independent protocol
contracts. An incompatible contract change receives a new identifier. Existing
identifiers are never silently reinterpreted.

Provider transports retain distinct versioned prompt identities while sharing
the same closed authoring projection. Evidence records an effective model
version only when the provider exposes one; `null` is the explicit compatible
representation for transports that expose only the requested model identity.

Authoring case and corpus contracts version their grading semantics separately.
The second contract generation replaces hidden complete-source equality with a
public semantic acceptance projection and versioned provider prompt contracts;
evidence produced against the earlier corpus is intentionally not comparable as
model-quality evidence without naming that older contract.

Runtime profiles and packaged corpora additionally carry canonical content
digests. A known identifier with a different digest is drift and fails closed.
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
their successor in this pre-stable project.

<!-- /section:compatibility-promises -->
<!-- section:release-procedure -->

## Release procedure

1. Update `CHANGELOG.md`, `pyproject.toml`, and `uv.lock` together.
2. Run `make ci` and `make package-check` from a clean checkout.
3. Inspect the built source distribution and wheel for required schemas,
   corpora, documentation, license, and importable public contracts.
4. Commit the release preparation with an atomic conventional commit.
5. Create an immutable `vX.Y.Z` tag only after reviewing the exact commit.
6. Publish artifacts built from that tag without rebuilding or replacing them.
7. Verify the installed package version, CLI entry point, and packaged contracts
   from the published artifacts.

Tagging and publishing are explicit external actions and are never implied by a
version bump or release-preparation commit.

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
