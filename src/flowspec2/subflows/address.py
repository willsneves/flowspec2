"""address@1 — geocode + optional confirmation.

Collects a free-text address, geocodes/validates it via the injectable
``geocode`` tool, optionally confirms it (Sim/Não), and exposes the ``address``
slot (a dict carrying ``kind`` so the praça gate can read ``address.kind``).
"""

from __future__ import annotations

from typing import Any

from ..domains import parse_affirmation
from ..interactive import options_from_domain
from ..models import AgentResponse, ServiceState
from ..nodes import NEXT, FlowContext, NodeDesc, clear_cascade
from langgraph.graph import END
from . import SubflowBuild

_AUX = ["address_confirmed", "address_needs_confirmation", "address_attempts"]

# The payload_schema the engine hands to constrained decoding so a generic LLM
# driver knows to extract the free-text address into the `address` key.
_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"address": {"type": "string", "description": "Endereço completo: rua/avenida, número e bairro."}},
    "required": ["address"],
}


class AddressSubflow:
    name = "address"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        needs_confirmation = with_cfg.get("needs_confirmation", True)
        max_attempts = int(with_cfg.get("max_attempts", ctx.max_attempts))

        ctx.node_for_slot["address"] = "collect_address"
        ctx.slot_aux["address"] = _AUX
        ctx.dependents.setdefault("address", set())  # populated by the compiler's requires pass

        # ── collect_address ──────────────────────────────────────────────
        async def collect(state: ServiceState) -> ServiceState:
            if state.data.get("correction_requested") == "address":
                # clear address + its aux + every requires-dependent (quadra,
                # ponto_referencia) so a corrected address never submits stale
                # downstream data.
                clear_cascade(state, "address", ctx)
                state.data.pop("correction_requested", None)
            if state.data.get("address") and state.data.get("address_confirmed"):
                state.agent_response = None
                return state
            raw = state.payload.get("address")
            if raw:
                result = await ctx.tools.call("geocode", address=str(raw))
                if result.get("status") == "ok":
                    state.data["address"] = result["address"]
                    if needs_confirmation and result.get("needs_confirmation"):
                        state.data["address_needs_confirmation"] = True
                        state.agent_response = None
                        return state
                    state.data["address_confirmed"] = True
                    state.agent_response = None
                    return state
                n = int(state.data.get("address_attempts", 0)) + 1
                state.data["address_attempts"] = n
                state.agent_response = AgentResponse(
                    description="Não encontrei esse endereço. Pode informar rua, número e bairro?",
                    payload_schema=_ADDRESS_SCHEMA,
                    error_message=result.get("error"),
                )
                return state
            state.agent_response = AgentResponse(
                description="Qual o endereço completo (rua, número, bairro)?",
                payload_schema=_ADDRESS_SCHEMA,
            )
            return state

        def collect_router(state: ServiceState) -> str:
            if state.data.get("address_confirmed"):
                return "address_done"
            if state.data.get("address") and state.data.get("address_needs_confirmation"):
                return "confirm_address"
            return END  # asking

        # ── confirm_address ──────────────────────────────────────────────
        async def confirm(state: ServiceState) -> ServiceState:
            if state.data.get("address_confirmed"):
                state.agent_response = None
                return state
            raw = state.payload.get("address_confirmacao", state.payload.get("confirmacao"))
            if raw is not None:
                if parse_affirmation(raw) is True:
                    state.data["address_confirmed"] = True
                    state.agent_response = None
                    return state
                for key in ["address", *_AUX]:
                    state.data.pop(key, None)
                state.agent_response = None
                return state
            addr = state.data.get("address") or {}
            body = f"Confirma o endereço: {addr.get('logradouro', '')}, {addr.get('bairro', '')}?"
            spec = options_from_domain(
                {"kind": "buttons", "field": "confirmacao", "from_domain": "SimNao", "body": body},
                ctx.domains, state=state, config=ctx.config,
            ) if "SimNao" in ctx.domains else None
            state.agent_response = AgentResponse(description=body, interactive=spec)
            return state

        def confirm_router(state: ServiceState) -> str:
            if state.data.get("address_confirmed"):
                return "address_done"
            if not state.data.get("address"):
                return "collect_address"  # rejected → re-collect
            return END  # asking

        # ── exit node ────────────────────────────────────────────────────
        async def done(state: ServiceState) -> ServiceState:
            state.agent_response = None
            return state

        descriptors = [
            NodeDesc("collect_address", collect, collect_router, targets=["confirm_address", "address_done"]),
            NodeDesc("confirm_address", confirm, confirm_router, targets=["collect_address", "address_done"]),
            NodeDesc("address_done", done, lambda s: NEXT),
        ]
        return SubflowBuild(descriptors=descriptors, entry_id="collect_address", node_for_slot={"address": "collect_address"})
