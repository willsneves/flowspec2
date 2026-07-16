<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 36 <!-- section:decision -->
- Consequences: 57 <!-- section:consequences -->
    - What becomes easier: 61 <!-- section:consequences-easier -->
    - What becomes harder: 73 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0012 — Public contract namespace

**Status:** Accepted
**Date:** 2026-07-15

<!-- section:context -->

## Context

The package publishes JSON Schema identifiers and compatibility-profile
identifiers as durable public contracts. Earlier identifiers used an
institutional domain that this repository does not control, while demonstration
backends used hostnames that looked operational. Those values could imply
affiliation, collide with independently managed resources, or prevent the
project owner from maintaining resolvable documentation.

The available choices were to retain non-controlled identifiers, use URNs with
no web ownership, or move project-owned contracts under the repository owner's
GitHub Pages namespace before the first public release.

<!-- /section:context -->
<!-- section:decision -->

## Decision

Every project-owned schema and profile identifier uses the
`https://wllsena.github.io/flowspec2/` namespace. Demonstration service URLs use
IANA-reserved example domains, and documentation describes integrations without
claiming deployment or affiliation.

A deterministic static-site build publishes each JSON Schema directly at its
canonical `$id` and publishes a human-readable resource for each profile
identifier. The build consumes package sources and generated schema APIs, so
the repository never maintains a second editable schema copy.

The identifier change is blocking immediately because no package release has
published the earlier values and exact-identity tests already cover every
consumer. No report-only window would produce additional compatibility signal.
Future identifier changes require a new versioned contract rather than
reinterpretation of an existing identifier.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- The repository owner controls the namespace used by all project contracts.
- Public consumers can resolve each schema and profile identifier without a
  package installation.
- Examples are clearly synthetic and cannot be mistaken for operational
  endpoints.
- Package smoke tests can enforce namespace ownership across distributions.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Any unpublished evidence or flow that captured an earlier schema identifier
  must be regenerated.
- Moving the repository or owner account requires preserving redirects or the
  existing namespace indefinitely.
- Additional public contract families must follow the same ownership and
  versioning convention.
- GitHub Pages availability becomes part of the public documentation surface;
  deterministic local checks cover content, while deployment monitoring covers
  hosting availability.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
