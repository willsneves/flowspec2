"""identification@2 — method select → gov.br (await_external) | CPF → email → name.

Exposes the ``cpf``/``email``/``name`` slots. gov.br is modelled as an
out-of-band suspend/resume: the first visit emits the login affordance and
pauses; a later turn carrying a ``govbr_token`` resumes and populates the slots.
Refusal/abort (when identification is optional) routes to anonymous. Every leg
caps re-asks at ``max_attempts`` then skips/defaults.
"""

from __future__ import annotations

from typing import Any, Optional

from ..domains import normalize_text
from ..models import AgentResponse, ServiceState
from ..nodes import NEXT, FlowContext, NodeDesc, inc_attempts
from langgraph.graph import END
from . import SubflowBuild

_REFUSAL = ("anonim", "pular", "sem ident", "nao quero", "nao me ident", "recus", "nenhum", "sem cpf", "skip")


def _is_refusal(text: str) -> bool:
    norm = normalize_text(text)
    return bool(norm) and any(tok in norm for tok in _REFUSAL)


def _normalize_method(raw: str) -> Optional[str]:
    norm = normalize_text(raw)
    if "govbr" in norm or "gov.br" in norm or norm == "gov" or "gov br" in norm:
        return "govbr"
    if "cpf" in norm:
        return "cpf"
    if _is_refusal(norm) or norm == "anonimo":
        return "anonimo"
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
        ctx.node_for_slot.update({"cpf": "collect_cpf", "email": "collect_email", "name": "collect_name"})
        ctx.slot_aux["cpf"] = ["cadastro_verificado", "cpf_attempts", "identificacao_pulada"]
        ctx.slot_aux["email"] = ["email_processed", "email_attempts"]
        ctx.slot_aux["name"] = ["name_processed", "name_attempts"]
        cpf_model = ctx.model_for("cpf")
        email_model = ctx.model_for("email")
        name_model = ctx.model_for("name")

        # ── select method ────────────────────────────────────────────────
        async def select(state: ServiceState) -> ServiceState:
            if state.data.get("identification_method") or state.data.get("cpf") or state.data.get("govbr_authenticated"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            raw = str(pay.get("identification_method", "")) if "identification_method" in pay else ""
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
            buttons = [{"id": m, "title": {"cpf": "CPF", "govbr": "Gov.br", "anonimo": "Sem me identificar"}[m]} for m in methods if (m != "anonimo" or not required)]
            state.agent_response = AgentResponse(
                description="Como prefere se identificar?",
                interactive={"body": "Como prefere se identificar?", "field": "identification_method", "buttons": buttons},
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
        async def authenticate(state: ServiceState) -> ServiceState:
            if state.data.get("identification_method") != "govbr" or state.data.get("govbr_authenticated"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            token = pay.get("govbr_token")
            if token:
                cpf = token.get("cpf")
                if not cpf:
                    state.data.pop("identification_method", None)
                    state.data.pop("govbr_auth_sent", None)
                    state.agent_response = AgentResponse(description="Não consegui validar o gov.br. Vamos tentar pelo CPF?")
                    return state
                state.data["cpf"] = cpf
                if token.get("nome"):
                    state.data["name"] = token["nome"]
                    state.data["name_processed"] = True
                if token.get("email"):
                    state.data["email"] = token["email"]
                    state.data["email_processed"] = True
                try:
                    enrich = await ctx.tools.call("get_user_info", cpf=cpf)
                    if enrich.get("phones"):
                        state.data["phone"] = str(enrich["phones"][0])
                except Exception:
                    pass
                state.data["govbr_authenticated"] = True
                state.data["cadastro_verificado"] = True
                state.data.pop("govbr_auth_sent", None)
                state.agent_response = None
                return state
            if state.data.get("govbr_auth_sent"):
                text = normalize_text(f"{pay.get('message', '')} {pay.get('identification_method', '')}")
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
                state.data.pop("correction_requested", None)
            if state.data.get("cpf") or state.data.get("identificacao_pulada"):
                state.agent_response = None
                return state
            pay = state.payload or {}
            if "cpf" in pay:
                raw = pay["cpf"]
                if (raw is None or str(raw).strip() == "") and not required:
                    state.data["identificacao_pulada"] = True
                    state.agent_response = None
                    return state
                try:
                    validated = cpf_model.model_validate({"cpf": raw})
                    state.data["cpf"] = validated.cpf
                    try:
                        info = await ctx.tools.call("cpf_lookup", cpf=validated.cpf)
                        if info.get("email"):
                            state.data["email"] = info["email"]
                            state.data["email_processed"] = True
                        if info.get("name"):
                            state.data["name"] = info["name"]
                            state.data["name_processed"] = True
                        state.data["cadastro_verificado"] = True
                    except Exception:
                        pass
                    state.agent_response = None
                    return state
                except Exception as exc:
                    if inc_attempts(state, "cpf") >= max_attempts:
                        if required:
                            state.agent_response = AgentResponse(description="Não consegui validar seu CPF.", error_message=str(exc))
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
                if raw is None or str(raw).strip() == "":
                    state.data["email_processed"] = True
                    state.agent_response = None
                    return state
                try:
                    validated = email_model.model_validate({"email": raw})
                    state.data["email"] = validated.email
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
                if raw is None or str(raw).strip() == "":
                    state.data["name_processed"] = True
                    state.agent_response = None
                    return state
                try:
                    validated = name_model.model_validate({"name": raw})
                    state.data["name"] = validated.name
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

        descriptors = [
            NodeDesc("select_identification_method", select, select_router,
                     targets=["authenticate_govbr", "collect_cpf", "identification_done"]),
            NodeDesc("authenticate_govbr", authenticate, authenticate_router,
                     targets=["collect_cpf", "collect_email", "collect_name", "identification_done"]),
            NodeDesc("collect_cpf", collect_cpf, cpf_router,
                     targets=["collect_email", "collect_name", "identification_done"]),
            NodeDesc("collect_email", collect_email, email_router, targets=["collect_name", "identification_done"]),
            NodeDesc("collect_name", collect_name, name_router, targets=["identification_done"]),
            NodeDesc("identification_done", done, lambda s: NEXT),
        ]
        return SubflowBuild(
            descriptors=descriptors,
            entry_id="select_identification_method",
            node_for_slot={"cpf": "collect_cpf", "email": "collect_email", "name": "collect_name"},
        )
