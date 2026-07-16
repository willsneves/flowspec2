"""Typed tool and subflow contracts enforced by the compiler linker."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from flowspec2.compiler import compile_flow
from flowspec2.models import ServiceState
from flowspec2.nodes import NEXT, FlowContext, NodeDesc
from flowspec2.semantics import FlowLinkError
from flowspec2.subflows import (
    SubflowBuild,
    SubflowDefinition,
    SubflowRegistry,
    default_subflows,
)
from flowspec2.tools import (
    ToolDefinition,
    ToolEffects,
    ToolRegistry,
    default_tool_registry,
)


async def _successful_tool(**_: Any) -> dict[str, Any]:
    return {"status": "success", "receipt": {"code": "REC-1"}}


async def _invalid_output_tool(**_: Any) -> dict[str, Any]:
    return {"status": "success", "receipt": {"code": 1}}


async def _invalid_email_output_tool(**_: Any) -> dict[str, Any]:
    return {"email": "not-an-email"}


async def _incomplete_geocode_tool(**_: Any) -> dict[str, Any]:
    return {"status": "ok", "needs_confirmation": False}


async def _invalid_contact_tool(**_: Any) -> dict[str, Any]:
    return {"status": "ok", "name": "Citizen", "email": "invalid", "phones": []}


class _ControlledSuccessfulTool:
    def __init__(self, expected_concurrent_calls: int) -> None:
        self.call_count = 0
        self.expected_concurrent_calls = expected_concurrent_calls
        self.expected_calls_started = asyncio.Event()
        self.release_calls = asyncio.Event()

    async def __call__(self, **_: Any) -> dict[str, Any]:
        self.call_count += 1
        if self.call_count == self.expected_concurrent_calls:
            self.expected_calls_started.set()
        await self.release_calls.wait()
        return {"status": "success", "receipt": {"code": "REC-CONTROLLED"}}


class _RetryThenSucceedTool:
    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(self, **_: Any) -> dict[str, Any]:
        self.call_count += 1
        return (
            {"status": "retryable", "receipt": {"code": "REC-RETRY"}}
            if self.call_count == 1
            else {"status": "success", "receipt": {"code": "REC-SUCCESS"}}
        )


class _InvocationTrackingTool:
    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(self, **_: Any) -> dict[str, Any]:
        self.call_count += 1
        return {"status": "success"}


class _ConfiguredPayloadTool:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.call_count = 0

    async def __call__(self, **_: Any) -> dict[str, Any]:
        self.call_count += 1
        return {"payload": self.payload}


def _typed_tool_definition(name: str = "submit_answer") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1",
        description="Submit one answer and return a receipt.",
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
                "receipt": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
            "required": ["status", "receipt"],
        },
        effects=ToolEffects(
            read_only=False,
            destructive=False,
            idempotent=True,
            open_world=True,
        ),
    )


def _retryable_tool_definition() -> ToolDefinition:
    base_definition = _typed_tool_definition()
    return ToolDefinition(
        name="submit_answer",
        version="1",
        description="Submit one answer with a retryable outcome.",
        input_schema=base_definition.input_schema,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"enum": ["success", "retryable"]},
                "receipt": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
            "required": ["status", "receipt"],
        },
        effects=base_definition.effects,
    )


def _terminal_flow(
    *,
    tool_name: str = "submit_answer",
    parameter_name: str = "answer",
    result_path: str = "result.receipt.code",
) -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "typed_terminal",
        "version": "1.0.0",
        "route": {"description": "Exercise a typed terminal contract."},
        "domains": {"Answer": {"type": "free_text"}},
        "slots": {"answer": {"domain": "Answer", "required": True}},
        "path": [
            {"slot": "answer", "prompt": {"text": "Answer?"}},
            {"terminal": True},
        ],
        "terminal": {
            "step": "submit_answer",
            "tool": tool_name,
            "idempotent": True,
            "input": [{"param": parameter_name, "slot": "answer"}],
            "outputs": {"receipt_code": result_path},
            "outcomes": {
                "success": {"reset_next": True},
                "retryable": {"preserve_state": True},
                "fatal": {"reset_next": True},
            },
        },
    }


def _typed_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        _successful_tool,
        definition=_typed_tool_definition(),
    )
    return registry


def _terminal_registry_with_input_schema(input_schema: dict[str, Any]) -> ToolRegistry:
    base_definition = _typed_tool_definition()
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        _successful_tool,
        definition=ToolDefinition(
            name="submit_answer",
            version="1",
            description="Submit an answer through a composed input contract.",
            input_schema=input_schema,
            output_schema=base_definition.output_schema,
            effects=base_definition.effects,
        ),
    )
    return registry


def _terminal_registry_with_output_schema(output_schema: dict[str, Any]) -> ToolRegistry:
    base_definition = _typed_tool_definition()
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        _successful_tool,
        definition=ToolDefinition(
            name="submit_answer",
            version="1",
            description="Submit an answer through a composed output contract.",
            input_schema=base_definition.input_schema,
            output_schema=output_schema,
            effects=base_definition.effects,
        ),
    )
    return registry


def _collect_flow() -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "collect_only",
        "version": "1.0.0",
        "route": {"description": "Collect one answer."},
        "domains": {"Answer": {"type": "free_text"}},
        "slots": {"answer": {"domain": "Answer"}},
        "path": [{"slot": "answer", "prompt": {"text": "Answer?"}}],
    }


def _auto_flow_contract_document() -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "auto_flow_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise auto-flow compiler targets."},
        "domains": {"Answer": {"type": "free_text"}},
        "slots": {
            "first_answer": {"domain": "Answer", "required": True},
            "second_answer": {"domain": "Answer", "required": True},
        },
        "path": [
            {
                "step": "collect_first",
                "slot": "first_answer",
                "prompt": {"text": "First?"},
            },
            {
                "step": "collect_second",
                "slot": "second_answer",
                "prompt": {"text": "Second?"},
            },
        ],
        "auto_flow": {
            "meta_flow_ref": "answer_form",
            "send_when": {"is_present": "payload.channel"},
            "resume_at": "collect_first",
            "recovery": {
                "fallback_at": "collect_first",
                "cancel": "END",
                "timeout": "fallback",
                "max_resends": 1,
                "timeout_seconds": 60,
            },
        },
    }


def test_compiler_validates_auto_flow_fallback_target() -> None:
    unknown_target_flow = _auto_flow_contract_document()
    unknown_target_flow["auto_flow"]["recovery"]["fallback_at"] = "missing_step"
    with pytest.raises(
        ValueError,
        match=r"auto_flow\.recovery\.fallback_at must reference exactly one native path step",
    ):
        compile_flow(unknown_target_flow)

    unsafe_prefix_flow = _auto_flow_contract_document()
    unsafe_prefix_flow["auto_flow"]["recovery"]["fallback_at"] = "collect_second"
    with pytest.raises(
        ValueError,
        match=r"auto_flow\.recovery\.fallback_at cannot bypass unsatisfied required slot",
    ):
        compile_flow(unsafe_prefix_flow)


def _await_flow(
    *, input_name: str = "payment_id", result_path: str = "$result.code"
) -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "typed_enrichment",
        "version": "1.0.0",
        "route": {"description": "Resume after a typed enrichment."},
        "domains": {"Placeholder": {"type": "free_text"}},
        "path": [{"step": "await_payment", "await_external": True}],
        "capabilities": {
            "await_external": {
                "kind": "cta_url",
                "step": "await_payment",
                "resume_on": "payment_token",
                "resume": {
                    "version": "1",
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                    },
                    "correlation": "$token.id",
                    "duplicate": "ignore",
                    "late": "reject",
                },
                "on_resume": {
                    "enrich": {
                        "tool": "payment_lookup",
                        "input": {input_name: "$token.id"},
                        "set": {"receipt_code": result_path},
                    }
                },
            }
        },
    }


def _payment_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "payment_lookup",
        _successful_tool,
        definition=ToolDefinition(
            name="payment_lookup",
            version="1",
            description="Look up a payment receipt.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"payment_id": {"type": "string"}},
                "required": ["payment_id"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
            effects=ToolEffects(read_only=True, idempotent=True, open_world=True),
        ),
    )
    return registry


def _conditional_payment_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "payment_lookup",
        _successful_tool,
        definition=ToolDefinition(
            name="payment_lookup",
            version="1",
            description="Look up a payment under a conditional tenant contract.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "payment_id": {"type": "string"},
                    "tenant": {"type": "string"},
                },
                "required": ["payment_id"],
                "if": {"required": ["payment_id"]},
                "then": {"required": ["tenant"]},
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
            effects=ToolEffects(read_only=True, idempotent=True, open_world=True),
        ),
    )
    return registry


async def _finish_subflow(state: ServiceState) -> ServiceState:
    state.agent_response = None
    return state


class _ManifestSubflow:
    name = "manifest"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        del ctx, with_cfg
        return SubflowBuild(
            descriptors=[NodeDesc("manifest_done", _finish_subflow, lambda state: NEXT)],
            entry_id="manifest_done",
            node_for_slot={},
        )


def test_tool_definition_is_deeply_immutable_and_versioned() -> None:
    definition = _typed_tool_definition()

    assert definition.identifier == "submit_answer@1"
    assert json.loads(json.dumps(definition.as_dict()))["identifier"] == "submit_answer@1"
    with pytest.raises(TypeError):
        definition.input_schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(TypeError):
        definition.output_schema["properties"]["status"]["const"] = "fatal"
    with pytest.raises(FrozenInstanceError):
        definition.effects.idempotent = False  # type: ignore[misc]


def test_tool_definition_rejects_invalid_json_schema() -> None:
    with pytest.raises(ValueError, match="input_schema is not a valid JSON Schema"):
        ToolDefinition(
            name="invalid",
            version="1",
            description="Invalid contract.",
            input_schema={"type": 42},
            output_schema={"type": "object"},
        )


@pytest.mark.parametrize(
    "impossible_schema",
    [
        {
            "type": "object",
            "properties": {"required_value": False},
            "required": ["required_value"],
        },
        {"type": "object", "propertyNames": False, "minProperties": 1},
        {
            "type": "object",
            "required": ["trigger"],
            "dependentSchemas": {"trigger": False},
        },
        {
            "type": "object",
            "patternProperties": {".*": False},
            "additionalProperties": False,
            "minProperties": 1,
        },
        {
            "type": "object",
            "properties": {"required_value": {"type": "string"}},
            "required": ["required_value"],
            "allOf": [
                {
                    "properties": {"required_value": {"type": "number"}},
                    "required": ["required_value"],
                }
            ],
        },
    ],
)
def test_tool_definition_rejects_impossible_object_boundaries(
    impossible_schema: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="cannot validate any object-shaped"):
        ToolDefinition(
            name="impossible",
            version="1",
            description="Reject an impossible callable boundary.",
            input_schema=impossible_schema,
            output_schema={"type": "object"},
        )


@pytest.mark.parametrize(
    "cyclic_schema",
    [
        {"type": "object", "$ref": "#"},
        {
            "type": "object",
            "$defs": {
                "first": {"$ref": "#/$defs/second"},
                "second": {"$ref": "#/$defs/first"},
            },
            "$ref": "#/$defs/first",
        },
    ],
)
def test_tool_definition_rejects_cyclic_local_references(
    cyclic_schema: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="cyclic local references"):
        ToolDefinition(
            name="cyclic",
            version="1",
            description="Reject a cyclic callable boundary.",
            input_schema=cyclic_schema,
            output_schema={"type": "object"},
        )


@pytest.mark.parametrize(
    ("boundary", "boundary_schema", "error_pattern"),
    [
        ("input_schema", {"type": "array"}, "cannot validate any object-shaped"),
        (
            "input_schema",
            {"type": ["object", "null"]},
            "must only validate object-shaped",
        ),
        (
            "output_schema",
            {"anyOf": [{"type": "object"}, {"type": "string"}]},
            "must only validate object-shaped",
        ),
        (
            "output_schema",
            {"$defs": {"response": {"type": "string"}}, "$ref": "#/$defs/response"},
            "cannot validate any object-shaped",
        ),
        (
            "input_schema",
            {"allOf": [{"type": "object"}, {"type": "string"}]},
            "cannot validate any object-shaped",
        ),
        (
            "output_schema",
            {"allOf": [{"type": "object"}, {"not": {"type": "object"}}]},
            "cannot validate any object-shaped",
        ),
        (
            "input_schema",
            {"type": "object", "required": ["answer"], "maxProperties": 0},
            "cannot validate any object-shaped",
        ),
        (
            "input_schema",
            {
                "type": "object",
                "minProperties": 1,
                "properties": {},
                "additionalProperties": False,
            },
            "cannot validate any object-shaped",
        ),
        (
            "output_schema",
            {"type": "object", "const": {}, "required": ["receipt"]},
            "cannot validate any object-shaped",
        ),
        (
            "output_schema",
            {"type": "object", "enum": [{}], "minProperties": 1},
            "cannot validate any object-shaped",
        ),
    ],
)
def test_tool_definition_rejects_non_object_or_impossible_callable_boundaries(
    boundary: str,
    boundary_schema: dict[str, Any],
    error_pattern: str,
) -> None:
    object_schema = {"type": "object"}

    with pytest.raises(
        ValueError,
        match=rf"{boundary} {error_pattern}",
    ):
        ToolDefinition(
            name="invalid_boundary",
            version="1",
            description="Reject a non-object callable boundary.",
            input_schema=boundary_schema if boundary == "input_schema" else object_schema,
            output_schema=boundary_schema if boundary == "output_schema" else object_schema,
        )


def test_tool_definition_accepts_object_union_narrowed_by_negation() -> None:
    definition = ToolDefinition(
        name="narrowed_boundary",
        version="1",
        description="Narrow an object-or-null boundary to objects.",
        input_schema={
            "type": ["object", "null"],
            "not": {"type": "null"},
        },
        output_schema={"type": "object"},
    )

    assert definition.input_schema["type"] == ("object", "null")


def test_tool_definition_rejects_unsupported_unevaluated_properties() -> None:
    with pytest.raises(ValueError, match="do not support unevaluatedProperties"):
        ToolDefinition(
            name="unsupported_projection",
            version="1",
            description="Reject unsupported key projection semantics.",
            input_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "unevaluatedProperties": False,
            },
            output_schema={"type": "object"},
        )


def test_tool_definition_enforces_draft_2020_12() -> None:
    with pytest.raises(ValueError, match="must use JSON Schema Draft 2020-12"):
        ToolDefinition(
            name="legacy_dialect",
            version="1",
            description="Reject a different JSON Schema dialect.",
            input_schema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
            },
            output_schema={"type": "object"},
        )


def test_tool_definition_resolves_local_references_and_object_compositions() -> None:
    definition = ToolDefinition(
        name="composed",
        version="1",
        description="Accept object-only local references and compositions.",
        input_schema={
            "$defs": {
                "request": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                }
            },
            "$ref": "#/$defs/request",
        },
        output_schema={
            "oneOf": [
                {"type": "object", "required": ["receipt"]},
                {
                    "allOf": [
                        {"type": "object"},
                        {"properties": {"error": {"type": "string"}}},
                    ]
                },
            ]
        },
    )

    assert definition.input_schema["$ref"] == "#/$defs/request"


@pytest.mark.parametrize(
    "invalid_reference",
    ["#/$defs/missing", "https://contracts.example/request.json"],
)
def test_tool_definition_rejects_unresolved_or_external_references(
    invalid_reference: str,
) -> None:
    with pytest.raises(ValueError, match="input_schema is not a valid JSON Schema"):
        ToolDefinition(
            name="invalid_reference",
            version="1",
            description="Reject an unsafe tool contract reference.",
            input_schema={
                "type": "object",
                "properties": {"answer": {"$ref": invalid_reference}},
            },
            output_schema={"type": "object"},
        )


def test_legacy_registration_stays_open_and_typed_replacement_keeps_contract() -> None:
    registry = ToolRegistry()
    registry.register("legacy", _successful_tool)
    assert registry.definition("legacy").version == "unversioned"
    assert registry.definition("legacy").input_schema["additionalProperties"] is True
    assert registry.definition("legacy").legacy_contract is True

    definition = _typed_tool_definition("legacy")
    registry.register("legacy", _successful_tool, definition=definition)
    registry.register("legacy", _successful_tool)

    assert registry.definition("legacy") is definition
    assert registry.definition("legacy").legacy_contract is False
    with pytest.raises(TypeError):
        registry.definitions["other"] = definition  # type: ignore[index]


def test_default_tool_registry_exposes_closed_contracts_and_effects() -> None:
    definitions = default_tool_registry().definitions

    assert definitions["hub_search"].effects.read_only is True
    assert definitions["open_service_request"].effects.read_only is False
    assert definitions["open_service_request"].input_schema["additionalProperties"] is False
    assert "protocol_id" in definitions["open_service_request"].output_schema["properties"]


@pytest.mark.asyncio
async def test_default_tool_contracts_reject_unusable_success_and_invalid_identity_data() -> None:
    registry = default_tool_registry()
    registry.register("geocode", _incomplete_geocode_tool)

    with pytest.raises(ValueError, match=r"geocode@1.*output.*required"):
        await registry.call("geocode", address="Street A")

    registry.register("brazilian_tax_id_lookup", _invalid_contact_tool)
    with pytest.raises(ValueError, match=r"brazilian_tax_id_lookup@1.*output at /email"):
        await registry.call("brazilian_tax_id_lookup", brazilian_tax_id="52998224725")

    with pytest.raises(
        ValueError, match=r"brazilian_tax_id_lookup@1.*input at /brazilian_tax_id \(pattern\)"
    ):
        await registry.call("brazilian_tax_id_lookup", brazilian_tax_id="invalid")


@pytest.mark.asyncio
async def test_tool_registry_enforces_input_and_output_contracts_at_call_boundary() -> None:
    registry = _typed_tool_registry()

    with pytest.raises(
        ValueError,
        match=r"tool contract 'submit_answer@1' rejected input at /answer \(type\)",
    ):
        await registry.call("submit_answer", answer=1)

    invalid_output_registry = ToolRegistry()
    invalid_output_registry.register(
        "submit_answer",
        _invalid_output_tool,
        definition=_typed_tool_definition(),
    )
    with pytest.raises(
        ValueError,
        match=(r"tool contract 'submit_answer@1' rejected output at /receipt/code \(type\)"),
    ):
        await invalid_output_registry.call("submit_answer", answer="accepted")


@pytest.mark.asyncio
async def test_tool_registry_checks_input_formats_before_invocation() -> None:
    invocation_tracking_tool = _InvocationTrackingTool()
    registry = ToolRegistry()
    registry.register(
        "send_email",
        invocation_tracking_tool,
        definition=ToolDefinition(
            name="send_email",
            version="1",
            description="Send a message to a validated email address.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"email": {"type": "string", "format": "email"}},
                "required": ["email"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"status": {"const": "success"}},
                "required": ["status"],
            },
        ),
    )

    with pytest.raises(
        ValueError,
        match=r"tool contract 'send_email@1' rejected input at /email \(format\)",
    ):
        await registry.call("send_email", email="not-an-email")

    assert invocation_tracking_tool.call_count == 0


@pytest.mark.asyncio
async def test_tool_registry_checks_output_formats() -> None:
    registry = ToolRegistry()
    registry.register(
        "lookup_email",
        _invalid_email_output_tool,
        definition=ToolDefinition(
            name="lookup_email",
            version="1",
            description="Return a validated email address.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"email": {"type": "string", "format": "email"}},
                "required": ["email"],
            },
        ),
    )

    with pytest.raises(
        ValueError,
        match=r"tool contract 'lookup_email@1' rejected output at /email \(format\)",
    ):
        await registry.call("lookup_email")


@pytest.mark.parametrize(
    ("invalid_payload", "error_pattern"),
    [
        (object(), "unsupported Python type object"),
        ({"not-json"}, "unsupported Python type set"),
        (math.nan, "non-finite number"),
        (math.inf, "non-finite number"),
    ],
)
@pytest.mark.asyncio
async def test_tool_registry_rejects_non_json_inputs_before_invocation(
    invalid_payload: Any,
    error_pattern: str,
) -> None:
    invocation_tracking_tool = _InvocationTrackingTool()
    registry = ToolRegistry()
    registry.register(
        "accept_payload",
        invocation_tracking_tool,
        definition=ToolDefinition(
            name="accept_payload",
            version="1",
            description="Accept one strict JSON payload.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"payload": {}},
                "required": ["payload"],
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"status": {"const": "success"}},
                "required": ["status"],
            },
        ),
    )

    with pytest.raises(
        ValueError,
        match=rf"rejected input at /payload: {error_pattern}",
    ):
        await registry.call("accept_payload", payload=invalid_payload)

    assert invocation_tracking_tool.call_count == 0


@pytest.mark.parametrize(
    ("invalid_payload", "error_pattern"),
    [
        (object(), "unsupported Python type object"),
        ({"not-json"}, "unsupported Python type set"),
        (math.nan, "non-finite number"),
        (-math.inf, "non-finite number"),
    ],
)
@pytest.mark.asyncio
async def test_tool_registry_rejects_non_json_outputs_after_invocation(
    invalid_payload: Any,
    error_pattern: str,
) -> None:
    configured_payload_tool = _ConfiguredPayloadTool(invalid_payload)
    registry = ToolRegistry()
    registry.register(
        "return_payload",
        configured_payload_tool,
        definition=ToolDefinition(
            name="return_payload",
            version="1",
            description="Return one strict JSON payload.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"payload": {}},
                "required": ["payload"],
            },
        ),
    )

    with pytest.raises(
        ValueError,
        match=rf"rejected output at /payload: {error_pattern}",
    ):
        await registry.call("return_payload")

    assert configured_payload_tool.call_count == 1


def test_idempotency_key_requires_strict_json_and_is_canonical() -> None:
    operation_namespace = ToolRegistry.operation_namespace(
        "typed_terminal",
        "1.0.0",
        "submit_answer",
        "submit_answer@1",
    )
    first_key = ToolRegistry.idempotency_key(
        "user",
        operation_namespace,
        {"nested": {"first": 1, "second": 2}},
    )
    second_key = ToolRegistry.idempotency_key(
        "user",
        operation_namespace,
        {"nested": {"second": 2, "first": 1}},
    )
    upgraded_operation_namespace = ToolRegistry.operation_namespace(
        "typed_terminal",
        "1.0.0",
        "submit_answer",
        "submit_answer@2",
    )

    assert first_key == second_key
    assert first_key != ToolRegistry.idempotency_key(
        "user",
        upgraded_operation_namespace,
        {"nested": {"first": 1, "second": 2}},
    )
    with pytest.raises(
        ValueError,
        match=r"idempotency inputs at /payload: unsupported Python type object",
    ):
        ToolRegistry.idempotency_key("user", operation_namespace, {"payload": object()})


def test_tool_registry_replay_cache_returns_defensive_copies() -> None:
    registry = ToolRegistry()
    replay_result: dict[str, Any] = {
        "status": "success",
        "receipt": {"code": "REC-1"},
    }

    registry.replay_put("key", replay_result)
    replay_result["receipt"]["code"] = "CHANGED-OUTSIDE"
    first_replay = registry.replay_get("key")
    assert first_replay == {"status": "success", "receipt": {"code": "REC-1"}}
    assert first_replay is not None
    first_replay["receipt"]["code"] = "CHANGED-BY-CALLER"
    assert registry.replay_get("key") == {
        "status": "success",
        "receipt": {"code": "REC-1"},
    }


@pytest.mark.asyncio
async def test_call_idempotent_coalesces_concurrent_calls_for_the_same_key() -> None:
    controlled_tool = _ControlledSuccessfulTool(expected_concurrent_calls=1)
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        controlled_tool,
        definition=_typed_tool_definition(),
    )

    first_call = asyncio.create_task(
        registry.call_idempotent("submit_answer", "shared-key", answer="accepted")
    )
    second_call = asyncio.create_task(
        registry.call_idempotent("submit_answer", "shared-key", answer="accepted")
    )
    await asyncio.wait_for(controlled_tool.expected_calls_started.wait(), timeout=1.0)
    controlled_tool.release_calls.set()
    first_result, second_result = await asyncio.gather(first_call, second_call)

    assert controlled_tool.call_count == 1
    assert first_result == second_result
    assert first_result is not second_result


@pytest.mark.asyncio
async def test_call_idempotent_does_not_serialize_unrelated_keys() -> None:
    controlled_tool = _ControlledSuccessfulTool(expected_concurrent_calls=2)
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        controlled_tool,
        definition=_typed_tool_definition(),
    )

    first_call = asyncio.create_task(
        registry.call_idempotent("submit_answer", "first-key", answer="first")
    )
    second_call = asyncio.create_task(
        registry.call_idempotent("submit_answer", "second-key", answer="second")
    )
    await asyncio.wait_for(controlled_tool.expected_calls_started.wait(), timeout=1.0)
    controlled_tool.release_calls.set()
    await asyncio.gather(first_call, second_call)

    assert controlled_tool.call_count == 2


@pytest.mark.asyncio
async def test_call_idempotent_retries_non_success_and_caches_success() -> None:
    retrying_tool = _RetryThenSucceedTool()
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        retrying_tool,
        definition=_retryable_tool_definition(),
    )

    retryable_result = await registry.call_idempotent(
        "submit_answer", "retry-key", answer="accepted"
    )
    successful_result = await registry.call_idempotent(
        "submit_answer", "retry-key", answer="accepted"
    )
    replayed_result = await registry.call_idempotent(
        "submit_answer", "retry-key", answer="accepted"
    )

    assert retryable_result["status"] == "retryable"
    assert successful_result["status"] == "success"
    assert replayed_result == successful_result
    assert replayed_result is not successful_result
    assert retrying_tool.call_count == 2


def test_compiler_accepts_terminal_bindings_resolved_by_typed_contract() -> None:
    compiled_flow = compile_flow(_terminal_flow(), tools=_typed_tool_registry())

    assert compiled_flow.terminal_id == "submit_answer"


@pytest.mark.parametrize(
    ("source_domain", "parameter_contract"),
    [
        ({"type": "integer"}, {"type": "string"}),
        (
            {"type": "categorical", "values": ["north"]},
            {"type": "string", "enum": ["south"]},
        ),
        (
            {"type": "integer", "minimum": 1, "maximum": 2},
            {"type": "integer", "minimum": 3},
        ),
    ],
)
def test_compiler_rejects_incompatible_slot_to_tool_parameter_contracts(
    source_domain: dict[str, Any],
    parameter_contract: dict[str, Any],
) -> None:
    flow_document = _terminal_flow()
    flow_document["domains"]["Answer"] = source_domain
    flow_document["slots"]["answer"]["required"] = True
    registry = _terminal_registry_with_input_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"answer": parameter_contract},
            "required": ["answer"],
        }
    )

    with pytest.raises(
        ValueError,
        match=(
            r"\$\.terminal\.input\[0\] binds slot 'answer' from domain 'Answer' "
            r"\(source contract .*\) to tool parameter 'answer' in 'submit_answer@1' "
            r"\(parameter contract .*\), but the source contract is not proven to be a "
            r"subset of the parameter contract"
        ),
    ):
        compile_flow(flow_document, tools=registry)


@pytest.mark.parametrize(
    ("source_domain", "parameter_contract"),
    [
        (
            {"type": "categorical", "values": ["north", "south"]},
            {"type": "string", "enum": ["north"]},
        ),
        (
            {"type": "integer", "minimum": 1, "maximum": 5},
            {"type": "integer", "minimum": 0, "maximum": 3},
        ),
        (
            {"type": "free_text"},
            {"type": "string", "minLength": 2},
        ),
        (
            {"type": "free_text"},
            {"type": "string", "pattern": "^A"},
        ),
    ],
)
def test_compiler_rejects_partially_compatible_terminal_input_contracts(
    source_domain: dict[str, Any],
    parameter_contract: dict[str, Any],
) -> None:
    flow_document = _terminal_flow()
    flow_document["domains"]["Answer"] = source_domain
    registry = _terminal_registry_with_input_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"answer": parameter_contract},
            "required": ["answer"],
        }
    )

    with pytest.raises(ValueError, match="not proven to be a subset"):
        compile_flow(flow_document, tools=registry)


@pytest.mark.parametrize(
    ("source_domain", "parameter_contract"),
    [
        (
            {"type": "integer", "minimum": 2, "maximum": 3},
            {"type": "number", "minimum": 1, "maximum": 4},
        ),
        (
            {"type": "name"},
            {"type": "string", "minLength": 1},
        ),
        (
            {"type": "email"},
            {"type": "string", "format": "email"},
        ),
    ],
)
def test_compiler_accepts_proven_terminal_input_subsets(
    source_domain: dict[str, Any],
    parameter_contract: dict[str, Any],
) -> None:
    flow_document = _terminal_flow()
    flow_document["domains"]["Answer"] = source_domain
    registry = _terminal_registry_with_input_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"answer": parameter_contract},
            "required": ["answer"],
        }
    )

    compile_flow(flow_document, tools=registry)


def test_compiler_includes_optional_slot_absence_in_terminal_contract() -> None:
    flow_document = _terminal_flow()
    flow_document["slots"]["answer"]["required"] = False

    with pytest.raises(ValueError, match="not proven to be a subset"):
        compile_flow(flow_document, tools=_typed_tool_registry())

    nullable_registry = _terminal_registry_with_input_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"answer": {"type": ["string", "null"]}},
            "required": ["answer"],
        }
    )
    compile_flow(flow_document, tools=nullable_registry)


def _derived_terminal_flow(*, total: bool = True) -> dict[str, Any]:
    flow_document = _terminal_flow()
    flow_document["domains"] = {"Source": {"type": "categorical", "values": ["a", "b"]}}
    flow_document["slots"] = {"source": {"domain": "Source", "required": True}}
    flow_document["path"] = [
        {"step": "collect_source", "slot": "source", "prompt": {"text": "Source?"}},
        {"step": "derive_answer", "derive": "answer"},
        {"terminal": True},
    ]
    flow_document["derive"] = [
        {
            "writes": "answer",
            "from": ["source"],
            "lookup": {"a": "north", **({"b": "south"} if total else {})},
        }
    ]
    return flow_document


def test_compiler_rejects_partial_derived_terminal_input() -> None:
    with pytest.raises(ValueError, match="derived value 'answer'.*not proven to be a subset"):
        compile_flow(_derived_terminal_flow(total=False), tools=_typed_tool_registry())


def test_compiler_checks_all_derived_outputs_against_terminal_parameter() -> None:
    registry = _terminal_registry_with_input_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"answer": {"enum": ["north"]}},
            "required": ["answer"],
        }
    )

    with pytest.raises(ValueError, match="derived value 'answer'.*not proven to be a subset"):
        compile_flow(_derived_terminal_flow(), tools=registry)


def test_compiler_accepts_total_derived_terminal_input() -> None:
    compile_flow(_derived_terminal_flow(), tools=_typed_tool_registry())


def test_compiler_rejects_derive_that_executes_after_terminal_consumer() -> None:
    flow_document = _derived_terminal_flow()
    flow_document["path"].remove({"step": "derive_answer", "derive": "answer"})
    flow_document["derive"][0]["after"] = "submit_answer"

    with pytest.raises(ValueError, match="reads derived value 'answer' before its producer"):
        compile_flow(flow_document, tools=_typed_tool_registry())


@pytest.mark.parametrize("location", ["entry", "terminal"])
def test_compiler_rejects_unknown_top_level_tool_with_path(location: str) -> None:
    flow_document = _collect_flow()
    if location == "entry":
        flow_document["entry"] = {"tool": "unknown_tool"}
    else:
        flow_document["path"].append({"terminal": True})
        flow_document["terminal"] = _terminal_flow(tool_name="unknown_tool")["terminal"]

    with pytest.raises(FlowLinkError) as linking_error:
        compile_flow(flow_document)

    assert [diagnostic.code for diagnostic in linking_error.value.diagnostics] == [
        "FLOWSPEC_PROFILE_TOOL_UNAVAILABLE"
    ]
    assert linking_error.value.diagnostics[0].path == f"/{location}/tool"


def test_compiler_accepts_safe_local_entry_contract_subset() -> None:
    flow_document = _terminal_flow()
    flow_document["route"]["entry_args_schema"] = {
        "$defs": {
            "answer": {"type": "string", "minLength": 1},
        },
        "type": "object",
        "additionalProperties": False,
        "properties": {"answer": {"$ref": "#/$defs/answer"}},
    }

    compile_flow(flow_document, tools=_typed_tool_registry())


@pytest.mark.parametrize(
    "entry_property_schema",
    [
        {"type": "string"},
        {"type": "string", "minLength": 1, "maxLength": 2},
    ],
)
def test_compiler_requires_entry_property_to_be_a_slot_domain_subset(
    entry_property_schema: dict[str, Any],
) -> None:
    flow_document = _terminal_flow()
    flow_document["domains"]["Answer"] = {
        "type": "categorical",
        "values": ["north", "south"],
    }
    flow_document["route"]["entry_args_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"answer": entry_property_schema},
    }

    with pytest.raises(
        ValueError,
        match=r"entry_args_schema\.properties\['answer'\].*not proven to be a subset",
    ):
        compile_flow(flow_document, tools=_typed_tool_registry())


@pytest.mark.parametrize(
    ("entry_args_schema", "error_pattern"),
    [
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"$ref": "https://example.invalid/answer"}},
            },
            "only supports local \\$ref values",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"$dynamicRef": "#answer"}},
            },
            "does not support dynamic schema references",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"$id": "nested", "type": "string", "minLength": 1}},
            },
            "does not support nested \\$id resources",
        ),
        (
            {
                "$defs": {
                    "answer": {"$ref": "#/$defs/answer"},
                },
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"$ref": "#/$defs/answer"}},
            },
            "does not support cyclic local references",
        ),
    ],
)
def test_compiler_rejects_unsafe_entry_schema_references(
    entry_args_schema: dict[str, Any],
    error_pattern: str,
) -> None:
    flow_document = _terminal_flow()
    flow_document["route"]["entry_args_schema"] = entry_args_schema

    with pytest.raises(ValueError, match=error_pattern):
        compile_flow(flow_document, tools=_typed_tool_registry())


def test_compiler_rejects_unknown_and_duplicate_terminal_parameters() -> None:
    with pytest.raises(
        ValueError,
        match=r"\$\.terminal\.input\[0\]\.param does not exist in tool contract",
    ):
        compile_flow(
            _terminal_flow(parameter_name="answer_typo"),
            tools=_typed_tool_registry(),
        )

    duplicate_flow = _terminal_flow()
    duplicate_flow["terminal"]["input"].append({"param": "answer", "slot": "answer"})
    with pytest.raises(ValueError, match="duplicates tool parameter 'answer'"):
        compile_flow(duplicate_flow, tools=_typed_tool_registry())


def test_compiler_rejects_missing_required_terminal_parameter() -> None:
    flow_document = _terminal_flow()
    flow_document["terminal"]["input"] = []

    with pytest.raises(ValueError, match="does not bind required parameters.*'answer'"):
        compile_flow(flow_document, tools=_typed_tool_registry())


@pytest.mark.parametrize(
    "effects",
    [
        ToolEffects(read_only=False, destructive=False, idempotent=True),
        ToolEffects(read_only=False, destructive=True, idempotent=True),
    ],
)
def test_compiler_requires_entry_tool_to_be_read_only_and_non_destructive(
    effects: ToolEffects,
) -> None:
    registry = ToolRegistry()
    registry.register(
        "load_context",
        _successful_tool,
        definition=ToolDefinition(
            name="load_context",
            version="1",
            description="Load entry context.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            effects=effects,
        ),
    )
    flow_document = _collect_flow()
    flow_document["entry"] = {"tool": "load_context"}

    with pytest.raises(ValueError, match="must be read_only and non-destructive"):
        compile_flow(flow_document, tools=registry)


def test_compiler_requires_await_enrichment_tool_to_be_read_only() -> None:
    base_definition = _payment_registry().definition("payment_lookup")
    registry = ToolRegistry()
    registry.register(
        "payment_lookup",
        _successful_tool,
        definition=ToolDefinition(
            name="payment_lookup",
            version="1",
            description="Mutate payment state while looking it up.",
            input_schema=base_definition.input_schema,
            output_schema=base_definition.output_schema,
            effects=ToolEffects(read_only=False, idempotent=True),
        ),
    )

    with pytest.raises(ValueError, match="must be read_only and non-destructive"):
        compile_flow(_await_flow(), tools=registry)


def _non_idempotent_retryable_terminal_registry() -> ToolRegistry:
    retryable_definition = _retryable_tool_definition()
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        _successful_tool,
        definition=ToolDefinition(
            name="submit_answer",
            version="1",
            description="Submit an answer without a replay-safe external contract.",
            input_schema=retryable_definition.input_schema,
            output_schema=retryable_definition.output_schema,
            effects=ToolEffects(read_only=False, idempotent=False),
        ),
    )
    return registry


def test_compiler_requires_tool_effect_idempotency_for_idempotent_terminal() -> None:
    with pytest.raises(ValueError, match="does not declare idempotent effects"):
        compile_flow(
            _terminal_flow(),
            tools=_non_idempotent_retryable_terminal_registry(),
        )


def test_compiler_rejects_retry_preservation_for_non_idempotent_effect() -> None:
    flow_document = _terminal_flow()
    flow_document["terminal"]["idempotent"] = False

    with pytest.raises(ValueError, match=r"retryable\.preserve_state requires"):
        compile_flow(
            flow_document,
            tools=_non_idempotent_retryable_terminal_registry(),
        )


def test_compiler_allows_non_idempotent_terminal_when_retry_does_not_reexecute() -> None:
    flow_document = _terminal_flow()
    flow_document["terminal"]["idempotent"] = False
    flow_document["terminal"]["outcomes"]["retryable"]["preserve_state"] = False

    compile_flow(
        flow_document,
        tools=_non_idempotent_retryable_terminal_registry(),
    )


@pytest.mark.parametrize(
    "status_schema",
    [
        {"type": "string"},
        {"enum": ["success", "ok"]},
    ],
)
def test_compiler_rejects_terminal_status_outside_closed_protocol(
    status_schema: dict[str, Any],
) -> None:
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": status_schema,
            "receipt": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        "required": ["status", "receipt"],
    }

    with pytest.raises(ValueError, match="must restrict status to the terminal protocol"):
        compile_flow(
            _terminal_flow(),
            tools=_terminal_registry_with_output_schema(output_schema),
        )


def test_compiler_rejects_terminal_contract_without_success_branch() -> None:
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"status": {"const": "retryable"}},
        "required": ["status"],
    }
    flow_document = _terminal_flow()
    flow_document["terminal"]["outputs"] = {}

    with pytest.raises(ValueError, match="has no success-compatible branch"):
        compile_flow(
            flow_document,
            tools=_terminal_registry_with_output_schema(output_schema),
        )


def test_compiler_accepts_absent_or_optional_closed_terminal_status() -> None:
    flow_document = _terminal_flow()
    flow_document["terminal"]["outputs"] = {}
    no_status_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"message": {"type": "string"}},
    }
    optional_status_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"status": {"const": "retryable"}},
    }

    compile_flow(
        flow_document,
        tools=_terminal_registry_with_output_schema(no_status_schema),
    )
    compile_flow(
        flow_document,
        tools=_terminal_registry_with_output_schema(optional_status_schema),
    )


@pytest.mark.parametrize("union_keyword", ["oneOf", "anyOf"])
def test_compiler_validates_complete_terminal_projection_across_union_and_ref(
    union_keyword: str,
) -> None:
    input_schema = {
        "$defs": {
            "answer_request": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "answer": {"type": "string"},
                    "confirmation": {"type": "string"},
                },
                "required": ["answer", "confirmation"],
            },
            "alternate_request": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"alternate": {"type": "string"}},
                "required": ["alternate"],
            },
        },
        union_keyword: [
            {"$ref": "#/$defs/answer_request"},
            {"$ref": "#/$defs/alternate_request"},
        ],
    }

    with pytest.raises(
        ValueError,
        match=r"\$\.terminal\.input bound-key projection \['answer'\].*complete input contract",
    ):
        compile_flow(
            _terminal_flow(),
            tools=_terminal_registry_with_input_schema(input_schema),
        )


def test_compiler_validates_conditional_terminal_projection() -> None:
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answer": {"type": "string"},
            "confirmation": {"type": "string"},
        },
        "required": ["answer"],
        "if": {"required": ["answer"]},
        "then": {"required": ["confirmation"]},
    }

    with pytest.raises(
        ValueError,
        match=r"\$\.terminal\.input bound-key projection \['answer'\].*complete input contract",
    ):
        compile_flow(
            _terminal_flow(),
            tools=_terminal_registry_with_input_schema(input_schema),
        )


def test_compiler_validates_terminal_projection_property_names() -> None:
    input_schema = {
        "$defs": {"parameter_name": {"type": "string", "pattern": "^x_"}},
        "type": "object",
        "propertyNames": {"$ref": "#/$defs/parameter_name"},
        "additionalProperties": True,
    }
    registry = _terminal_registry_with_input_schema(input_schema)

    compile_flow(_terminal_flow(parameter_name="x_answer"), tools=registry)
    with pytest.raises(
        ValueError,
        match=r"\$\.terminal\.input bound-key projection \['answer'\].*complete input contract",
    ):
        compile_flow(_terminal_flow(), tools=registry)


def test_compiler_rejects_terminal_result_path_absent_from_contract() -> None:
    with pytest.raises(
        ValueError,
        match=r"\$\.terminal\.outputs\['receipt_code'\] does not resolve in tool contract",
    ):
        compile_flow(
            _terminal_flow(result_path="result.receipt.unknown"),
            tools=_typed_tool_registry(),
        )


def test_compiler_rejects_optional_terminal_result_path_on_success() -> None:
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"const": "success"},
            "receipt": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        "required": ["status"],
    }

    with pytest.raises(
        ValueError,
        match=(
            r"\$\.terminal\.outputs\['receipt_code'\] must be required in every "
            r"success-compatible output branch"
        ),
    ):
        compile_flow(
            _terminal_flow(),
            tools=_terminal_registry_with_output_schema(output_schema),
        )


@pytest.mark.parametrize("union_keyword", ["oneOf", "anyOf"])
def test_compiler_rejects_terminal_result_path_missing_from_a_success_branch(
    union_keyword: str,
) -> None:
    successful_receipt_branch = _typed_tool_definition().output_schema
    missing_receipt_branch = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"status": {"const": "success"}},
        "required": ["status"],
    }

    with pytest.raises(
        ValueError,
        match="must be required in every success-compatible output branch",
    ):
        compile_flow(
            _terminal_flow(),
            tools=_terminal_registry_with_output_schema(
                {
                    union_keyword: [
                        successful_receipt_branch,
                        missing_receipt_branch,
                    ]
                }
            ),
        )


def test_compiler_allows_terminal_result_path_absent_from_non_success_branch() -> None:
    successful_receipt_branch = _typed_tool_definition().output_schema
    retryable_branch = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"status": {"const": "retryable"}},
        "required": ["status"],
    }

    compile_flow(
        _terminal_flow(),
        tools=_terminal_registry_with_output_schema(
            {"oneOf": [successful_receipt_branch, retryable_branch]}
        ),
    )


def test_compiler_proves_default_ticketing_conditional_success_result_path() -> None:
    ticketing_output_schema = (
        default_tool_registry().definition("open_service_request").output_schema
    )

    compile_flow(
        _terminal_flow(result_path="result.protocol_id"),
        tools=_terminal_registry_with_output_schema(dict(ticketing_output_schema)),
    )


def test_compiler_proves_if_then_result_path_when_success_makes_condition_true() -> None:
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"enum": ["success", "retryable"]},
            "receipt": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        "required": ["status"],
        "if": {
            "properties": {"status": {"const": "success"}},
            "required": ["status"],
        },
        "then": {"required": ["receipt"]},
    }

    compile_flow(
        _terminal_flow(),
        tools=_terminal_registry_with_output_schema(output_schema),
    )


def test_compiler_resolves_terminal_result_path_through_local_reference() -> None:
    base_definition = _typed_tool_definition()
    registry = ToolRegistry()
    registry.register(
        "submit_answer",
        _successful_tool,
        definition=ToolDefinition(
            name="submit_answer",
            version="1",
            description="Return a locally referenced receipt object.",
            input_schema=base_definition.input_schema,
            output_schema={
                "$defs": {
                    "response": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "status": {"const": "success"},
                            "receipt": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"code": {"type": "string"}},
                                "required": ["code"],
                            },
                        },
                        "required": ["status", "receipt"],
                    }
                },
                "$ref": "#/$defs/response",
            },
            effects=base_definition.effects,
        ),
    )

    compile_flow(_terminal_flow(), tools=registry)
    with pytest.raises(ValueError, match="does not resolve in tool contract"):
        compile_flow(
            _terminal_flow(result_path="result.receipt.unknown"),
            tools=registry,
        )


def test_compiler_validates_typed_await_enrichment_inputs_and_results() -> None:
    compile_flow(_await_flow(), tools=_payment_registry())

    with pytest.raises(ValueError, match=r"input\['payment_typo'\].*does not exist"):
        compile_flow(
            _await_flow(input_name="payment_typo"),
            tools=_payment_registry(),
        )
    with pytest.raises(ValueError, match=r"set\['receipt_code'\].*does not resolve"):
        compile_flow(
            _await_flow(result_path="$result.unknown"),
            tools=_payment_registry(),
        )


def test_compiler_validates_complete_await_enrichment_projection() -> None:
    with pytest.raises(
        ValueError,
        match=(
            r"\$\.capabilities\.await_external\.on_resume\.enrich\.input bound-key "
            r"projection \['payment_id'\].*complete input contract"
        ),
    ):
        compile_flow(_await_flow(), tools=_conditional_payment_registry())


def test_compiler_rejects_unknown_await_enrichment_tool_with_path() -> None:
    flow_document = _await_flow()
    flow_document["capabilities"]["await_external"]["on_resume"]["enrich"]["tool"] = "unknown_tool"

    with pytest.raises(FlowLinkError) as linking_error:
        compile_flow(flow_document, tools=_payment_registry())

    assert [diagnostic.code for diagnostic in linking_error.value.diagnostics] == [
        "FLOWSPEC_PROFILE_TOOL_UNAVAILABLE"
    ]
    assert linking_error.value.diagnostics[0].path.endswith("/enrich/tool")


def test_default_subflow_manifests_expose_configuration_slots_and_capabilities() -> None:
    definitions = default_subflows().definitions

    assert definitions["address@1"].exposed_slots == frozenset({"address"})
    assert definitions["address@1"].capabilities == frozenset({"geocoding"})
    assert definitions["identification@2"].exposed_slots == frozenset(
        {"brazilian_tax_id", "email", "name"}
    )
    catalog_entry = json.loads(json.dumps(definitions["address@1"].as_dict()))
    assert catalog_entry["exposed_slots"] == ["address"]
    with pytest.raises(TypeError):
        definitions["other@1"] = definitions["address@1"]  # type: ignore[index]


def test_subflow_definition_rejects_invalid_json_schema() -> None:
    with pytest.raises(ValueError, match="configuration_schema is not a valid JSON Schema"):
        SubflowDefinition(
            ref="invalid@1",
            description="Invalid configuration contract.",
            configuration_schema={"type": 42},
        )


def test_compiler_validates_subflow_configuration_at_declaration_path() -> None:
    flow_document = {
        **_collect_flow(),
        "path": [{"use": "address@1"}],
        "uses": [{"ref": "address@1", "with": {"required": "yes"}}],
    }

    with pytest.raises(FlowLinkError) as linking_error:
        compile_flow(flow_document)

    assert [diagnostic.code for diagnostic in linking_error.value.diagnostics] == [
        "FLOWSPEC_PROFILE_SUBFLOW_CONFIGURATION_INVALID"
    ]
    assert linking_error.value.diagnostics[0].path == "/uses/0/with/required"


def test_compiler_rejects_missing_or_orphan_subflow_declaration() -> None:
    missing_declaration_flow = {**_collect_flow(), "path": [{"use": "address@1"}]}
    with pytest.raises(FlowLinkError) as missing_declaration_error:
        compile_flow(missing_declaration_flow)
    assert [diagnostic.code for diagnostic in missing_declaration_error.value.diagnostics] == [
        "FLOWSPEC_SEMANTIC_USE_DECLARATION_MISSING"
    ]

    orphan_declaration_flow = {
        **_collect_flow(),
        "uses": [{"ref": "address@1", "with": {}}],
    }
    with pytest.raises(FlowLinkError) as orphan_declaration_error:
        compile_flow(orphan_declaration_flow)
    assert [diagnostic.code for diagnostic in orphan_declaration_error.value.diagnostics] == [
        "FLOWSPEC_SEMANTIC_ORPHAN_USE_DECLARATION"
    ]


def test_compiler_rejects_unknown_and_duplicate_subflow_anchors() -> None:
    unknown_flow = {
        **_collect_flow(),
        "path": [{"use": "unknown@1"}],
        "uses": [{"ref": "unknown@1"}],
    }
    with pytest.raises(FlowLinkError) as unknown_subflow_error:
        compile_flow(unknown_flow)
    assert [diagnostic.code for diagnostic in unknown_subflow_error.value.diagnostics] == [
        "FLOWSPEC_PROFILE_SUBFLOW_UNAVAILABLE"
    ]

    duplicate_anchor_flow = {
        **_collect_flow(),
        "path": [{"use": "address@1"}, {"use": "address@1"}],
        "uses": [{"ref": "address@1"}],
    }
    with pytest.raises(FlowLinkError) as duplicate_anchor_error:
        compile_flow(duplicate_anchor_flow)
    assert [diagnostic.code for diagnostic in duplicate_anchor_error.value.diagnostics] == [
        "FLOWSPEC_SEMANTIC_DUPLICATE_USE_ANCHOR"
    ]


def test_compiler_checks_subflow_build_against_manifest_exposures() -> None:
    subflow_registry = SubflowRegistry()
    subflow_registry.register(
        _ManifestSubflow(),
        definition=SubflowDefinition(
            ref="manifest@1",
            description="Manifest mismatch fixture.",
            configuration_schema={"type": "object", "additionalProperties": False},
            exposed_slots=frozenset({"external_slot"}),
            exposed_slot_schemas={"external_slot": {}},
        ),
    )
    flow_document = {
        **_collect_flow(),
        "path": [{"use": "manifest@1"}],
        "uses": [{"ref": "manifest@1"}],
    }

    with pytest.raises(ValueError, match=r"manifest declares \[external_slot\]"):
        compile_flow(flow_document, subflows=subflow_registry)
