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
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Final, Optional, cast

from jsonschema import Draft202012Validator, FormatChecker
from langgraph.graph import END as LANGGRAPH_END
from pydantic import BaseModel

from .clock import UtcClock, utc_timestamp
from .derive import encode_derive_key
from .domains import make_slot_model
from .interactive import options_from_domain
from .models import (
    CORRECTION_REQUESTED_INTERNAL_KEY,
    CORRECTION_TARGETS_SCHEMA_KEY,
    AgentResponse,
    AwaitResumeProvenance,
    ServiceState,
)
from .observability import SnowflakeIdGenerator, log_event
from .predicates import evaluate
from .tools import ToolRegistry

END: Final[str] = LANGGRAPH_END
NEXT = "__NEXT__"  # router sentinel: "the next node in the flat sequence"
AUTO_FLOW_SKIPPED_PRESENTATIONS_PAYLOAD_KEY: Final[str] = (
    "_flowspec2_auto_flow_skipped_presentations"
)
AUTO_FLOW_RESUME_TARGET_INTERNAL_KEY: Final[str] = "_flowspec2_auto_flow_resume_target"
FLOW_FINISHED_INTERNAL_KEY: Final[str] = "_flowspec2_flow_finished"
RESET_ON_NEXT_CALL_DATA_KEY: Final[str] = "_reset_on_next_call"
DEFAULT_AWAIT_EXTERNAL_TIMEOUT_SECONDS: Final[int] = 900
_CORRECTION_UNAVAILABLE_INTERNAL_KEY: Final[str] = "_flowspec2_correction_unavailable"
_MISSING = object()
_ASKED_SLOT_KEY: Final[str] = "_collection_asked_slot"
_ANSWERING_SLOT_KEY: Final[str] = "_collection_answering_slot"
_SLOT_PARTITIONS: Final[tuple[str, ...]] = ("data", "internal")

logger = logging.getLogger(__name__)

NodeFn = Callable[[ServiceState], Awaitable[ServiceState]]
RouterFn = Callable[[ServiceState], str]


def await_resume_contract_digest(resume_contract: dict[str, Any]) -> str:
    """Return the canonical identity hosts persist beside a resume signal."""

    canonical_contract = json.dumps(
        resume_contract,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_contract).hexdigest()


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
    clock: UtcClock
    flow_name: str
    flow_revision: str
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


def mark_flow_finished(state: ServiceState, *, reset_next: bool) -> None:
    """Record a terminal lifecycle decision independently of response status."""

    state.internal[FLOW_FINISHED_INTERNAL_KEY] = True
    if reset_next:
        state.data[RESET_ON_NEXT_CALL_DATA_KEY] = True


def _att_key(slot: str) -> str:
    return f"_attempts_{slot}"


def _skipped_slot_key(slot: str) -> str:
    return f"_slot_skipped:{slot}"


def mark_slot_skipped(state: ServiceState, slot: str) -> None:
    """Persist the declared no-value state used for optional position recovery."""

    state.internal[_skipped_slot_key(slot)] = True


def _slot_partition_name(ctx: FlowContext, slot: str) -> str:
    partition_name = str(ctx.slots.get(slot, {}).get("persist", "data"))
    if partition_name not in _SLOT_PARTITIONS:
        raise ValueError(f"slots.{slot}.persist has unsupported partition {partition_name!r}")
    return partition_name


def _slot_partition(state: ServiceState, slot: str, ctx: FlowContext) -> dict[str, Any]:
    return cast(dict[str, Any], getattr(state, _slot_partition_name(ctx, slot)))


def _slot_is_persisted(state: ServiceState, slot: str, ctx: FlowContext) -> bool:
    return slot in _slot_partition(state, slot, ctx)


def _slot_content(state: ServiceState, slot: str, ctx: FlowContext) -> Any:
    return _slot_partition(state, slot, ctx).get(slot)


def _store_slot(state: ServiceState, slot: str, content: Any, ctx: FlowContext) -> None:
    target_partition = _slot_partition(state, slot, ctx)
    for partition_name in ("data", "internal"):
        partition = cast(dict[str, Any], getattr(state, partition_name))
        if partition is not target_partition:
            partition.pop(slot, None)
    target_partition[slot] = content
    state.internal.pop(_skipped_slot_key(slot), None)


def _clear_slot(
    state: ServiceState,
    slot: str,
    *,
    clear_payload: bool = False,
) -> None:
    state.data.pop(slot, None)
    state.internal.pop(slot, None)
    if clear_payload:
        state.payload.pop(slot, None)


