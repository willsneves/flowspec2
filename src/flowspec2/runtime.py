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

import asyncio
import base64
import copy
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Final, Optional, cast

from jsonschema import Draft202012Validator, FormatChecker

from .clock import UtcClock, system_utc_now, utc_timestamp
from .compiler import CompiledFlow, compile_flow
from .interactive import build_flow
from .ir import FlowIR, build_flow_ir
from .json_codec import validate_json_value
from .models import AgentResponse, ServiceMetadata, ServiceState
from .nodes import (
    AUTO_FLOW_RESUME_TARGET_INTERNAL_KEY,
    AUTO_FLOW_SKIPPED_PRESENTATIONS_PAYLOAD_KEY,
    FLOW_FINISHED_INTERNAL_KEY,
    RESET_ON_NEXT_CALL_DATA_KEY,
    await_resume_contract_digest,
    mark_flow_finished,
    mark_slot_skipped,
)
from .observability import SnowflakeIdGenerator, default_log_id_generator, log_event
from .predicates import evaluate
from .profiles import FlowProfile, reference_profile
from .schema import load_flow
from .semantics import FlowLinkError, semantic_diagnostics
from .subflows import SubflowRegistry
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

_AUTO_FLOW_PENDING_INTERNAL_KEY: Final[str] = "_flowspec2_auto_flow_pending"
_AUTO_FLOW_DEADLINE_INTERNAL_KEY: Final[str] = "_flowspec2_auto_flow_deadline"
_AUTO_FLOW_RESEND_COUNT_INTERNAL_KEY: Final[str] = "_flowspec2_auto_flow_resend_count"
_AUTO_FLOW_EVENT_PAYLOAD_KEY: Final[str] = "_auto_flow_event"
_AUTO_FLOW_EVENTS: Final[tuple[str, ...]] = ("cancel", "resend", "fallback", "timeout")


@dataclass
class _UserExecutionLock:
    """One event-loop lock retained while callers hold or await a user lease."""

    lock: asyncio.Lock
    lease_count: int = 0


