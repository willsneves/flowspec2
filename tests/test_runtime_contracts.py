"""Regression coverage for declared domain and runtime lifecycle contracts."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from flowspec2 import (
    FlowLinkError,
    FlowRuntime,
    ToolDefinition,
    ToolEffects,
    ToolRegistry,
)
from flowspec2.domains import make_slot_model


def _coerce_domain_value(domain_specification: dict[str, Any], raw_value: Any) -> Any:
    domain_registry = {"ContractDomain": domain_specification}
    slot_model = make_slot_model("contract_value", "ContractDomain", domain_registry)
    validated_domain = slot_model.model_validate({"contract_value": raw_value})
    return validated_domain.model_dump()["contract_value"]


def test_runtime_rejects_semantically_unlinked_source(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["path"][0]["slot"] = "missing_slot"

    with pytest.raises(FlowLinkError) as error:
        FlowRuntime(invalid_flow)

    assert "FLOWSPEC_SEMANTIC_UNKNOWN_NATIVE_PATH_SLOT" in {
        diagnostic.code for diagnostic in error.value.diagnostics
    }


def test_categorical_accent_fold_normalizes_values_aliases_and_input_when_enabled() -> None:
    domain_specification = {
        "type": "categorical",
        "values": ["Ação"],
        "normalize": {
            "accent_fold": True,
            "synonyms": {"resolução": "Ação"},
        },
    }

    assert _coerce_domain_value(domain_specification, "acao") == "Ação"
    assert _coerce_domain_value(domain_specification, "RESOLUCAO") == "Ação"


def test_categorical_accent_fold_preserves_accents_when_disabled() -> None:
    domain_specification = {
        "type": "categorical",
        "values": ["Ação"],
        "normalize": {
            "accent_fold": False,
            "synonyms": {"resolução": "Ação"},
        },
    }

    assert _coerce_domain_value(domain_specification, " AÇÃO ") == "Ação"
    assert _coerce_domain_value(domain_specification, "RESOLUÇÃO") == "Ação"
    with pytest.raises(ValidationError):
        _coerce_domain_value(domain_specification, "acao")
    with pytest.raises(ValidationError):
        _coerce_domain_value(domain_specification, "resolucao")


def test_categorical_accent_fold_defaults_to_disabled() -> None:
    domain_specification = {
        "type": "categorical",
        "values": ["Ação"],
        "normalize": {},
    }

    with pytest.raises(ValidationError):
        _coerce_domain_value(domain_specification, "acao")


@pytest.mark.parametrize(
    ("emoji_veto", "expected_value"),
    [
        (True, False),
        (False, True),
    ],
)
def test_boolean_emoji_veto_controls_mixed_affirmation(
    emoji_veto: bool,
    expected_value: bool,
) -> None:
    domain_specification = {
        "type": "bool",
        "normalize": {
            "affirmation": True,
            "emoji_veto": emoji_veto,
            "synonyms": {"sim 👎": True},
        },
    }

    assert _coerce_domain_value(domain_specification, "sim 👎") is expected_value


@pytest.mark.parametrize(
    ("emoji_veto", "raw_value", "expected_value"),
    [
        (True, "👍", True),
        (False, "👍", True),
        (True, "👎", False),
        (False, "👎", False),
        (False, "não 👍", False),
    ],
)
def test_boolean_affirmation_remains_deterministic_without_emoji_veto(
    emoji_veto: bool,
    raw_value: str,
    expected_value: bool,
) -> None:
    domain_specification = {
        "type": "bool",
        "normalize": {"affirmation": True, "emoji_veto": emoji_veto},
    }

    assert _coerce_domain_value(domain_specification, raw_value) is expected_value


def test_boolean_emoji_veto_defaults_to_disabled() -> None:
    domain_specification = {
        "type": "bool",
        "normalize": {"affirmation": True},
    }

    assert _coerce_domain_value(domain_specification, "sim 👎") is True


def _runtime_contract_document(
    *,
    preserve_retryable_state: bool = True,
    never_saved_empty_payload: str = "reset",
    in_progress_empty_payload: str = "ignore",
) -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "runtime_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise runtime lifecycle contracts."},
        "domains": {
            "FirstAnswer": {"type": "categorical", "values": ["first"]},
            "SecondAnswer": {"type": "categorical", "values": ["second"]},
        },
        "slots": {
            "first_answer": {"domain": "FirstAnswer", "required": True},
            "second_answer": {"domain": "SecondAnswer", "required": True},
        },
        "path": [
            {
                "step": "collect_first_answer",
                "slot": "first_answer",
                "prompt": {"text": "Provide the first answer."},
            },
            {
                "step": "collect_second_answer",
                "slot": "second_answer",
                "prompt": {"text": "Provide the second answer."},
            },
            {"terminal": True},
        ],
        "terminal": {
            "step": "record_answers",
            "tool": "record_answers",
            "idempotent": False,
            "input": [
                {"param": "first_answer", "slot": "first_answer"},
                {"param": "second_answer", "slot": "second_answer"},
            ],
            "outcomes": {
                "success": {"reset_next": False},
                "retryable": {"preserve_state": preserve_retryable_state},
                "fatal": {"reset_next": True},
            },
            "empty_payload": {
                "never_saved": never_saved_empty_payload,
                "in_progress": in_progress_empty_payload,
            },
        },
    }


@pytest.mark.parametrize(
    ("previously_saved", "empty_payload_action", "expected_prompt_slot"),
    [
        (False, "reset", "first_answer"),
        (False, "ignore", "second_answer"),
        (True, "reset", "first_answer"),
        (True, "ignore", "second_answer"),
    ],
)
async def test_empty_payload_policy_controls_reset_for_each_lifecycle_stage(
    previously_saved: bool,
    empty_payload_action: str,
    expected_prompt_slot: str,
) -> None:
    flow_document = _runtime_contract_document(
        never_saved_empty_payload=(empty_payload_action if not previously_saved else "reset"),
        in_progress_empty_payload=(empty_payload_action if previously_saved else "ignore"),
    )
    registry, _ = _retrying_tool_registry()
    runtime = FlowRuntime(flow_document, tools=registry)
    state = runtime.new_state("empty-payload", data={"first_answer": "first"})
    state.metadata.saved = previously_saved
    state.internal["preserved_marker"] = True

    state = await runtime.execute(state, {})

    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    assert expected_prompt_slot in state.agent_response.payload_schema["properties"]
    if empty_payload_action == "reset":
        assert "first_answer" not in state.data
        assert "preserved_marker" not in state.internal
    else:
        assert state.data["first_answer"] == "first"
        assert state.internal["preserved_marker"] is True


def _retrying_tool_registry() -> tuple[ToolRegistry, list[str]]:
    terminal_statuses = ["retryable", "success"]
    observed_statuses: list[str] = []
    registry = ToolRegistry()

    async def record_answers(first_answer: str, second_answer: str) -> dict[str, Any]:
        assert first_answer == "first"
        assert second_answer == "second"
        terminal_status = terminal_statuses[len(observed_statuses)]
        observed_statuses.append(terminal_status)
        if terminal_status == "retryable":
            return {"status": terminal_status, "error": "temporarily unavailable"}
        return {"status": terminal_status, "message": "recorded"}

    registry.register(
        "record_answers",
        record_answers,
        definition=ToolDefinition(
            name="record_answers",
            version="1",
            description="Record two answers under the retry lifecycle contract.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "first_answer": {"type": "string", "const": "first"},
                    "second_answer": {"type": "string", "const": "second"},
                },
                "required": ["first_answer", "second_answer"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"enum": ["success", "retryable"]},
                    "message": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["status"],
            },
            effects=ToolEffects(idempotent=True),
        ),
    )
    return registry, observed_statuses


async def test_retryable_outcome_preserves_state_and_retries_when_enabled() -> None:
    registry, observed_statuses = _retrying_tool_registry()
    runtime = FlowRuntime(
        _runtime_contract_document(preserve_retryable_state=True),
        tools=registry,
    )
    state = runtime.new_state(
        "preserved-retry",
        data={"first_answer": "first", "second_answer": "second"},
    )
    state.metadata.saved = True

    state = await runtime.execute(state, {"retry_request": True})

    assert state.status == "error"
    assert "_reset_on_next_call" not in state.data
    assert state.data["first_answer"] == "first"
    assert state.data["second_answer"] == "second"

    state = await runtime.execute(state, {})

    assert state.status == "completed"
    assert observed_statuses == ["retryable", "success"]


async def test_retryable_outcome_resets_before_retry_when_preservation_is_disabled() -> None:
    registry, observed_statuses = _retrying_tool_registry()
    runtime = FlowRuntime(
        _runtime_contract_document(preserve_retryable_state=False),
        tools=registry,
    )
    state = runtime.new_state(
        "reset-retry",
        data={"first_answer": "first", "second_answer": "second"},
    )
    state.metadata.saved = True

    state = await runtime.execute(state, {"retry_request": True})

    assert state.status == "error"
    assert state.data["_reset_on_next_call"] is True

    state = await runtime.execute(state, {})

    assert state.status == "progress"
    assert "first_answer" not in state.data
    assert "second_answer" not in state.data
    assert observed_statuses == ["retryable"]
    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    assert "first_answer" in state.agent_response.payload_schema["properties"]


def test_auto_flow_resume_at_rejects_unsatisfied_prior_summary(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["path"][0].pop("skip_when")

    with pytest.raises(ValueError, match="cannot bypass confirmation"):
        FlowRuntime(flow_document)


async def test_auto_flow_submission_accepts_same_name_slot_without_alias(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["auto_flow"]["resume_at"] = "collect_quantidade"
    runtime = FlowRuntime(flow_document)
    state = await runtime.execute(runtime.new_state("auto-flow-identity"), {})

    state = await runtime.execute(
        state,
        {
            "_source": "whatsapp_flow",
            "luminaria_defeito": "Apagada",
        },
    )

    assert state.status == "progress"
    assert state.data["luminaria_defeito"] == "Apagada"
    assert state.agent_response is not None
    assert "luminaria_quantidade" in (state.agent_response.payload_schema or {}).get(
        "properties", {}
    )


async def test_auto_flow_send_when_reads_the_current_turn_payload(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["auto_flow"]["send_when"] = {"eq": ["payload.channel", "whatsapp"]}
    runtime = FlowRuntime(flow_document)

    state = await runtime.execute(
        runtime.new_state("auto-flow-current-payload"),
        {"channel": "whatsapp"},
    )

    assert state.agent_response is not None
    assert state.agent_response.interactive is not None
    assert state.agent_response.interactive["status"] == "flow_sent"
    assert state.payload == {}


async def test_auto_flow_waits_without_resending_until_submission(
    luminaria_doc: dict[str, Any],
) -> None:
    runtime = FlowRuntime(luminaria_doc)
    state = await runtime.execute(runtime.new_state("auto-flow-single-send"), {})
    assert state.agent_response is not None
    assert state.agent_response.interactive is not None
    first_flow_token = state.agent_response.interactive["flow_token"]

    waiting_state = await runtime.execute(state, {"message": "ainda estou preenchendo"})

    assert waiting_state.status == "progress"
    assert waiting_state.agent_response is not None
    assert waiting_state.agent_response.interactive is None
    assert first_flow_token not in waiting_state.agent_response.description
    assert waiting_state.payload == {}


async def test_auto_flow_recovery_bounds_resends_and_falls_back(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["auto_flow"]["recovery"].update({"max_resends": 1, "timeout_seconds": 60})
    timestamp = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    runtime = FlowRuntime(flow_document, clock=lambda: timestamp)
    state = await runtime.execute(runtime.new_state("auto-flow-resend-bound"), {})
    assert state.agent_response is not None
    first_interactive = state.agent_response.interactive
    assert first_interactive is not None
    first_token = first_interactive["flow_token"]
    assert first_interactive["recovery"]["remaining_resends"] == 1

    state = await runtime.execute(state, {"_auto_flow_event": "resend"})
    assert state.agent_response is not None
    resent_interactive = state.agent_response.interactive
    assert resent_interactive is not None
    assert resent_interactive["flow_token"] == first_token
    assert resent_interactive["recovery"]["remaining_resends"] == 0

    state = await runtime.execute(state, {"_auto_flow_event": "resend"})

    assert state.status == "progress"
    assert state.agent_response is not None
    assert (state.agent_response.interactive or {}).get("status") != "flow_sent"
    assert "service_confirmed" in (state.agent_response.payload_schema or {}).get("properties", {})


async def test_auto_flow_applies_declared_timeout_on_the_next_turn(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["auto_flow"]["recovery"]["timeout_seconds"] = 30
    current_timestamp = [datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)]
    runtime = FlowRuntime(flow_document, clock=lambda: current_timestamp[0])
    state = await runtime.execute(runtime.new_state("auto-flow-timeout"), {})
    assert state.agent_response is not None
    assert state.agent_response.interactive is not None
    deadline = state.agent_response.interactive["recovery"]["deadline"]
    assert deadline == (current_timestamp[0] + timedelta(seconds=30)).isoformat()

    current_timestamp[0] += timedelta(seconds=31)
    state = await runtime.execute(state, {"message": "ainda preenchendo"})

    assert state.status == "progress"
    assert state.agent_response is not None
    assert "service_confirmed" in (state.agent_response.payload_schema or {}).get("properties", {})


async def test_auto_flow_cancel_event_uses_declared_end_policy(
    luminaria_doc: dict[str, Any],
) -> None:
    runtime = FlowRuntime(luminaria_doc)
    state = await runtime.execute(runtime.new_state("auto-flow-cancel"), {})

    state = await runtime.execute(state, {"_auto_flow_event": "cancel"})

    assert state.status == "completed"
    assert state.agent_response is not None
    assert "cancelado" in state.agent_response.description


def test_auto_flow_requires_a_closed_recovery_policy(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    del flow_document["auto_flow"]["recovery"]

    with pytest.raises(JsonSchemaValidationError):
        FlowRuntime(flow_document)


def test_auto_flow_resume_at_rejects_an_unknown_native_path_step(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["auto_flow"]["resume_at"] = "missing_step"

    with pytest.raises(ValueError, match=r"auto_flow\.resume_at"):
        FlowRuntime(flow_document)


def test_auto_flow_prefill_rejects_internal_slots(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["slots"]["private_prefill"] = {
        "domain": "PontoReferencia",
        "required": False,
        "persist": "internal",
    }
    flow_document["auto_flow"]["prefill_from"].append("private_prefill")

    with pytest.raises(ValueError, match=r"cannot expose internal slot 'private_prefill'"):
        FlowRuntime(flow_document)
