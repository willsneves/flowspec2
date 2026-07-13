"""Generic await_external compilation, suspension, mapping, and recovery."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from conftest import require_agent_response
from jsonschema import ValidationError as JsonSchemaValidationError

from flowspec2 import (
    FlowLinkError,
    FlowRuntime,
    ToolRegistry,
    default_tool_registry,
    validate_flow,
)
from flowspec2.nodes import DEFAULT_AWAIT_EXTERNAL_TIMEOUT_SECONDS
from flowspec2.tools import Tool, ToolDefinition, ToolEffects


def _await_flow() -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "external_payment",
        "version": "1.0.0",
        "route": {"description": "Confirm an external payment."},
        "domains": {"Reference": {"type": "free_text"}},
        "slots": {"fallback_reference": {"domain": "Reference"}},
        "path": [
            {
                "step": "await_payment",
                "await_external": True,
                "prompt": {"text": "Complete o pagamento."},
                "interactive": {
                    "kind": "cta_url",
                    "field": "payment_token",
                    "out_of_band": True,
                },
            },
            {
                "step": "collect_fallback_reference",
                "slot": "fallback_reference",
                "prompt": {"text": "Informe a referência alternativa."},
            },
        ],
        "capabilities": {
            "await_external": {
                "kind": "cta_url",
                "step": "await_payment",
                "resume_on": "payment_token",
                "resume": {
                    "version": "1",
                    "schema": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"id": {"type": "string", "minLength": 1}},
                        "required": ["id"],
                    },
                    "correlation": "$token.id",
                    "duplicate": "ignore",
                    "late": "reject",
                },
                "on_resume": {
                    "set": {
                        "payment_id": "$token.id",
                        "payment_source": "external",
                        "payment_confirmed": True,
                    },
                    "enrich": {
                        "tool": "payment_lookup",
                        "input": {
                            "payment_id": "$token.id",
                            "attempt": 1,
                        },
                        "set": {"receipt_code": "$result.receipts.0.code"},
                    },
                },
                "timeout": {
                    "goto": "collect_fallback_reference",
                    "set": {"payment_timed_out": True},
                },
                "recovery": {
                    "abort": {"goto": "END", "set": {"payment_aborted": True}},
                    "resend": {"goto": "await_payment"},
                    "switch": {
                        "goto": "collect_fallback_reference",
                        "set": {"payment_switched": True},
                    },
                },
            }
        },
    }


def _payment_lookup_definition() -> ToolDefinition:
    return ToolDefinition(
        name="payment_lookup",
        version="1",
        description="Load a receipt for one external payment.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "payment_id": {"type": "string"},
                "attempt": {"type": "integer"},
            },
            "required": ["payment_id", "attempt"],
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "receipts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    },
                }
            },
            "required": ["receipts"],
        },
        effects=ToolEffects(read_only=True, idempotent=True),
    )


def _payment_registry(*, fails: bool = False) -> tuple[ToolRegistry, list[dict[str, Any]]]:
    registry = default_tool_registry()
    calls: list[dict[str, Any]] = []

    async def payment_lookup(**tool_inputs: Any) -> dict[str, Any]:
        calls.append(tool_inputs)
        if fails:
            raise RuntimeError("payment backend unavailable")
        return {"receipts": [{"code": "REC-42"}]}

    registry.register(
        "payment_lookup",
        payment_lookup,
        definition=_payment_lookup_definition(),
    )
    return registry, calls


async def test_explicit_path_wait_maps_token_and_enrichment_result_atomically():
    flow_document = _await_flow()
    validate_flow(flow_document)
    registry, calls = _payment_registry()
    runtime = FlowRuntime(flow_document, tools=registry)
    state = runtime.new_state("payment-user")

    state = await runtime.execute(state, {"start": True})
    interactive = require_agent_response(state).interactive or {}
    assert interactive["out_of_band_sent"] is True
    assert interactive["next_step"] == "await_payment"
    assert interactive["field"] == "payment_token"

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]
    assert state.data["payment_id"] == "PAY-7"
    assert state.data["payment_source"] == "external"
    assert state.data["payment_confirmed"] is True
    assert state.data["receipt_code"] == "REC-42"
    assert "referência alternativa" in require_agent_response(state).description

    state = await runtime.execute(state, {"fallback_reference": "manual-8"})
    assert state.status == "completed"
    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]


async def test_wait_marker_and_state_provenance_pin_the_resume_contract():
    registry, _ = _payment_registry()
    runtime = FlowRuntime(_await_flow(), tools=registry)

    state = await runtime.execute(runtime.new_state("resume-provenance"), {"start": True})

    marker_contract = (require_agent_response(state).interactive or {})["resume_contract"]
    provenance = state.metadata.await_resume
    assert provenance is not None
    assert provenance.deadline is not None
    assert marker_contract == {
        "version": "1",
        "digest": provenance.digest,
        "correlation": "$token.id",
        "duplicate": "ignore",
        "late": "reject",
        "deadline": provenance.deadline.isoformat(),
    }
    assert provenance.step == "await_payment"
    assert provenance.version == "1"
    assert provenance.correlation_path == "$token.id"
    assert provenance.correlation_value is None

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert state.metadata.await_resume is not None
    assert state.metadata.await_resume.correlation_value == "PAY-7"


async def test_resume_token_schema_and_formats_are_enforced_before_mapping_or_tools():
    flow_document = _await_flow()
    token_schema = flow_document["capabilities"]["await_external"]["resume"]["schema"]
    token_schema["properties"]["email"] = {"type": "string", "format": "email"}
    token_schema["required"].append("email")
    registry, calls = _payment_registry()
    runtime = FlowRuntime(flow_document, tools=registry)
    state = await runtime.execute(runtime.new_state("typed-token"), {"start": True})

    state = await runtime.execute(
        state,
        {"payment_token": {"id": "PAY-7", "email": "not-an-email"}},
    )

    assert state.status == "error"
    assert calls == []
    assert "does not satisfy contract" in (require_agent_response(state).error_message or "")
    assert "payment_id" not in state.data


@pytest.mark.parametrize(
    ("schema_mutation", "message"),
    [
        (
            lambda schema: schema["properties"].update(
                {"id": {"$ref": "https://example.invalid/payment-id"}}
            ),
            "only supports local \\$ref",
        ),
        (
            lambda schema: schema["properties"].update({"id": {"$dynamicRef": "#payment-id"}}),
            "does not support dynamic schema references",
        ),
        (
            lambda schema: schema["properties"].update({"id": {"$id": "nested", "type": "string"}}),
            "does not support nested \\$id",
        ),
        (
            lambda schema: schema.update(
                {
                    "$defs": {"id": {"$ref": "#/$defs/id"}},
                    "properties": {"id": {"$ref": "#/$defs/id"}},
                }
            ),
            "does not support cyclic local references",
        ),
        (
            lambda schema: schema["properties"].update(
                {
                    "id": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    }
                }
            ),
            "must close every declared object",
        ),
    ],
)
def test_compiler_rejects_unsafe_or_open_resume_schemas(schema_mutation, message):
    flow_document = _await_flow()
    token_schema = flow_document["capabilities"]["await_external"]["resume"]["schema"]
    schema_mutation(token_schema)
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match=message):
        FlowRuntime(flow_document, tools=registry)


def test_compiler_accepts_closed_acyclic_local_resume_schema_references():
    flow_document = _await_flow()
    token_schema = flow_document["capabilities"]["await_external"]["resume"]["schema"]
    token_schema["$defs"] = {"payment_id": {"type": "string", "minLength": 1}}
    token_schema["properties"]["id"] = {"$ref": "#/$defs/payment_id"}
    registry, _ = _payment_registry()

    FlowRuntime(flow_document, tools=registry)


def test_compiler_requires_correlation_path_in_every_token():
    flow_document = _await_flow()
    flow_document["capabilities"]["await_external"]["resume"]["schema"]["required"] = []
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match="must be required by every resume.schema alternative"):
        FlowRuntime(flow_document, tools=registry)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda flow: flow["capabilities"]["await_external"]["on_resume"]["set"].update(
                {"payment_id": "$token.missing"}
            ),
            "does not resolve exactly in resume.schema",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["on_resume"]["enrich"][
                "input"
            ].update({"attempt": "$token.id"}),
            "not proven to satisfy tool parameter 'attempt'",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["on_resume"]["enrich"][
                "input"
            ].update({"attempt": "one"}),
            "literal does not satisfy tool parameter 'attempt'",
        ),
    ],
)
def test_compiler_checks_resume_paths_and_tool_input_types(mutate, message):
    flow_document = _await_flow()
    mutate(flow_document)
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match=message):
        FlowRuntime(flow_document, tools=registry)


def test_compiler_checks_resume_literal_against_declared_state_schema():
    flow_document = _await_flow()
    flow_document["domains"]["Confirmation"] = {"type": "bool"}
    flow_document["slots"]["payment_confirmed"] = {"domain": "Confirmation"}
    flow_document["capabilities"]["await_external"]["on_resume"]["set"]["payment_confirmed"] = (
        "confirmed"
    )
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match="literal does not satisfy slot 'payment_confirmed'"):
        FlowRuntime(flow_document, tools=registry)


def test_compiler_checks_enrichment_result_against_declared_state_schema():
    flow_document = _await_flow()
    flow_document["domains"]["ReceiptNumber"] = {"type": "integer"}
    flow_document["slots"]["receipt_code"] = {"domain": "ReceiptNumber"}
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match="tool result path.*not proven to satisfy"):
        FlowRuntime(flow_document, tools=registry)


async def test_required_enrichment_failure_commits_none_of_the_pending_writes():
    registry, calls = _payment_registry(fails=True)
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("payment-user"), {"start": True})

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]
    assert state.status == "error"
    assert "payment backend unavailable" in (require_agent_response(state).error_message or "")
    assert require_agent_response(state).interactive is None
    assert "payment_id" not in state.data
    assert "payment_source" not in state.data
    assert "payment_confirmed" not in state.data


async def test_required_enrichment_can_recover_on_the_next_token_turn():
    registry = default_tool_registry()
    attempts = 0

    async def flaky_payment_lookup(**_: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary payment failure")
        return {"receipts": [{"code": "REC-RECOVERED"}]}

    registry.register(
        "payment_lookup",
        flaky_payment_lookup,
        definition=_payment_lookup_definition(),
    )
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("payment-user"), {"start": True})
    payment_token = {"payment_token": {"id": "PAY-7"}}

    state = await runtime.execute(state, payment_token)
    assert state.status == "error"
    assert "payment_id" not in state.data

    state = await runtime.execute(state, payment_token)

    assert attempts == 2
    assert state.status == "progress"
    assert state.data["payment_id"] == "PAY-7"
    assert state.data["receipt_code"] == "REC-RECOVERED"
    assert "referência alternativa" in require_agent_response(state).description


async def test_optional_enrichment_failure_is_logged_and_keeps_token_writes(caplog):
    flow_document = _await_flow()
    flow_document["capabilities"]["await_external"]["on_resume"]["enrich"]["optional"] = True
    registry, _ = _payment_registry(fails=True)
    runtime = FlowRuntime(flow_document, tools=registry)
    state = await runtime.execute(runtime.new_state("payment-user"), {"start": True})

    with caplog.at_level(logging.WARNING, logger="flowspec2.nodes"):
        state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert state.status == "progress"
    assert state.data["payment_id"] == "PAY-7"
    assert "receipt_code" not in state.data
    assert any(
        record.message == "Optional await_external enrichment failed"
        and getattr(record, "tool", None) == "payment_lookup"
        and str(getattr(record, "log_id", "")).isdigit()
        for record in caplog.records
    )


async def test_duplicate_resume_with_ignore_policy_does_not_repeat_enrichment():
    registry, calls = _payment_registry()
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("duplicate-ignore"), {"start": True})
    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert state.status == "progress"
    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]
    assert state.data["payment_id"] == "PAY-7"
    assert "referência alternativa" in require_agent_response(state).description


async def test_duplicate_resume_with_reject_policy_is_atomic():
    flow_document = _await_flow()
    flow_document["capabilities"]["await_external"]["resume"]["duplicate"] = "reject"
    registry, calls = _payment_registry()
    runtime = FlowRuntime(flow_document, tools=registry)
    state = await runtime.execute(runtime.new_state("duplicate-reject"), {"start": True})
    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})
    accepted_data = copy.deepcopy(state.data)

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert state.status == "error"
    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]
    assert state.data == accepted_data
    assert "rejected duplicate resume delivery" in (
        require_agent_response(state).error_message or ""
    )


@pytest.mark.parametrize(
    ("late_policy", "expected_status"),
    [("ignore", "progress"), ("reject", "error")],
)
async def test_different_correlation_uses_declared_late_policy_atomically(
    late_policy: str,
    expected_status: str,
):
    flow_document = _await_flow()
    flow_document["capabilities"]["await_external"]["resume"]["late"] = late_policy
    registry, calls = _payment_registry()
    runtime = FlowRuntime(flow_document, tools=registry)
    state = await runtime.execute(runtime.new_state(f"late-{late_policy}"), {"start": True})
    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})
    accepted_data = copy.deepcopy(state.data)

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-8"}})

    assert state.status == expected_status
    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]
    assert state.data == accepted_data
    if late_policy == "reject":
        assert "rejected late resume delivery" in (
            require_agent_response(state).error_message or ""
        )
    else:
        assert "referência alternativa" in require_agent_response(state).description


async def test_completed_lifecycle_classifies_duplicate_before_reset():
    registry, calls = _payment_registry()
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("completed-duplicate"), {"start": True})
    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})
    state.data["_reset_on_next_call"] = True
    state.status = "completed"
    previous_description = require_agent_response(state).description

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert state.status == "completed"
    assert calls == [{"payment_id": "PAY-7", "attempt": 1}]
    assert state.data["payment_id"] == "PAY-7"
    assert require_agent_response(state).description == previous_description

    state = await runtime.execute(state, {"start": True})

    assert state.metadata.await_resume is not None
    assert state.metadata.await_resume.correlation_value is None


async def test_restored_resume_provenance_must_match_runtime_contract():
    registry, _ = _payment_registry()
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("tampered-resume"), {"start": True})
    assert state.metadata.await_resume is not None
    state.metadata.await_resume.digest = "0" * 64

    state = await runtime.execute(state, {"payment_token": {"id": "PAY-7"}})

    assert state.status == "error"
    assert "await_resume.digest" in (require_agent_response(state).error_message or "")


async def test_timeout_requires_a_host_event_and_resend_reemits_the_marker():
    registry, _ = _payment_registry()
    current_time = [datetime(2026, 7, 13, 12, tzinfo=timezone.utc)]
    runtime = FlowRuntime(_await_flow(), tools=registry, clock=lambda: current_time[0])
    state = await runtime.execute(runtime.new_state("payment-user"), {"start": True})
    initial_deadline = current_time[0] + timedelta(seconds=DEFAULT_AWAIT_EXTERNAL_TIMEOUT_SECONDS)
    assert state.metadata.await_resume is not None
    assert state.metadata.await_resume.deadline == initial_deadline

    state = await runtime.execute(state, {"message": "ainda estou pagando"})
    assert "payment_timed_out" not in state.data
    assert require_agent_response(state).interactive is None
    assert "Complete o pagamento" in require_agent_response(state).description

    state = await runtime.execute(state, {"_external_event": "resend"})
    assert (require_agent_response(state).interactive or {})["out_of_band_sent"] is True

    current_time[0] = initial_deadline
    state = await runtime.execute(state, {"_external_event": "timeout"})
    assert state.data["payment_timed_out"] is True
    assert "referência alternativa" in require_agent_response(state).description


async def test_timeout_before_persisted_deadline_is_rejected_atomically():
    registry, _ = _payment_registry()
    current_time = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    runtime = FlowRuntime(_await_flow(), tools=registry, clock=lambda: current_time)
    state = await runtime.execute(runtime.new_state("early-timeout"), {"start": True})

    state = await runtime.execute(state, {"_external_event": "timeout"})

    assert state.status == "error"
    assert "payment_timed_out" not in state.data
    assert "before its deadline" in (require_agent_response(state).error_message or "")


def test_timeout_duration_is_materialized_and_cannot_exist_without_transition():
    runtime = FlowRuntime(_await_flow(), tools=_payment_registry()[0])

    assert (
        runtime.doc["capabilities"]["await_external"]["timeout_seconds"]
        == DEFAULT_AWAIT_EXTERNAL_TIMEOUT_SECONDS
    )

    invalid_flow = _await_flow()
    invalid_flow["capabilities"]["await_external"].pop("timeout")
    invalid_flow["capabilities"]["await_external"]["timeout_seconds"] = 1
    with pytest.raises(JsonSchemaValidationError):
        validate_flow(invalid_flow)


@pytest.mark.parametrize(
    ("event", "expected_key", "expected_status"),
    [
        ("switch", "payment_switched", "progress"),
        ("abort", "payment_aborted", "completed"),
    ],
)
async def test_configured_recovery_events_route_atomically(
    event: str,
    expected_key: str,
    expected_status: str,
):
    registry, _ = _payment_registry()
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("payment-user"), {"start": True})

    state = await runtime.execute(state, {"_external_event": event})

    assert state.data[expected_key] is True
    assert state.status == expected_status
    if event == "switch":
        assert "referência alternativa" in require_agent_response(state).description
    else:
        assert require_agent_response(state).description == "A ação externa foi cancelada."


async def test_end_recovery_resets_the_flow_on_the_next_call():
    registry, _ = _payment_registry()
    runtime = FlowRuntime(_await_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("payment-user"), {"start": True})

    state = await runtime.execute(state, {"_external_event": "abort"})
    assert state.data["payment_aborted"] is True
    assert state.data["_reset_on_next_call"] is True

    state = await runtime.execute(state, {"start": True})

    assert "payment_aborted" not in state.data
    assert (require_agent_response(state).interactive or {})["out_of_band_sent"] is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda flow: flow["capabilities"]["await_external"].update({"step": "different_wait"}),
            "conflicts",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["on_resume"]["set"].update(
                {"bad": "$slots.payment_id"}
            ),
            r"must use \$token\.path",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["timeout"].update(
                {"goto": "unknown_step"}
            ),
            "does not resolve to a node",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["on_resume"]["enrich"].update(
                {"tool": "unknown_tool"}
            ),
            "not registered",
        ),
        (
            lambda flow: flow["path"][0]["interactive"].update({"field": "different_token"}),
            "interactive.field must match resume_on",
        ),
        (
            lambda flow: flow["path"][0]["interactive"].update({"next_step": "different_wait"}),
            "interactive.next_step must match step",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["on_resume"].update(
                {"enrich": "payment_lookup"}
            ),
            "requires a versioned typed resume contract and object-form enrichment",
        ),
        (
            lambda flow: flow["capabilities"]["await_external"]["recovery"]["resend"].update(
                {"goto": "collect_fallback_reference"}
            ),
            "resend target must match",
        ),
    ],
)
def test_compile_time_validation_rejects_invalid_wait_contracts(mutate, message):
    flow_document = _await_flow()
    mutate(flow_document)
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match=message):
        FlowRuntime(flow_document, tools=registry)


def test_schema_rejects_unused_wait_step_fields() -> None:
    flow_document = _await_flow()
    flow_document["path"][0]["ask_when"] = {"is_present": "slots.payment_id"}

    with pytest.raises(JsonSchemaValidationError):
        validate_flow(flow_document)


def test_compile_time_validation_rejects_unbound_capability():
    flow_document = _await_flow()
    flow_document["path"].pop(0)
    flow_document["capabilities"]["await_external"].pop("step")
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match="must declare step"):
        FlowRuntime(flow_document, tools=registry)


def test_compile_time_validation_rejects_binding_to_a_regular_node():
    flow_document = _await_flow()
    flow_document["path"].pop(0)
    flow_document["capabilities"]["await_external"]["step"] = "collect_fallback_reference"
    registry, _ = _payment_registry()

    with pytest.raises(ValueError, match="does not implement await_external"):
        FlowRuntime(flow_document, tools=registry)


def test_runtime_keeps_a_private_document_copy():
    flow_document = _await_flow()
    registry, _ = _payment_registry()
    runtime = FlowRuntime(flow_document, tools=registry)

    flow_document["capabilities"]["await_external"]["resume_on"] = "changed_token"
    runtime.doc["route"]["description"] = "runtime-private"

    assert runtime.doc["capabilities"]["await_external"]["resume_on"] == "payment_token"
    assert flow_document["route"]["description"] == "Confirm an external payment."
    assert runtime.compiled.doc["route"]["description"] == ("Confirm an external payment.")


def _identification_flow() -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "identify_citizen",
        "version": "1.0.0",
        "route": {"description": "Identify a citizen."},
        "domains": {"Placeholder": {"type": "free_text"}},
        "path": [{"use": "identification@2"}],
        "uses": [{"ref": "identification@2", "with": {"required": False}}],
    }


def _govbr_resume_contract() -> dict[str, Any]:
    return {
        "version": "1",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cpf": {"type": "string", "pattern": "^[0-9]{11}$"},
                "nome": {"type": "string", "minLength": 2},
                "email": {"type": "string", "format": "email"},
            },
            "required": ["cpf"],
        },
        "correlation": "$token.cpf",
        "duplicate": "ignore",
        "late": "reject",
    }


async def test_legacy_cpf_lookup_failure_is_logged_best_effort(caplog):
    registry = default_tool_registry()

    async def failing_cpf_lookup(**_: Any) -> dict[str, Any]:
        raise RuntimeError("registry unavailable")

    registry.register("cpf_lookup", failing_cpf_lookup)
    runtime = FlowRuntime(_identification_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("citizen"), {"identification_method": "cpf"})

    with caplog.at_level(logging.WARNING, logger="flowspec2.subflows.identification"):
        state = await runtime.execute(state, {"cpf": "52998224725"})

    assert state.data["cpf"] == "52998224725"
    assert "e-mail" in require_agent_response(state).description.lower()
    assert any(
        record.message == "Optional identification enrichment failed"
        and getattr(record, "tool", None) == "cpf_lookup"
        and str(getattr(record, "log_id", "")).isdigit()
        for record in caplog.records
    )


async def test_legacy_govbr_enrichment_failure_is_logged_best_effort(caplog):
    registry = default_tool_registry()

    async def failing_user_info(**_: Any) -> dict[str, Any]:
        raise RuntimeError("enrichment unavailable")

    registry.register("get_user_info", failing_user_info)
    runtime = FlowRuntime(_identification_flow(), tools=registry)
    state = await runtime.execute(runtime.new_state("citizen"), {"identification_method": "govbr"})

    with caplog.at_level(logging.WARNING, logger="flowspec2.subflows.identification"):
        state = await runtime.execute(
            state,
            {
                "govbr_token": {
                    "cpf": "52998224725",
                    "nome": "Maria Silva",
                    "email": "maria@example.com",
                }
            },
        )

    assert state.data["govbr_authenticated"] is True
    assert "phone" not in state.data
    assert any(
        record.message == "Optional identification enrichment failed"
        and getattr(record, "tool", None) == "get_user_info"
        and str(getattr(record, "log_id", "")).isdigit()
        for record in caplog.records
    )


async def test_malformed_optional_tool_results_are_logged_and_ignored(caplog):
    registry = default_tool_registry()

    async def malformed_result(**_: Any) -> list[str]:
        return ["not", "an", "object"]

    registry.register("cpf_lookup", cast(Tool, malformed_result))
    registry.register("get_user_info", cast(Tool, malformed_result))
    runtime = FlowRuntime(_identification_flow(), tools=registry)

    cpf_state = await runtime.execute(
        runtime.new_state("cpf-citizen"),
        {"identification_method": "cpf"},
    )
    with caplog.at_level(
        logging.WARNING,
        logger="flowspec2.subflows.identification",
    ):
        cpf_state = await runtime.execute(cpf_state, {"cpf": "52998224725"})

    assert cpf_state.data["cpf"] == "52998224725"
    assert "e-mail" in require_agent_response(cpf_state).description.lower()

    govbr_state = await runtime.execute(
        runtime.new_state("govbr-citizen"),
        {"identification_method": "govbr"},
    )
    govbr_state = await runtime.execute(
        govbr_state,
        {"govbr_token": {"cpf": "52998224725"}},
    )

    assert govbr_state.data["govbr_authenticated"] is True
    assert "phone" not in govbr_state.data
    assert (
        sum(
            record.message == "Optional identification enrichment failed"
            for record in caplog.records
        )
        >= 2
    )


def test_reference_profile_rejects_legacy_untyped_wait_capability():
    flow_document = _identification_flow()
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "resume_on": "govbr_token",
        }
    }
    with pytest.raises(FlowLinkError, match="LEGACY_AWAIT_CONTRACT_FORBIDDEN"):
        FlowRuntime(flow_document)


async def test_legacy_invalid_govbr_response_has_correlated_log_id():
    flow_document = _identification_flow()
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "resume_on": "govbr_token",
            "resume": _govbr_resume_contract(),
        }
    }
    runtime = FlowRuntime(flow_document)
    state = await runtime.execute(
        runtime.new_state("citizen"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(state, {"govbr_token": {"nome": "Untrusted"}})

    agent_response = require_agent_response(state)
    assert "cpf" in agent_response.description.lower()
    assert agent_response.log_id is not None
    assert agent_response.log_id.isdigit()


async def test_govbr_token_without_cpf_has_no_writes_or_enrichment():
    flow_document = _identification_flow()
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "resume_on": "govbr_token",
            "resume": _govbr_resume_contract(),
            "on_resume": {
                "set": {
                    "cpf": "$token.cpf",
                    "name": "$token.nome",
                    "email": "$token.email",
                },
                "enrich": {
                    "tool": "get_user_info",
                    "optional": True,
                    "input": {"cpf": "$token.cpf"},
                    "set": {"phone": "$result.phones.0"},
                },
            },
            "recovery": {
                "switch": {"goto": "collect_cpf"},
            },
        }
    }
    registry = default_tool_registry()
    enrichment_calls = 0

    async def count_enrichment(**_: Any) -> dict[str, Any]:
        nonlocal enrichment_calls
        enrichment_calls += 1
        return {"phones": ["5521999999999"]}

    registry.register("get_user_info", count_enrichment)
    runtime = FlowRuntime(flow_document, tools=registry)
    state = await runtime.execute(
        runtime.new_state("citizen"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(
        state,
        {"govbr_token": {"nome": "Untrusted", "email": "bad@example.com"}},
    )

    assert enrichment_calls == 0
    assert state.data["identification_method"] == "cpf"
    assert "name" not in state.data
    assert "email" not in state.data
    assert "phone" not in state.data
    assert "cpf" in require_agent_response(state).description.lower()


async def test_typed_subflow_wait_binding_and_enrichment_execute():
    flow_document = _identification_flow()
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "resume_on": "govbr_token",
            "resume": _govbr_resume_contract(),
            "on_resume": {
                "set": {
                    "cpf": "$token.cpf",
                    "name": "$token.nome",
                    "email": "$token.email",
                },
                "enrich": {
                    "tool": "get_user_info",
                    "optional": True,
                    "input": {"cpf": "$token.cpf"},
                    "set": {"phone": "$result.phones.0"},
                },
            },
            "recovery": {
                "abort": {"goto": "select_identification_method"},
                "resend": {"goto": "authenticate_govbr"},
                "switch": {"goto": "collect_cpf"},
            },
        }
    }
    runtime = FlowRuntime(flow_document)
    state = await runtime.execute(runtime.new_state("citizen"), {"identification_method": "govbr"})

    state = await runtime.execute(
        state,
        {
            "govbr_token": {
                "cpf": "52998224725",
                "nome": "Maria Silva",
                "email": "maria@example.com",
            }
        },
    )

    assert runtime.compiled.doc["capabilities"]["await_external"]["step"] == ("authenticate_govbr")
    assert state.data["govbr_authenticated"] is True
    assert state.data["phone"] == "5521999999999"


def test_schema_rejects_non_scalar_mapping_values():
    flow_document = copy.deepcopy(_await_flow())
    flow_document["capabilities"]["await_external"]["on_resume"]["set"]["unbounded"] = {
        "nested": True
    }

    with pytest.raises(JsonSchemaValidationError):
        validate_flow(flow_document)