def _payload_slot_input(
    state: ServiceState,
    slot: str,
    payload_field: str,
) -> tuple[bool, Any]:
    slot_is_present = slot in state.payload
    field_is_present = payload_field in state.payload
    if (
        payload_field != slot
        and slot_is_present
        and field_is_present
        and state.payload[slot] != state.payload[payload_field]
    ):
        raise ValueError(
            f"payload fields {slot!r} and {payload_field!r} contain conflicting values"
        )
    if field_is_present:
        return True, state.payload[payload_field]
    if slot_is_present:
        return True, state.payload[slot]
    return False, None


def _clear_payload_slot_input(state: ServiceState, slot: str, payload_field: str) -> None:
    state.payload.pop(slot, None)
    state.payload.pop(payload_field, None)


def inc_attempts(state: ServiceState, slot: str) -> int:
    n = int(state.data.get(_att_key(slot), 0)) + 1
    state.data[_att_key(slot)] = n
    return n


def reset_attempts(state: ServiceState, slot: str) -> None:
    state.data.pop(_att_key(slot), None)


def clear_cascade(
    state: ServiceState,
    slot: str,
    ctx: FlowContext,
    *,
    clear_payload: bool = False,
) -> None:
    """Pop a slot + its transitive ``requires[]``-dependents + derives reading them."""
    to_clear = {slot} | ctx.dependents.get(slot, set())
    for s in to_clear:
        _clear_slot(state, s, clear_payload=clear_payload)
        reset_attempts(state, s)
        state.internal.pop(_skipped_slot_key(s), None)
        if state.internal.get(_ASKED_SLOT_KEY) == s:
            state.internal.pop(_ASKED_SLOT_KEY, None)
        if state.internal.get(_ANSWERING_SLOT_KEY) == s:
            state.internal.pop(_ANSWERING_SLOT_KEY, None)
        for aux in ctx.slot_aux.get(s, []):
            state.data.pop(aux, None)
        for derived in ctx.derive_readers.get(s, []):
            if derived in ctx.slots:
                _clear_slot(state, derived, clear_payload=clear_payload)
            else:
                state.data.pop(derived, None)


def _dig(obj: Any, path: str) -> Any:
    parts = path.split(".")
    if not parts or parts[0] != "result":
        raise ValueError(f"terminal output path must use the result.* namespace: {path!r}")
    parts = parts[1:]
    cur = obj
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(f"terminal output path does not exist: {path!r}")
        cur = cur[p]
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


def _validate_state_writes(
    ctx: FlowContext,
    pending_writes: dict[str, Any],
) -> dict[str, tuple[str, Any]]:
    """Validate all declared-slot destinations without mutating state."""

    validated_writes: dict[str, tuple[str, Any]] = {}
    for state_key, pending_value in pending_writes.items():
        if state_key not in ctx.slots:
            validated_writes[state_key] = ("data", copy.deepcopy(pending_value))
            continue
        slot_definition = cast(dict[str, Any], ctx.slots[state_key])
        validated_model = ctx.model_for(state_key).model_validate({state_key: pending_value})
        validated_writes[state_key] = (
            cast(str, slot_definition.get("persist", "data")),
            copy.deepcopy(getattr(validated_model, state_key)),
        )
    return validated_writes


