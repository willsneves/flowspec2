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

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from langgraph.graph import END
from pydantic import BaseModel

from .domains import make_slot_model, normalize_text, parse_affirmation
from .interactive import options_from_domain
from .models import AgentResponse, ServiceState
from .predicates import evaluate
from .tools import ToolRegistry

NEXT = "__NEXT__"  # router sentinel: "the next node in the flat sequence"

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
    slot_models: dict[str, type[BaseModel]] = field(default_factory=dict)
    dependents: dict[str, set[str]] = field(default_factory=dict)  # slot -> slots that (transitively) require it
    derive_readers: dict[str, list[str]] = field(default_factory=dict)  # slot -> derive writes-keys reading it
    node_for_slot: dict[str, str] = field(default_factory=dict)  # slot -> node id that collects it
    slot_aux: dict[str, list[str]] = field(default_factory=dict)  # slot -> extra data keys to clear with it

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


def _handle_node_error(state: ServiceState, exc: Exception) -> ServiceState:
    prior = state.agent_response or AgentResponse()
    prior.error_message = str(exc)
    state.agent_response = prior
    state.status = "error"
    return state


# ── init / entry node ────────────────────────────────────────────────────────

def make_init_node(ctx: FlowContext, entry: Optional[dict[str, Any]], service_seed: dict[str, Any]) -> NodeDesc:
    async def node(state: ServiceState) -> ServiceState:
        for key, value in service_seed.items():
            state.data.setdefault(key, value)
        if entry and not state.data.get("_entry_done"):
            try:
                result = await ctx.tools.call(entry["tool"])
                if entry.get("writes"):
                    state.data[entry["writes"]] = result
            except Exception:  # blocking:false swallows failure
                if entry.get("blocking"):
                    raise
            state.data["_entry_done"] = True
        state.agent_response = None
        return state

    return NodeDesc(id="__init__", fn=node, router=lambda s: NEXT)


# ── collect (slot) node ──────────────────────────────────────────────────────

def make_collect_node(ctx: FlowContext, step: dict[str, Any], gate: Optional[dict[str, Any]]) -> NodeDesc:
    node_id = step["id"]
    slot = step["slot"]
    slot_cfg = ctx.slots[slot]
    model = ctx.model_for(slot)
    prompt = (step.get("prompt") or {}).get("text", f"Informe {slot}.")
    extract_hint = (step.get("prompt") or {}).get("extract_hint")
    if extract_hint:  # bake the hint into the schema description
        ctx.slot_models[slot] = make_slot_model(
            slot, slot_cfg["domain"], ctx.domains,
            nullable=slot_cfg.get("nullable", False), extract_hint=extract_hint,
        )
        model = ctx.slot_models[slot]
    interactive = step.get("interactive")
    skip_when = step.get("skip_when")
    max_attempts = int(slot_cfg.get("max_attempts", ctx.max_attempts))
    on_exhaust = slot_cfg.get("on_exhaust", "reask")

    def ask(state: ServiceState, error: Optional[str] = None) -> AgentResponse:
        spec = options_from_domain({**interactive, "body": interactive.get("body", prompt)}, ctx.domains, state=state, config=ctx.config) if interactive else None
        return AgentResponse(description=prompt, payload_schema=model.model_json_schema(), error_message=error, interactive=spec)

    def exhaust(state: ServiceState) -> ServiceState:
        if on_exhaust in ("skip", "default"):
            state.data[slot] = slot_cfg.get("default")
            state.agent_response = None
        elif on_exhaust == "handoff":
            state.agent_response = AgentResponse(description="Vou te encaminhar para um atendente da Central 1746.")
        elif on_exhaust == "END":
            state.status = "completed"
            state.agent_response = AgentResponse(description="Não consegui prosseguir. Tente novamente mais tarde.")
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
            return _handle_node_error(state, exc)

    return NodeDesc(id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT)


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
            key = "|".join("" if state.data.get(f) is None else str(state.data.get(f)) for f in from_slots)
            value = lookup.get(key)
            if value is None and default is not None:
                if isinstance(default, str) and default.startswith("$from["):
                    idx = int(default[len("$from["):-1])
                    value = state.data.get(from_slots[idx])
                else:
                    value = default
            if value is not None:
                state.data[writes] = value
            state.agent_response = None
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc)

    return NodeDesc(id=node_id, fn=node, router=lambda s: NEXT)


# ── confirm nodes (summary / bool / hub) ─────────────────────────────────────

_CORR_KEYWORDS = {
    "luminaria_defeito": ["defeito", "luminaria", "luminária", "problema", "tipo"],
    "luminaria_localizacao": ["local", "localizacao", "localização", "onde", "praca", "praça", "quadra"],
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
            spec = options_from_domain({**interactive, "body": interactive.get("body", prompt)}, ctx.domains, state=state, config=ctx.config) if interactive else None
            state.agent_response = AgentResponse(description=prompt, interactive=spec)
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc)

    return NodeDesc(id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT)


def make_bool_confirm_node(ctx: FlowContext, step: dict[str, Any], gate: Optional[dict[str, Any]]) -> NodeDesc:
    node_id = step["id"]
    slot = step["confirm"]
    model = ctx.model_for(slot)
    field = (step.get("interactive") or {}).get("field", slot)
    interactive = step.get("interactive")
    prompt = (step.get("prompt") or {}).get("text", "Confirma?")
    max_attempts = ctx.max_attempts

    def ask(state: ServiceState, error: Optional[str] = None) -> AgentResponse:
        spec = options_from_domain({**interactive, "body": interactive.get("body", prompt)}, ctx.domains, state=state, config=ctx.config) if interactive else None
        return AgentResponse(description=prompt, payload_schema=model.model_json_schema(), error_message=error, interactive=spec)

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
            return _handle_node_error(state, exc)

    return NodeDesc(id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT)


def make_hub_confirm_node(ctx: FlowContext, confirm: dict[str, Any], terminal_id: str) -> NodeDesc:
    node_id = confirm["step"]
    slot = confirm["slot"]
    field = (confirm.get("interactive") or {}).get("field", "confirmacao")
    interactive = confirm.get("interactive")
    prompt = (confirm.get("prompt") or {}).get("text", "Confirma os dados?")
    correctable = confirm["correctable"]
    on_confirm = confirm.get("on_confirm", terminal_id)

    def ask(state: ServiceState, description: Optional[str] = None) -> AgentResponse:
        spec = options_from_domain({**interactive, "body": interactive.get("body", prompt)}, ctx.domains, state=state, config=ctx.config) if interactive else None
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
            return _handle_node_error(state, exc)

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
    retryable = outcomes.get("retryable", {})
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
                state.agent_response = AgentResponse(
                    description="O sistema está temporariamente indisponível. Pode tentar de novo em instantes?",
                    error_message=result.get("error"),
                )
            else:  # fatal
                if fatal.get("reset_next", True):
                    state.data["_reset_on_next_call"] = True
                state.status = "error"
                state.agent_response = AgentResponse(
                    description=result.get("message", "Não foi possível concluir agora."),
                    error_message=result.get("error"),
                )
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc)

    return NodeDesc(id=node_id, fn=node, router=lambda s: END)
