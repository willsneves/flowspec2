<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 37 <!-- section:decision -->
- Consequences: 70 <!-- section:consequences -->
    - What becomes easier: 74 <!-- section:consequences-easier -->
    - What becomes harder: 85 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0009 — V3 Preview Lowering Boundary

**Status:** Accepted
**Date:** 2026-07-14

<!-- section:context -->

## Context

The v3 preview tests a local authoring surface while the v2 format remains the only stable,
executable contract. Migration from v2 makes proposed source changes measurable, but it cannot by
itself show that a preview retains enough information to produce a valid executable flow.

The preview deliberately omits some v2 source choices, including original domain names, declaration
sharing, and certain declaration-placement details. Other preview constructs are more expressive
than their v2 counterparts. Consequently, claiming original-source reconstruction or canonical IR
identity would be false even when a preview can produce a behaviorally acceptable v2 document.

ADR 0008 classifies non-native v2 fragments and permits only `compatibility_only` entries to be
rehydrated. It leaves the lowerer's concrete proof boundary to a subsequent decision.

<!-- /section:context -->
<!-- section:decision -->

## Decision

The experimental package exposes a deterministic lowerer for an explicit preview subset. The
lowerer reconstructs a canonical v2 source, rehydrates only `compatibility_only` loss entries at
source paths allowed by their packaged policy rules, and rejects every loss entry whose handling is
`reject`. It refuses collisions rather than overwriting native output.

The reconstructed source must pass the complete v2 structural, semantic, selected-profile, and
compilation checks. A successful report contains the immutable canonical v2 source, the rehydrated
source paths, and the complete flow-check report.

The equivalence proof is exact preview fixed-point equality:

```text
v3 preview -> canonical v2 -> v3 preview
```

This proves that lowering preserves every preview-level distinction understood by the migrator. It
does not prove reconstruction of the original v2 text, declaration ownership, shared-domain
identity, row placement, or canonical FlowIR identity. Deterministic synthesized domain names are
therefore part of the lowering implementation rather than preview semantics.

Valid preview constructs that cannot satisfy this proof fail with stable, path-addressed lowering
diagnostics. The supported subset may expand only when the new mapping remains deterministic,
compile-checked, and covered by the same fixed-point obligation.

Enforcement is blocking immediately. The preview is experimental, excluded from runtime dispatch,
and has no deployed compatibility promise. Report-only acceptance would let callers mistake an
unproven reconstruction for executable equivalence without protecting an existing user surface.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Preview experiments can produce executable v2 evidence without changing the stable runtime.
- Every successful lowering carries profile, compilation, and fixed-point evidence.
- Loss rehydration is constrained by category-specific paths and cannot silently overwrite native output.
- Unsupported preview expressiveness fails with attributable machine-readable diagnostics.
- Independent callers receive deterministic output and cannot mutate a completed report.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- The lowerable preview surface is narrower than the structurally valid preview schema.
- Expanding preview expressiveness may require a v2 representation or an explicit lowering rejection.
- Fixed-point equality couples lowerer changes to the preview migrator's canonical representation.
- Consumers needing original v2 source or FlowIR identity must retain and compare those artifacts separately.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