def _commit_state_writes(
    state: ServiceState,
    validated_writes: dict[str, tuple[str, Any]],
) -> None:
    for state_key, (partition_name, validated_value) in validated_writes.items():
        target_partition = cast(dict[str, Any], getattr(state, partition_name))
        for persistent_partition_name in ("data", "internal"):
            persistent_partition = cast(dict[str, Any], getattr(state, persistent_partition_name))
            if persistent_partition is not target_partition:
                persistent_partition.pop(state_key, None)
        target_partition[state_key] = validated_value


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
    resume_contract = cast(dict[str, Any] | None, capability.get("resume"))
    resume_contract_digest = (
        await_resume_contract_digest(resume_contract) if resume_contract is not None else None
    )
    resume_schema_validator = (
        Draft202012Validator(
            cast(dict[str, Any], resume_contract["schema"]),
            format_checker=FormatChecker(),
        )
        if resume_contract is not None
        else None
    )
    correlation_reference = (
        cast(str, resume_contract["correlation"]) if resume_contract is not None else None
    )
    timeout_transition = capability.get("timeout")
    timeout_seconds = (
        cast(int, capability.get("timeout_seconds", DEFAULT_AWAIT_EXTERNAL_TIMEOUT_SECONDS))
        if timeout_transition is not None
        else None
    )

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

    def provenance_for(
        correlation_value: str | int | float | bool | None = None,
        *,
        deadline: datetime | None = None,
    ) -> AwaitResumeProvenance | None:
        if (
            resume_contract is None
            or resume_contract_digest is None
            or correlation_reference is None
        ):
            return None
        return AwaitResumeProvenance(
            step=node_id,
            version=cast(str, resume_contract["version"]),
            digest=resume_contract_digest,
            correlation_path=correlation_reference,
            correlation_value=correlation_value,
            deadline=deadline,
        )

    def validate_resume_token(token: Any) -> str | int | float | bool | None:
        if (
            resume_contract is None
            or resume_schema_validator is None
            or correlation_reference is None
        ):
            return None
        validation_errors = sorted(
            resume_schema_validator.iter_errors(token),
            key=lambda error: (
                tuple(str(segment) for segment in error.absolute_path),
                tuple(str(segment) for segment in error.absolute_schema_path),
            ),
        )
        if validation_errors:
            validation_error = validation_errors[0]
            instance_path = "".join(f"[{segment!r}]" for segment in validation_error.absolute_path)
            raise ValueError(
                f"resume token{instance_path} does not satisfy contract "
                f"{resume_contract['version']!r}: {validation_error.message}"
            )
        correlation_value = _dig_binding(
            token,
            correlation_reference.removeprefix("$token."),
        )
        if (
            correlation_value is _MISSING
            or correlation_value is None
            or not isinstance(
                correlation_value,
                (str, int, float, bool),
            )
        ):
            raise ValueError(
                f"resume token correlation {correlation_reference!r} must resolve to a "
                "non-null JSON scalar"
            )
        return correlation_value

    def canonical_correlation(correlation_value: str | int | float | bool) -> str:
        return json.dumps(
            correlation_value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    async def node(state: ServiceState) -> ServiceState:
        try:
            state.internal.pop(route_internal_key, None)
            payload = state.payload or {}
            has_resume_token = resume_on in payload
            has_external_event = "_external_event" in payload
            if has_resume_token and has_external_event:
                raise ValueError(
                    f"await_external {node_id!r} received both {resume_on!r} and _external_event"
                )

            if state.internal.get(completed_internal_key):
                if has_resume_token and resume_contract is not None:
                    delivered_correlation = validate_resume_token(payload[resume_on])
                    assert delivered_correlation is not None
                    accepted_provenance = state.metadata.await_resume
                    same_correlation = (
                        accepted_provenance is not None
                        and accepted_provenance.step == node_id
                        and accepted_provenance.digest == resume_contract_digest
                        and accepted_provenance.correlation_value is not None
                        and canonical_correlation(accepted_provenance.correlation_value)
                        == canonical_correlation(delivered_correlation)
                    )
                    replay_policy = cast(
                        str,
                        resume_contract["duplicate" if same_correlation else "late"],
                    )
                    if replay_policy == "reject":
                        delivery_kind = "duplicate" if same_correlation else "late"
                        raise ValueError(
                            f"await_external {node_id!r} rejected {delivery_kind} resume delivery"
                        )
                    state.payload.pop(resume_on, None)
                elif has_external_event:
                    raise ValueError(f"await_external {node_id!r} is already completed")
                state.status = "progress"
                state.agent_response = None
                return state

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
                if external_event == "timeout":
                    persisted_deadline = (
                        state.metadata.await_resume.deadline
                        if state.metadata.await_resume is not None
                        else None
                    )
                    if persisted_deadline is None:
                        raise ValueError(
                            f"await_external {node_id!r} timeout has no persisted deadline"
                        )
                    if utc_timestamp(ctx.clock) < persisted_deadline:
                        raise ValueError(
                            f"await_external {node_id!r} rejected timeout before its deadline"
                        )
                validated_transition_writes = _validate_state_writes(
                    ctx,
                    copy.deepcopy(transition.get("set") or {}),
                )
                state.payload.pop("_external_event", None)
                clear_sent(state)
                if external_event == "resend":
                    state.internal.pop(completed_internal_key, None)
                else:
                    state.internal[completed_internal_key] = True
                _commit_state_writes(state, validated_transition_writes)
                transition_target = transition["goto"]
                state.internal[route_internal_key] = transition_target
                if transition_target == "END":
                    mark_flow_finished(state, reset_next=True)
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
                correlation_value = validate_resume_token(token)
                token_writes = _resolve_bindings(token_bindings, "token", token)
                validated_writes = _validate_state_writes(ctx, token_writes)
                if enrichment:
                    tool_name = enrichment["tool"]
                    try:
                        tool_inputs = _resolve_bindings(
                            enrichment.get("input") or {}, "token", token
                        )
                        enrichment_result = await ctx.tools.call(tool_name, **tool_inputs)
                        if not isinstance(enrichment_result, dict):
                            raise TypeError(f"tool {tool_name!r} returned a non-object result")
                        enrichment_writes = _resolve_bindings(
                            enrichment.get("set") or {}, "result", enrichment_result
                        )
                        validated_writes = _validate_state_writes(
                            ctx,
                            {**token_writes, **enrichment_writes},
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
                _commit_state_writes(state, validated_writes)
                clear_sent(state)
                state.internal[completed_internal_key] = True
                prior_provenance = state.metadata.await_resume
                if (
                    correlation_value is not None
                    and (
                        accepted_provenance := provenance_for(
                            correlation_value,
                            deadline=(
                                prior_provenance.deadline if prior_provenance is not None else None
                            ),
                        )
                    )
                    is not None
                ):
                    state.metadata.await_resume = accepted_provenance
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
            if resume_contract is not None and resume_contract_digest is not None:
                deadline = (
                    utc_timestamp(ctx.clock) + timedelta(seconds=timeout_seconds)
                    if timeout_seconds is not None
                    else None
                )
                host_marker["resume_contract"] = {
                    "version": resume_contract["version"],
                    "digest": resume_contract_digest,
                    "correlation": resume_contract["correlation"],
                    "duplicate": resume_contract["duplicate"],
                    "late": resume_contract["late"],
                }
                if deadline is not None:
                    host_marker["resume_contract"]["deadline"] = deadline.isoformat()
                state.metadata.await_resume = provenance_for(deadline=deadline)
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
    ctx: FlowContext,
    entry: Optional[dict[str, Any]],
    service_seed: dict[str, Any],
    *,
    auto_flow_resume_targets: frozenset[str] = frozenset(),
) -> NodeDesc:
    async def node(state: ServiceState) -> ServiceState:
        asked_slot = state.internal.pop(_ASKED_SLOT_KEY, None)
        if asked_slot is None:
            state.internal.pop(_ANSWERING_SLOT_KEY, None)
        else:
            state.internal[_ANSWERING_SLOT_KEY] = asked_slot
        if service_seed:
            state.data["service"] = copy.deepcopy(service_seed)
        if entry and not state.internal.get("_entry_done"):
            try:
                result = await ctx.tools.call(entry["tool"])
                if entry.get("writes"):
                    state.data[entry["writes"]] = result
            except Exception as exc:
                if entry.get("blocking"):
                    return _handle_node_error(
                        state,
                        exc,
                        ctx=ctx,
                        operation="__init__",
                    )
                log_event(
                    logger,
                    logging.WARNING,
                    "Non-blocking entry tool failed",
                    operation="__init__",
                    log_id_generator=ctx.log_id_generator,
                    context={"tool": entry["tool"]},
                    exc_info=True,
                )
            state.internal["_entry_done"] = True
        state.agent_response = None
        return state

    def router(state: ServiceState) -> str:
        if state.agent_response is not None:
            return END
        resume_target = state.internal.pop(AUTO_FLOW_RESUME_TARGET_INTERNAL_KEY, None)
        if resume_target is None:
            return NEXT
        if resume_target not in auto_flow_resume_targets:
            raise ValueError(f"unexpected auto-flow resume target: {resume_target!r}")
        return cast(str, resume_target)

    return NodeDesc(
        id="__init__",
        fn=node,
        router=router,
        targets=sorted(auto_flow_resume_targets),
    )


# ── collect (slot) node ──────────────────────────────────────────────────────


def _collection_payload_schema(
    model: type[BaseModel], slot: str, *, required: bool
) -> dict[str, Any]:
    """Expose explicit null as the deterministic skip token for an optional slot."""

    payload_schema = copy.deepcopy(model.model_json_schema())
    if required:
        return payload_schema

    slot_schema = cast(dict[str, Any], payload_schema["properties"][slot])
    slot_type = slot_schema.get("type")
    any_of = slot_schema.get("anyOf")
    allows_null = (
        slot_type == "null"
        or (isinstance(slot_type, list) and "null" in slot_type)
        or (isinstance(any_of, list) and any(option.get("type") == "null" for option in any_of))
        or slot_schema.get("const", _MISSING) is None
        or None in slot_schema.get("enum", [])
    )
    if not allows_null:
        if "const" in slot_schema:
            constant = slot_schema.pop("const")
            slot_schema["enum"] = [constant, None]
        elif "enum" in slot_schema:
            slot_schema["enum"] = [*slot_schema["enum"], None]
        elif isinstance(slot_type, str):
            slot_schema["type"] = [slot_type, "null"]
        elif isinstance(slot_type, list):
            slot_schema["type"] = [*slot_type, "null"]
        elif isinstance(any_of, list):
            any_of.append({"type": "null"})
        else:
            payload_schema["properties"][slot] = {
                "anyOf": [slot_schema, {"type": "null"}],
            }
            slot_schema = payload_schema["properties"][slot]

        updated_slot_type = slot_schema.get("type")
        if isinstance(updated_slot_type, str):
            slot_schema["type"] = [updated_slot_type, "null"]
        elif isinstance(updated_slot_type, list) and "null" not in updated_slot_type:
            slot_schema["type"] = [*updated_slot_type, "null"]

    description = str(slot_schema.get("description", "")).strip()
    skip_description = "Use null when the citizen chooses not to provide this optional value."
    slot_schema["description"] = f"{description} {skip_description}".strip()
    return payload_schema


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
    payload_field = (interactive or {}).get("field", slot)
    skip_when = step.get("skip_when")
    required = bool(slot_cfg.get("required", False))
    max_attempts = int(slot_cfg.get("max_attempts", ctx.max_attempts))
    on_exhaust = slot_cfg.get("on_exhaust", "reask")
    validated_default: Any = _MISSING
    if on_exhaust == "default":
        if "default" not in slot_cfg:
            raise ValueError(f"slots.{slot}.default is required when on_exhaust is 'default'")
        try:
            default_model = model.model_validate({slot: slot_cfg["default"]})
        except Exception as exc:
            raise ValueError(f"slots.{slot}.default is invalid for its domain") from exc
        validated_default = getattr(default_model, slot)
    payload_schema = _collection_payload_schema(model, slot, required=required)
    skipped_slot_key = _skipped_slot_key(slot)
    fill_only_when_asked = bool(slot_cfg.get("fill_only_when_asked", False))
    prefill_sources = frozenset(slot_cfg.get("prefill_sources", []) or [])
    required_slots = tuple(slot_cfg.get("requires", []) or [])

    def ask(state: ServiceState, error: Optional[str] = None) -> AgentResponse:
        state.internal[_ASKED_SLOT_KEY] = slot
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
            payload_schema=payload_schema,
            error_message=error,
            interactive=spec,
        )

    def exhaust(state: ServiceState) -> ServiceState:
        if on_exhaust == "skip":
            _clear_slot(state, slot, clear_payload=True)
            _clear_payload_slot_input(state, slot, payload_field)
            state.internal[skipped_slot_key] = True
            reset_attempts(state, slot)
            state.agent_response = None
        elif on_exhaust == "default":
            _store_slot(state, slot, validated_default, ctx)
            state.internal.pop(skipped_slot_key, None)
            reset_attempts(state, slot)
            state.agent_response = None
        elif on_exhaust == "handoff":
            state.agent_response = AgentResponse(
                description="Vou te encaminhar para um atendente da Central 1746."
            )
        elif on_exhaust == "END":
            mark_flow_finished(state, reset_next=True)
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
            reset_attempts(state, slot)
        return state

    async def node(state: ServiceState) -> ServiceState:
        try:
            correction_requested = state.internal.get(CORRECTION_REQUESTED_INTERNAL_KEY) == slot
            if correction_requested:
                clear_cascade(state, slot, ctx)
                state.internal.pop(CORRECTION_REQUESTED_INTERNAL_KEY, None)
            skip_is_active = bool(skip_when and evaluate(skip_when, state, ctx.config))
            gate_is_closed = bool(gate is not None and not evaluate(gate, state, ctx.config))
            requirements_are_missing = any(
                not _slot_is_persisted(state, required_slot, ctx)
                for required_slot in required_slots
            )
            if correction_requested and (
                skip_is_active or gate_is_closed or requirements_are_missing
            ):
                state.internal[_CORRECTION_UNAVAILABLE_INTERNAL_KEY] = slot
            if skip_is_active:
                state.agent_response = None
                return state
            if gate_is_closed:
                state.agent_response = None  # gated out → satisfied by vacuity
                return state
            if requirements_are_missing:
                state.internal[skipped_slot_key] = True
                state.agent_response = None
                return state
            if _slot_is_persisted(state, slot, ctx) or state.internal.get(skipped_slot_key):
                state.agent_response = None
                return state
            payload_has_slot, payload_content = _payload_slot_input(state, slot, payload_field)
            if payload_has_slot:
                payload_source = state.payload.get("_source")
                answers_asked_slot = state.internal.get(_ANSWERING_SLOT_KEY) == slot
                permitted_prefill = payload_source in prefill_sources
                if fill_only_when_asked and not (answers_asked_slot or permitted_prefill):
                    state.agent_response = ask(state)
                    return state
                if payload_content is None and not required:
                    _clear_slot(state, slot, clear_payload=True)
                    _clear_payload_slot_input(state, slot, payload_field)
                    state.internal[skipped_slot_key] = True
                    reset_attempts(state, slot)
                    state.agent_response = None
                    return state
                try:
                    validated = model.model_validate({slot: payload_content})
                    _store_slot(state, slot, getattr(validated, slot), ctx)
                    state.internal.pop(skipped_slot_key, None)
                    reset_attempts(state, slot)
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


