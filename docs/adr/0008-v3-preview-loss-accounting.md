<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 36 <!-- section:decision -->
- Consequences: 64 <!-- section:consequences -->
    - What becomes easier: 68 <!-- section:consequences-easier -->
    - What becomes harder: 79 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0008 — V3 Preview Loss Accounting

**Status:** Accepted
**Date:** 2026-07-14

<!-- section:context -->

## Context

The v2-to-v3 preview migrator cannot express every valid v2 source fragment in native preview
syntax. The initial preview stored those fragments in an untyped JSON Pointer-to-value map. That
made omission visible, but it did not distinguish reconstructible compatibility data from source
that the lowerer must reject. It also allowed a repeated pointer to overwrite an earlier
classification silently.

Promotion requires deterministic lowering and an explicit decision for every non-native fragment.
Leaving the decision in prose or inferring it from a fragment's shape would make independently
implemented lowerers disagree. Allowing authors to write passthrough data would also turn a
migration evidence mechanism into an unbounded extension point.

<!-- /section:context -->
<!-- section:decision -->

## Decision

V3 preview loss accounting is a migration-only artifact governed by the packaged
`flowspec2/v3-preview-loss-policy@1` contract. The policy defines a closed category enum and the
normative disposition and lowerer handling for each category. `compatibility_only` categories are
eligible for deterministic rehydration; `excluded` categories require rejection. Rehydration is
permission to reconstruct a candidate v2 source, not proof of validity: the lowerer must run
the complete v2 structural, semantic, profile, and compilation checks after reconstruction.

`v2_passthrough` contains ordered entries with the source JSON Pointer, category, disposition, and
an exact copy of the source fragment. The immutable migration report returns fresh fragment copies
to callers. Entries are ordered by source path. A source path may appear only once, and duplicate
registration fails immediately. The schema closes the serialized shape; semantic validation
checks category membership, policy disposition, ordering, and source path uniqueness against the
packaged policy.

Normal authoring validation rejects `v2_passthrough`. Only the explicit v2-migration validation
mode accepts it. This boundary prevents new v3 sources from using loss accounting as an escape
hatch while retaining exact source evidence for migration analysis.

The contract is blocking immediately rather than starting report-only. The preview is experimental,
non-executable, and has no supported authored-passthrough compatibility surface. A report-only
period would establish the ambiguous extension behavior this decision removes without protecting
deployed sources or users.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Every non-native fragment carries a machine-readable reason and deterministic future handling.
- Migration reports can be audited without interpreting arbitrary fragment contents.
- Independent lowerers can agree on reconstruction versus rejection behavior.
- Silent loss and repeated-pointer overwrite become testable contract violations.
- Authored v3 sources remain closed and cannot smuggle unsupported semantics through migration data.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Adding or reclassifying a loss category requires a policy-contract change and coordinated tests.
- Migration consumers must use typed entries instead of a direct pointer-to-value lookup.
- The lowerer must revalidate the fully reconstructed v2 source before claiming equivalence.
- Loss-policy compatibility must be considered separately from the experimental source schema.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
