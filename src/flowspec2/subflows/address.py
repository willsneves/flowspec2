"""address@1 — geocode + optional confirmation.

Collects a free-text address, geocodes/validates it via the injectable
``geocode`` tool, optionally confirms it (Sim/Não), and exposes the ``address``
slot (a dict carrying ``kind`` so the praça gate can read ``address.kind``).
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal

from langgraph.graph import END

from ..domains import parse_affirmation
from ..interactive import options_from_domain
from ..models import CORRECTION_REQUESTED_INTERNAL_KEY, AgentResponse, ServiceState
from ..nodes import (
    NEXT,
    FlowContext,
    NodeDesc,
    _handle_node_error,
    clear_cascade,
    mark_flow_finished,
)
from ..observability import log_event
from . import SubflowBuild

_ADDRESS_COMPLETED_KEY = "address_completed"
_ADDRESS_DEFAULTED_KEY = "address_defaulted"
_ADDRESS_SKIPPED_KEY = "address_skipped"
_ADDRESS_EXHAUSTION_MODES = frozenset({"reask", "skip", "default", "handoff", "END"})
_AddressCompletionOutcome = Literal["confirmed", "skipped", "defaulted"]
_AUX = [
    "address_confirmed",
    "address_needs_confirmation",
    "address_attempts",
    _ADDRESS_COMPLETED_KEY,
    _ADDRESS_DEFAULTED_KEY,
    _ADDRESS_SKIPPED_KEY,
]
_ADDRESS_NOT_RESOLVED_ERROR = "não foi possível localizar o endereço"
_ADDRESS_REQUIRED_ERROR = "o endereço é obrigatório"
_ADDRESS_CONFIRMATION_ERROR = "não consegui identificar se a resposta foi sim ou não"
_ADDRESS_EXHAUSTED_ERROR = "máximo de tentativas — vamos tentar de novo"
_ADDRESS_HANDOFF_DESCRIPTION = "Vou te encaminhar para um atendente da Central 1746."
_ADDRESS_END_DESCRIPTION = "Não consegui validar o endereço. Tente novamente mais tarde."
_ADDRESS_PROMPT = "Qual o endereço completo (rua, número, bairro)?"
_OPTIONAL_ADDRESS_PROMPT = f"{_ADDRESS_PROMPT} Se preferir não informar, você pode pular."
logger = logging.getLogger(__name__)


def _address_payload_schema(required: bool) -> dict[str, Any]:
    """Describe a required address or an optional address with an explicit null skip."""

    address_type: str | list[str] = "string" if required else ["string", "null"]
    address_description = "Endereço completo: rua/avenida, número e bairro."
    if not required:
        address_description += " Use null quando a pessoa optar por não informar."
    return {
        "type": "object",
        "properties": {
            "address": {
                "type": address_type,
                "description": address_description,
            }
        },
        "required": ["address"],
    }


def _address_is_complete(state: ServiceState) -> bool:
    return bool(state.data.get(_ADDRESS_COMPLETED_KEY) or state.data.get("address_confirmed"))


def _complete_address(state: ServiceState, *, outcome: _AddressCompletionOutcome) -> None:
    state.data[_ADDRESS_COMPLETED_KEY] = True
    state.data.pop("address_attempts", None)
    if outcome == "confirmed":
        state.data["address_confirmed"] = True
    elif outcome == "skipped":
        state.data.pop("address", None)
        state.data[_ADDRESS_SKIPPED_KEY] = True
    elif outcome == "defaulted":
        state.data["address"] = None
        state.data[_ADDRESS_DEFAULTED_KEY] = True
    state.agent_response = None


class AddressSubflow:
    name = "address"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        required = bool(with_cfg.get("required", ctx.config.get("address_required", False)))
        needs_confirmation = bool(with_cfg.get("needs_confirmation", True))
        max_attempts = int(with_cfg.get("max_attempts", ctx.max_attempts))
        on_exhaust = str(with_cfg.get("on_exhaust", "reask"))
        if max_attempts < 1:
            raise ValueError("address@1 max_attempts must be positive")
        if on_exhaust not in _ADDRESS_EXHAUSTION_MODES:
            raise ValueError(f"address@1 has unsupported on_exhaust mode: {on_exhaust!r}")
        payload_schema = _address_payload_schema(required)
        prompt = _ADDRESS_PROMPT if required else _OPTIONAL_ADDRESS_PROMPT

        ctx.slot_aux["address"] = _AUX
        ctx.dependents.setdefault("address", set())  # populated by the compiler's requires pass

        def ask(error_message: str | None = None) -> AgentResponse:
            return AgentResponse(
                description=prompt,
                payload_schema=payload_schema,
                error_message=error_message,
            )

        def confirmation_response(
            state: ServiceState,
            error_message: str | None = None,
        ) -> AgentResponse:
            resolved_address = state.data.get("address") or {}
            body = (
                "Confirma o endereço: "
                f"{resolved_address.get('logradouro', '')}, {resolved_address.get('bairro', '')}?"
            )
            interactive_specification = (
                options_from_domain(
                    {
                        "kind": "buttons",
                        "field": "confirmacao",
                        "from_domain": "SimNao",
                        "body": body,
                    },
                    ctx.domains,
                    state=state,
                    config=ctx.config,
                )
                if "SimNao" in ctx.domains
                else None
            )
            return AgentResponse(
                description=body,
                error_message=error_message,
                interactive=interactive_specification,
            )

        def exhaust(
            state: ServiceState,
            *,
            reask_response: AgentResponse | None = None,
        ) -> ServiceState:
            if on_exhaust == "skip":
                _complete_address(state, outcome="skipped")
            elif on_exhaust == "default":
                _complete_address(state, outcome="defaulted")
            elif on_exhaust == "handoff":
                state.agent_response = AgentResponse(description=_ADDRESS_HANDOFF_DESCRIPTION)
            elif on_exhaust == "END":
                mark_flow_finished(state, reset_next=True)
                state.status = "completed"
                log_id = log_event(
                    logger,
                    logging.WARNING,
                    "Flow stopped after address collection attempts were exhausted",
                    operation="collect_address",
                    log_id_generator=ctx.log_id_generator,
                    context={"flow": state.service_name, "slot": "address"},
                )
                state.agent_response = AgentResponse(
                    description=_ADDRESS_END_DESCRIPTION,
                    log_id=log_id,
                )
            else:
                state.data.pop("address_attempts", None)
                state.agent_response = reask_response or ask(_ADDRESS_EXHAUSTED_ERROR)
            return state

        def register_failed_attempt(
            state: ServiceState,
            error_message: str,
            *,
            response_factory: Callable[[str | None], AgentResponse] = ask,
        ) -> ServiceState:
            address_attempts = int(state.data.get("address_attempts", 0)) + 1
            state.data["address_attempts"] = address_attempts
            if address_attempts >= max_attempts:
                reask_response = (
                    response_factory(_ADDRESS_EXHAUSTED_ERROR) if on_exhaust == "reask" else None
                )
                return exhaust(
                    state,
                    reask_response=reask_response,
                )
            state.agent_response = response_factory(error_message)
            return state

        # ── collect_address ──────────────────────────────────────────────
        async def collect(state: ServiceState) -> ServiceState:
            state.status = "progress"
            if state.internal.get(CORRECTION_REQUESTED_INTERNAL_KEY) == "address":
                # clear address + its aux + every requires-dependent (quadra,
                # ponto_referencia) so a corrected address never submits stale
                # downstream data.
                clear_cascade(state, "address", ctx)
                state.internal.pop(CORRECTION_REQUESTED_INTERNAL_KEY, None)
            if _address_is_complete(state):
                state.agent_response = None
                return state
            if "address" in state.payload:
                address_input = state.payload["address"]
                if address_input is None:
                    if required:
                        return register_failed_attempt(state, _ADDRESS_REQUIRED_ERROR)
                    _complete_address(state, outcome="skipped")
                    return state
                resolved_address: Mapping[str, Any] | None = None
                try:
                    geocode_result = await ctx.tools.call("geocode", address=str(address_input))
                    if not isinstance(geocode_result, Mapping):
                        raise TypeError("geocode returned a non-object result")
                    if geocode_result.get("status") == "ok":
                        resolved_address = geocode_result.get("address")
                        if not isinstance(resolved_address, Mapping):
                            raise ValueError("geocode success did not contain an address object")
                        street_name = resolved_address.get("logradouro")
                        if not isinstance(street_name, str) or not street_name.strip():
                            raise ValueError("geocode success did not contain a meaningful address")
                except Exception as exc:  # noqa: BLE001
                    error_state = _handle_node_error(
                        state,
                        exc,
                        ctx=ctx,
                        operation="collect_address",
                    )
                    assert error_state.agent_response is not None
                    error_state.agent_response.description = (
                        "Não consegui consultar o endereço agora. Pode tentar novamente?"
                    )
                    error_state.agent_response.payload_schema = payload_schema
                    return error_state

                if geocode_result.get("status") == "ok":
                    assert isinstance(resolved_address, Mapping)
                    state.data["address"] = copy.deepcopy(dict(resolved_address))
                    state.data.pop("address_attempts", None)
                    if needs_confirmation and geocode_result.get("needs_confirmation"):
                        state.data["address_needs_confirmation"] = True
                        state.agent_response = None
                        return state
                    _complete_address(state, outcome="confirmed")
                    return state
                return register_failed_attempt(
                    state,
                    str(geocode_result.get("error") or _ADDRESS_NOT_RESOLVED_ERROR),
                )
            state.agent_response = ask()
            return state

        def collect_router(state: ServiceState) -> str:
            if _address_is_complete(state):
                return "address_done"
            if state.data.get("address") and state.data.get("address_needs_confirmation"):
                return "confirm_address"
            return END  # asking

        # ── confirm_address ──────────────────────────────────────────────
        async def confirm(state: ServiceState) -> ServiceState:
            if _address_is_complete(state):
                state.agent_response = None
                return state
            confirmation_field = (
                "address_confirmacao" if "address_confirmacao" in state.payload else "confirmacao"
            )
            if confirmation_field in state.payload:
                confirmation = parse_affirmation(state.payload[confirmation_field])
                if confirmation is True:
                    _complete_address(state, outcome="confirmed")
                    return state
                if confirmation is False:
                    for key in ["address", *_AUX]:
                        state.data.pop(key, None)
                    state.agent_response = None
                    return state
                return register_failed_attempt(
                    state,
                    _ADDRESS_CONFIRMATION_ERROR,
                    response_factory=lambda error_message: confirmation_response(
                        state, error_message
                    ),
                )
            state.agent_response = confirmation_response(state)
            return state

        def confirm_router(state: ServiceState) -> str:
            if _address_is_complete(state):
                return "address_done"
            if not state.data.get("address"):
                return "collect_address"  # rejected → re-collect
            return END  # asking

        # ── exit node ────────────────────────────────────────────────────
        async def done(state: ServiceState) -> ServiceState:
            state.agent_response = None
            return state

        descriptors = [
            NodeDesc(
                "collect_address",
                collect,
                collect_router,
                targets=["confirm_address", "address_done"],
            ),
            NodeDesc(
                "confirm_address",
                confirm,
                confirm_router,
                targets=["collect_address", "address_done"],
            ),
            NodeDesc("address_done", done, lambda s: NEXT),
        ]
        return SubflowBuild(
            descriptors=descriptors,
            entry_id="collect_address",
            node_for_slot={"address": "collect_address"},
        )