def make_derive_node(
    ctx: FlowContext,
    derive: dict[str, Any],
    *,
    node_id: str | None = None,
) -> NodeDesc:
    writes = derive["writes"]
    from_slots = derive["from"]
    lookup = derive["lookup"]
    default = derive.get("default")
    effective_node_id = node_id or f"derive_{writes}"

    async def node(state: ServiceState) -> ServiceState:
        try:
            if writes in ctx.slots and _slot_is_persisted(state, writes, ctx):
                state.agent_response = None
                return state
            if writes not in ctx.slots and writes in state.data:
                state.agent_response = None
                return state
            key = encode_derive_key(_slot_content(state, source, ctx) for source in from_slots)
            value = lookup.get(key)
            if value is None and default is not None:
                if isinstance(default, str) and default.startswith("$from["):
                    idx = int(default[len("$from[") : -1])
                    value = _slot_content(state, from_slots[idx], ctx)
                else:
                    value = default
            if value is not None:
                if writes in ctx.slots:
                    model = ctx.model_for(writes)
                    validated = model.model_validate({writes: value})
                    _store_slot(state, writes, getattr(validated, writes), ctx)
                else:
                    state.data[writes] = value
            state.agent_response = None
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=effective_node_id)

    return NodeDesc(id=effective_node_id, fn=node, router=lambda s: NEXT)


