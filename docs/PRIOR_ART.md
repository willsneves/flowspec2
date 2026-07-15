<!-- section:toc -->

Table of Contents:

- Closest projects and formats: 24 <!-- section:closest-projects-and-formats -->
- Positioning decision: 44 <!-- section:positioning-decision -->
- Borrowed constraints: 65 <!-- section:borrowed-constraints -->
- Compatibility strategy: 91 <!-- section:compatibility-strategy -->

<!-- /section:toc -->

# Prior art and positioning

Reviewed on 2026-07-13.

flowspec2 is not the first system to combine conversation state, slot collection,
guards, tool calls, and reusable subflows. Its narrower contribution is a
self-contained conversational document whose closed value domains become both
runtime validators and LLM-facing extraction schemas, while a compiler derives
the executable graph and lifecycle behavior.

<!-- section:closest-projects-and-formats -->

## Closest projects and formats

| Project or format | Shared objective | Important difference | flowspec2 position |
|---|---|---|---|
| [Rasa CALM flows](https://rasa.com/docs/reference/primitives/flows/) | Declarative conversational business logic with ordered collection, actions, conditions, and child flows | A Rasa assistant spreads behavior across flow and domain files and relies on the Rasa runtime and conversation-repair patterns | Closest direct peer; supported through the bounded portable profile described in [COMPATIBILITY.md](COMPATIBILITY.md) |
| [Pipecat Flows](https://docs.pipecat.ai/api-reference/pipecat-flows/overview) | Structured, stateful conversations in an LLM-driven voice pipeline | Nodes and transitions are configured through a Python runtime API and can be created dynamically | A runtime/library analogue, not the canonical interchange format for flowspec2 |
| [Dialogflow CX](https://cloud.google.com/dialogflow/cx/docs/concept/page) | Page-based conversational state machines with forms, parameters, routes, and fulfillment | Managed platform resources, APIs, and console configuration replace a portable, self-contained source document | A product-level analogue; no compatibility adapter is currently promised |
| [Microsoft Agent Framework declarative workflows](https://learn.microsoft.com/en-us/agent-framework/workflows/declarative) | YAML workflow definitions compiled into executable workflow graphs | General agent orchestration with action kinds and an expression language, rather than a closed conversational-domain spine | Evidence that declarative graph compilation is a useful model, but not a replacement for the conversational contract |
| [Open Workflow Specification](https://serverlessworkflow.io/) | Vendor-neutral JSON/YAML workflow DSL with calls, events, branching, reuse, and fault handling | General workflow semantics do not define conversational prompting, LLM extraction, corrections, or closed token domains | Standards envelope for the flowspec2 conversational profile; not the native authoring model |
| [OpenAPI Arazzo](https://spec.openapis.org/arazzo/latest.html) | Declarative API-call sequences, typed inputs/outputs, success criteria, and asynchronous operations | Runtime expressions and external references are broader than the closed flowspec2 linker | Reference for parse-before-resolve linking, operation identity, correlation, and precise asynchronous boundaries |
| [BPMN](https://www.omg.org/spec/BPMN/2.0.2/PDF) | Durable processes, messages, correlation, subprocesses, cancellation, and compensation | The complete process metamodel and diagram/XML surface are far broader than conversational collection | Reference for separating cancellation from compensation; compensation stays out until flows can commit multiple effects |
| [DMN](https://www.omg.org/spec/DMN/1.5) | Typed decision tables and explicit rule hit policies | FEEL and the full decision metamodel would widen the expression surface substantially | Decision tables remain deferred unless real flows demonstrate that the closed predicate grammar is insufficient |
| [CMMN](https://www.omg.org/spec/CMMN/) | Human-led cases with milestones and partially ordered work | Discretionary planning conflicts with compiler-proven rails and liveness | Not a core model; reconsider only if operator-directed adaptive case work enters scope |
| [VoiceXML](https://www.w3.org/TR/voicexml21/) | Declarative forms, prompts, field collection, validation, and event handling for voice dialogs | Telephony-oriented XML and grammar execution predate tool-using LLM agents | Formal ancestor for declarative conversational collection |
| [SCXML](https://www.w3.org/TR/scxml/) | Executable state-machine notation with events and transitions | Explicit general statecharts require authors to model graph mechanics that flowspec2 synthesizes | Formal ancestor for graph semantics, not the preferred authoring surface |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Stateful graph runtime for long-running agents and workflows | Runtime primitives and deployment configuration do not define this project's conversational source format | Execution target of the reference compiler |

<!-- /section:closest-projects-and-formats -->
<!-- section:positioning-decision -->

## Positioning decision

flowspec2 remains specialized instead of becoming a generic workflow language.
The specialization is intentional:

- A domain declaration produces the accepted token set, deterministic
  normalization, Pydantic validation, constrained-decoding schema, and static
  interactive options.
- A flat conversational path remains the authoring surface; graph edges, pauses,
  correction cascades, and terminal lifecycle are compiler responsibilities.
- External waits and channel interactions remain explicit host/runtime contracts,
  rather than being approximated as ordinary user-input slots.
- Compatibility is a projection with evidence. A target artifact is never
  presented as equivalent when a source rail could not be preserved.

This positioning is recorded in
[ADR 0001](adr/0001-interoperability-boundaries.md).

<!-- /section:positioning-decision -->
<!-- section:borrowed-constraints -->

## Borrowed constraints

The comparison changes contracts, not the format's specialization:

- Rasa CALM validates a compact conversational command boundary: language models
  may identify a flow, value, correction, or recovery intent, while the runtime
  owns transitions. FlowSpec2 therefore exposes exact closed correction and
  interaction schemas instead of accepting fuzzy control text.
- SCXML and Open Workflow make deterministic traces and conformance corpora more
  useful than prose-only lifecycle claims. FlowSpec2 conformance covers source
  validation, ordered diagnostics, canonical IR/digests, and executable traces.
- Arazzo makes asynchronous correlation part of the operation contract rather
  than host convention. External waits therefore pin a versioned token schema,
  correlation path, and duplicate/late-delivery policy.
- BPMN distinguishes ending a pending interaction from undoing committed work.
  FlowSpec2 models cancel/recovery now and defers compensation until multiple
  committed effects make a reverse-order contract necessary.
- General expression languages, remote references, discretionary planning,
  arbitrary author-defined loops, parallel task orchestration, and open
  extension fields remain outside the stable core. They would make generation
  easier to improvise but harder to link, prove, migrate, and execute
  reproducibly.

<!-- /section:borrowed-constraints -->
<!-- section:compatibility-strategy -->

## Compatibility strategy

Rasa CALM receives a portable subset because its native conversational concepts
have direct counterparts for linear slot collection. Open Workflow receives a
profile because its standard metadata and custom-call surfaces can carry the full
flowspec2 document without claiming that an unmodified generic runtime knows how
to execute conversational tasks.

Other systems remain prior art rather than conversion targets. An adapter should
only be added when its executable overlap can be stated as a falsifiable profile,
validated locally, and accompanied by complete incompatibility diagnostics.

<!-- /section:compatibility-strategy -->
