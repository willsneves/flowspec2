<!-- section:toc -->

Table of Contents:

- Purpose: 25 <!-- section:purpose -->
- Evaluation contract: 40 <!-- section:evaluation-contract -->
- Author integration: 102 <!-- section:author-integration -->
- Structured-output projection: 213 <!-- section:structured-output-projection -->
- Conformance kit: 240 <!-- section:conformance-kit -->
- Interpreting reports: 269 <!-- section:interpreting-reports -->
- Evidence authenticity: 325 <!-- section:evidence-authenticity -->
- Format promotion: 360 <!-- section:format-promotion -->

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
6. Project successful compilations into semantic observations and compare all
   of them with the task's complete public acceptance contract.
7. Return deterministic diagnostics to the author for the next bounded repair
   request when the attempt fails.
8. Record the canonical result without mutating source, cases, or profiles.

`AuthoringBenchmarkCase` carries the prompt, feature tags, a private fixture
source, and a public `AuthoringAcceptanceContract`. The loader canonicalizes
internal domain and step identifiers, projects every resulting semantic leaf
into that contract, and keeps source syntax out of the provider request.
Required and forbidden constructs are public focused constraints, while flow identity,
version labels, route prose, and non-verbatim path prompt text are explicitly
variable presentation paths. A case passes only when its executable flow
compiles, matches every public semantic observation, adds no unexpected
observation, and contains no forbidden source construct. A merely valid but
irrelevant flow, or a flow that adds unrelated slots, requirements, or steps,
therefore fails.

Required constructs carry an exact expected JSON value whenever the task fixes
that behavior. The public semantic observations additionally close domain,
slot, ordered-path, derive, confirmation, terminal, subflow, and capability
details. Presence-only assertions remain focused diagnostic aids. A mismatch
reports the observed and expected JSON and includes a machine-readable
suggested fix for the next repair request.

The acceptance projection is complete by construction: every scalar or empty
container in the canonical semantic projection is public. Exact source identity,
route prose, and non-verbatim prompt text are explicitly variable; consistent
domain and step renaming lowers to the same semantic observations. New
unclassified normalized fields therefore become public grading dimensions
instead of silently entering a private oracle. Unexpected observations are
errors, preserving closed cardinality and absence semantics without comparing
against a hidden source document.

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
request contains a source-answer-free `AuthoringTask`, its complete public
acceptance contract, source-format identifier, runtime-profile identifier, and
canonical complete profile contract. A repair request additionally contains the
exact prior source and its ordered machine diagnostics. `AuthoredSource` carries
the exact source plus the provider-reported effective model version when the
transport exposes one.

`AuthoringBenchmarkCase` is evaluator-private. The author-facing request object
graph contains the task identifier, prompt, public semantic expectations,
required constructs, forbidden constructs, and variable presentation paths.
The reference source and feature tags remain structurally unreachable from the
model transport rather than merely omitted by prompt convention.

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

`GeminiAuthor` and `CodexAuthor` are optional model transports. Each receives
the task, public acceptance contract, normative schema, exact runtime-profile
contract, prior source, and repair diagnostics. The complete reference source
remains evaluator-only. Each provider returns the closed authoring projection;
`flow_document_json` is preserved verbatim so malformed model source becomes a
benchmark attempt and can be repaired rather than silently normalized. Every
Gemini response must expose its provider-reported effective model version; a
missing version fails that transport instead of silently substituting the
requested model alias. Codex does not report a distinct effective version, so
its capture records `null` while provenance retains the requested model,
reasoning effort, and exact `llmgate` version.

The Codex transport lazy-loads the operator's `~/Code/llmgate` checkout and
requires its subscription-authentication contract. It excludes API-key and
parent-environment inheritance, requires ChatGPT login, disables built-in tools,
ignores user/project rules, uses safe isolation with a read-only sandbox, and
runs ephemeral turns. The known library requires Python 3.12. The package with
the same name on PyPI is unrelated and must not be used for this integration.

The CLI requires explicit network consent even when a key is configured:

```bash
flowspec2 authoring-benchmark-gemini --allow-network --repository-revision <revision> --output authoring-evidence.json
uv run --python 3.12 --with ~/Code/llmgate flowspec2 authoring-benchmark-codex --allow-network --repository-revision <revision> --output codex-authoring-evidence.json
flowspec2 authoring-evidence-verify authoring-evidence.json --repository-revision <revision>
flowspec2 authoring-presentation-review-init authoring-evidence.json --repository-revision <revision> --output presentation-review.draft.json
flowspec2 authoring-presentation-review-finalize authoring-evidence.json --draft presentation-review.draft.json --repository-revision <revision> --output presentation-review.json
flowspec2 authoring-presentation-review-verify authoring-evidence.json --review presentation-review.json --repository-revision <revision> --json
```

Provider failures produce no partial artifact. Completed semantic failures do
produce evidence and return a failing command status, preserving negative
results instead of selecting only successful runs.

The verifier performs no network calls. It requires canonical strict JSON,
validates the closed packaged evidence schema, recomputes the content digest,
matches the recorded package, corpus, profile, and source-adapter contracts,
checks the recorded correction limit and every capture hash, then re-evaluates
each exact source through the installed adapter and checker. The replayed report
must equal the recorded report in full. An optional repository-revision
assertion lets automation bind the artifact to an expected checkout without
invoking Git inside the library.

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
profile compatibility, compilation, and the complete public acceptance contract
remain the responsibility of `check_flow` and the benchmark after lowering. Constrained
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
external resume behavior. Gated-derivation traces cover an open guard, a closed
guard that ignores bundled gated input, ordered lookup with a null source, and
the declared fallback when a lookup key is absent. The resume trace includes
the host deadline and duplicate/late policy outcomes. IR oracles include the
explicit IR-format identity as well as complete canonical and execution
digests.

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
- whether the complete public semantic contract passed;
- the repair round that produced the attempt.