# ── confirm nodes (summary / bool / hub) ─────────────────────────────────────


@dataclass(frozen=True)
class _ConfirmationPolicy:
    max_attempts: int
    on_exhaust: str
    default: Any = _MISSING


def _confirmation_policy(
    ctx: FlowContext,
    slot: str,
    model: type[BaseModel],
) -> _ConfirmationPolicy:
    slot_definition = cast(dict[str, Any], ctx.slots[slot])
    on_exhaust = cast(str, slot_definition.get("on_exhaust", "reask"))
    validated_default: Any = _MISSING
    if on_exhaust == "default":
        if "default" not in slot_definition:
            raise ValueError(f"slots.{slot}.default is required when on_exhaust is 'default'")
        try:
            validated_model = model.model_validate({slot: slot_definition["default"]})
        except Exception as exc:
            raise ValueError(f"slots.{slot}.default is invalid for its domain") from exc
        validated_default = cast(bool, getattr(validated_model, slot))
    return _ConfirmationPolicy(
        max_attempts=int(slot_definition.get("max_attempts", ctx.max_attempts)),
        on_exhaust=on_exhaust,
        default=validated_default,
    )


def _confirmation_failure_action(
    state: ServiceState,
    *,
    slot: str,
    node_id: str,
    policy: _ConfirmationPolicy,
    ctx: FlowContext,
) -> tuple[str, bool | None]:
    if inc_attempts(state, slot) < policy.max_attempts:
        return "reask", None
    reset_attempts(state, slot)
    if policy.on_exhaust == "reask":
        return "reask", None
    if policy.on_exhaust == "skip":
        _clear_slot(state, slot, clear_payload=True)
        state.internal[_skipped_slot_key(slot)] = True
        return "skip", None
    if policy.on_exhaust == "default":
        return "default", cast(bool, policy.default)
    if policy.on_exhaust == "handoff":
        state.agent_response = AgentResponse(
            description="Vou te encaminhar para um atendente da Central 1746."
        )
        return "pause", None
    if policy.on_exhaust == "END":
        mark_flow_finished(state, reset_next=True)
        state.status = "completed"
        log_id = log_event(
            logger,
            logging.WARNING,
            "Flow stopped after confirmation attempts were exhausted",
            operation=node_id,
            log_id_generator=ctx.log_id_generator,
            context={"flow": state.service_name, "slot": slot},
        )
        state.agent_response = AgentResponse(
            description="Não consegui prosseguir. Tente novamente mais tarde.",
            log_id=log_id,
        )
        return "pause", None
    raise ValueError(f"slots.{slot}.on_exhaust has unsupported value {policy.on_exhaust!r}")


