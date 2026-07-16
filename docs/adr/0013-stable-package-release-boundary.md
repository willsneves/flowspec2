<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 37 <!-- section:decision -->
- Consequences: 53 <!-- section:consequences -->
    - What becomes easier: 57 <!-- section:consequences-easier -->
    - What becomes harder: 68 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0013 — Stable package release boundary

**Status:** Accepted
**Date:** 2026-07-15

<!-- section:context -->

## Context

The `flowspec/2` source format is documented as stable and has a reference
runtime, closed validation, compatibility profiles, migration contracts, and
packaged conformance tests. The Python distribution has not been publicly
released and still declares an alpha package version. The same distribution
also contains `flowspec/3-draft`, an isolated authoring experiment whose
promotion depends on model evidence.

The release could remain pre-stable until the experiment resolves, publish a
new pre-stable package while calling the v2 contract stable, or establish a
stable package boundary around v2 while explicitly excluding the draft from
compatibility promises.

<!-- /section:context -->
<!-- section:decision -->

## Decision

The first public package release is `1.0.0`. Its compatibility promise covers
the stable `flowspec/2` format and the public Python, CLI, IR, migration,
evidence, and adapter contracts documented for that release.

`flowspec/3-draft` remains namespaced as experimental, non-executable, and
outside semantic-versioning compatibility guarantees. Its presence does not
delay the stable v2 release, and its eventual promotion requires a new explicit
contract. The release gate is blocking immediately because version, artifact,
and tag consistency are deterministic and the package has no prior public
release to migrate.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Users receive an unambiguous stable installation target for `flowspec/2`.
- Semantic-versioning rules align with the format's documented stability.
- Experimental source research can continue without weakening v2 guarantees.
- Release artifacts, documentation, and vulnerability policy share one public
  release line.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Incompatible changes to stable package surfaces require a major release or an
  explicit versioned replacement.
- Maintainers must keep experimental imports and documentation clearly isolated.
- The release cannot proceed until remote CI, history sanitization, and public
  distribution verification all pass for the exact commit.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
