<!-- section:toc -->

Table of Contents:

- Supported versions: 17 <!-- section:supported-versions -->
- Reporting a vulnerability: 30 <!-- section:reporting -->
- Artifact signing keys: 44 <!-- section:evidence-signing-keys -->
- Model execution: 67 <!-- section:model-execution -->
- Scope and handling: 75 <!-- section:scope-handling -->

<!-- /section:toc -->

# Security policy

<!-- section:supported-versions -->

## Supported versions

Security fixes target the current release line. Users should reproduce a report
against the newest available release before submitting it. Older releases may
require upgrading to receive a fix.

The `1.x` release line receives security fixes. Experimental interfaces remain
outside the stable compatibility promise described in
[VERSIONING.md](docs/VERSIONING.md).

<!-- /section:supported-versions -->
<!-- section:reporting -->

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/wllsena/flowspec2/security/advisories/new).
Do not open a public issue for an undisclosed vulnerability.

Include the affected version, impact, minimal reproduction, relevant runtime
profile, and any proposed mitigation. Remove API keys, citizen data, access
tokens, private flow documents, and other secrets before submitting. If a
credential was exposed while investigating, rotate it through its owning
platform rather than attaching it to the report.

<!-- /section:reporting -->
<!-- section:evidence-signing-keys -->

## Artifact signing keys

Treat authoring-evidence private keys as deployment secrets. Provision them
through the owning platform or secret store, expose them to the signing process
through a narrowly scoped file, and never commit or attach them to evidence.
The CLI rejects environment-file key paths and never emits key material.

Distribute trusted public keys independently from evidence and signature
artifacts. A valid signature proves control of the corresponding private key;
the trust policy that maps a key identifier to an operator or automation
identity remains an external operational responsibility. Rotate a compromised
key, remove it from trust stores, and re-authenticate retained evidence only
when policy requires a currently trusted signer.

Presentation-review signatures use the same key-handling rules but authenticate
the reviewer artifact rather than model-run evidence. The review's
`reviewer_identifier` is a claim until an independently distributed trusted key
authenticates it; an embedded key identifier alone never establishes reviewer
identity.

<!-- /section:evidence-signing-keys -->
<!-- section:model-execution -->

## Model execution

Live-model commands require an explicit invocation flag; configured credentials
alone are not consent. Gemini keys remain within its SDK boundary.

<!-- /section:model-execution -->
<!-- section:scope-handling -->

## Scope and handling

Reports involving structural validation bypasses, profile or tool-boundary
escapes, unsafe state restoration, idempotency failures, secret disclosure, or
evidence-verification and evidence-signature bypasses are in scope.

The maintainer will validate the report privately, coordinate a fix and release,
and publish an advisory when disclosure is safe. Public artifacts never include
working credentials or private reporter data.

<!-- /section:scope-handling -->
