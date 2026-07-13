"""identification@2 — method select → gov.br (await_external) | CPF → email → name.

Exposes the ``cpf``/``email``/``name`` slots. gov.br is modelled as an
out-of-band suspend/resume: the first visit emits the login affordance and
pauses; a later turn carrying a ``govbr_token`` resumes and populates the slots.
Refusal/abort (when identification is optional) routes to anonymous. Every
collection leg caps re-asks at ``max_attempts`` and dispatches the configured
``on_exhaust`` policy.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any, Final, Literal, Optional, cast

from langgraph.graph import END as LANGGRAPH_END

from ..domains import normalize_text
from ..models import CORRECTION_REQUESTED_INTERNAL_KEY, AgentResponse, ServiceState
from ..nodes import (
    NEXT,
    FlowContext,
    NodeDesc,
    clear_cascade,
    inc_attempts,
    make_await_external_node,
    mark_flow_finished,
    reset_attempts,
)
from ..observability import log_event
from . import SubflowBuild

_REFUSAL = (
    "anonim",
    "pular",
    "sem ident",
    "nao quero",
    "nao me ident",
    "recus",
    "nenhum",
    "sem cpf",
    "skip",
)
END: Final[str] = LANGGRAPH_END
_EXHAUSTION_MODES = frozenset({"reask", "skip", "default", "handoff", "END"})
_EXHAUSTED_ERROR = "máximo de tentativas — vamos tentar de novo"
_HANDOFF_DESCRIPTION = "Vou te encaminhar para um atendente da Central 1746."
_END_DESCRIPTION = "Não consegui concluir a identificação. Tente novamente mais tarde."
_CPF_LOOKUP_DERIVED_MARKERS: Final[dict[str, str]] = {
    "email": "_cpf_lookup_derived:email",
    "name": "_cpf_lookup_derived:name",
}
_IdentificationStage = Literal["method", "cpf", "email", "name"]
_IdentificationMethod = Literal["cpf", "govbr", "anonimo"]
_ExhaustionMode = Literal["reask", "skip", "default", "handoff", "END"]

logger = logging.getLogger(__name__)


def _is_refusal(text: str) -> bool:
    norm = normalize_text(text)
    return bool(norm) and any(tok in norm for tok in _REFUSAL)


# Exact-token skip for the optional contact slots (email/name/cpf). Exact match —
# not substring — so a real value like "skipper@x.com" is never read as a skip.
_SKIP_TOKENS = {"pular", "skip", "nao", "nenhum", "passar", "proximo", "depois"}


def _is_skip(text: str) -> bool:
    return normalize_text(text) in _SKIP_TOKENS


def _clear_cpf_lookup_derived_contacts(state: ServiceState, ctx: FlowContext) -> None:
    for contact_slot, marker_key in _CPF_LOOKUP_DERIVED_MARKERS.items():
        if state.internal.pop(marker_key, False):
            clear_cascade(state, contact_slot, ctx)


def _mark_cpf_lookup_derived_contact(state: ServiceState, contact_slot: str) -> None:
    state.internal[_CPF_LOOKUP_DERIVED_MARKERS[contact_slot]] = True


def _clear_cpf_lookup_provenance(state: ServiceState, contact_slot: str) -> None:
    state.internal.pop(_CPF_LOOKUP_DERIVED_MARKERS[contact_slot], None)


def _govbr_validation_failure_response(
    ctx: FlowContext,
    state: ServiceState,
    *,
    cpf_fallback: bool = True,
) -> AgentResponse:
    log_id = log_event(
        logger,
        logging.WARNING,
        "gov.br authentication response could not be validated",
        operation="authenticate_govbr",
        log_id_generator=ctx.log_id_generator,
        context={"flow": state.service_name},
    )
    return AgentResponse(
        description=(
            "Não consegui validar o gov.br. Vamos tentar pelo CPF?"
            if cpf_fallback
            else "Não consegui validar o gov.br. Tente novamente."
        ),
        log_id=log_id,
    )


def _normalize_method(raw: str) -> Optional[_IdentificationMethod]:
    norm = normalize_text(raw)
    if "govbr" in norm or "gov.br" in norm or norm == "gov" or "gov br" in norm:
        return "govbr"
    if "cpf" in norm:
        return "cpf"
    if _is_refusal(norm) or norm == "anonimo":
        return "anonimo"
    return None


async def _call_optional_identification_tool(
    ctx: FlowContext,
    tool_name: str,
    **tool_inputs: Any,
) -> Optional[dict[str, Any]]:
    """Call a subflow enrichment whose contract is explicitly best-effort."""
    try:
        tool_result = await ctx.tools.call(tool_name, **tool_inputs)
        if not isinstance(tool_result, Mapping):
            raise TypeError(f"tool {tool_name!r} returned a non-object result")
        return dict(tool_result)
    except Exception:
        log_event(
            logger,
            logging.WARNING,
            "Optional identification enrichment failed",
            operation="identification",
            log_id_generator=ctx.log_id_generator,
            context={"tool": tool_name},
            exc_info=True,
        )
        return None


class IdentificationSubflow:
    name = "identification"
    major = 2

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        required = bool(with_cfg.get("required", ctx.config.get("identification_required", False)))
        methods: tuple[_IdentificationMethod, ...] = tuple(
            cast(list[_IdentificationMethod], with_cfg.get("methods", ["cpf", "govbr", "anonimo"]))
        )
        eligible_methods: tuple[_IdentificationMethod, ...] = tuple(
            method for method in methods if method != "anonimo" or not required
        )
        if not eligible_methods:
            raise ValueError(
                "identification@2 requires an eligible method; required identification "
                "must configure cpf or govbr"
            )
        eligible_method_set: frozenset[_IdentificationMethod] = frozenset(eligible_methods)
        max_attempts = int(with_cfg.get("max_attempts", ctx.max_attempts))
        configured_on_exhaust = str(with_cfg.get("on_exhaust", "reask"))
        if max_attempts < 1:
            raise ValueError("identification@2 max_attempts must be positive")
        if configured_on_exhaust not in _EXHAUSTION_MODES:
            raise ValueError(
                f"identification@2 has unsupported on_exhaust mode: {configured_on_exhaust!r}"
            )
        on_exhaust = cast(_ExhaustionMode, configured_on_exhaust)

        # register the slots this subflow contributes
        ctx.domains.setdefault("CPF", {"type": "cpf"})
        ctx.domains.setdefault("Email", {"type": "email"})
        ctx.domains.setdefault("Name", {"type": "name"})
        ctx.slots.setdefault("cpf", {"domain": "CPF"})
        ctx.slots.setdefault("email", {"domain": "Email"})
        ctx.slots.setdefault("name", {"domain": "Name"})
        ctx.slot_aux["cpf"] = ["cadastro_verificado", "identificacao_pulada"]
        ctx.slot_aux["email"] = ["email_processed"]
        ctx.slot_aux["name"] = ["name_processed"]
        cpf_model = ctx.model_for("cpf")
        email_model = ctx.model_for("email")
        name_model = ctx.model_for("name")

        def set_identification_method(
            state: ServiceState,
            identification_method: _IdentificationMethod,
        ) -> None:
            if identification_method not in eligible_method_set:
                raise ValueError(
                    "identification@2 attempted to select disabled method "
                    f"{identification_method!r}"
                )
            state.data["identification_method"] = identification_method
            if identification_method == "anonimo":
                state.data["identificacao_pulada"] = True
            else:
                state.data.pop("identificacao_pulada", None)

        def alternative_method(
            state: ServiceState,
            *,
            prefer_anonymous: bool,
        ) -> _IdentificationMethod | None:
            current_method = state.data.get("identification_method")
            if prefer_anonymous and "anonimo" in eligible_method_set:
                return "anonimo"
            return next(
                (method for method in eligible_methods if method != current_method),
                None,
            )

        def validate_optional_lookup_contact(
            state: ServiceState,
            contact_slot: Literal["email", "name"],
            raw_contact: Any,
        ) -> str | None:
            try:
                validated_contact_model = ctx.model_for(contact_slot).model_validate(
                    {contact_slot: raw_contact}
                )
            except Exception:  # noqa: BLE001
                log_event(
                    logger,
                    logging.WARNING,
                    "CPF lookup contact failed slot validation",
                    operation="cpf_lookup",
                    log_id_generator=ctx.log_id_generator,
                    context={
                        "flow": state.service_name,
                        "slot": contact_slot,
                    },
                    exc_info=True,
                )
                return None
            return cast(str, getattr(validated_contact_model, contact_slot))

        def complete_without_value(
            state: ServiceState,
            identification_stage: _IdentificationStage,
            *,
            reask_response: AgentResponse,
        ) -> ServiceState:
            reset_attempts(state, identification_stage)
            if identification_stage in {"email", "name"}:
                _clear_cpf_lookup_provenance(state, identification_stage)
                state.data.pop(identification_stage, None)
                state.data[f"{identification_stage}_processed"] = True
                state.agent_response = None
                return state

            if identification_stage == "cpf":
                _clear_cpf_lookup_derived_contacts(state, ctx)
                state.data.pop("cpf", None)
            fallback_method = alternative_method(state, prefer_anonymous=True)
            if fallback_method is None:
                state.agent_response = reask_response
                return state
            set_identification_method(state, fallback_method)
            state.agent_response = None
            return state

        def complete_with_default(
            state: ServiceState,
            identification_stage: _IdentificationStage,
            *,
            reask_response: AgentResponse,
        ) -> ServiceState:
            if identification_stage in {"email", "name"}:
                return complete_without_value(
                    state,
                    identification_stage,
                    reask_response=reask_response,
                )

            reset_attempts(state, identification_stage)
            current_method = state.data.get("identification_method")
            default_method = (
                "anonimo"
                if identification_stage == "cpf" and "anonimo" in eligible_method_set
                else eligible_methods[0]
            )
            if identification_stage == "cpf":
                _clear_cpf_lookup_derived_contacts(state, ctx)
                state.data.pop("cpf", None)
            set_identification_method(state, default_method)
            state.agent_response = reask_response if default_method == current_method else None
            return state

        def exhaust(
            state: ServiceState,
            *,
            stage: _IdentificationStage,
            reask_response: AgentResponse,
        ) -> ServiceState:
            if on_exhaust == "reask":
                reset_attempts(state, stage)
                state.agent_response = reask_response
            elif on_exhaust == "skip":
                complete_without_value(
                    state,
                    stage,
                    reask_response=reask_response,
                )
            elif on_exhaust == "default":
                complete_with_default(
                    state,
                    stage,
                    reask_response=reask_response,
                )
            elif on_exhaust == "handoff":
                state.agent_response = AgentResponse(description=_HANDOFF_DESCRIPTION)
            else:
                mark_flow_finished(state, reset_next=True)
                state.status = "completed"
                log_id = log_event(
                    logger,
                    logging.WARNING,
                    "Flow stopped after identification attempts were exhausted",
                    operation=f"identification:{stage}",
                    log_id_generator=ctx.log_id_generator,
                    context={"flow": state.service_name, "stage": stage},
                )
                state.agent_response = AgentResponse(
                    description=_END_DESCRIPTION,
                    log_id=log_id,
                )
            return state

        def method_response(error_message: str | None = None) -> AgentResponse:
            buttons = [
                {
                    "id": method,
                    "title": {
                        "cpf": "CPF",
                        "govbr": "Gov.br",
                        "anonimo": "Sem me identificar",
                    }[method],
                }
                for method in eligible_methods
            ]
            return AgentResponse(
                description="Como prefere se identificar?",
                error_message=error_message,
                interactive={
                    "body": "Como prefere se identificar?",
                    "field": "identification_method",
                    "buttons": buttons,
                },
            )

        def recover_from_govbr_validation_failure(state: ServiceState) -> ServiceState:
            state.data.pop("govbr_auth_sent", None)
            if "cpf" in eligible_method_set:
                set_identification_method(state, "cpf")
                state.agent_response = _govbr_validation_failure_response(ctx, state)
                return state

            validation_failure_response = _govbr_validation_failure_response(
                ctx,
                state,
                cpf_fallback=False,
            )
            if inc_attempts(state, "method") >= max_attempts:
                return exhaust(
                    state,
                    stage="method",
                    reask_response=validation_failure_response,
                )
            state.agent_response = validation_failure_response
            return state

        def reject_disabled_method(
            state: ServiceState,
            requested_method: _IdentificationMethod,
        ) -> ServiceState:
            state.data.pop("govbr_auth_sent", None)
            unavailable_method_response = method_response(
                f"o método {requested_method!r} não está disponível neste atendimento"
            )
            if inc_attempts(state, "method") >= max_attempts:
                return exhaust(
                    state,
                    stage="method",
                    reask_response=unavailable_method_response,
                )
            state.agent_response = unavailable_method_response
            return state

        def reject_unavailable_anonymous_cpf_skip(state: ServiceState) -> ServiceState:
            unavailable_skip_response = AgentResponse(
                description="Este atendimento exige um método de identificação disponível.",
                payload_schema=cpf_model.model_json_schema(),
                error_message="a continuação anônima não está configurada",
            )
            if inc_attempts(state, "cpf") >= max_attempts:
                return exhaust(
                    state,
                    stage="cpf",
                    reask_response=unavailable_skip_response,
                )
            state.agent_response = unavailable_skip_response
            return state

        # ── select method ────────────────────────────────────────────────
        async def select(state: ServiceState) -> ServiceState:
            if (
                state.data.get("identification_method")
                or state.data.get("cpf")
                or state.data.get("govbr_authenticated")
            ):
                state.agent_response = None
                return state
            pay = state.payload or {}
            raw = (
                str(pay.get("identification_method", "")) if "identification_method" in pay else ""
            )
            if not required and not pay and "anonimo" in eligible_method_set:
                set_identification_method(state, "anonimo")
                state.agent_response = None
                return state
            if "identification_method" in pay:
                method = _normalize_method(raw)
                if method in eligible_method_set:
                    set_identification_method(state, method)
                    reset_attempts(state, "method")
                    state.agent_response = None
                    return state
                if inc_attempts(state, "method") >= max_attempts:
                    return exhaust(
                        state,
                        stage="method",
                        reask_response=method_response(_EXHAUSTED_ERROR),
                    )
            state.agent_response = method_response()
            return state

        def select_router(state: ServiceState) -> str:
            if state.agent_response is not None:
                return END
            method = state.data.get("identification_method")
            if method == "govbr":
                return "authenticate_govbr"
            if method == "cpf":
                return "collect_cpf"
            return "identification_done"  # anonimo / pulada

        # ── gov.br (await_external) ───────────────────────────────────────
        async def authenticate_legacy(state: ServiceState) -> ServiceState:
            if state.data.get("identification_method") != "govbr" or state.data.get(
                "govbr_authenticated"
            ):
                state.agent_response = None
                return state
            pay = state.payload or {}
            token = pay.get("govbr_token")
            if "govbr_token" in pay:
                try:
                    if not isinstance(token, Mapping):
                        raise TypeError("gov.br token must be an object")
                    if "cpf" not in token:
                        raise ValueError("gov.br token did not contain CPF")
                    validated_cpf = cast(
                        str,
                        cpf_model.model_validate({"cpf": token["cpf"]}).model_dump()["cpf"],
                    )
                    identity_writes: dict[str, Any] = {"cpf": validated_cpf}
                    if "nome" in token:
                        identity_writes["name"] = cast(
                            str,
                            name_model.model_validate({"name": token["nome"]}).model_dump()["name"],
                        )
                    if "email" in token:
                        identity_writes["email"] = cast(
                            str,
                            email_model.model_validate({"email": token["email"]}).model_dump()[
                                "email"
                            ],
                        )
                except Exception:  # noqa: BLE001
                    return recover_from_govbr_validation_failure(state)

                enrich = await _call_optional_identification_tool(
                    ctx,
                    "get_user_info",
                    cpf=validated_cpf,
                )
                if enrich and enrich.get("phones"):
                    identity_writes["phone"] = str(enrich["phones"][0])
                if "name" in identity_writes:
                    identity_writes["name_processed"] = True
                if "email" in identity_writes:
                    identity_writes["email_processed"] = True
                identity_writes["govbr_authenticated"] = True
                identity_writes["cadastro_verificado"] = True
                state.data.update(identity_writes)
                if "name" in identity_writes:
                    _clear_cpf_lookup_provenance(state, "name")
                if "email" in identity_writes:
                    _clear_cpf_lookup_provenance(state, "email")
                state.data.pop("govbr_auth_sent", None)
                state.agent_response = None
                return state
            if state.data.get("govbr_auth_sent"):
                text = normalize_text(
                    f"{pay.get('message', '')} {pay.get('identification_method', '')}"
                )
                if not required and _is_refusal(text):
                    if "anonimo" not in eligible_method_set:
                        return reject_disabled_method(state, "anonimo")
                    set_identification_method(state, "anonimo")
                    state.data.pop("govbr_auth_sent", None)
                    state.agent_response = None
                    return state
                if "cpf" in text:
                    if "cpf" not in eligible_method_set:
                        return reject_disabled_method(state, "cpf")
                    set_identification_method(state, "cpf")
                    state.data.pop("govbr_auth_sent", None)
                    state.agent_response = None
                    return state
                state.agent_response = AgentResponse(
                    description="Estou aguardando você concluir o login gov.br. Quando terminar, é só me avisar. 🙂"
                )
                return state
            state.data["govbr_auth_sent"] = True
            state.agent_response = AgentResponse(
                description="Para se identificar pelo gov.br, toque no botão de login que enviei. 🔐",
                interactive={"out_of_band_sent": True, "next_step": "await_govbr_auth"},
            )
            return state

        def authenticate_router(state: ServiceState) -> str:
            if state.data.get("govbr_authenticated"):
                if not state.data.get("email") and not state.data.get("email_processed"):
                    return "collect_email"
                if not state.data.get("name") and not state.data.get("name_processed"):
                    return "collect_name"
                return "identification_done"
            if state.agent_response is not None:
                return END
            method = state.data.get("identification_method")
            if method == "cpf":
                return "collect_cpf"
            if method == "anonimo" or state.data.get("identificacao_pulada"):
                return "identification_done"
            return "identification_done"

        # ── collect CPF ──────────────────────────────────────────────────
        async def collect_cpf(state: ServiceState) -> ServiceState:
            if state.data.get("govbr_authenticated"):
                state.agent_response = None
                return state
            if state.internal.get(CORRECTION_REQUESTED_INTERNAL_KEY) == "cpf":
                _clear_cpf_lookup_derived_contacts(state, ctx)
                clear_cascade(state, "cpf", ctx)
                state.internal.pop(CORRECTION_REQUESTED_INTERNAL_KEY, None)
                if "cpf" not in eligible_method_set:
                    return reject_disabled_method(state, "cpf")
                # Persist the method so the NEXT turn's select node re-enters the
                # subflow instead of short-circuiting to done (the citizen may have
                # been "anonimo" before correcting). correction_requested is consumed
                # this turn, so the flip must outlive it.
                set_identification_method(state, "cpf")
            if state.data.get("cpf") or state.data.get("identificacao_pulada"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "cpf" in pay:
                raw = pay["cpf"]
                if (raw is None or str(raw).strip() == "" or _is_skip(str(raw))) and not required:
                    if "anonimo" not in eligible_method_set:
                        return reject_unavailable_anonymous_cpf_skip(state)
                    return complete_without_value(
                        state,
                        "cpf",
                        reask_response=AgentResponse(
                            description="Qual o seu CPF?",
                            payload_schema=cpf_model.model_json_schema(),
                        ),
                    )
                try:
                    validated = cpf_model.model_validate({"cpf": raw})
                    validated_cpf = cast(str, validated.model_dump()["cpf"])
                except Exception as exc:
                    if inc_attempts(state, "cpf") >= max_attempts:
                        return exhaust(
                            state,
                            stage="cpf",
                            reask_response=AgentResponse(
                                description="Não consegui validar seu CPF. Pode tentar de novo?",
                                payload_schema=cpf_model.model_json_schema(),
                                error_message=_EXHAUSTED_ERROR,
                            ),
                        )
                    state.agent_response = AgentResponse(
                        description="CPF inválido. Pode conferir e enviar de novo? (11 dígitos)",
                        payload_schema=cpf_model.model_json_schema(),
                        error_message=str(exc),
                    )
                    return state

                state.data["cpf"] = validated_cpf
                reset_attempts(state, "cpf")
                info = await _call_optional_identification_tool(
                    ctx, "cpf_lookup", cpf=validated_cpf
                )
                if info and info.get("status") == "ok":
                    if info.get("email") and not state.data.get("email_processed"):
                        validated_email = validate_optional_lookup_contact(
                            state,
                            "email",
                            info["email"],
                        )
                        if validated_email is not None:
                            state.data["email"] = validated_email
                            state.data["email_processed"] = True
                            _mark_cpf_lookup_derived_contact(state, "email")
                    if info.get("name") and not state.data.get("name_processed"):
                        validated_name = validate_optional_lookup_contact(
                            state,
                            "name",
                            info["name"],
                        )
                        if validated_name is not None:
                            state.data["name"] = validated_name
                            state.data["name_processed"] = True
                            _mark_cpf_lookup_derived_contact(state, "name")
                    state.data["cadastro_verificado"] = True
                state.agent_response = None
                return state
            if not required and _is_refusal(str(pay.get("message", ""))):
                if "anonimo" not in eligible_method_set:
                    return reject_unavailable_anonymous_cpf_skip(state)
                return complete_without_value(
                    state,
                    "cpf",
                    reask_response=AgentResponse(
                        description="Qual o seu CPF?",
                        payload_schema=cpf_model.model_json_schema(),
                    ),
                )
            state.agent_response = AgentResponse(
                description=(
                    "Qual o seu CPF?"
                    if required
                    else "Qual o seu CPF? (ou diga 'pular' para seguir sem se identificar)"
                ),
                payload_schema=cpf_model.model_json_schema(),
            )
            return state

        def cpf_router(state: ServiceState) -> str:
            if state.agent_response is not None:
                return END
            if state.data.get("identification_method") == "govbr":
                return "authenticate_govbr"
            if state.data.get("identificacao_pulada"):
                return "identification_done"
            if not state.data.get("email") and not state.data.get("email_processed"):
                return "collect_email"
            if not state.data.get("name") and not state.data.get("name_processed"):
                return "collect_name"
            return "identification_done"

        # ── collect email (optional) ─────────────────────────────────────
        async def collect_email(state: ServiceState) -> ServiceState:
            if state.internal.get(CORRECTION_REQUESTED_INTERNAL_KEY) == "email":
                _clear_cpf_lookup_provenance(state, "email")
                clear_cascade(state, "email", ctx)
                state.internal.pop(CORRECTION_REQUESTED_INTERNAL_KEY, None)
            if state.data.get("email_processed"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "email" in pay:
                raw = pay["email"]
                if raw is None or str(raw).strip() == "" or _is_skip(str(raw)):
                    return complete_without_value(
                        state,
                        "email",
                        reask_response=AgentResponse(
                            description="Qual o seu e-mail? (ou diga 'pular')",
                            payload_schema=email_model.model_json_schema(),
                        ),
                    )
                try:
                    validated = email_model.model_validate({"email": raw})
                    state.data["email"] = cast(str, validated.model_dump()["email"])
                    state.data["email_processed"] = True
                    _clear_cpf_lookup_provenance(state, "email")
                    reset_attempts(state, "email")
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, "email") >= max_attempts:
                        return exhaust(
                            state,
                            stage="email",
                            reask_response=AgentResponse(
                                description="E-mail inválido. Pode reenviar? (ou 'pular')",
                                payload_schema=email_model.model_json_schema(),
                                error_message=_EXHAUSTED_ERROR,
                            ),
                        )
                    state.agent_response = AgentResponse(
                        description="E-mail inválido. Pode reenviar? (ou 'pular')",
                        payload_schema=email_model.model_json_schema(),
                        error_message=str(exc),
                    )
                    return state
            state.agent_response = AgentResponse(
                description="Qual o seu e-mail? (ou diga 'pular')",
                payload_schema=email_model.model_json_schema(),
            )
            return state

        def email_router(state: ServiceState) -> str:
            if state.agent_response is not None:
                return END
            if not state.data.get("name") and not state.data.get("name_processed"):
                return "collect_name"
            return "identification_done"

        # ── collect name (optional) ──────────────────────────────────────
        async def collect_name(state: ServiceState) -> ServiceState:
            if state.internal.get(CORRECTION_REQUESTED_INTERNAL_KEY) == "name":
                _clear_cpf_lookup_provenance(state, "name")
                clear_cascade(state, "name", ctx)
                state.internal.pop(CORRECTION_REQUESTED_INTERNAL_KEY, None)
            if state.data.get("name_processed"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "name" in pay:
                raw = pay["name"]
                if raw is None or str(raw).strip() == "" or _is_skip(str(raw)):
                    return complete_without_value(
                        state,
                        "name",
                        reask_response=AgentResponse(
                            description="Qual o seu nome completo? (ou diga 'pular')",
                            payload_schema=name_model.model_json_schema(),
                        ),
                    )
                try:
                    validated = name_model.model_validate({"name": raw})
                    state.data["name"] = cast(str, validated.model_dump()["name"])
                    state.data["name_processed"] = True
                    _clear_cpf_lookup_provenance(state, "name")
                    reset_attempts(state, "name")
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, "name") >= max_attempts:
                        return exhaust(
                            state,
                            stage="name",
                            reask_response=AgentResponse(
                                description="Nome inválido. Pode reenviar? (ou 'pular')",
                                payload_schema=name_model.model_json_schema(),
                                error_message=_EXHAUSTED_ERROR,
                            ),
                        )
                    state.agent_response = AgentResponse(
                        description="Nome inválido. Pode reenviar? (ou 'pular')",
                        payload_schema=name_model.model_json_schema(),
                        error_message=str(exc),
                    )
                    return state
            state.agent_response = AgentResponse(
                description="Qual o seu nome completo? (ou diga 'pular')",
                payload_schema=name_model.model_json_schema(),
            )
            return state

        def name_router(state: ServiceState) -> str:
            return END if state.agent_response is not None else "identification_done"

        async def done(state: ServiceState) -> ServiceState:
            state.agent_response = None
            return state

        authenticate_descriptor = NodeDesc(
            "authenticate_govbr",
            authenticate_legacy,
            authenticate_router,
            targets=[
                "collect_cpf",
                "collect_email",
                "collect_name",
                "identification_done",
            ],
        )
        await_external = ctx.await_external
        if await_external and await_external.get("step") == "authenticate_govbr":
            if "govbr" not in eligible_method_set:
                raise ValueError("identification@2 await_external targets disabled method 'govbr'")
            configured_capability = copy.deepcopy(await_external)
            configured_transitions = {
                "timeout": configured_capability.get("timeout"),
                **(configured_capability.get("recovery") or {}),
            }
            transition_methods: dict[str, _IdentificationMethod | None] = {}
            method_for_target: dict[str, _IdentificationMethod] = {
                "collect_cpf": "cpf",
                "authenticate_govbr": "govbr",
                "identification_done": "anonimo",
            }
            for transition_name, transition in configured_transitions.items():
                if transition is None:
                    transition_methods[transition_name] = None
                    continue
                transition_target = transition["goto"]
                target_method = method_for_target.get(transition_target)
                transition_set = transition.get("set") or {}
                configured_method = transition_set.get("identification_method")
                if configured_method is not None and configured_method not in {
                    "cpf",
                    "govbr",
                    "anonimo",
                }:
                    raise ValueError(
                        "identification@2 await_external transition "
                        f"{transition_name!r} sets invalid identification method "
                        f"{configured_method!r}"
                    )
                if (
                    target_method is not None
                    and configured_method is not None
                    and target_method != configured_method
                ):
                    raise ValueError(
                        "identification@2 await_external transition "
                        f"{transition_name!r} has conflicting method and target"
                    )
                implied_method = cast(
                    _IdentificationMethod | None,
                    configured_method or target_method,
                )
                if implied_method is not None and implied_method not in eligible_method_set:
                    raise ValueError(
                        "identification@2 await_external transition "
                        f"{transition_name!r} routes to disabled method {implied_method!r}"
                    )
                transition_methods[transition_name] = implied_method
            configured_on_resume = configured_capability.setdefault("on_resume", {})
            if isinstance(configured_on_resume.get("enrich"), str):
                configured_on_resume["enrich"] = {
                    "tool": configured_on_resume["enrich"],
                    "optional": True,
                    "input": {"cpf": "$token.cpf"},
                    "set": {"phone": "$result.phones.0"},
                }
            configured_step: dict[str, Any] = {"step": "authenticate_govbr"}
            if not configured_capability.get("interactive"):
                configured_step["interactive"] = {
                    "kind": "cta_url",
                    "field": configured_capability["resume_on"],
                    "out_of_band": True,
                    "next_step": "await_govbr_auth",
                }
            generic_descriptor = make_await_external_node(
                ctx,
                configured_capability,
                configured_step,
                default_router=authenticate_router,
                additional_targets=[
                    "collect_cpf",
                    "collect_email",
                    "collect_name",
                    "identification_done",
                ],
                legacy_sent_data_key="govbr_auth_sent",
                waiting_description=(
                    "Estou aguardando você concluir o login gov.br. "
                    "Quando terminar, é só me avisar. 🙂"
                ),
            )
            await_sent_internal_key = f"_await_external_sent:{generic_descriptor.id}"
            await_completed_internal_key = f"_await_external_completed:{generic_descriptor.id}"

            def clear_configured_await_markers(state: ServiceState) -> None:
                state.data.pop("govbr_auth_sent", None)
                state.internal.pop(await_sent_internal_key, None)
                state.internal.pop(await_completed_internal_key, None)

            async def authenticate_configured(state: ServiceState) -> ServiceState:
                if state.data.get("identification_method") != "govbr" or state.data.get(
                    "govbr_authenticated"
                ):
                    state.agent_response = None
                    return state

                payload = state.payload or {}
                resume_on = configured_capability["resume_on"]
                recovery = configured_capability.get("recovery") or {}
                if (
                    state.data.get("govbr_auth_sent")
                    and resume_on not in payload
                    and "_external_event" not in payload
                ):
                    recovery_text = normalize_text(
                        f"{payload.get('message', '')} {payload.get('identification_method', '')}"
                    )
                    if not required and _is_refusal(recovery_text):
                        if "abort" in recovery:
                            payload["_external_event"] = "abort"
                        elif "anonimo" not in eligible_method_set:
                            clear_configured_await_markers(state)
                            return reject_disabled_method(state, "anonimo")
                        else:
                            set_identification_method(state, "anonimo")
                            clear_configured_await_markers(state)
                            state.status = "progress"
                            state.agent_response = None
                            return state
                    elif "cpf" in recovery_text:
                        if "cpf" not in eligible_method_set:
                            clear_configured_await_markers(state)
                            return reject_disabled_method(state, "cpf")
                        if "switch" in recovery:
                            payload["_external_event"] = "switch"
                        else:
                            set_identification_method(state, "cpf")
                            clear_configured_await_markers(state)
                            state.status = "progress"
                            state.agent_response = None
                            return state

                resume_token = payload.get(resume_on)
                if resume_on in payload and (
                    not isinstance(resume_token, Mapping) or not resume_token.get("cpf")
                ):
                    if "switch" in recovery:
                        payload.pop(resume_on, None)
                        payload["_external_event"] = "switch"
                    else:
                        clear_configured_await_markers(state)
                        state.status = "progress"
                        return recover_from_govbr_validation_failure(state)

                external_event = payload.get("_external_event")
                has_resume_token = resume_on in payload
                updated_state = await generic_descriptor.fn(state)
                if updated_state.agent_response is not None:
                    return updated_state

                transition = configured_transitions.get(cast(str, external_event))
                if external_event is not None and transition is not None:
                    if transition_method := transition_methods.get(cast(str, external_event)):
                        set_identification_method(updated_state, transition_method)
                        if transition_method == "govbr":
                            updated_state.internal.pop(await_completed_internal_key, None)
                    if transition["goto"] == "select_identification_method":
                        if transition_method is None:
                            updated_state.data.pop("identification_method", None)
                            updated_state.data.pop("identificacao_pulada", None)
                        updated_state.internal.pop(await_completed_internal_key, None)

                if not has_resume_token:
                    return updated_state
                if not updated_state.data.get("cpf"):
                    updated_state.data.pop("govbr_authenticated", None)
                    updated_state.data.pop("cadastro_verificado", None)
                    clear_configured_await_markers(updated_state)
                    return recover_from_govbr_validation_failure(updated_state)
                if updated_state.data.get("name"):
                    updated_state.data["name_processed"] = True
                    _clear_cpf_lookup_provenance(updated_state, "name")
                if updated_state.data.get("email"):
                    updated_state.data["email_processed"] = True
                    _clear_cpf_lookup_provenance(updated_state, "email")
                updated_state.data["govbr_authenticated"] = True
                updated_state.data["cadastro_verificado"] = True
                return updated_state

            authenticate_descriptor = NodeDesc(
                generic_descriptor.id,
                authenticate_configured,
                generic_descriptor.router,
                generic_descriptor.targets,
            )

        descriptors = [
            NodeDesc(
                "select_identification_method",
                select,
                select_router,
                targets=["authenticate_govbr", "collect_cpf", "identification_done"],
            ),
            authenticate_descriptor,
            NodeDesc(
                "collect_cpf",
                collect_cpf,
                cpf_router,
                targets=[
                    "authenticate_govbr",
                    "collect_email",
                    "collect_name",
                    "identification_done",
                ],
            ),
            NodeDesc(
                "collect_email",
                collect_email,
                email_router,
                targets=["collect_name", "identification_done"],
            ),
            NodeDesc("collect_name", collect_name, name_router, targets=["identification_done"]),
            NodeDesc("identification_done", done, lambda s: NEXT),
        ]
        return SubflowBuild(
            descriptors=descriptors,
            entry_id="select_identification_method",
            node_for_slot={"cpf": "collect_cpf", "email": "collect_email", "name": "collect_name"},
        )
