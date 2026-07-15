<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 37 <!-- section:decision -->
- Consequences: 58 <!-- section:consequences -->
    - What becomes easier: 62 <!-- section:consequences-easier -->
    - What becomes harder: 75 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0010 — External-Wait Resend Policy

**Status:** Accepted
**Date:** 2026-07-15

<!-- section:context -->

## Context

`auto_flow` has a format-owned resend budget, but the stable
`capabilities.await_external` contract previously allowed every configured
`resend` event indefinitely. Hosts could impose their own limit, but the source
could not declare that requirement and the emitted wait marker could not expose
the remaining budget.

Making a finite budget implicit would silently reinterpret existing
`flowspec/2` documents, contrary to the compatibility policy. Keeping the
policy exclusively in each host would preserve behavior but leave a safety
rail outside the canonical source, runtime, tests, and evidence. A required
field would also reject every existing external-wait document.

<!-- /section:context -->
<!-- section:decision -->

## Decision

Add optional `capabilities.await_external.max_resends`. When declared, it
requires a configured `recovery.resend` transition. The runtime persists the
accepted resend count in private state, exposes `remaining_resends` in the
out-of-band marker, and rejects an exhausted resend before applying transition
writes or routing.

When omitted, resend limiting remains host-owned and runtime behavior is
unchanged. Successful resume and non-resend recovery clear the private count;
an accepted resend preserves it across runtime invocations.

Enforcement is blocking immediately only for documents that explicitly opt in.
The check is a closed integer comparison over runtime-owned state, has no
heuristic false-positive surface, and exhausted events leave the wait resumable
through its other declared inputs. A report-only trial would weaken the rail
without producing meaningful calibration evidence.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Authors can place a deterministic resend limit beside the external-wait
  contract.
- Hosts can render or suppress resend controls from the emitted remaining
  budget.
- Persisted state prevents a new runtime invocation from silently restoring the
  budget.
- Existing documents retain their prior host-owned policy without migration.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- Hosts must handle a correlated runtime error if they emit resend after
  exhaustion.
- A host-owned policy and a format-owned policy may coexist, so operators must
  choose the stricter effective limit deliberately.
- State and IR inspection gain another private runtime key when the budget is
  declared.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
