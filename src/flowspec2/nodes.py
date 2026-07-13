"""Node templates + the shared compile-time context.

Every ``path`` step (and every spliced subflow node) becomes one async node
following the verified production pattern:

1. honour ``skip_when`` / gate (skip-by-vacuity) and any pending correction;
2. early-return if the slot is already filled (position is rediscovered, never
   stored);
3. if the slot arrived in ``payload`` this turn, validate it against the domain
   model → **advance** (``agent_response=None``) or **re-ask** (with
   ``error_message``, incrementing the attempt counter);
4. otherwise set ``agent_response`` (description + ``payload_schema`` +
   optional ``interactive``) and **pause**.

A node's router returns the next node id, ``END`` (pause), or the ``NEXT``
sentinel (the compiler resolves it to the following node in sequence).
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Optional, cast

from langgraph.graph import END as LANGGRAPH_END
from pydantic import BaseModel

from .domains import make_slot_model, normalize_text, parse_affirmation
from .interactive import options_from_domain
from .models import AgentResponse, ServiceState
from .observability import SnowflakeIdGenerator, log_event
from .predicates import evaluate
from .tools import ToolRegistry

END: Final[str] = LANGGRAPH_END
NEXT = "__NEXT__"  # router sentinel: "the next node in the flat sequence"
_MISSING = object()

logger = logging.getLogger(__name__)

NodeFn = Callable[[ServiceState], Awaitable[ServiceState]]
RouterFn = Callable[[ServiceState], str]


@dataclass
class NodeDesc:
    id: str
    fn: NodeFn
    router: RouterFn
    targets: list[str] = field(default_factory=list)  # literal target ids besides NEXT/END


@dataclass
class FlowContext:
    """Everything the node factories close over, computed once at compile time."""

    domains: dict[str, Any]
    slots: dict[str, Any]
    config: dict[str, Any]
    tools: ToolRegistry
    log_id_generator: SnowflakeIdGenerator
    await_external: Optional[dict[str, Any]] = None
    slot_models: dict[str, type[BaseModel]] = field(default_factory=dict)
    await_external_nodes: set[str] = field(default_factory=set)
    dependents: dict[str, set[str]] = field(
        default_factory=dict
    )  # slot -> slots that (transitively) require it
    derive_readers: dict[str, list[str]] = field(
        default_factory=dict
    )  # slot -> derive writes-keys reading it
    node_for_slot: dict[str, str] = field(default_factory=dict)  # slot -> node id that collects it
    slot_aux: dict[str, list[str]] = field(
        default_factory=dict
    )  # slot -> extra data keys to clear with it

    def model_for(self, slot: str) -> type[BaseModel]:
        if slot not in self.slot_models:
            cfg = self.slots[slot]
            self.slot_models[slot] = make_slot_model(
                slot,
                cfg["domain"],
                self.domains,
                nullable=cfg.get("nullable", False),
            )
        return self.slot_models[slot]

    @property
    def max_attempts(self) -> int:
        return int(self.config.get("max_attempts", 3))


# ── shared helpers ───────────────────────────────────────────────────────────


def _att_key(slot: str) -> str:
    return f"_attempts_{slot}"


def inc_attempts(state: ServiceState, slot: str) -> int:
    n = int(state.data.get(_att_key(slot), 0)) + 1
    state.data[_att_key(slot)] = n
    return n


def clear_cascade(state: ServiceState, slot: str, ctx: FlowContext) -> None:
    """Pop a slot + its transitive ``requires[]``-dependents + derives reading them."""
    to_clear = {slot} | ctx.dependents.get(slot, set())
    for s in to_clear:
        state.data.pop(s, None)
        state.data.pop(_att_key(s), None)
        for aux in ctx.slot_aux.get(s, []):
            state.data.pop(aux, None)
        for derived in ctx.derive_readers.get(s, []):
            state.data.pop(derived, None)


def _dig(obj: Any, path: str) -> Any:
    parts = path.split(".")
    if parts and parts[0] == "result":
        parts = parts[1:]
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _dig_binding(source: Any, path: str) -> Any:
    current = source
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _resolve_bindings(
    bindings: dict[str, Any],
    namespace: str,
    source: Any,
) -> dict[str, Any]:
    """Resolve one bounded namespace into a private pending-write mapping.

    Missing optional source paths produce no write. The compiler rejects every
    other ``$`` namespace before graph construction, so runtime evaluation stays
    deliberately smaller than an expression language.
    """
    prefix = f"${namespace}."
    resolved: dict[str, Any] = {}
    for target, binding in bindings.items():
        if isinstance(binding, str) and binding.startswith("$"):
            if not binding.startswith(prefix):
                raise ValueError(f"{target!r} must use the {prefix} namespace")
            mapped = _dig_binding(source, binding.removeprefix(prefix))
            if mapped is _MISSING:
                continue
            resolved[target] = copy.deepcopy(mapped)
        else:
            resolved[target] = copy.deepcopy(binding)
    return resolved


def _handle_node_error(
    state: ServiceState,
    exc: Exception,
    *,
    ctx: FlowContext,
    operation: str,
) -> ServiceState:
    log_id = log_event(
        logger,
        logging.ERROR,
        "Flow node failed",
        operation=operation,
        log_id_generator=ctx.log_id_generator,
        context={"flow": state.service_name, "error_type": type(exc).__name__},
        exc_info=True,
    )
    prior = state.agent_response or AgentResponse()
    prior.error_message = str(exc)
    prior.log_id = log_id
    state.agent_response = prior
    state.status = "error"
    return state


# ── external suspend/resume ─────────────────────────────────────────────────


def make_await_external_node(
    ctx: FlowContext,
    capability: dict[str, Any],
    step: Optional[dict[str, Any]] = None,
    *,
    default_router: Optional[RouterFn] = None,
    additional_targets: Optional[list[str]] = None,
    legacy_sent_data_key: Optional[str] = None,
    waiting_description: Optional[str] = None,
) -> NodeDesc:
    """Build the singular out-of-band wait primitive.

    The host delivers a resume token under ``resume_on`` or a recovery signal
    under ``_external_event``. In particular, timeout is a host event; this node
    does not own a clock or scheduler.
    """
    step = step or {}
    node_id = step.get("step") or capability.get("step") or "await_external"
    ctx.await_external_nodes.add(node_id)
    resume_on = capability["resume_on"]
    prompt = step.get("prompt") or capability.get("prompt") or {}
    description = prompt.get("text", "Conclua a ação externa para continuar.")
    interactive = step.get("interactive") or capability.get("interactive") or {}
    on_resume = capability.get("on_resume") or {}
    token_bindings = on_resume.get("set") or {}
    enrichment = on_resume.get("enrich")
    if isinstance(enrichment, str):
        # Legacy documents declared only a best-effort tool name. Subflows may
        # add compatibility inputs/outputs before calling this factory.
        enrichment = {"tool": enrichment, "optional": True, "input": {}, "set": {}}

    sent_internal_key = f"_await_external_sent:{node_id}"
    completed_internal_key = f"_await_external_completed:{node_id}"
    route_internal_key = f"_await_external_route:{node_id}"

    transitions = {
        "timeout": capability.get("timeout"),
        **(capability.get("recovery") or {}),
    }
    targets = list(additional_targets or [])
    targets.extend(
        transition["goto"]
        for transition in transitions.values()
        if transition is not None and transition["goto"] != "END"
    )
    targets = list(dict.fromkeys(targets))

    def clear_sent(state: ServiceState) -> None:
        state.internal.pop(sent_internal_key, None)
        if legacy_sent_data_key:
            state.data.pop(legacy_sent_data_key, None)

    def mark_sent(state: ServiceState) -> None:
        state.internal[sent_internal_key] = True
        if legacy_sent_data_key:
            state.data[legacy_sent_data_key] = True

    def was_sent(state: ServiceState) -> bool:
        return bool(
            state.internal.get(sent_internal_key)
            or (legacy_sent_data_key and state.data.get(legacy_sent_data_key))
        )

    async def node(state: ServiceState) -> ServiceState:
        try:
            state.internal.pop(route_internal_key, None)
            if state.internal.get(completed_internal_key):
                state.status = "progress"
                state.agent_response = None
                return state
            payload = state.payload or {}
            has_resume_token = resume_on in payload
            has_external_event = "_external_event" in payload
            if has_resume_token and has_external_event:
                raise ValueError(
                    f"await_external {node_id!r} received both {resume_on!r} and _external_event"
                )

            if has_external_event:
                external_event = payload.get("_external_event")
                if not isinstance(external_event, str) or external_event not in transitions:
                    raise ValueError(
                        f"unsupported _external_event for {node_id!r}: {external_event!r}"
                    )
                transition = transitions.get(external_event)
                if transition is None:
                    raise ValueError(
                        f"_external_event {external_event!r} is not configured for {node_id!r}"
                    )
                state.payload.pop("_external_event", None)
                clear_sent(state)
                if external_event == "resend":
                    state.internal.pop(completed_internal_key, None)
                else:
                    state.internal[completed_internal_key] = True
                state.data.update(copy.deepcopy(transition.get("set") or {}))
                transition_target = transition["goto"]
                state.internal[route_internal_key] = transition_target
                if transition_target == "END":
                    state.data["_reset_on_next_call"] = True
                    state.status = "completed"
                    state.agent_response = AgentResponse(
                        description={
                            "abort": "A ação externa foi cancelada.",
                            "timeout": "O prazo para concluir a ação externa terminou.",
                        }.get(external_event, "A ação externa foi encerrada.")
                    )
                else:
                    state.status = "progress"
                    state.agent_response = None
                return state

            if has_resume_token:
                token = payload[resume_on]
                pending_writes = _resolve_bindings(token_bindings, "token", token)
                if enrichment:
                    tool_name = enrichment["tool"]
                    try:
                        tool_inputs = _resolve_bindings(
                            enrichment.get("input") or {}, "token", token
                        )
                        enrichment_result = await ctx.tools.call(tool_name, **tool_inputs)
                        if not isinstance(enrichment_result, dict):
                            raise TypeError(f"tool {tool_name!r} returned a non-object result")
                        pending_writes.update(
                            _resolve_bindings(
                                enrichment.get("set") or {}, "result", enrichment_result
                            )
                        )
                    except Exception:
                        if not enrichment.get("optional", False):
                            raise
                        log_event(
                            logger,
                            logging.WARNING,
                            "Optional await_external enrichment failed",
                            operation=node_id,
                            log_id_generator=ctx.log_id_generator,
                            context={"tool": tool_name},
                            exc_info=True,
                        )
                state.data.update(pending_writes)
                clear_sent(state)
                state.internal[completed_internal_key] = True
                state.status = "progress"
                state.agent_response = None
                return state

            if was_sent(state):
                state.status = "progress"
                state.agent_response = AgentResponse(
                    description=waiting_description or description,
                )
                return state

            mark_sent(state)
            state.status = "progress"
            host_marker = copy.deepcopy(interactive)
            host_marker.setdefault("kind", capability["kind"])
            host_marker.setdefault("field", resume_on)
            host_marker.setdefault("out_of_band", True)
            host_marker.setdefault("next_step", node_id)
            host_marker["out_of_band_sent"] = True
            state.agent_response = AgentResponse(
                description=description,
                interactive=host_marker,
            )
            return state
        except Exception as exc:  # noqa: BLE001
            error_state = _handle_node_error(state, exc, ctx=ctx, operation=node_id)
            if error_state.agent_response:
                # A failed resume is still waiting on the same external action,
                # but the host must not interpret the retained prior response as
                # a fresh out-of-band send.
                error_state.agent_response.interactive = None
            return error_state

    def router(state: ServiceState) -> str:
        if transition_target := state.internal.pop(route_internal_key, None):
            if not isinstance(transition_target, str):
                raise TypeError(f"await_external {node_id!r} produced a non-string route target")
            return END if transition_target == "END" else transition_target
        if state.agent_response is not None:
            return END
        return default_router(state) if default_router else NEXT

    return NodeDesc(id=node_id, fn=node, router=router, targets=targets)


# ── init / entry node ────────────────────────────────────────────────────────


def make_init_node(
    ctx: FlowContext, entry: Optional[dict[str, Any]], service_seed: dict[str, Any]
) -> NodeDesc:
    async def node(state: ServiceState) -> ServiceState:
        for key, value in service_seed.items():
            state.data.setdefault(key, value)
        if entry and not state.data.get("_entry_done"):
            try:
                result = await ctx.tools.call(entry["tool"])
                if entry.get("writes"):
                    state.data[entry["writes"]] = result
            except Exception:
                if entry.get("blocking"):
                    raise
                log_event(
                    logger,
                    logging.WARNING,
                    "Non-blocking entry tool failed",
                    operation="__init__",
                    log_id_generator=ctx.log_id_generator,
                    context={"tool": entry["tool"]},
                    exc_info=True,
                )
            state.data["_entry_done"] = True
        state.agent_response = None
        return state

    return NodeDesc(id="__init__", fn=node, router=lambda s: NEXT)


# ── collect (slot) node ──────────────────────────────────────────────────────


def make_collect_node(
    ctx: FlowContext, step: dict[str, Any], gate: Optional[dict[str, Any]]
) -> NodeDesc:
    node_id = step["id"]
    slot = step["slot"]
    slot_cfg = ctx.slots[slot]
    model = ctx.model_for(slot)
    prompt = (step.get("prompt") or {}).get("text", f"Informe {slot}.")
    extract_hint = (step.get("prompt") or {}).get("extract_hint")
    if extract_hint:  # bake the hint into the schema description
        ctx.slot_models[slot] = make_slot_model(
            slot,
            slot_cfg["domain"],
            ctx.domains,
            nullable=slot_cfg.get("nullable", False),
            extract_hint=extract_hint,
        )
        model = ctx.slot_models[slot]
    interactive = step.get("interactive")
    skip_when = step.get("skip_when")
    max_attempts = int(slot_cfg.get("max_attempts", ctx.max_attempts))
    on_exhaust = slot_cfg.get("on_exhaust", "reask")

    def ask(state: ServiceState, error: Optional[str] = None) -> AgentResponse:
        spec = (
            options_from_domain(
                {**interactive, "body": interactive.get("body", prompt)},
                ctx.domains,
                state=state,
                config=ctx.config,
            )
            if interactive
            else None
        )
        return AgentResponse(
            description=prompt,
            payload_schema=model.model_json_schema(),
            error_message=error,
            interactive=spec,
        )

    def exhaust(state: ServiceState) -> ServiceState:
        if on_exhaust in ("skip", "default"):
            state.data[slot] = slot_cfg.get("default")
            state.agent_response = None
        elif on_exhaust == "handoff":
            state.agent_response = AgentResponse(
                description="Vou te encaminhar para um atendente da Central 1746."
            )
        elif on_exhaust == "END":
            state.status = "completed"
            log_id = log_event(
                logger,
                logging.WARNING,
                "Flow stopped after collection attempts were exhausted",
                operation=node_id,
                log_id_generator=ctx.log_id_generator,
                context={"flow": state.service_name, "slot": slot},
            )
            state.agent_response = AgentResponse(
                description="Não consegui prosseguir. Tente novamente mais tarde.",
                log_id=log_id,
            )
        else:  # reask
            state.agent_response = ask(state, error="máximo de tentativas — vamos tentar de novo")
            state.data.pop(_att_key(slot), None)
        return state

    async def node(state: ServiceState) -> ServiceState:
        try:
            if skip_when and evaluate(skip_when, state, ctx.config):
                state.agent_response = None
                return state
            if gate is not None and not evaluate(gate, state, ctx.config):
                state.agent_response = None  # gated out → satisfied by vacuity
                return state
            if state.data.get("correction_requested") == slot:
                clear_cascade(state, slot, ctx)
                state.data.pop("correction_requested", None)
            if slot in state.data:
                state.agent_response = None
                return state
            if state.payload and slot in state.payload:
                try:
                    validated = model.model_validate({slot: state.payload[slot]})
                    state.data[slot] = getattr(validated, slot)
                    state.data.pop(_att_key(slot), None)
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, slot) >= max_attempts:
                        return exhaust(state)
                    state.agent_response = ask(state, error=str(exc))
                    return state
            state.agent_response = ask(state)
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    return NodeDesc(
        id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT
    )


# ── derive node ──────────────────────────────────────────────────────────────


def make_derive_node(ctx: FlowContext, derive: dict[str, Any]) -> NodeDesc:
    writes = derive["writes"]
    from_slots = derive["from"]
    lookup = derive["lookup"]
    default = derive.get("default")
    node_id = f"derive_{writes}"

    async def node(state: ServiceState) -> ServiceState:
        try:
            if writes in state.data:
                state.agent_response = None
                return state
            key = "|".join(
                "" if state.data.get(f) is None else str(state.data.get(f)) for f in from_slots
            )
            value = lookup.get(key)
            if value is None and default is not None:
                if isinstance(default, str) and default.startswith("$from["):
                    idx = int(default[len("$from[") : -1])
                    value = state.data.get(from_slots[idx])
                else:
                    value = default
            if value is not None:
                state.data[writes] = value
            state.agent_response = None
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    return NodeDesc(id=node_id, fn=node, router=lambda s: NEXT)


# ── confirm nodes (summary / bool / hub) ─────────────────────────────────────

_CORR_KEYWORDS = {
    "luminaria_defeito": ["defeito", "luminaria", "luminária", "problema", "tipo"],
    "luminaria_localizacao": [
        "local",
        "localizacao",
        "localização",
        "onde",
        "praca",
        "praça",
        "quadra",
    ],
    "address": ["endereco", "endereço", "rua", "avenida", "logradouro"],
    "ponto_referencia": ["ponto", "referencia", "referência"],
    "cpf": ["cpf"],
    "email": ["email", "e-mail"],
    "name": ["nome"],
}


def match_correction(text: str, correctable: list[str]) -> Optional[str]:
    norm = normalize_text(text)
    if not norm:
        return None
    for slot in correctable:  # exact slot name first
        if normalize_text(slot) in norm:
            return slot
    for slot in correctable:
        for kw in _CORR_KEYWORDS.get(slot, [slot.split("_")[-1]]):
            if normalize_text(kw) in norm:
                return slot
    return None


def make_summary_confirm_node(ctx: FlowContext, step: dict[str, Any]) -> NodeDesc:
    node_id = step["id"]
    slot = step["confirm"]
    field = (step.get("interactive") or {}).get("field", slot)
    interactive = step.get("interactive")
    prompt = (step.get("prompt") or {}).get("text", "Confirma?")
    skip_when = step.get("skip_when")
    reject_msg = (step.get("on_reject") or {}).get("end", "Tudo bem, não vou prosseguir.")

    async def node(state: ServiceState) -> ServiceState:
        try:
            if skip_when and evaluate(skip_when, state, ctx.config):
                state.data[slot] = True  # implicitly confirmed (e.g. Flow submission)
                state.agent_response = None
                return state
            if state.data.get(slot) is True:
                state.agent_response = None
                return state
            raw = state.payload.get(field, state.payload.get(slot))
            if raw is not None:
                if parse_affirmation(raw) is True:
                    state.data[slot] = True
                    state.agent_response = None
                    return state
                state.status = "completed"
                state.agent_response = AgentResponse(description=reject_msg)
                return state
            spec = (
                options_from_domain(
                    {**interactive, "body": interactive.get("body", prompt)},
                    ctx.domains,
                    state=state,
                    config=ctx.config,
                )
                if interactive
                else None
            )
            state.agent_response = AgentResponse(description=prompt, interactive=spec)
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    return NodeDesc(
        id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT
    )


def make_bool_confirm_node(
    ctx: FlowContext, step: dict[str, Any], gate: Optional[dict[str, Any]]
) -> NodeDesc:
    node_id = step["id"]
    slot = step["confirm"]
    model = ctx.model_for(slot)
    field = (step.get("interactive") or {}).get("field", slot)
    interactive = step.get("interactive")
    prompt = (step.get("prompt") or {}).get("text", "Confirma?")
    max_attempts = ctx.max_attempts

    def ask(state: ServiceState, error: Optional[str] = None) -> AgentResponse:
        spec = (
            options_from_domain(
                {**interactive, "body": interactive.get("body", prompt)},
                ctx.domains,
                state=state,
                config=ctx.config,
            )
            if interactive
            else None
        )
        return AgentResponse(
            description=prompt,
            payload_schema=model.model_json_schema(),
            error_message=error,
            interactive=spec,
        )

    async def node(state: ServiceState) -> ServiceState:
        try:
            if gate is not None and not evaluate(gate, state, ctx.config):
                state.agent_response = None
                return state
            if slot in state.data:
                state.agent_response = None
                return state
            raw = state.payload.get(field, state.payload.get(slot))
            if raw is not None:
                try:
                    validated = model.model_validate({slot: raw})
                    state.data[slot] = getattr(validated, slot)
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, slot) >= max_attempts:
                        state.data[slot] = False
                        state.agent_response = None
                        return state
                    state.agent_response = ask(state, error=str(exc))
                    return state
            state.agent_response = ask(state)
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    return NodeDesc(
        id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT
    )


def make_hub_confirm_node(ctx: FlowContext, confirm: dict[str, Any], terminal_id: str) -> NodeDesc:
    node_id = confirm["step"]
    slot = confirm["slot"]
    field = (confirm.get("interactive") or {}).get("field", "confirmacao")
    interactive = confirm.get("interactive")
    prompt = (confirm.get("prompt") or {}).get("text", "Confirma os dados?")
    correctable = confirm["correctable"]
    on_confirm = cast(str, confirm.get("on_confirm", terminal_id))

    def ask(state: ServiceState, description: Optional[str] = None) -> AgentResponse:
        spec = (
            options_from_domain(
                {**interactive, "body": interactive.get("body", prompt)},
                ctx.domains,
                state=state,
                config=ctx.config,
            )
            if interactive
            else None
        )
        return AgentResponse(description=description or prompt, interactive=spec)

    async def node(state: ServiceState) -> ServiceState:
        try:
            if state.data.get(slot) is True:
                state.agent_response = None
                return state
            payload = state.payload or {}
            raw = payload.get(field, payload.get(slot))
            corr_text = payload.get("correcao")
            if raw is not None or corr_text is not None:
                if raw is not None and parse_affirmation(raw) is True:
                    state.data[slot] = True
                    state.agent_response = None
                    return state
                text = corr_text or (raw if isinstance(raw, str) else "")
                target = match_correction(str(text), correctable)
                if target:
                    state.data["correction_requested"] = target
                    state.data.pop(slot, None)
                    state.agent_response = None
                    return state
                state.agent_response = ask(
                    state,
                    description="O que você gostaria de corrigir? (" + ", ".join(correctable) + ")",
                )
                return state
            state.agent_response = ask(state)
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    def router(state: ServiceState) -> str:
        if state.data.get(slot) is True:
            return on_confirm
        cr = state.data.get("correction_requested")
        if cr and cr in ctx.node_for_slot:
            return ctx.node_for_slot[cr]
        return END

    targets = [on_confirm] + [ctx.node_for_slot[s] for s in correctable if s in ctx.node_for_slot]
    return NodeDesc(id=node_id, fn=node, router=router, targets=targets)


# ── terminal node ────────────────────────────────────────────────────────────


def make_terminal_node(ctx: FlowContext, terminal: dict[str, Any]) -> NodeDesc:
    node_id = terminal["step"]
    tool = terminal["tool"]
    idempotent = terminal.get("idempotent", False)
    input_map = terminal.get("input", [])
    outputs = terminal.get("outputs", {})
    outcomes = terminal.get("outcomes", {})
    success = outcomes.get("success", {})
    fatal = outcomes.get("fatal", {})

    async def node(state: ServiceState) -> ServiceState:
        try:
            inputs = {p["param"]: state.data.get(p["slot"]) for p in input_map}
            result: dict[str, Any]
            if idempotent:
                key = ToolRegistry.idempotency_key(state.user_id, tool, inputs)
                cached = ctx.tools.replay_get(key)
                if cached is not None:
                    result = cached
                else:
                    result = await ctx.tools.call(tool, **inputs)
                    if result.get("status", "success") == "success":
                        ctx.tools.replay_put(key, result)
            else:
                result = await ctx.tools.call(tool, **inputs)

            status = result.get("status", "success")
            if status == "success":
                for out_key, path in outputs.items():
                    state.data[out_key] = _dig(result, path)
                for k, v in (success.get("set") or {}).items():
                    state.data[k] = v
                if success.get("reset_next", True):
                    state.data["_reset_on_next_call"] = True
                state.status = "completed"
                protocol = state.data.get("protocol_id", "")
                state.agent_response = AgentResponse(
                    description=result.get("message", f"✅ Pronto! Protocolo: {protocol}")
                )
            elif status == "retryable":
                # preserve_state: keep everything so the next turn re-fires this node
                state.status = "error"
                log_id = log_event(
                    logger,
                    logging.WARNING,
                    "Terminal tool returned a retryable failure",
                    operation=node_id,
                    log_id_generator=ctx.log_id_generator,
                    context={"flow": state.service_name, "tool": tool},
                )
                state.agent_response = AgentResponse(
                    description="O sistema está temporariamente indisponível. Pode tentar de novo em instantes?",
                    error_message=result.get("error"),
                    log_id=log_id,
                )
            else:  # fatal
                if fatal.get("reset_next", True):
                    state.data["_reset_on_next_call"] = True
                state.status = "error"
                log_id = log_event(
                    logger,
                    logging.ERROR,
                    "Terminal tool returned a fatal failure",
                    operation=node_id,
                    log_id_generator=ctx.log_id_generator,
                    context={"flow": state.service_name, "tool": tool},
                )
                state.agent_response = AgentResponse(
                    description=result.get("message", "Não foi possível concluir agora."),
                    error_message=result.get("error"),
                    log_id=log_id,
                )
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    return NodeDesc(id=node_id, fn=node, router=lambda s: END)