def make_summary_confirm_node(ctx: FlowContext, step: dict[str, Any]) -> NodeDesc:
    node_id = step["id"]
    slot = step["confirm"]
    model = ctx.model_for(slot)
    field = (step.get("interactive") or {}).get("field", slot)
    interactive = step.get("interactive")
    prompt = (step.get("prompt") or {}).get("text", "Confirma?")
    skip_when = step.get("skip_when")
    reject_msg = (step.get("on_reject") or {}).get("end", "Tudo bem, não vou prosseguir.")
    policy = _confirmation_policy(ctx, slot, model)

    def ask(state: ServiceState, error: str | None = None) -> AgentResponse:
        interactive_specification = (
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
            interactive=interactive_specification,
        )

    def apply_confirmation(state: ServiceState, confirmation_value: bool) -> ServiceState:
        _store_slot(state, slot, confirmation_value, ctx)
        reset_attempts(state, slot)
        if confirmation_value:
            state.agent_response = None
            return state
        mark_flow_finished(state, reset_next=True)
        state.status = "completed"
        state.agent_response = AgentResponse(description=reject_msg)
        return state

    async def node(state: ServiceState) -> ServiceState:
        try:
            if state.internal.get(_skipped_slot_key(slot)):
                state.agent_response = None
                return state
            skipped_presentations = state.payload.get(
                AUTO_FLOW_SKIPPED_PRESENTATIONS_PAYLOAD_KEY, ()
            )
            if (isinstance(skipped_presentations, list) and node_id in skipped_presentations) or (
                skip_when and evaluate(skip_when, state, ctx.config)
            ):
                _store_slot(state, slot, True, ctx)  # implicitly confirmed (e.g. Flow submission)
                state.agent_response = None
                return state
            if _slot_is_persisted(state, slot, ctx) and _slot_content(state, slot, ctx) is True:
                state.agent_response = None
                return state
            payload_has_slot, raw = _payload_slot_input(state, slot, field)
            if payload_has_slot and raw is not None:
                try:
                    validated = model.model_validate({slot: raw})
                    confirmation_value = cast(bool, getattr(validated, slot))
                except Exception as exc:
                    action, default_value = _confirmation_failure_action(
                        state,
                        slot=slot,
                        node_id=node_id,
                        policy=policy,
                        ctx=ctx,
                    )
                    if action == "reask":
                        state.agent_response = ask(state, str(exc))
                    elif action == "default":
                        apply_confirmation(state, cast(bool, default_value))
                    elif action == "skip":
                        state.agent_response = None
                    return state
                return apply_confirmation(state, confirmation_value)
            state.agent_response = ask(state)
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
    policy = _confirmation_policy(ctx, slot, model)

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
            if state.internal.get(_skipped_slot_key(slot)):
                state.agent_response = None
                return state
            if gate is not None and not evaluate(gate, state, ctx.config):
                state.agent_response = None
                return state
            if _slot_is_persisted(state, slot, ctx):
                state.agent_response = None
                return state
            payload_has_slot, raw = _payload_slot_input(state, slot, field)
            if payload_has_slot and raw is not None:
                try:
                    validated = model.model_validate({slot: raw})
                    _store_slot(state, slot, getattr(validated, slot), ctx)
                    reset_attempts(state, slot)
                    state.agent_response = None
                    return state
                except Exception as exc:
                    action, default_value = _confirmation_failure_action(
                        state,
                        slot=slot,
                        node_id=node_id,
                        policy=policy,
                        ctx=ctx,
                    )
                    if action == "default":
                        _store_slot(state, slot, cast(bool, default_value), ctx)
                        state.agent_response = None
                        return state
                    if action == "skip":
                        state.agent_response = None
                        return state
                    if action == "reask":
                        state.agent_response = ask(state, error=str(exc))
                    return state
            state.agent_response = ask(state)
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    return NodeDesc(
        id=node_id, fn=node, router=lambda s: END if s.agent_response is not None else NEXT
    )


