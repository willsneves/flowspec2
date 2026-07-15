<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 35 <!-- section:decision -->
- Consequences: 55 <!-- section:consequences -->
    - What becomes easier: 59 <!-- section:consequences-easier -->
    - What becomes harder: 69 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0011 — Public Model Transport Scope

**Status:** Accepted
**Date:** 2026-07-15

<!-- section:context -->

## Context

The package included model adapters, CLI commands, tests, and documentation
whose execution depended on a sibling implementation that is not part of the
public distribution. The source distribution also carried repository-local
harness doctrine and packaging exceptions for those artifacts.

Publishing that tree would expose internal development structure and leave
public consumers with interfaces they could not install from the declared
package dependencies. Textual redaction alone would retain broken imports,
commands, and historical objects.

<!-- /section:context -->
<!-- section:decision -->

## Decision

The public repository retains only live-model transports implemented through
declared public package dependencies. Provider-neutral authoring, evidence,
verification, signing, presentation-review, and operational-probe contracts
remain unchanged.

Remove the unavailable transport's runtime adapter, authoring adapter, CLI
commands, exports, tests, documentation, and package smoke assertions. Remove
repository-local harness doctrine from the tree and rewrite repository history
so the deleted paths and private identifiers are unreachable.

This removal is blocking immediately. The repository is still pre-production,
the unavailable integration cannot satisfy the public installation contract,
and a report-only period would continue distributing the sensitive references
the decision exists to remove.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- A public clone contains only installable, documented integrations.
- Package tests no longer require undeclared sibling source trees.
- Secret scanning and provenance review operate on a self-contained repository.
- Provider-neutral evidence remains usable by future public transports.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Consumers of the removed Python and CLI interfaces must migrate to a public
  transport before those interfaces can return.
- Reintroducing another provider requires a declared dependency, public threat
  model, tests, documentation, and a new compatibility decision.
- Rewritten commit identifiers require downstream clones to re-clone or reset
  explicitly after the sanitized history is published.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
