<!-- section:toc -->

Table of Contents:

- Development setup: 21 <!-- section:development-setup -->
- Contract changes: 38 <!-- section:contract-changes -->
- Verification: 58 <!-- section:verification -->
- Pull requests: 75 <!-- section:pull-requests -->
- Security reports: 88 <!-- section:security-reports -->

<!-- /section:toc -->

# Contributing to flowspec2

Thank you for improving flowspec2. Contributions must preserve the closed
format boundary: authors describe valid conversational rails, and the runtime
rejects behavior outside those declared contracts.

<!-- section:development-setup -->

## Development setup

Clone the repository, install the locked environment, and run the project gate:

```bash
uv sync --locked --extra dev
make ci
make package-check
```

Docker is required by the Pyright wrapper used by `make typecheck` and
`make ci`. Live-model commands are optional, require explicit network consent,
and are never part of the default test suite.

<!-- /section:development-setup -->
<!-- section:contract-changes -->

## Contract changes

Changes to public Python APIs, CLI output, JSON Schemas, format identifiers,
runtime profiles, IR, evidence envelopes, or compatibility adapters must update
their tests, documentation, callers, and changelog in the same contribution.
Incompatible contracts receive a new versioned identifier; existing identifiers
are never silently reinterpreted.

Architecture decisions that change a public interface or impose migration cost
require an ADR in `docs/adr/`. Experimental work remains isolated from the
stable `flowspec/2` runtime contract.

Since the public `1.0.0` release, an incompatible change to a stable public
package surface requires a major package release as well as any new contract
identifier or migration required by that surface. Patch and minor releases
must preserve the documented stable compatibility boundary.

<!-- /section:contract-changes -->
<!-- section:verification -->

## Verification

Before opening a pull request, run:

```bash
make ci
make package-check
```

Add regression tests that exercise the real failure path. Do not skip, delete,
or weaken a failing test to make a change pass. Generated package artifacts,
credentials, environment files, and live-model evidence do not belong in the
repository.

<!-- /section:verification -->
<!-- section:pull-requests -->

## Pull requests

Use a focused branch and a conventional-commit title. The pull request must
explain what changed, why it changed, how it was verified, and how to roll it
back. Keep unrelated refactors out of the diff and disclose AI assistance when
it materially authored the change.

By submitting a contribution, you agree that it may be distributed under the
project's MIT License and that you have the right to contribute it.

<!-- /section:pull-requests -->
<!-- section:security-reports -->

## Security reports

Do not disclose a suspected vulnerability in a public issue. Follow
[SECURITY.md](SECURITY.md) and remove credentials, private flow documents, and
citizen data from every report and reproduction.

<!-- /section:security-reports -->
