"""Native slot collection contracts for required, optional, and exhausted values."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

from flowspec2 import FlowRuntime, ToolRegistry
from flowspec2.models import CORRECTION_REQUESTED_INTERNAL_KEY
from flowspec2.tools import ToolDefinition


def _collection_flow_document(
    *,
    required: bool,
    nullable: bool = False,
    max_attempts: int = 2,
    on_exhaust: str = "reask",
    default: object | None = None,
) -> dict[str, Any]:
    slot_declaration: dict[str, Any] = {
        "domain": "Answer",
        "required": required,
        "nullable": nullable,
        "max_attempts": max_attempts,
        "on_exhaust": on_exhaust,
    }
    if on_exhaust == "default":
        slot_declaration["default"] = default
    return {
        "schema": "flowspec/2",
        "flow": "collection_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise native slot collection."},
        "domains": {"Answer": {"type": "categorical", "values": ["provided"]}},
        "slots": {"answer": slot_declaration},
        "path": [
            {
                "step": "collect_answer",
                "slot": "answer",
                "prompt": {"text": "Provide an answer."},
            }
        ],
    }


async def test_optional_slot_advertises_and_persists_explicit_null_skip() -> None:
    runtime = FlowRuntime(_collection_flow_document(required=False))
    state = await runtime.execute(runtime.new_state("optional-slot"), {})

    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    answer_schema = state.agent_response.payload_schema["properties"]["answer"]
    assert answer_schema["enum"] == ["provided", None]
    assert answer_schema["type"] == ["string", "null"]

    state = await runtime.execute(state, {"answer": None})

    assert state.status == "completed"
    assert "answer" not in state.data
    assert state.internal["_slot_skipped:answer"] is True

    state = await runtime.execute(state, {})

    assert state.status == "completed"
    assert "answer" not in state.data


async def test_required_slot_rejects_null_and_does_not_advertise_it() -> None:
    runtime = FlowRuntime(_collection_flow_document(required=True))
    state = await runtime.execute(runtime.new_state("required-slot"), {})

    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    answer_schema = state.agent_response.payload_schema["properties"]["answer"]
    assert answer_schema["const"] == "provided"
    assert answer_schema["type"] == "string"

    state = await runtime.execute(state, {"answer": None})

    assert state.status == "progress"
    assert state.data["_attempts_answer"] == 1
    assert "answer" not in state.data
    assert state.agent_response is not None
    assert state.agent_response.error_message is not None


async def test_required_nullable_domain_stores_null_as_a_value() -> None:
    runtime = FlowRuntime(
        _collection_flow_document(
            required=True,
            nullable=True,
        )
    )

    state = await runtime.execute(runtime.new_state("required-nullable-slot"), {"answer": None})

    assert state.status == "completed"
    assert "answer" in state.data
    assert state.data["answer"] is None
    assert "_slot_skipped:answer" not in state.internal


def test_optional_nullable_slot_is_rejected_as_an_ambiguous_contract() -> None:
    with pytest.raises(jsonschema.ValidationError):
        FlowRuntime(_collection_flow_document(required=False, nullable=True))


async def test_correction_reopens_an_optional_slot_after_skip() -> None:
    runtime = FlowRuntime(_collection_flow_document(required=False))
    state = await runtime.execute(runtime.new_state("correct-optional-slot"), {"answer": None})
    state.internal[CORRECTION_REQUESTED_INTERNAL_KEY] = "answer"

    state = await runtime.execute(state, {"answer": "provided"})

    assert state.data["answer"] == "provided"
    assert "_slot_skipped:answer" not in state.internal
    assert CORRECTION_REQUESTED_INTERNAL_KEY not in state.internal


async def test_missing_requirement_skips_dependent_collection_by_vacuity() -> None:
    flow_document = _collection_flow_document(required=False)
    flow_document["domains"]["Dependent"] = {
        "type": "categorical",
        "values": ["dependent"],
    }
    flow_document["slots"]["dependent"] = {
        "domain": "Dependent",
        "required": True,
        "requires": ["answer"],
    }
    flow_document["path"].append(
        {
            "step": "collect_dependent",
            "slot": "dependent",
            "prompt": {"text": "Provide the dependent answer."},
        }
    )
    runtime = FlowRuntime(flow_document)

    state = await runtime.execute(runtime.new_state("missing-requirement"), {"answer": None})

    assert state.status == "completed"
    assert "dependent" not in state.data
    assert state.internal["_slot_skipped:dependent"] is True


async def test_skip_exhaustion_advances_without_storing_a_value() -> None:
    runtime = FlowRuntime(
        _collection_flow_document(required=True, max_attempts=1, on_exhaust="skip")
    )

    state = await runtime.execute(runtime.new_state("skip-exhaustion"), {"answer": "invalid"})

    assert state.status == "completed"
    assert "answer" not in state.data
    assert state.internal["_slot_skipped:answer"] is True
    assert "_attempts_answer" not in state.data


async def test_default_exhaustion_stores_the_declared_fallback() -> None:
    runtime = FlowRuntime(
        _collection_flow_document(
            required=True,
            max_attempts=1,
            on_exhaust="default",
            default="provided",
        )
    )

    state = await runtime.execute(runtime.new_state("default-exhaustion"), {"answer": "invalid"})

    assert state.status == "completed"
    assert state.data["answer"] == "provided"
    assert "_slot_skipped:answer" not in state.internal
    assert "_attempts_answer" not in state.data


async def test_default_exhaustion_stores_the_domain_normalized_fallback() -> None:
    runtime = FlowRuntime(
        _collection_flow_document(
            required=True,
            max_attempts=1,
            on_exhaust="default",
            default="PROVIDED",
        )
    )

    state = await runtime.execute(runtime.new_state("normalized-default"), {"answer": "invalid"})

    assert state.data["answer"] == "provided"


def test_default_exhaustion_requires_an_explicit_fallback_in_the_schema() -> None:
    flow_document = _collection_flow_document(
        required=True,
        on_exhaust="default",
        default="provided",
    )
    del flow_document["slots"]["answer"]["default"]

    with pytest.raises(jsonschema.ValidationError):
        FlowRuntime(flow_document)


def test_default_exhaustion_requires_an_explicit_fallback_without_schema_validation() -> None:
    flow_document = _collection_flow_document(
        required=True,
        on_exhaust="default",
        default="provided",
    )
    del flow_document["slots"]["answer"]["default"]

    with pytest.raises(ValueError, match=r"slots\.answer\.default is required"):
        FlowRuntime(flow_document, validate=False)


def test_default_exhaustion_rejects_a_fallback_outside_the_slot_domain() -> None:
    flow_document = _collection_flow_document(
        required=True,
        on_exhaust="default",
        default="invalid",
    )

    with pytest.raises(ValueError, match=r"slots\.answer\.default is invalid"):
        FlowRuntime(flow_document)


def _partition_flow_document(partition: str) -> dict[str, Any]:
    flow_document = _collection_flow_document(required=True)
    flow_document["slots"]["answer"]["persist"] = partition
    flow_document["path"].append({"terminal": True})
    flow_document["terminal"] = {
        "step": "record_answer",
        "tool": "record_answer",
        "idempotent": False,
        "input": [{"param": "answer", "slot": "answer"}],
        "outcomes": {
            "success": {"reset_next": False},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    }
    return flow_document


async def test_internal_slot_persistence_controls_storage_and_terminal_input() -> None:
    partition = "internal"
    recorded_answers: list[str] = []
    registry = ToolRegistry()

    async def record_answer(answer: str) -> dict[str, Any]:
        recorded_answers.append(answer)
        return {"status": "success", "message": "recorded"}

    registry.register(
        "record_answer",
        record_answer,
        definition=ToolDefinition(
            name="record_answer",
            version="1",
            description="Record one collected answer.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"const": "success"},
                    "message": {"type": "string"},
                },
                "required": ["status"],
            },
        ),
    )
    runtime = FlowRuntime(_partition_flow_document(partition), tools=registry)

    state = await runtime.execute(
        runtime.new_state(f"partition-{partition}"), {"answer": "PROVIDED"}
    )

    assert recorded_answers == ["provided"]
    assert "answer" not in state.data
    assert state.internal["answer"] == "provided"


def test_slot_persistence_rejects_ephemeral_payload_partition() -> None:
    with pytest.raises(jsonschema.ValidationError, match="is not one of"):
        FlowRuntime(_partition_flow_document("payload"))


def _confirmation_skip_flow_document(confirmation_kind: str) -> dict[str, Any]:
    confirmation_step: dict[str, Any] = {
        "step": "confirm_answer",
        "confirm": "confirmed",
        "prompt": {"text": "Confirm?"},
    }
    flow_document: dict[str, Any] = {
        "schema": "flowspec/2",
        "flow": "confirmation_skip_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise persistent confirmation skips."},
        "domains": {
            "Confirmation": {"type": "bool"},
            "Later": {"type": "categorical", "values": ["done"]},
        },
        "slots": {
            "confirmed": {
                "domain": "Confirmation",
                "max_attempts": 1,
                "on_exhaust": "skip",
            },
            "later": {"domain": "Later", "required": True},
        },
        "path": [confirmation_step, {"step": "collect_later", "slot": "later"}],
    }
    if confirmation_kind == "summary":
        confirmation_step["on_reject"] = {"end": "Rejected."}
    elif confirmation_kind == "hub":
        flow_document["domains"]["Answer"] = {
            "type": "categorical",
            "values": ["provided"],
        }
        flow_document["slots"]["answer"] = {
            "domain": "Answer",
            "required": True,
        }
        confirmation_step["correctable"] = True
        flow_document["path"].insert(0, {"step": "collect_answer", "slot": "answer"})
        flow_document["confirm"] = {
            "step": "confirm_answer",
            "slot": "confirmed",
            "correctable": ["answer"],
        }
    return flow_document


@pytest.mark.parametrize("confirmation_kind", ["bool", "summary", "hub"])
async def test_confirmation_skip_remains_satisfied_across_later_turns(
    confirmation_kind: str,
) -> None:
    runtime = FlowRuntime(_confirmation_skip_flow_document(confirmation_kind))
    state = await runtime.execute(
        runtime.new_state(f"confirmation-skip-{confirmation_kind}"),
        {"answer": "provided"} if confirmation_kind == "hub" else {},
    )
    confirmation_field = "confirmacao" if confirmation_kind == "hub" else "confirmed"

    state = await runtime.execute(state, {confirmation_field: "invalid"})

    assert state.agent_response is not None
    assert "later" in (state.agent_response.payload_schema or {}).get("properties", {})
    assert state.internal["_slot_skipped:confirmed"] is True

    state = await runtime.execute(state, {"later": "done"})

    assert state.status == "completed"
    assert state.data["later"] == "done"
    assert "confirmed" not in state.data
    assert state.internal["_slot_skipped:confirmed"] is True


def _fill_only_when_asked_flow_document() -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "fill_only_when_asked_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise prompt-bound slot collection."},
        "domains": {
            "FirstAnswer": {"type": "categorical", "values": ["first"]},
            "SensitiveAnswer": {"type": "categorical", "values": ["sensitive"]},
        },
        "slots": {
            "first_answer": {"domain": "FirstAnswer", "required": True},
            "sensitive_answer": {
                "domain": "SensitiveAnswer",
                "required": True,
                "fill_only_when_asked": True,
                "prefill_sources": ["trusted_form"],
            },
        },
        "path": [
            {
                "step": "collect_first_answer",
                "slot": "first_answer",
                "prompt": {"text": "Provide the first answer."},
            },
            {
                "step": "collect_sensitive_answer",
                "slot": "sensitive_answer",
                "prompt": {"text": "Provide the sensitive answer."},
            },
        ],
    }


async def test_fill_only_when_asked_rejects_a_value_from_another_slots_turn() -> None:
    runtime = FlowRuntime(_fill_only_when_asked_flow_document())
    state = await runtime.execute(runtime.new_state("prompt-bound"), {})

    state = await runtime.execute(
        state,
        {"first_answer": "first", "sensitive_answer": "sensitive"},
    )

    assert state.status == "progress"
    assert state.data["first_answer"] == "first"
    assert "sensitive_answer" not in state.data
    assert state.agent_response is not None
    assert state.agent_response.description == "Provide the sensitive answer."

    state = await runtime.execute(state, {"sensitive_answer": "sensitive"})

    assert state.status == "completed"
    assert state.data["sensitive_answer"] == "sensitive"


async def test_fill_only_when_asked_accepts_an_authorized_prefill_source() -> None:
    flow_document = _fill_only_when_asked_flow_document()
    flow_document["path"] = [flow_document["path"][1]]
    del flow_document["slots"]["first_answer"]
    del flow_document["domains"]["FirstAnswer"]
    runtime = FlowRuntime(flow_document)

    state = await runtime.execute(
        runtime.new_state("trusted-prefill"),
        {"_source": "trusted_form", "sensitive_answer": "sensitive"},
    )

    assert state.status == "completed"
    assert state.data["sensitive_answer"] == "sensitive"
