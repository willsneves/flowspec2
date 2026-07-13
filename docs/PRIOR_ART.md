<!-- section:toc -->

Table of Contents:

- Closest projects and formats: 23 <!-- section:closest-projects-and-formats -->
- Positioning decision: 39 <!-- section:positioning-decision -->
- Compatibility strategy: 60 <!-- section:compatibility-strategy -->

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