Aggregate success, attempt, and repair fields are descriptive observations.
Compactness is useful only after correctness: a shorter source that diverges
from the public semantic contract or weakens runtime guarantees is a failed
candidate. Raw-source fields preserve exact response provenance; canonical
source fields compare the real syntax defined by each adapter rather than a
shared executable JSON representation.

`AuthoringBenchmarkEvidence` stores the report and its provenance in one
canonical content-addressed envelope. It includes the exact emitted sources,
provider and requested-model identifiers, provider-reported effective model
version when exposed, closed non-secret generation configuration, provider SDK,
prompt format and digest, package and operator-supplied repository revision,
source-adapter contract, corpus identity and digest, and exact runtime profile
identity and digest. Gemini requires that effective identity; transports that do
not expose one record `null` rather than inventing it. The envelope also binds
the configured correction-round limit, so replay proves the interaction
protocol as well as the observed attempts. The current envelope contract is
`flowspec2/authoring-benchmark-evidence@3`. It deliberately excludes timestamps,
hostnames, latency, request IDs, credentials, and other operational fields that
would make equivalent semantic evidence unequal. Captures must align exactly
with every reported case and correction attempt, including matching source
hashes, before the artifact can serialize.

The content digest detects modification but is not an authenticity mechanism.
Unsigned evidence remains valid integrity-only evidence.

Route descriptions and author-owned non-verbatim prompts use a separate human
presentation-review artifact. Its fixed public rubric evaluates scope,
standalone meaning, requested-action clarity, input-contract alignment,
interaction context, and claim fidelity without prescribing reference wording.
Subjects are extracted from final semantically successful captures and bound to
their case, repair round, source digest, JSON Pointer, and text digest. The
review packet contains candidate prose plus the public task, acceptance, and
interaction context; it never contains fixture source or reference prose.

Presentation review is content-addressed, verified offline against the exact
benchmark evidence, and optionally authenticated with a detached Ed25519
signature whose trusted public key is supplied by the caller. It remains
report-only until the promotion signal in
[ADR 0006](adr/0006-authoring-presentation-review.md) is satisfied, so review
outcomes never rewrite deterministic benchmark success.

<!-- /section:interpreting-reports -->
<!-- section:evidence-authenticity -->

## Evidence authenticity

Authenticity is an explicit optional layer over verified evidence. Signing
first performs the complete offline replay, then signs a domain-separated
message that binds the verified evidence digest. Verification repeats the
evidence replay before authenticating the detached signature against the public
key supplied by the caller:

```bash
flowspec2 authoring-evidence-sign authoring-evidence.json --private-key authoring-private-key.pem --repository-revision <revision> --output authoring-evidence.signature.json
flowspec2 authoring-evidence-signature-verify authoring-evidence.json --signature authoring-evidence.signature.json --public-key authoring-public-key.pem --repository-revision <revision> --json
flowspec2 authoring-presentation-review-sign authoring-evidence.json --review presentation-review.json --private-key reviewer-private-key.pem --repository-revision <revision> --output presentation-review.signature.json
flowspec2 authoring-presentation-review-signature-verify authoring-evidence.json --review presentation-review.json --signature presentation-review.signature.json --public-key reviewer-public-key.pem --repository-revision <revision> --json
flowspec2 authoring-promotion-verify authoring-evidence.json --review presentation-review.json --signature presentation-review.signature.json --public-key reviewer-public-key.pem --repository-revision <revision> --json
```

The signature artifact uses the closed canonical
`flowspec2/authoring-evidence-signature@1` contract and records the Ed25519
algorithm, verified evidence digest, signature, and public-key identifier. It
does not contain the public key: the caller-managed key file or trust store is
the trust root. Private keys use unencrypted PKCS8 PEM, public keys use
SubjectPublicKeyInfo PEM, and key files are never evidence inputs or benchmark
captures. See [ADR 0004](adr/0004-evidence-authenticity.md) for the trust and
rotation decision.

Presentation-review authentication uses its own domain-separated signature
format and signs the verified review digest. Signing and authentication first
repeat evidence replay and exact subject closure. The promotion verifier
requires the evidence, review, review signature, and trusted reviewer public
key; it derives eligibility from deterministic success, presentation success,
and signature authentication without modifying any artifact.

<!-- /section:evidence-authenticity -->
<!-- section:format-promotion -->

## Format promotion

A source revision is eligible for stability only when controlled real-model
runs use equivalent tasks and execution contracts, improve authoring outcomes,
preserve every supported semantic rail, and introduce no weaker profile or
runtime boundary. Correction quality and diagnostic usability are part of the
decision; compact source size alone is not.

Presentation review is a separate report-only promotion observation. Once its
trial completes under [ADR 0006](adr/0006-authoring-presentation-review.md),
promotion requires both fully successful deterministic authoring evidence and
an authenticated passing presentation review bound to that evidence.

`flowspec/3-draft` currently has a closed preview schema and a loss-reporting
v2 migration, but no stable lowering adapter or runtime registration. It must
remain experimental until the evidence above exists and the remaining
passthrough fragments receive explicit semantics or documented exclusion.

<!-- /section:format-promotion -->
