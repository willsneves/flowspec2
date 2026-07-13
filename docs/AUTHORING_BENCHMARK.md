<!-- section:toc -->

Table of Contents:

- Purpose: 24 <!-- section:purpose -->
- Evaluation contract: 39 <!-- section:evaluation-contract -->
- Author integration: 89 <!-- section:author-integration -->
- Structured-output projection: 176 <!-- section:structured-output-projection -->
- Conformance kit: 203 <!-- section:conformance-kit -->
- Interpreting reports: 229 <!-- section:interpreting-reports -->
- Format promotion: 263 <!-- section:format-promotion -->

<!-- /section:toc -->

# AI authoring benchmark

This document defines how flowspec source formats are evaluated as authoring
interfaces for language models. It complements the field contract in
[SPEC.md](SPEC.md) and the architectural boundary in
[ADR 0002](adr/0002-format-authoring-execution-boundary.md).

<!-- section:purpose -->

## Purpose

The benchmark answers whether an author can produce the requested executable
conversation under a named runtime profile, diagnose an invalid attempt, and
repair it without introducing behavior outside the format's rails. It does not
claim that a fixture-generated baseline measures model quality.

The harness is provider-neutral and performs no network calls. A caller injects
the author function, model configuration, source adapter, and task corpus. This
keeps model access outside the library while making evaluation deterministic
after each source response is captured.

<!-- /section:purpose -->
<!-- section:evaluation-contract -->

## Evaluation contract

Every candidate format is evaluated through the same sequence:

1. Capture the author's source exactly as emitted.
2. Let the format adapter parse that source and return its authoritative
   authored-document projection, canonical authored syntax, and executable
   `flowspec/2` projection.
3. Check the authored-document projection for forbidden constructs before
   judging the executable projection, so lowering cannot hide arbitrary code,
   expressions, loops, parallelism, or manual graph routing.
4. Run aggregate structural, semantic, runtime-profile, and compilation checks.
5. Verify case-specific required constructs to produce focused repair
   diagnostics.
6. Compare successful compilations with the case's complete canonical
   normalized flow projection.
7. Return deterministic diagnostics to the author for the next bounded repair
   request when the attempt fails.
8. Record the canonical result without mutating source, cases, or profiles.

`AuthoringBenchmarkCase` carries the prompt, feature tags, a complete normalized
reference flow, `RequiredFlowConstruct` repair assertions, and
`ForbiddenConstruct` assertions. A case passes only when its executable flow
compiles, matches the complete closed reference semantics, and contains no
forbidden source construct. A merely valid but irrelevant flow, or a flow that
adds unrelated slots, requirements, or steps, therefore fails.

Required constructs should carry an exact expected JSON value whenever the
prompt fixes that behavior. The reference corpus pins semantic identifiers,
tool bindings, result mappings, subflow configuration, predicate operands, and
ordered derive inputs. Presence-only assertions are reserved for requirements
whose value is intentionally unconstrained, and remain diagnostic aids rather
than the success oracle. A mismatch diagnostic reports the observed and
expected JSON and includes a machine-readable suggested fix for the next repair
request.

The current harness admits closed cases only. Open benchmark tasks must define
explicit positive and negative execution traces together with absence and
cardinality constraints before they can become success oracles. Presence-only
assertions are not an acceptable open-case contract because they cannot reject
semantically unrelated additions.

The fixture corpus exercises linear collection, gated derivation, versioned
subflows, typed terminal fulfillment, external suspension, and correction. The
fixtures prove the harness and reference contracts; real-model evidence belongs
in separately captured reports.

<!-- /section:evaluation-contract -->
<!-- section:author-integration -->

## Author integration

An author is any callable from `AuthoringRequest` to `AuthoredSource`. The
request contains an oracle-free `AuthoringTask`, source-format identifier,
runtime-profile identifier, and canonical complete profile contract. A repair
request additionally contains the exact prior source and its ordered machine
diagnostics. `AuthoredSource` carries the exact source plus the provider-reported
effective model version when the transport exposes one.

`AuthoringBenchmarkCase` is evaluator-private. The author-facing request object
graph contains only the task identifier and prompt, so the reference flow,
required constructs, forbidden constructs, and feature tags are structurally
unreachable from the model transport rather than merely omitted by prompt
convention.

```python
from collections.abc import Callable

from flowspec2.authoring import (
    AuthoredSource,
    AuthoringBenchmarkCase,
    AuthoringRequest,
    RequiredFlowConstruct,
    run_authoring_benchmark,
)


def evaluate(
    model_author: Callable[[AuthoringRequest], AuthoredSource],
    reviewed_reference_flow: dict[str, object],
) -> str:
    cases = (
        AuthoringBenchmarkCase.expecting_flow(
            identifier="terminal-service",
            prompt="Author a service flow that submits the request with sgrc_open_ticket.",
            expected_flow=reviewed_reference_flow,
            required_constructs=(
                RequiredFlowConstruct.expecting(
                    identifier="terminal-tool",
                    pointer_pattern="/terminal/tool",
                    expected_value="sgrc_open_ticket",
                ),
            ),
        ),
    )
    report = run_authoring_benchmark("candidate-model", cases, model_author)
    return report.to_json()
```

`FlowSpec2JsonAdapter` is the stable identity adapter. Candidate syntaxes need
an `AuthoringSourceAdapter` that owns parsing and returns the authored-document
JSON projection, the format's canonical authored syntax, the executable-flow
JSON projection, and adaptation diagnostics. The canonical authored syntax must
be deterministic, meaning-preserving, and accepted by the same format; it is not
the executable JSON abstract syntax tree unless JSON is the authored format.
The adapter must not silently drop unsupported behavior.