def _encode_prefill_token(prefill: dict[str, Any]) -> str:
    canonical_prefill = json.dumps(
        prefill,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    raw = base64.urlsafe_b64encode(canonical_prefill).decode()
    return f"v1:{raw}"


def _alias_value_key(value: Any) -> str:
    """Match JSON-authored value-map keys for string and non-string scalars."""

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _reset_execution_state(state: ServiceState) -> None:
    state.data = {}
    state.internal = {}
    state.status = "progress"
    state.agent_response = None
    state.metadata.await_resume = None


def _empty_payload_action(document: dict[str, Any], *, previously_saved: bool) -> str:
    terminal = document.get("terminal") or {}
    empty_payload = terminal.get("empty_payload") or {}
    lifecycle_stage = "in_progress" if previously_saved else "never_saved"
    default_action = "ignore" if previously_saved else "reset"
    action = empty_payload.get(lifecycle_stage, default_action)
    if action not in {"reset", "ignore"}:
        raise ValueError(f"terminal.empty_payload.{lifecycle_stage} must be 'reset' or 'ignore'")
    return str(action)


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
        self.log_id_generator = log_id_generator or default_log_id_generator()
        self.clock = clock if clock is not None else system_utc_now
        self.profile: FlowProfile = reference_profile(tools=tools, subflows=subflows)
        self.ir: FlowIR = build_flow_ir(
            private_doc,
            profile=self.profile,
            validate=validate,
        )
        self._doc = self.ir.to_document()
        self._state_schema_validator = Draft202012Validator(
            self.ir.state_schema(),
            format_checker=FormatChecker(),
        )
        self._profile_exposed_slots = frozenset(
            exposed_slot
            for use_definition in self._doc.get("uses", [])
            for exposed_slot in (
                self.profile.subflows.definition(use_definition["ref"]).exposed_slots or ()
            )
        )
        if validate and (
            linking_diagnostics := semantic_diagnostics(self._doc, profile=self.profile)
        ):
            raise FlowLinkError(linking_diagnostics)
        self.flow = self._doc["flow"]
        self.compiled: CompiledFlow = compile_flow(
            self._doc,
            tools=self.profile.tools,
            subflows=self.profile.subflows,
            log_id_generator=self.log_id_generator,
            clock=self.clock,
            validate_semantics=validate,
        )
        self._tool_binding_revisions = {
            required_capability.removeprefix("tool:"): self.profile.tools.binding_revision(
                required_capability.removeprefix("tool:")
            )
            for required_capability in self.ir.required_capabilities
            if required_capability.startswith("tool:")
        }
        self._store: dict[str, ServiceState] = {}
        self._user_execution_locks: dict[str, _UserExecutionLock] = {}
        self._as_tool_event_loop: asyncio.AbstractEventLoop | None = None

    @property
    def doc(self) -> dict[str, Any]:
        """Return an owned copy of the immutable runtime source snapshot."""

        return copy.deepcopy(self._doc)

    @classmethod
    def from_path(cls, path: str, **kwargs: Any) -> "FlowRuntime":
        return cls(load_flow(path), **kwargs)

    def new_state(self, user_id: str, data: Optional[dict[str, Any]] = None) -> ServiceState:
        seeded_data = copy.deepcopy(data or {})
        seeded_internal: dict[str, Any] = {}
        for state_key in tuple(seeded_data):
            if state_key not in self.compiled.ctx.slots:
                continue
            slot_definition = cast(dict[str, Any], self.compiled.ctx.slots[state_key])
            validated_model = self.compiled.ctx.model_for(state_key).model_validate(
                {state_key: seeded_data.pop(state_key)}
            )
            validated_value = copy.deepcopy(getattr(validated_model, state_key))
            partition_name = cast(str, slot_definition.get("persist", "data"))
            if partition_name == "data":
                seeded_data[state_key] = validated_value
            elif partition_name == "internal":
                seeded_internal[state_key] = validated_value
            else:
                raise ValueError(
                    f"slots.{state_key}.persist has unsupported partition {partition_name!r}"
                )
        state = ServiceState(
            user_id=user_id,
            service_name=self.flow,
            data=seeded_data,
            internal=seeded_internal,
            metadata=ServiceMetadata.from_clock(
                self.clock,
                flow_version=self.ir.version,
                flow_ir_digest=self.ir.digest,
                dependency_digest=self.ir.dependency_digest,
                profile_digest=self.ir.profile_digest,
            ),
        )
        self._validate_execution_state(state)
        return state

    def _retain_user_execution_lock(self, user_id: str) -> _UserExecutionLock:
        running_event_loop = asyncio.get_running_loop()
        if (
            self._as_tool_event_loop is not None
            and self._as_tool_event_loop is not running_event_loop
        ):
            raise RuntimeError(
                "one FlowRuntime.as_tool() adapter cannot execute concurrently across event loops"
            )
        self._as_tool_event_loop = running_event_loop
        execution_lock = self._user_execution_locks.get(user_id)
        if execution_lock is None:
            execution_lock = _UserExecutionLock(lock=asyncio.Lock())
            self._user_execution_locks[user_id] = execution_lock
        execution_lock.lease_count += 1
        return execution_lock

    def _release_user_execution_lock(
        self,
        user_id: str,
        execution_lock: _UserExecutionLock,
    ) -> None:
        execution_lock.lease_count -= 1
        if execution_lock.lease_count < 0:
            raise RuntimeError(f"negative execution-lock lease count for user {user_id!r}")
        if execution_lock.lease_count == 0:
            registered_lock = self._user_execution_locks.pop(user_id, None)
            if registered_lock is not execution_lock:
                raise RuntimeError(f"execution-lock registry changed for user {user_id!r}")
            if not self._user_execution_locks:
                self._as_tool_event_loop = None

    # ── auto_flow (pre-graph) ────────────────────────────────────────────────

    def _should_send_flow(
        self, af: dict[str, Any], state: ServiceState, payload: dict[str, Any]
    ) -> bool:
        source = af.get("on_submit_source", "whatsapp_flow")
        if payload.get("_source") == source:
            return False  # this turn IS the Flow submission
        if state.internal.get(_AUTO_FLOW_PENDING_INTERNAL_KEY):
            return False  # the same external submission is still pending
        if state.internal.get("_started"):
            return False  # mid-flow (e.g. a correction cleared the gating slot) — never re-send
        return bool(evaluate(af["send_when"], state, self.compiled.ctx.config))

    def _flow_sent_state(self, af: dict[str, Any], state: ServiceState) -> ServiceState:
        def prefill_value(slot_name: str) -> Any:
            slot_definition = cast(dict[str, Any], self._doc.get("slots", {}).get(slot_name, {}))
            partition_name = cast(str, slot_definition.get("persist", "data"))
            partition = cast(dict[str, Any], getattr(state, partition_name))
            return partition.get(slot_name)

        prefill = {
            k: prefill_value(k) for k in af.get("prefill_from", []) if prefill_value(k) is not None
        }
        token = _encode_prefill_token(prefill)
        envelope = build_flow(
            flow_id=af["meta_flow_ref"],
            body="Para agilizar, preencha o formulário abaixo. 📋",
            flow_token=token,
        )
        recovery = cast(dict[str, Any], af["recovery"])
        timestamp = utc_timestamp(self.clock)
        deadline = state.internal.get(_AUTO_FLOW_DEADLINE_INTERNAL_KEY)
        if deadline is None:
            deadline = (
                timestamp + timedelta(seconds=cast(int, recovery["timeout_seconds"]))
            ).isoformat()
            state.internal[_AUTO_FLOW_DEADLINE_INTERNAL_KEY] = deadline
        resend_count = cast(int, state.internal.get(_AUTO_FLOW_RESEND_COUNT_INTERNAL_KEY, 0))
        remaining_resends = max(0, cast(int, recovery["max_resends"]) - resend_count)
        state.internal[_AUTO_FLOW_PENDING_INTERNAL_KEY] = True
        state.payload = {}
        state.agent_response = AgentResponse(
            service_name=self.flow,
            description="Enviei um formulário para você preencher. 📋",
            interactive={
                "flow": True,
                "envelope": envelope,
                "flow_token": token,
                "status": "flow_sent",
                "recovery": {
                    "event_field": _AUTO_FLOW_EVENT_PAYLOAD_KEY,
                    "events": list(_AUTO_FLOW_EVENTS),
                    "deadline": deadline,
                    "remaining_resends": remaining_resends,
                },
            },
            data=state.data,
        )
        state.metadata.saved = True
        state.metadata.updated_at = timestamp
        return state

    def _flow_waiting_state(self, state: ServiceState) -> ServiceState:
        state.payload = {}
        state.status = "progress"
        state.agent_response = AgentResponse(
            service_name=self.flow,
            description="Estou aguardando o formulário que enviei. 📋",
            payload_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {_AUTO_FLOW_EVENT_PAYLOAD_KEY: {"enum": list(_AUTO_FLOW_EVENTS)}},
                "required": [_AUTO_FLOW_EVENT_PAYLOAD_KEY],
            },
            data=state.data,
        )
        state.metadata.saved = True
        state.metadata.touch(self.clock)
        return state

    @staticmethod
    def _clear_auto_flow_pending(state: ServiceState) -> None:
        state.internal.pop(_AUTO_FLOW_PENDING_INTERNAL_KEY, None)
        state.internal.pop(_AUTO_FLOW_DEADLINE_INTERNAL_KEY, None)
        state.internal.pop(_AUTO_FLOW_RESEND_COUNT_INTERNAL_KEY, None)

    def _auto_flow_has_expired(self, state: ServiceState) -> bool:
        deadline_value = state.internal.get(_AUTO_FLOW_DEADLINE_INTERNAL_KEY)
        if not isinstance(deadline_value, str):
            raise ValueError("pending auto_flow state has no valid deadline")
        deadline = datetime.fromisoformat(deadline_value)
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("pending auto_flow deadline must include a timezone")
        return utc_timestamp(self.clock) >= deadline

    def _apply_auto_flow_fallback(
        self,
        auto_flow: dict[str, Any],
        state: ServiceState,
    ) -> None:
        recovery = cast(dict[str, Any], auto_flow["recovery"])
        fallback_flow = {**auto_flow, "resume_at": recovery["fallback_at"]}
        state.payload = {}
        self._prepare_auto_flow_submission(
            fallback_flow,
            state,
            state.payload,
            frozenset(),
        )
        self._clear_auto_flow_pending(state)
        state.internal["_started"] = True
        state.status = "progress"
        state.agent_response = None

    def _apply_auto_flow_end(self, state: ServiceState, event: str) -> ServiceState:
        self._clear_auto_flow_pending(state)
        state.payload = {}
        mark_flow_finished(state, reset_next=True)
        state.status = "completed"
        state.agent_response = AgentResponse(
            service_name=self.flow,
            description=(
                "O formulário foi cancelado."
                if event == "cancel"
                else "O prazo do formulário terminou."
            ),
            data=state.data,
        )
        state.metadata.saved = True
        state.metadata.touch(self.clock)
        return state

    def _apply_auto_flow_event(
        self,
        auto_flow: dict[str, Any],
        state: ServiceState,
        event: str,
    ) -> ServiceState | None:
        if event not in _AUTO_FLOW_EVENTS:
            raise ValueError(f"unsupported auto_flow event: {event!r}")
        recovery = cast(dict[str, Any], auto_flow["recovery"])
        if event == "resend":
            resend_count = cast(
                int,
                state.internal.get(_AUTO_FLOW_RESEND_COUNT_INTERNAL_KEY, 0),
            )
            if resend_count >= cast(int, recovery["max_resends"]):
                event = "timeout"
            else:
                state.internal[_AUTO_FLOW_RESEND_COUNT_INTERNAL_KEY] = resend_count + 1
                return self._flow_sent_state(auto_flow, state)
        action = "fallback" if event == "fallback" else recovery[event]
        if action == "END":
            return self._apply_auto_flow_end(state, event)
        if action != "fallback":
            raise ValueError(f"auto_flow event {event!r} has unsupported action {action!r}")
        self._apply_auto_flow_fallback(auto_flow, state)
        return None

    @staticmethod
    def _apply_alias_map(
        alias_map: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any], frozenset[str]]:
        mapped_payload = dict(payload)
        mapped_slots: set[str] = set()

        def bind(slot: str, slot_value: Any) -> None:
            if slot in mapped_payload and mapped_payload[slot] != slot_value:
                raise ValueError(f"auto_flow aliases assign conflicting values to slot {slot!r}")
            mapped_payload[slot] = copy.deepcopy(slot_value)
            mapped_slots.add(slot)

        for field, mapping in alias_map.items():
            if field not in mapped_payload:
                continue
            value = mapped_payload[field]
            value_keyed = all(isinstance(candidate, dict) for candidate in mapping.values())
            if value_keyed:
                selected_mapping = mapping.get(_alias_value_key(value))
                if isinstance(selected_mapping, dict):
                    for slot, configured_value in selected_mapping.items():
                        bind(slot, value if configured_value == "$value" else configured_value)
            else:
                for slot, configured_value in mapping.items():
                    bind(slot, value if configured_value == "$value" else configured_value)
        return mapped_payload, frozenset(mapped_slots)

    def _prepare_auto_flow_submission(
        self,
        auto_flow: dict[str, Any],
        state: ServiceState,
        payload: dict[str, Any],
        mapped_slots: frozenset[str],
    ) -> None:
        """Validate mapped values and atomically seed state before the resume jump."""

        context = self.compiled.ctx
        pending_values: dict[str, Any] = {}
        source = cast(str, auto_flow.get("on_submit_source", "whatsapp_flow"))
        for slot in sorted(mapped_slots):
            if slot not in context.slots:
                raise ValueError(f"auto_flow alias writes unknown slot {slot!r}")
            slot_definition = cast(dict[str, Any], context.slots[slot])
            if slot_definition.get("fill_only_when_asked") and source not in set(
                slot_definition.get("prefill_sources", [])
            ):
                raise ValueError(
                    f"auto_flow source {source!r} is not allowed to prefill slot {slot!r}"
                )
            validated_model = context.model_for(slot).model_validate({slot: payload[slot]})
            pending_values[slot] = copy.deepcopy(getattr(validated_model, slot))

        resume_target = auto_flow.get("resume_at")
        if resume_target is None:
            self._commit_auto_flow_values(state, payload, pending_values)
            return
        compiled_document = self.compiled.doc
        matching_indexes = [
            path_index
            for path_index, path_step in enumerate(compiled_document["path"])
            if path_step.get("id") == resume_target
        ]
        if len(matching_indexes) != 1:
            raise ValueError(
                "auto_flow.resume_at must reference exactly one native path step: "
                f"{resume_target!r}"
            )

        prefix = compiled_document["path"][: matching_indexes[0]]
        skipped_prefix_slots: list[str] = []
        preview_state = state.model_copy(deep=True)
        preview_state.payload = copy.deepcopy(payload)
        self._commit_auto_flow_values(preview_state, preview_state.payload, pending_values)
        override_gates = cast(
            dict[str, Any],
            cast(dict[str, Any], compiled_document.get("overrides", {})).get("gates", {}),
        )
        for path_step in prefix:
            slot = path_step.get("slot")
            confirmation_slot = path_step.get("confirm")
            if slot is not None:
                slot_definition = cast(dict[str, Any], context.slots[slot])
                gate = path_step.get("ask_when") or override_gates.get(path_step.get("id"))
                is_satisfied_by_vacuity = bool(
                    path_step.get("skip_when")
                    and evaluate(path_step["skip_when"], preview_state, context.config)
                ) or bool(gate is not None and not evaluate(gate, preview_state, context.config))
                if is_satisfied_by_vacuity:
                    skipped_prefix_slots.append(cast(str, slot))
                    continue
                if slot_definition.get("required") and not self._slot_available(
                    state, cast(str, slot), pending_values
                ):
                    raise ValueError(
                        f"auto_flow submission cannot resume past required slot {slot!r}"
                    )
                if not slot_definition.get("required") and not self._slot_available(
                    state, cast(str, slot), pending_values
                ):
                    skipped_prefix_slots.append(cast(str, slot))
            elif confirmation_slot is not None:
                confirmation_gate = path_step.get("ask_when") or override_gates.get(
                    path_step.get("id")
                )
                confirmation_is_vacuous = bool(
                    path_step.get("skip_when")
                    and evaluate(path_step["skip_when"], preview_state, context.config)
                ) or bool(
                    confirmation_gate is not None
                    and not evaluate(confirmation_gate, preview_state, context.config)
                )
                if confirmation_is_vacuous:
                    skipped_prefix_slots.append(cast(str, confirmation_slot))
                elif not self._slot_available(state, cast(str, confirmation_slot), pending_values):
                    raise ValueError(
                        "auto_flow submission cannot resume past unanswered confirmation "
                        f"{confirmation_slot!r}"
                    )

        self._commit_auto_flow_values(state, payload, pending_values)
        for skipped_slot in skipped_prefix_slots:
            mark_slot_skipped(state, skipped_slot)
        state.internal[AUTO_FLOW_RESUME_TARGET_INTERNAL_KEY] = resume_target

    def _slot_available(
        self,
        state: ServiceState,
        slot: str,
        pending_values: dict[str, Any],
    ) -> bool:
        if slot in pending_values:
            return True
        slot_definition = cast(dict[str, Any], self.compiled.ctx.slots[slot])
        partition_name = cast(str, slot_definition.get("persist", "data"))
        return slot in cast(dict[str, Any], getattr(state, partition_name))

    def _commit_auto_flow_values(
        self,
        state: ServiceState,
        payload: dict[str, Any],
        pending_values: dict[str, Any],
    ) -> None:
        for slot, validated_value in pending_values.items():
            slot_definition = cast(dict[str, Any], self.compiled.ctx.slots[slot])
            partition_name = cast(str, slot_definition.get("persist", "data"))
            target_partition = cast(dict[str, Any], getattr(state, partition_name))
            for persistent_partition_name in ("data", "internal"):
                persistent_partition = cast(
                    dict[str, Any], getattr(state, persistent_partition_name)
                )
                if persistent_partition is not target_partition:
                    persistent_partition.pop(slot, None)
            target_partition[slot] = validated_value
            payload[slot] = copy.deepcopy(validated_value)

    def _validate_execution_state(self, state: ServiceState) -> None:
        """Validate and normalize restored persistent slots before any effect runs."""

        if state.service_name != self.flow:
            raise ValueError(
                f"state service_name {state.service_name!r} does not match runtime flow {self.flow!r}"
            )
        if not state.user_id.strip():
            raise ValueError("state user_id must be non-empty")
        for tool_name, compiled_binding_revision in self._tool_binding_revisions.items():
            current_binding_revision = self.profile.tools.binding_revision(tool_name)
            if current_binding_revision != compiled_binding_revision:
                raise ValueError(
                    f"tool binding {tool_name!r} changed after this runtime was compiled"
                )
        expected_provenance = {
            "flow_version": self.ir.version,
            "flow_ir_digest": self.ir.digest,
            "dependency_digest": self.ir.dependency_digest,
            "profile_digest": self.ir.profile_digest,
        }
        for metadata_field, expected_value in expected_provenance.items():
            restored_value = getattr(state.metadata, metadata_field)
            if restored_value != expected_value:
                raise ValueError(
                    f"state metadata {metadata_field} {restored_value!r} does not match "
                    f"runtime contract {expected_value!r}"
                )
        if await_resume_provenance := state.metadata.await_resume:
            await_definition = cast(
                dict[str, Any],
                cast(dict[str, Any], self._doc.get("capabilities", {})).get("await_external", {}),
            )
            resume_contract = cast(dict[str, Any] | None, await_definition.get("resume"))
            if resume_contract is None:
                raise ValueError("state metadata await_resume has no typed runtime resume contract")
            compiled_await_definition = self.compiled.ctx.await_external or await_definition
            expected_resume_provenance = {
                "step": compiled_await_definition["step"],
                "version": resume_contract["version"],
                "digest": await_resume_contract_digest(resume_contract),
                "correlation_path": resume_contract["correlation"],
            }
            for provenance_field, expected_value in expected_resume_provenance.items():
                restored_value = getattr(await_resume_provenance, provenance_field)
                if restored_value != expected_value:
                    raise ValueError(
                        f"state metadata await_resume.{provenance_field} "
                        f"{restored_value!r} does not match runtime contract "
                        f"{expected_value!r}"
                    )
            persisted_deadline = await_resume_provenance.deadline
            if await_definition.get("timeout") is not None:
                if persisted_deadline is None:
                    raise ValueError("state metadata await_resume.deadline is missing")
                if persisted_deadline.utcoffset() is None:
                    raise ValueError("state metadata await_resume.deadline must include a timezone")
            elif persisted_deadline is not None:
                raise ValueError(
                    "state metadata await_resume.deadline exists without a runtime timeout contract"
                )

        for exposed_slot in self._profile_exposed_slots:
            present_partitions = [
                partition_name
                for partition_name in ("data", "internal", "payload")
                if exposed_slot in cast(dict[str, Any], getattr(state, partition_name))
            ]
            if unexpected_partitions := [
                partition_name for partition_name in present_partitions if partition_name != "data"
            ]:
                raise ValueError(
                    f"restored profile slot {exposed_slot!r} belongs in 'data', "
                    f"not {unexpected_partitions!r}"
                )

        pending_values: dict[tuple[str, str], Any] = {}
        for slot_name, slot_definition_value in self.compiled.ctx.slots.items():
            slot_definition = cast(dict[str, Any], slot_definition_value)
            expected_partition_name = cast(str, slot_definition.get("persist", "data"))
            if expected_partition_name not in {"data", "internal"}:
                raise ValueError(
                    f"slots.{slot_name}.persist has unsupported partition "
                    f"{expected_partition_name!r}"
                )
            present_partitions = [
                partition_name
                for partition_name in ("data", "internal", "payload")
                if slot_name in cast(dict[str, Any], getattr(state, partition_name))
            ]
            unexpected_partitions = [
                partition_name
                for partition_name in present_partitions
                if partition_name != expected_partition_name
            ]
            if unexpected_partitions:
                raise ValueError(
                    f"restored slot {slot_name!r} belongs in {expected_partition_name!r}, "
                    f"not {unexpected_partitions!r}"
                )
            if expected_partition_name not in present_partitions:
                continue
            expected_partition = cast(dict[str, Any], getattr(state, expected_partition_name))
            validated_model = self.compiled.ctx.model_for(slot_name).model_validate(
                {slot_name: expected_partition[slot_name]}
            )
            pending_values[(expected_partition_name, slot_name)] = copy.deepcopy(
                getattr(validated_model, slot_name)
            )

        state_partitions = {
            "data": copy.deepcopy(state.data),
            "internal": copy.deepcopy(state.internal),
            "payload": copy.deepcopy(state.payload),
        }
        for (partition_name, slot_name), validated_value in pending_values.items():
            state_partitions[partition_name][slot_name] = copy.deepcopy(validated_value)
        validate_json_value(state_partitions, boundary="restored flow state")
        state_validation_errors = sorted(
            self._state_schema_validator.iter_errors(state_partitions),
            key=lambda error: (
                tuple(str(segment) for segment in error.absolute_path),
                tuple(str(segment) for segment in error.absolute_schema_path),
            ),
        )
        if state_validation_errors:
            state_validation_error = state_validation_errors[0]
            instance_path = "".join(
                f"[{segment!r}]" for segment in state_validation_error.absolute_path
            )
            raise ValueError(f"restored state{instance_path}: {state_validation_error.message}")
        for (partition_name, slot_name), validated_value in pending_values.items():
            cast(dict[str, Any], getattr(state, partition_name))[slot_name] = validated_value

    def _completed_resume_delivery(
        self,
        state: ServiceState,
        payload: dict[str, Any],
    ) -> ServiceState | None:
        """Apply replay/late policy before lifecycle reset can erase wait markers."""

        if not (
            state.data.get(RESET_ON_NEXT_CALL_DATA_KEY)
            or state.internal.get(FLOW_FINISHED_INTERNAL_KEY)
        ):
            return None
        await_definition = cast(
            dict[str, Any],
            cast(dict[str, Any], self._doc.get("capabilities", {})).get("await_external", {}),
        )
        resume_contract = cast(dict[str, Any] | None, await_definition.get("resume"))
        resume_on = await_definition.get("resume_on")
        if resume_contract is None or not isinstance(resume_on, str) or resume_on not in payload:
            return None
        if set(payload) != {resume_on}:
            raise ValueError(f"completed await_external delivery must contain only {resume_on!r}")

        token = payload[resume_on]
        token_validator = Draft202012Validator(
            cast(dict[str, Any], resume_contract["schema"]),
            format_checker=FormatChecker(),
        )
        token_errors = sorted(
            token_validator.iter_errors(token),
            key=lambda error: (
                tuple(str(segment) for segment in error.absolute_path),
                tuple(str(segment) for segment in error.absolute_schema_path),
            ),
        )
        if token_errors:
            token_error = token_errors[0]
            raise ValueError(
                f"completed resume token does not satisfy contract: {token_error.message}"
            )
        correlation_reference = cast(str, resume_contract["correlation"])
        correlation_value: Any = token
        for path_segment in correlation_reference.removeprefix("$token.").split("."):
            if not isinstance(correlation_value, dict) or path_segment not in correlation_value:
                raise ValueError(
                    f"completed resume token correlation {correlation_reference!r} is missing"
                )
            correlation_value = correlation_value[path_segment]
        if correlation_value is None or not isinstance(correlation_value, (str, int, float, bool)):
            raise ValueError(
                f"completed resume token correlation {correlation_reference!r} must be a "
                "non-null JSON scalar"
            )
        accepted_correlation = (
            state.metadata.await_resume.correlation_value
            if state.metadata.await_resume is not None
            else None
        )
        canonical_delivered_correlation = json.dumps(
            correlation_value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        canonical_accepted_correlation = (
            json.dumps(
                accepted_correlation,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if accepted_correlation is not None
            else None
        )
        same_correlation = (
            canonical_accepted_correlation is not None
            and canonical_delivered_correlation == canonical_accepted_correlation
        )
        delivery_kind = "duplicate" if same_correlation else "late"
        delivery_policy = cast(str, resume_contract[delivery_kind])
        compiled_await_definition = self.compiled.ctx.await_external or await_definition
        if delivery_policy == "reject":
            raise ValueError(
                f"await_external {compiled_await_definition['step']!r} rejected "
                f"{delivery_kind} resume delivery"
            )
        return self._finalize_response(state)

    # ── execute ──────────────────────────────────────────────────────────────

    async def execute(
        self, state: ServiceState, payload: Optional[dict[str, Any]] = None
    ) -> ServiceState:
        try:
            self._validate_execution_state(state)
        except Exception as exc:  # noqa: BLE001
            log_id = log_event(
                logger,
                logging.ERROR,
                "Restored flow state failed validation",
                operation="restore_state_validation",
                log_id_generator=self.log_id_generator,
                context={"flow": self.flow, "error_type": type(exc).__name__},
                exc_info=True,
            )
            state.status = "error"
            state.agent_response = AgentResponse(
                description="Não consegui restaurar o estado deste atendimento.",
                error_message=str(exc),
                log_id=log_id,
            )
            return self._finalize_response(state)

        payload = dict(payload or {})
        try:
            validate_json_value(payload, boundary="flow payload")
        except Exception as exc:  # noqa: BLE001
            log_id = log_event(
                logger,
                logging.WARNING,
                "Flow payload failed strict JSON validation",
                operation="payload_validation",
                log_id_generator=self.log_id_generator,
                context={"flow": self.flow, "error_type": type(exc).__name__},
                exc_info=True,
            )
            state.status = "error"
            state.agent_response = AgentResponse(
                description="Não consegui validar os dados enviados neste turno.",
                error_message=str(exc),
                log_id=log_id,
            )
            return self._finalize_response(state)
        payload.pop(AUTO_FLOW_SKIPPED_PRESENTATIONS_PAYLOAD_KEY, None)
        state.internal.pop(AUTO_FLOW_RESUME_TARGET_INTERNAL_KEY, None)

        try:
            if completed_delivery_state := self._completed_resume_delivery(state, payload):
                return completed_delivery_state
        except Exception as exc:  # noqa: BLE001
            log_id = log_event(
                logger,
                logging.WARNING,
                "Completed external-resume delivery failed validation",
                operation="await_external_replay",
                log_id_generator=self.log_id_generator,
                context={"flow": self.flow, "error_type": type(exc).__name__},
                exc_info=True,
            )
            state.status = "error"
            state.agent_response = AgentResponse(
                description="Não consegui processar o retorno da ação externa.",
                error_message=str(exc),
                log_id=log_id,
            )
            return self._finalize_response(state)

        # Reset semantics run FIRST so the auto_flow gate and the graph both see a
        # clean slate after a completed/aborted run (post-success reset) or a fresh
        # never-saved start. Mirrors base_workflow.execute, hoisted above the
        # tool-layer auto_flow check.
        if state.data.get(RESET_ON_NEXT_CALL_DATA_KEY):
            _reset_execution_state(state)
        elif state.internal.get(FLOW_FINISHED_INTERNAL_KEY):
            return self._finalize_response(state)
        elif (
            not payload
            and _empty_payload_action(self._doc, previously_saved=state.metadata.saved) == "reset"
        ):
            _reset_execution_state(state)

        state.payload = copy.deepcopy(payload)
        af = self._doc.get("auto_flow")
        recovered_from_pending_flow = False
        if af:
            try:
                pending_flow = bool(state.internal.get(_AUTO_FLOW_PENDING_INTERNAL_KEY))
                explicit_event_present = _AUTO_FLOW_EVENT_PAYLOAD_KEY in payload
                explicit_event = payload.get(_AUTO_FLOW_EVENT_PAYLOAD_KEY)
                if explicit_event_present and not pending_flow:
                    raise ValueError("auto_flow events require a pending form")
                if pending_flow:
                    recovery_event: str | None = None
                    if explicit_event_present:
                        if not isinstance(explicit_event, str):
                            raise ValueError("auto_flow event must be a string")
                        if set(payload) != {_AUTO_FLOW_EVENT_PAYLOAD_KEY}:
                            raise ValueError(
                                "auto_flow event payload cannot include unrelated fields"
                            )
                        recovery_event = explicit_event
                    elif self._auto_flow_has_expired(state):
                        recovery_event = "timeout"
                        payload = {}
                        state.payload = {}
                    if recovery_event is not None:
                        recovery_state = self._apply_auto_flow_event(
                            af,
                            state,
                            recovery_event,
                        )
                        if recovery_state is not None:
                            return recovery_state
                        payload = {}
                        recovered_from_pending_flow = True
            except Exception as exc:  # noqa: BLE001
                log_id = log_event(
                    logger,
                    logging.WARNING,
                    "Auto-flow recovery failed validation",
                    operation="auto_flow_recovery",
                    log_id_generator=self.log_id_generator,
                    context={"flow": self.flow, "error_type": type(exc).__name__},
                    exc_info=True,
                )
                state.status = "error"
                state.agent_response = AgentResponse(
                    description="Não consegui processar a ação do formulário.",
                    error_message=str(exc),
                    log_id=log_id,
                )
                return self._finalize_response(state)
        if af and not recovered_from_pending_flow and self._should_send_flow(af, state, payload):
            return self._flow_sent_state(af, state)
        if (
            af
            and not recovered_from_pending_flow
            and state.internal.get(_AUTO_FLOW_PENDING_INTERNAL_KEY)
            and payload.get("_source") != af.get("on_submit_source", "whatsapp_flow")
        ):
            return self._flow_waiting_state(state)
        if af and payload.get("_source") == af.get("on_submit_source", "whatsapp_flow"):
            try:
                payload, aliased_slots = self._apply_alias_map(
                    af.get("alias_map", {}) or {}, payload
                )
                identity_slots = frozenset(payload) & frozenset(self.compiled.ctx.slots)
                mapped_slots = aliased_slots | identity_slots
                state.payload = payload
                self._prepare_auto_flow_submission(af, state, payload, mapped_slots)
                self._clear_auto_flow_pending(state)
            except Exception as exc:  # noqa: BLE001
                log_id = log_event(
                    logger,
                    logging.WARNING,
                    "Auto-flow submission failed validation",
                    operation="auto_flow_submission",
                    log_id_generator=self.log_id_generator,
                    context={"flow": self.flow, "error_type": type(exc).__name__},
                    exc_info=True,
                )
                state.status = "error"
                state.agent_response = AgentResponse(
                    description="Não consegui validar o formulário enviado.",
                    error_message=str(exc),
                    log_id=log_id,
                )
                return self._finalize_response(state)

        state.payload = payload
        state.internal["_started"] = (
            True  # the graph is about to run; corrections now route through it
        )
        result = await self.compiled.graph.ainvoke(state)
        final = result if isinstance(result, ServiceState) else ServiceState(**result)

        return self._finalize_response(final)

    def _finalize_response(self, final: ServiceState) -> ServiceState:
        """Apply the stable caller-facing envelope to a graph or lifecycle result."""

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
        final.internal.pop(AUTO_FLOW_RESUME_TARGET_INTERNAL_KEY, None)
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
        payload_schema + status + optional interactive/error/log ID). Calls for
        one user are serialized across every adapter returned by this runtime;
        calls for distinct users remain concurrent. The lock and state store are
        in-memory contracts scoped to one ``FlowRuntime`` and one event loop at a
        time. A multi-process host or a host with multiple runtime instances must
        provide transactional shared persistence and distributed per-user
        serialization outside this reference adapter.

        ``out_of_band``/flow sends surface as ``status: interactive_sent`` so an
        outer agent ends the turn. Returned structured values are owned copies,
        so caller mutation cannot alter the saved state.
        """

        async def multi_step_service(
            service_name: str, user_id: str, payload: Optional[dict[str, Any]] = None
        ) -> dict[str, Any]:
            if service_name != self.flow:
                raise ValueError(
                    f"service_name {service_name!r} does not match runtime flow {self.flow!r}"
                )

            execution_lock = self._retain_user_execution_lock(user_id)
            try:
                async with execution_lock.lock:
                    state = self._store.get(user_id) or self.new_state(user_id)
                    state = await self.execute(state, payload or {})
                    agent_response = state.agent_response or AgentResponse()
                    output: dict[str, Any] = {
                        "status": state.status,
                        "description": agent_response.description,
                        "payload_schema": copy.deepcopy(agent_response.payload_schema),
                        "data": copy.deepcopy(state.data),
                    }
                    if agent_response.error_message is not None:
                        output["error_message"] = agent_response.error_message
                    if agent_response.log_id is not None:
                        output["log_id"] = agent_response.log_id
                    if agent_response.interactive:
                        output["interactive"] = copy.deepcopy(agent_response.interactive)
                        if (
                            agent_response.interactive.get("out_of_band_sent")
                            or agent_response.interactive.get("status") == "flow_sent"
                        ):
                            output["status"] = "interactive_sent"
                    validate_json_value(output, boundary="flow tool output")
                    self._store[user_id] = state
                    return output
            finally:
                self._release_user_execution_lock(user_id, execution_lock)

        return multi_step_service