def make_hub_confirm_node(ctx: FlowContext, confirm: dict[str, Any]) -> NodeDesc:
    node_id = confirm["step"]
    slot = confirm["slot"]
    model = ctx.model_for(slot)
    field = (confirm.get("interactive") or {}).get("field", "confirmacao")
    interactive = confirm.get("interactive")
    prompt = (confirm.get("prompt") or {}).get("text", "Confirma os dados?")
    correctable = confirm["correctable"]
    on_confirm = cast(str | None, confirm.get("on_confirm"))
    policy = _confirmation_policy(ctx, slot, model)

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
        payload_schema = model.model_json_schema()
        payload_schema[CORRECTION_TARGETS_SCHEMA_KEY] = copy.deepcopy(correctable)
        return AgentResponse(
            description=description or prompt,
            payload_schema=payload_schema,
            interactive=spec,
        )

    async def node(state: ServiceState) -> ServiceState:
        try:
            unavailable_correction = state.internal.pop(_CORRECTION_UNAVAILABLE_INTERNAL_KEY, None)
            if state.internal.get(_skipped_slot_key(slot)):
                state.agent_response = None
                return state
            if _slot_is_persisted(state, slot, ctx) and _slot_content(state, slot, ctx) is True:
                state.agent_response = None
                return state
            payload = state.payload or {}
            payload_has_slot, raw = _payload_slot_input(state, slot, field)
            correction_target = payload.get("correcao")
            if correction_target is not None:
                if isinstance(correction_target, str) and correction_target in correctable:
                    target = correction_target
                    clear_cascade(state, target, ctx, clear_payload=True)
                    state.internal[CORRECTION_REQUESTED_INTERNAL_KEY] = target
                    _clear_slot(state, slot, clear_payload=True)
                    _clear_payload_slot_input(state, slot, field)
                    state.payload.pop("correcao", None)
                    state.agent_response = None
                    return state
                state.agent_response = ask(
                    state,
                    description="O que você gostaria de corrigir? (" + ", ".join(correctable) + ")",
                )
                return state
            if payload_has_slot and raw is not None:
                try:
                    validated = model.model_validate({slot: raw})
                    confirmation_value = cast(bool, getattr(validated, slot))
                except Exception as exc:
                    action, default_value = _confirmation_failure_action(
                        state,
                        slot=slot,
                        node_id=node_id,
                        policy=policy,
                        ctx=ctx,
                    )
                    if action == "reask":
                        state.agent_response = ask(state)
                        state.agent_response.error_message = str(exc)
                        return state
                    elif action == "default":
                        confirmation_value = cast(bool, default_value)
                    elif action == "skip":
                        state.agent_response = None
                        return state
                    else:
                        return state
                reset_attempts(state, slot)
                if confirmation_value:
                    _store_slot(state, slot, True, ctx)
                    state.agent_response = None
                    return state
                _store_slot(state, slot, False, ctx)
                state.agent_response = ask(
                    state,
                    description="O que você gostaria de corrigir? (" + ", ".join(correctable) + ")",
                )
                return state
            state.agent_response = ask(
                state,
                description=(
                    f"O campo {unavailable_correction!r} não se aplica às respostas atuais. "
                    "Confirme os dados ou escolha outro campo para corrigir."
                    if unavailable_correction is not None
                    else None
                ),
            )
            return state
        except Exception as exc:  # noqa: BLE001
            return _handle_node_error(state, exc, ctx=ctx, operation=node_id)

    def router(state: ServiceState) -> str:
        if state.internal.get(_skipped_slot_key(slot)):
            return on_confirm or NEXT
        if _slot_content(state, slot, ctx) is True:
            return on_confirm or NEXT
        correction_target = state.internal.get(CORRECTION_REQUESTED_INTERNAL_KEY)
        if correction_target and correction_target in ctx.node_for_slot:
            return ctx.node_for_slot[correction_target]
        return END

    targets = ([on_confirm] if on_confirm is not None else []) + [
        ctx.node_for_slot[correctable_slot]
        for correctable_slot in correctable
        if correctable_slot in ctx.node_for_slot
    ]
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
    tool_identifier = ctx.tools.definition(tool).identifier
    operation_namespace = ToolRegistry.operation_namespace(
        ctx.flow_name,
        ctx.flow_revision,
        node_id,
        tool_identifier,
    )

    async def node(state: ServiceState) -> ServiceState:
        try:
            inputs = {p["param"]: _slot_content(state, p["slot"], ctx) for p in input_map}
            result: dict[str, Any]
            if idempotent:
                idempotency_key = ToolRegistry.idempotency_key(
                    state.user_id,
                    operation_namespace,
                    inputs,
                )
                result = await ctx.tools.call_idempotent(
                    tool,
                    idempotency_key,
                    **inputs,
                )
            else:
                result = await ctx.tools.call(tool, **inputs)

            status = result.get("status", "success")
            if status == "success":
                pending_state_writes = {
                    output_key: copy.deepcopy(_dig(result, result_path))
                    for output_key, result_path in outputs.items()
                }
                pending_state_writes.update(copy.deepcopy(success.get("set") or {}))
                state.data.update(pending_state_writes)
                mark_flow_finished(state, reset_next=bool(success.get("reset_next", True)))
                state.status = "completed"
                protocol = state.data.get("protocol_id", "")
                state.agent_response = AgentResponse(
                    description=result.get("message", f"✅ Pronto! Protocolo: {protocol}")
                )
            elif status == "retryable":
                # Preserve by default so the next turn re-fires this node. When
                # disabled, use the same deferred-reset lifecycle as terminal
                # success/fatal outcomes so the current error remains observable.
                if not retryable.get("preserve_state", True):
                    mark_flow_finished(state, reset_next=True)
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
                mark_flow_finished(state, reset_next=bool(fatal.get("reset_next", True)))
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