`load_reference_authoring_corpus()` loads the versioned cases distributed with
the package. Its manifest closes membership and order and pins every case by
SHA-256; the corpus digest covers the fully resolved canonical contracts rather
than file formatting. Tests, the CTK, and live evaluation consume those same
resources, so there is no test-only answer corpus that can drift from the
installed package.

`GeminiAuthor` is the optional reference model transport. It receives only the
task, normative schema, exact runtime-profile contract, prior source, and repair
diagnostics. The complete reference flow and required-construct oracle remain
evaluator-only. The provider returns the closed authoring projection;
`flow_document_json` is preserved verbatim so malformed model source becomes a
benchmark attempt and can be repaired rather than silently normalized. Every
response must also expose the effective model version reported by Gemini; a
missing version fails the transport instead of silently substituting the
requested model alias.

The CLI requires explicit network consent even when a key is configured:

```bash
flowspec2 authoring-benchmark-gemini --allow-network --repository-revision <revision> --output authoring-evidence.json
```

Provider failures produce no partial artifact. Completed semantic failures do
produce evidence and return a failing command status, preserving negative
results instead of selecting only successful runs.

<!-- /section:author-integration -->
<!-- section:structured-output-projection -->

## Structured-output projection

`authoring_projection()` exposes a closed, provider-neutral structured-output
schema derived from the normative schema identifier and canonical digest. The
projection has its own version and digest, contains no references or open
objects, and carries canonical compact `flowspec/2` JSON in a string field.
`project_flow_document()` creates the envelope and
`lower_authoring_projection()` decodes it through strict JSON and normative
structural validation. Returned schemas and documents are defensive copies.

The string boundary is deliberate. Normative FlowSpec uses author-chosen object
keys for domains, slots, lookup tables, and bindings. A provider schema that
requires a fixed object property set cannot represent those maps directly
without rejecting valid documents or weakening their meaning. The projection
therefore preserves exact semantics at the cost of JSON escaping. It is a safe
generation transport, not evidence that escaped JSON is the easiest authoring
surface. Candidate native syntaxes still belong in the benchmark and must lower
through an explicit adapter.

The projection proves envelope and structural validity only. Semantic linking,
profile compatibility, compilation, and the complete case oracle remain the
responsibility of `check_flow` and the benchmark after lowering. Constrained
generation narrows possible shapes; it never replaces those checks.

<!-- /section:structured-output-projection -->
<!-- section:conformance-kit -->

## Conformance kit

The machine-readable CTK freezes behavior below the authoring syntax. Its
packaged reference manifest identifies safe local fixtures, optional JSON Pointer projections and
deterministic mutations, expected compilation status, ordered diagnostic
identities, canonical IR, content digests, and caller-visible runtime traces.
`load_ctk_corpus()` rejects paths outside the fixture root, environment files,
open corpus contracts, and non-canonical JSON. `run_ctk_corpus()` returns a
content-addressed canonical report without timestamps or private runtime state.

The reference corpus freezes the linear collection spine, structural diagnostic
ordering, typed terminal execution, versioned subflow execution, and typed
external resume behavior. The resume trace includes the host deadline and
duplicate/late policy outcomes. IR oracles include the explicit IR-format
identity as well as complete canonical and execution digests.

This makes diagnostic order, normalization, linking, dependency resolution, and
execution traces conformance contracts rather than snapshots hidden in unit
tests. Alternative runtimes or source adapters can run the same corpus and
compare the complete report. Corpus growth must reuse the versioned format and
add reviewed semantic or trace oracles; changing an oracle to match an
implementation regression is not a compatibility strategy.

<!-- /section:conformance-kit -->
<!-- section:interpreting-reports -->

## Interpreting reports

Reports are immutable and canonically ordered. Each attempt records:

- source SHA-256 and raw UTF-8 size;
- the adapter's canonical authored-syntax size when adaptation succeeds;
- a named deterministic token proxy rather than a provider tokenizer estimate;
- compilation status and ordered diagnostics;
- whether the complete closed case contract passed;
- the repair round that produced the attempt.

Aggregate success, attempt, and repair fields are descriptive observations.
Compactness is useful only after correctness: a shorter source that diverges
from the complete semantic contract or weakens runtime guarantees is a failed
candidate. Raw-source fields preserve exact response provenance; canonical
source fields compare the real syntax defined by each adapter rather than a
shared executable JSON representation.

`AuthoringBenchmarkEvidence` stores the report and its provenance in one
canonical content-addressed envelope. It includes the exact emitted sources,
provider and requested-model identifiers, provider-reported effective model
version for every Gemini attempt, closed non-secret generation configuration,
provider SDK, prompt format and digest, package and operator-supplied repository
revision, source-adapter contract, corpus identity and digest, and exact runtime
profile identity and digest. The current envelope contract is
`flowspec2/authoring-benchmark-evidence@2`. It deliberately excludes timestamps,
hostnames, latency, request IDs, credentials, and other operational fields that
would make equivalent semantic evidence unequal. Captures must align exactly
with every reported case and correction attempt, including matching source
hashes, before the artifact can serialize.

<!-- /section:interpreting-reports -->
<!-- section:format-promotion -->

## Format promotion

A source revision is eligible for stability only when controlled real-model
runs use equivalent tasks and execution contracts, improve authoring outcomes,
preserve every supported semantic rail, and introduce no weaker profile or
runtime boundary. Correction quality and diagnostic usability are part of the
decision; compact source size alone is not.

`flowspec/3-draft` currently has a closed preview schema and a loss-reporting
v2 migration, but no stable lowering adapter or runtime registration. It must
remain experimental until the evidence above exists and the remaining
passthrough fragments receive explicit semantics or documented exclusion.

<!-- /section:format-promotion -->
