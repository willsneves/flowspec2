<!-- section:toc -->

Table of Contents:

- Supported versions: 15 <!-- section:supported-versions -->
- Reporting a vulnerability: 27 <!-- section:reporting -->
- Scope and handling: 41 <!-- section:scope-handling -->

<!-- /section:toc -->

# Security policy

<!-- section:supported-versions -->

## Supported versions

Security fixes target the current release line. Users should reproduce a report
against the newest available release before submitting it. Older releases may
require upgrading to receive a fix.

The repository has not published a stable release line. Pre-release interfaces
may change according to [VERSIONING.md](docs/VERSIONING.md).

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
<!-- section:scope-handling -->

## Scope and handling

Reports involving structural validation bypasses, profile or tool-boundary
escapes, unsafe state restoration, idempotency failures, secret disclosure, or
evidence-verification bypasses are in scope.

The maintainer will validate the report privately, coordinate a fix and release,
and publish an advisory when disclosure is safe. Public artifacts never include
working credentials or private reporter data.

<!-- /section:scope-handling -->
