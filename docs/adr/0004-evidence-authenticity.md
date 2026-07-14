<!-- section:toc -->

Table of Contents:

- Context: 20 <!-- section:context -->
- Decision: 38 <!-- section:decision -->
- Consequences: 66 <!-- section:consequences -->
    - What becomes easier: 70 <!-- section:consequences-easier -->
    - What becomes harder: 84 <!-- section:consequences-harder -->

<!-- /section:toc -->

# 0004 — Evidence authenticity

**Status:** Accepted
**Date:** 2026-07-13

<!-- section:context -->

## Context

The authoring evidence envelope is content-addressed and deterministically
replayable. Its digest proves that content did not change after the digest was
computed, but anyone can create a different internally consistent envelope.
Promotion decisions need an optional way to prove that a trusted operator or
automation identity approved a particular verified evidence digest.

Embedding a public key in the evidence would prove only self-consistency because
an attacker could replace both the key and signature. Symmetric authentication
would require every verifier to hold the signing secret. Implementing an
asymmetric primitive locally would create an unacceptable cryptographic review
surface. Replacing the evidence envelope to add a signature would also couple
stable semantic evidence to key rotation and signing policy.

<!-- /section:context -->
<!-- section:decision -->

## Decision

Evidence authenticity uses a detached, canonical
`flowspec2/authoring-evidence-signature@1` envelope with Ed25519. The signed
message is domain-separated and binds the verified evidence SHA-256 digest. The
signature envelope records the algorithm, digest, signature, and a SHA-256 key
identifier derived from the raw public key; it never embeds a trusted key.

Signing first performs the complete offline evidence verification. Signature
verification also verifies the evidence before checking the detached signature
against a caller-supplied public key. The caller's key file or trust store is the
root of trust. Private keys are accepted only as unencrypted PKCS8 PEM through an
explicit path and are never included in output, logs, or evidence.

The implementation uses the maintained `cryptography` Ed25519 primitive, pinned
with the project lockfile. Signature support is part of the base package because
the CLI and public verification API must behave consistently in every installed
artifact.

This enforcement is blocking immediately only when a caller explicitly invokes
signature creation or verification. The underlying evidence verifier remains
unchanged, so existing unsigned evidence stays valid. Cryptographic verification
has an exact result and no heuristic false-positive surface; a report-only trial
would not provide a useful promotion signal.

<!-- /section:decision -->
<!-- section:consequences -->

## Consequences

<!-- section:consequences-easier -->

### What becomes easier

- Reviewers can distinguish integrity-only evidence from evidence authenticated
  by a configured trust root.
- Keys can rotate without rewriting or invalidating the semantic evidence
  envelope.
- Signature artifacts remain deterministic and safe to distribute separately
  from private signing material.
- Automation can enforce an expected public-key identity before accepting
  evidence for a format-promotion decision.

<!-- /section:consequences-easier -->
<!-- section:consequences-harder -->

### What becomes harder

- The base package gains a compiled cryptographic dependency and its associated
  update and vulnerability-management obligation.
- Operators must provision private keys securely and distribute trusted public
  keys out of band.
- A valid signature proves control of a key, not the real-world identity of its
  holder; trust-store governance remains an operational responsibility.
- Detached evidence and signature files must be retained together when
  authenticity is required.

<!-- /section:consequences-harder -->
<!-- /section:consequences -->
