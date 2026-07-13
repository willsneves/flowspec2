"""FlowRuntime — load · validate · compile · execute a flowspec/2 document.

``execute`` mirrors the production ``base_workflow.execute``: it injects the
turn's payload, applies the reset semantics (post-success / empty-payload), runs
the compiled graph until it pauses or completes, then rebuilds the
``AgentResponse`` (propagating ``interactive`` and correlated ``log_id`` values).
Two tool-layer concerns live *above* the graph: the ``auto_flow`` pre-graph
short-circuit (send a WhatsApp Flow and return ``flow_sent`` without entering
the graph) and the ``alias_map`` fan-out applied to Flow submissions.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Awaitable, Callable, Optional

from .clock import UtcClock, system_utc_now
from .compiler import CompiledFlow, compile_flow
from .interactive import build_flow
from .models import AgentResponse, ServiceMetadata, ServiceState
from .observability import SnowflakeIdGenerator, default_log_id_generator, log_event
from .predicates import evaluate
from .schema import load_flow, validate_flow
from .subflows import SubflowRegistry
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


def _encode_prefill_token(prefill: dict[str, Any]) -> str:
    import base64
    import json

    raw = base64.urlsafe_b64encode(json.dumps(prefill, default=str).encode()).decode()
    return f"v1:{raw}"


class FlowRuntime:
    def __init__(
        self,
        doc: dict[str, Any],
        *,
        tools: Optional[ToolRegistry] = None,
        subflows: Optional[SubflowRegistry] = None,
        log_id_generator: Optional[SnowflakeIdGenerator] = None,
        clock: Optional[UtcClock] = None,
        validate: bool = True,
    ) -> None:
        private_doc = copy.deepcopy(doc)
        if validate:
            validate_flow(private_doc)
        self.doc = private_doc
        self.flow = private_doc["flow"]
        self.log_id_generator = log_id_generator or default_log_id_generator()
        self.clock = clock if clock is not None else system_utc_now
        self.compiled: CompiledFlow = compile_flow(
            private_doc,
            tools=tools,
            subflows=subflows,
            log_id_generator=self.log_id_generator,
        )
        self._store: dict[str, ServiceState] = {}

    @classmethod
    def from_path(cls, path: str, **kwargs: Any) -> "FlowRuntime":
        return cls(load_flow(path), **kwargs)

    def new_state(self, user_id: str, data: Optional[dict[str, Any]] = None) -> ServiceState:
        return ServiceState(
            user_id=user_id,
            service_name=self.flow,
            data=dict(data or {}),
            metadata=ServiceMetadata.from_clock(self.clock),
        )

    # ── auto_flow (pre-graph) ────────────────────────────────────────────────

    def _should_send_flow(
        self, af: dict[str, Any], state: ServiceState, payload: dict[str, Any]
    ) -> bool:
        source = af.get("on_submit_source", "whatsapp_flow")
        if payload.get("_source") == source:
            return False  # this turn IS the Flow submission
        if state.internal.get("_started"):
            return False  # mid-flow (e.g. a correction cleared the gating slot) — never re-send
        return bool(evaluate(af["send_when"], state, self.compiled.ctx.config))

    def _flow_sent_state(self, af: dict[str, Any], state: ServiceState) -> ServiceState:
        prefill = {
            k: state.data.get(k)
            for k in af.get("prefill_from", [])
            if state.data.get(k) is not None
        }
        token = _encode_prefill_token(prefill)
        envelope = build_flow(
            flow_id=af["meta_flow_ref"],
            body="Para agilizar, preencha o formulário abaixo. 📋",
            flow_token=token,
        )
        state.internal["flow_sent"] = True
        state.agent_response = AgentResponse(
            service_name=self.flow,
            description="Enviei um formulário para você preencher. 📋",
            interactive={
                "flow": True,
                "envelope": envelope,
                "flow_token": token,
                "status": "flow_sent",
            },
            data=state.data,
        )
        state.metadata.saved = True
        state.metadata.touch(self.clock)
        return state

    @staticmethod
    def _apply_alias_map(alias_map: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        out = dict(payload)
        for field, mapping in alias_map.items():
            if field not in out:
                continue
            value = out[field]
            value_keyed = bool(mapping) and all(isinstance(v, dict) for v in mapping.values())
            if value_keyed:
                sub = mapping.get(str(value))
                if isinstance(sub, dict):
                    for slot, setval in sub.items():
                        out[slot] = value if setval == "$value" else setval
            else:
                for slot, setval in mapping.items():
                    out[slot] = value if setval == "$value" else setval
        return out

    # ── execute ──────────────────────────────────────────────────────────────

    async def execute(
        self, state: ServiceState, payload: Optional[dict[str, Any]] = None
    ) -> ServiceState:
        payload = dict(payload or {})

        # Reset semantics run FIRST so the auto_flow gate and the graph both see a
        # clean slate after a completed/aborted run (post-success reset) or a fresh
        # never-saved start. Mirrors base_workflow.execute, hoisted above the
        # tool-layer auto_flow check.
        if state.data.get("_reset_on_next_call"):
            state.data = {}
            state.internal = {}
            state.status = "progress"
            state.agent_response = None
        elif not payload and not state.metadata.saved:
            state.data = {}
            state.internal = {}
            state.status = "progress"
            state.agent_response = None

        af = self.doc.get("auto_flow")
        if af and self._should_send_flow(af, state, payload):
            return self._flow_sent_state(af, state)
        if af and payload.get("_source") == af.get("on_submit_source", "whatsapp_flow"):
            payload = self._apply_alias_map(af.get("alias_map", {}) or {}, payload)

        state.payload = payload
        state.internal["_started"] = (
            True  # the graph is about to run; corrections now route through it
        )
        result = await self.compiled.graph.ainvoke(state)
        final = result if isinstance(result, ServiceState) else ServiceState(**result)

        if final.agent_response is None:
            final.status = "completed"
            final.agent_response = AgentResponse(
                service_name=self.flow,
                description="Serviço concluído com sucesso.",
                data=final.data,
            )

        ar = final.agent_response
        assert ar is not None  # set above when the graph ended without one
        if ar.log_id is None and (final.status == "error" or ar.error_message is not None):
            ar.log_id = log_event(
                logger,
                logging.ERROR if final.status == "error" else logging.WARNING,
                "Flow response contains an error",
                operation="execute",
                log_id_generator=self.log_id_generator,
                context={
                    "flow": self.flow,
                    "status": final.status,
                    "error_message": ar.error_message or ar.description,
                },
            )
        final.payload = {}
        final.agent_response = AgentResponse(
            service_name=self.flow,
            error_message=ar.error_message,
            log_id=ar.log_id,
            description=ar.description,
            payload_schema=ar.payload_schema,
            data=final.data,
            interactive=ar.interactive,
        )
        final.metadata.saved = True
        final.metadata.touch(self.clock)
        return final

    # ── callable-tool adapter (the multi_step_service entry point) ────────────

    def as_tool(
        self,
    ) -> Callable[
        [str, str, Optional[dict[str, Any]]],
        Awaitable[dict[str, Any]],
    ]:
        """Return an async ``(service_name, user_id, payload) -> dict`` callable.

        Mirrors the production ``multi_step_service`` MCP tool: it loads/saves the
        per-user state and returns the agent-facing dict (description +
        payload_schema + status + optional interactive/error/log ID).
        ``out_of_band``/flow sends surface as ``status: interactive_sent`` so an
        outer agent ends the turn.
        """

        async def multi_step_service(
            service_name: str, user_id: str, payload: Optional[dict[str, Any]] = None
        ) -> dict[str, Any]:
            state = self._store.get(user_id) or self.new_state(user_id)
            state = await self.execute(state, payload or {})
            self._store[user_id] = state
            ar = state.agent_response or AgentResponse()
            out: dict[str, Any] = {
                "status": state.status,
                "description": ar.description,
                "payload_schema": ar.payload_schema,
                "data": state.data,
            }
            if ar.error_message is not None:
                out["error_message"] = ar.error_message
            if ar.log_id is not None:
                out["log_id"] = ar.log_id
            if ar.interactive:
                out["interactive"] = ar.interactive
                if (
                    ar.interactive.get("out_of_band_sent")
                    or ar.interactive.get("status") == "flow_sent"
                ):
                    out["status"] = "interactive_sent"
            return out

        return multi_step_service
