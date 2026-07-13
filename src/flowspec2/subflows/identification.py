"""identification@2 — method select → gov.br (await_external) | CPF → email → name.

Exposes the ``cpf``/``email``/``name`` slots. gov.br is modelled as an
out-of-band suspend/resume: the first visit emits the login affordance and
pauses; a later turn carrying a ``govbr_token`` resumes and populates the slots.
Refusal/abort (when identification is optional) routes to anonymous. Every leg
caps re-asks at ``max_attempts`` then skips/defaults.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any, Final, Optional, cast

from langgraph.graph import END as LANGGRAPH_END

from ..domains import normalize_text
from ..models import AgentResponse, ServiceState
from ..nodes import (
    NEXT,
    FlowContext,
    NodeDesc,
    inc_attempts,
    make_await_external_node,
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

logger = logging.getLogger(__name__)


def _is_refusal(text: str) -> bool:
    norm = normalize_text(text)
    return bool(norm) and any(tok in norm for tok in _REFUSAL)


# Exact-token skip for the optional contact slots (email/name/cpf). Exact match —
# not substring — so a real value like "skipper@x.com" is never read as a skip.
_SKIP_TOKENS = {"pular", "skip", "nao", "nenhum", "passar", "proximo", "depois"}


def _is_skip(text: str) -> bool:
    return normalize_text(text) in _SKIP_TOKENS


def _govbr_validation_failure_response(
    ctx: FlowContext,
    state: ServiceState,
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
        description="Não consegui validar o gov.br. Vamos tentar pelo CPF?",
        log_id=log_id,
    )


def _normalize_method(raw: str) -> Optional[str]:
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
        methods = with_cfg.get("methods", ["cpf", "govbr", "anonimo"])
        max_attempts = int(with_cfg.get("max_attempts", ctx.max_attempts))

        # register the slots this subflow contributes
        ctx.domains.setdefault("CPF", {"type": "cpf"})
        ctx.domains.setdefault("Email", {"type": "email"})
        ctx.domains.setdefault("Name", {"type": "name"})
        ctx.slots.setdefault("cpf", {"domain": "CPF"})
        ctx.slots.setdefault("email", {"domain": "Email"})
        ctx.slots.setdefault("name", {"domain": "Name"})
        ctx.node_for_slot.update(
            {"cpf": "collect_cpf", "email": "collect_email", "name": "collect_name"}
        )
        ctx.slot_aux["cpf"] = ["cadastro_verificado", "cpf_attempts", "identificacao_pulada"]
        ctx.slot_aux["email"] = ["email_processed", "email_attempts"]
        ctx.slot_aux["name"] = ["name_processed", "name_attempts"]
        cpf_model = ctx.model_for("cpf")
        email_model = ctx.model_for("email")
        name_model = ctx.model_for("name")

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
            if not required and (not pay or _is_refusal(raw)):
                state.data["identification_method"] = "anonimo"
                state.data["identificacao_pulada"] = True
                state.agent_response = None
                return state
            if "identification_method" in pay:
                method = _normalize_method(raw)
                if method:
                    state.data["identification_method"] = method
                    if method == "anonimo":
                        state.data["identificacao_pulada"] = True
                    state.agent_response = None
                    return state
                if inc_attempts(state, "method") >= max_attempts:
                    state.data["identification_method"] = "cpf"
                    state.agent_response = None
                    return state
            buttons = [
                {
                    "id": m,
                    "title": {"cpf": "CPF", "govbr": "Gov.br", "anonimo": "Sem me identificar"}[m],
                }
                for m in methods
                if (m != "anonimo" or not required)
            ]
            state.agent_response = AgentResponse(
                description="Como prefere se identificar?",
                interactive={
                    "body": "Como prefere se identificar?",
                    "field": "identification_method",
                    "buttons": buttons,
                },
            )
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
            if token:
                cpf = token.get("cpf")
                if not cpf:
                    state.data.pop("identification_method", None)
                    state.data.pop("govbr_auth_sent", None)
                    state.agent_response = _govbr_validation_failure_response(ctx, state)
                    return state
                state.data["cpf"] = cpf
                if token.get("nome"):
                    state.data["name"] = token["nome"]
                    state.data["name_processed"] = True
                if token.get("email"):
                    state.data["email"] = token["email"]
                    state.data["email_processed"] = True
                enrich = await _call_optional_identification_tool(ctx, "get_user_info", cpf=cpf)
                if enrich and enrich.get("phones"):
                    state.data["phone"] = str(enrich["phones"][0])
                state.data["govbr_authenticated"] = True
                state.data["cadastro_verificado"] = True
                state.data.pop("govbr_auth_sent", None)
                state.agent_response = None
                return state
            if state.data.get("govbr_auth_sent"):
                text = normalize_text(
                    f"{pay.get('message', '')} {pay.get('identification_method', '')}"
                )
                if not required and _is_refusal(text):
                    state.data["identification_method"] = "anonimo"
                    state.data["identificacao_pulada"] = True
                    state.data.pop("govbr_auth_sent", None)
                    state.agent_response = None
                    return state
                if "cpf" in text:
                    state.data["identification_method"] = "cpf"
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
            method = state.data.get("identification_method")
            if method == "cpf":
                return "collect_cpf"
            if method == "anonimo" or state.data.get("identificacao_pulada"):
                return "identification_done"
            if state.agent_response is not None:
                return END
            return "identification_done"

        # ── collect CPF ──────────────────────────────────────────────────
        async def collect_cpf(state: ServiceState) -> ServiceState:
            if state.data.get("govbr_authenticated"):
                state.agent_response = None
                return state
            if state.data.get("correction_requested") == "cpf":
                for key in ["cpf", "cadastro_verificado", "identificacao_pulada", "cpf_attempts"]:
                    state.data.pop(key, None)
                # Persist the method so the NEXT turn's select node re-enters the
                # subflow instead of short-circuiting to done (the citizen may have
                # been "anonimo" before correcting). correction_requested is consumed
                # this turn, so the flip must outlive it.
                state.data["identification_method"] = "cpf"
                state.data.pop("correction_requested", None)
            if state.data.get("cpf") or state.data.get("identificacao_pulada"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "cpf" in pay:
                raw = pay["cpf"]
                if (raw is None or str(raw).strip() == "" or _is_skip(str(raw))) and not required:
                    state.data["identificacao_pulada"] = True
                    state.agent_response = None
                    return state
                try:
                    validated = cpf_model.model_validate({"cpf": raw})
                    validated_cpf = cast(str, validated.model_dump()["cpf"])
                    state.data["cpf"] = validated_cpf
                    info = await _call_optional_identification_tool(
                        ctx, "cpf_lookup", cpf=validated_cpf
                    )
                    if info:
                        if info.get("email"):
                            state.data["email"] = info["email"]
                            state.data["email_processed"] = True
                        if info.get("name"):
                            state.data["name"] = info["name"]
                            state.data["name_processed"] = True
                        state.data["cadastro_verificado"] = True
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, "cpf") >= max_attempts:
                        if required:
                            state.agent_response = AgentResponse(
                                description="Não consegui validar seu CPF.", error_message=str(exc)
                            )
                            return state
                        state.data["identificacao_pulada"] = True
                        state.agent_response = None
                        return state
                    state.agent_response = AgentResponse(
                        description="CPF inválido. Pode conferir e enviar de novo? (11 dígitos)",
                        payload_schema=cpf_model.model_json_schema(),
                        error_message=str(exc),
                    )
                    return state
            if not required and _is_refusal(str(pay.get("message", ""))):
                state.data["identificacao_pulada"] = True
                state.agent_response = None
                return state
            state.agent_response = AgentResponse(
                description="Qual o seu CPF? (ou diga 'pular' para seguir sem se identificar)",
                payload_schema=cpf_model.model_json_schema(),
            )
            return state

        def cpf_router(state: ServiceState) -> str:
            if state.agent_response is not None:
                return END
            if state.data.get("identificacao_pulada"):
                return "identification_done"
            if not state.data.get("email") and not state.data.get("email_processed"):
                return "collect_email"
            if not state.data.get("name") and not state.data.get("name_processed"):
                return "collect_name"
            return "identification_done"

        # ── collect email (optional) ─────────────────────────────────────
        async def collect_email(state: ServiceState) -> ServiceState:
            if state.data.get("correction_requested") == "email":
                for key in ["email", "email_processed", "email_attempts"]:
                    state.data.pop(key, None)
                state.data.pop("correction_requested", None)
            if state.data.get("email_processed"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "email" in pay:
                raw = pay["email"]
                if raw is None or str(raw).strip() == "" or _is_skip(str(raw)):
                    state.data["email_processed"] = True
                    state.agent_response = None
                    return state
                try:
                    validated = email_model.model_validate({"email": raw})
                    state.data["email"] = cast(str, validated.model_dump()["email"])
                    state.data["email_processed"] = True
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, "email") >= max_attempts:
                        state.data["email_processed"] = True
                        state.agent_response = None
                        return state
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
            if state.data.get("correction_requested") == "name":
                for key in ["name", "name_processed", "name_attempts"]:
                    state.data.pop(key, None)
                state.data.pop("correction_requested", None)
            if state.data.get("name_processed"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "name" in pay:
                raw = pay["name"]
                if raw is None or str(raw).strip() == "" or _is_skip(str(raw)):
                    state.data["name_processed"] = True
                    state.agent_response = None
                    return state
                try:
                    validated = name_model.model_validate({"name": raw})
                    state.data["name"] = cast(str, validated.model_dump()["name"])
                    state.data["name_processed"] = True
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, "name") >= max_attempts:
                        state.data["name_processed"] = True
                        state.agent_response = None
                        return state
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
            configured_capability = copy.deepcopy(await_external)
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
                        else:
                            state.data["identification_method"] = "anonimo"
                            state.data["identificacao_pulada"] = True
                            state.data.pop("govbr_auth_sent", None)
                            state.internal.pop(
                                f"_await_external_sent:{generic_descriptor.id}",
                                None,
                            )
                            state.status = "progress"
                            state.agent_response = None
                            return state
                    elif "cpf" in recovery_text:
                        if "switch" in recovery:
                            payload["_external_event"] = "switch"
                        else:
                            state.data["identification_method"] = "cpf"
                            state.data.pop("govbr_auth_sent", None)
                            state.internal.pop(
                                f"_await_external_sent:{generic_descriptor.id}",
                                None,
                            )
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
                        state.data["identification_method"] = "cpf"
                        state.data.pop("govbr_auth_sent", None)
                        state.internal.pop(
                            f"_await_external_sent:{generic_descriptor.id}",
                            None,
                        )
                        state.status = "progress"
                        state.agent_response = _govbr_validation_failure_response(ctx, state)
                        return state

                external_event = payload.get("_external_event")
                has_resume_token = resume_on in payload
                updated_state = await generic_descriptor.fn(state)
                if updated_state.agent_response is not None:
                    return updated_state

                if external_event == "abort":
                    if required:
                        updated_state.data.pop("identification_method", None)
                    else:
                        updated_state.data["identification_method"] = "anonimo"
                        updated_state.data["identificacao_pulada"] = True
                elif external_event in {"switch", "timeout"}:
                    updated_state.data["identification_method"] = "cpf"

                if not has_resume_token:
                    return updated_state
                if not updated_state.data.get("cpf"):
                    updated_state.data["identification_method"] = "cpf"
                    updated_state.data.pop("govbr_authenticated", None)
                    updated_state.data.pop("cadastro_verificado", None)
                    updated_state.agent_response = _govbr_validation_failure_response(
                        ctx, updated_state
                    )
                    return updated_state
                if updated_state.data.get("name"):
                    updated_state.data["name_processed"] = True
                if updated_state.data.get("email"):
                    updated_state.data["email_processed"] = True
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
                targets=["collect_email", "collect_name", "identification_done"],
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
