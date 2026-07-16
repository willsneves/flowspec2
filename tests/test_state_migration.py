"""Deterministic active-state migration contracts."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from flowspec2 import AgentResponse, FlowRuntime, ServiceMetadata, ServiceState
from flowspec2.ir import FlowIR, build_flow_ir
from flowspec2.models import AwaitResumeProvenance
from flowspec2.state_migration import (
    FlowStateContract,
    StateCopy,
    StateDefault,
    StateMigrationError,
    StateMigrationPlan,
    StatePath,
    migrate_service_state,
)

_CREATED_AT = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
_MIGRATED_AT = datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc)


def _flow_ir_pair(flow_document: dict[str, Any]) -> tuple[FlowIR, FlowIR]:
    source_ir = build_flow_ir(flow_document)
    target_document = copy.deepcopy(flow_document)
    target_document["version"] = "1.1.0"
    target_document["slots"]["pothole_type"]["persist"] = "internal"
    return source_ir, build_flow_ir(target_document)


def _state_for_ir(
    flow_ir: FlowIR,
    *,
    data: dict[str, Any] | None = None,
    internal: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    agent_response: AgentResponse | None = None,
) -> ServiceState:
    return ServiceState(
        user_id="citizen-1",
        service_name=flow_ir.flow,
        data=copy.deepcopy(data or {}),
        internal=copy.deepcopy(internal or {}),
        payload=copy.deepcopy(payload or {}),
        metadata=ServiceMetadata(
            created_at=_CREATED_AT,
            updated_at=_CREATED_AT,
            flow_version=flow_ir.version,
            flow_ir_digest=flow_ir.digest,
            dependency_digest=flow_ir.dependency_digest,
            profile_digest=flow_ir.profile_digest,
            saved=True,
        ),
        agent_response=agent_response,
    )


def _complete_plan(
    source_ir: FlowIR,
    target_ir: FlowIR,
) -> StateMigrationPlan:
    return StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(
            StateCopy(StatePath("data", "service"), StatePath("data", "service")),
            StateCopy(
                StatePath("data", "pothole_type"),
                StatePath("internal", "pothole_type"),
            ),
            StateCopy(
                StatePath("internal", "_started"),
                StatePath("internal", "_started"),
            ),
        ),
        defaults=(StateDefault(StatePath("data", "pothole_size"), "Large"),),
        drops=(
            StatePath("data", "pothole_size"),
            StatePath("payload", "latest_input"),
        ),
    )


def test_migration_is_atomic_fresh_deterministic_and_verifiable(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_state = _state_for_ir(
        source_ir,
        data={
            "service": copy.deepcopy(source_ir.to_document()["service"]),
            "pothole_type": "Crater",
            "pothole_size": "Small",
        },
        internal={"_started": True},
        payload={"latest_input": {"text": "large crater"}},
        agent_response=AgentResponse(description="Stale source prompt"),
    )
    source_snapshot = source_state.model_copy(deep=True)
    plan = _complete_plan(source_ir, target_ir)

    first_result = migrate_service_state(
        source_state,
        source_ir=source_ir,
        target_ir=target_ir,
        plan=plan,
        clock=lambda: _MIGRATED_AT,
    )
    second_result = migrate_service_state(
        source_state,
        source_ir=source_ir,
        target_ir=target_ir,
        plan=plan,
        clock=lambda: _MIGRATED_AT,
    )

    assert source_state == source_snapshot
    assert first_result == second_result
    assert first_result.report.canonical_json() == second_result.report.canonical_json()
    assert json.loads(plan.canonical_json())["digest"] == plan.digest
    assert first_result.state.data == {
        "service": target_ir.to_document()["service"],
        "pothole_size": "Large",
    }
    assert first_result.state.internal == {"pothole_type": "Crater", "_started": True}
    assert first_result.state.payload == {}
    assert first_result.state.agent_response is None
    assert first_result.state.metadata.created_at == _CREATED_AT
    assert first_result.state.metadata.updated_at == _MIGRATED_AT
    assert first_result.state.metadata.flow_version == target_ir.version
    assert first_result.state.metadata.flow_ir_digest == target_ir.digest
    assert first_result.state.metadata.dependency_digest == target_ir.dependency_digest
    assert first_result.state.metadata.profile_digest == target_ir.profile_digest
    assert plan.source.ir_format == source_ir.ir_format
    assert plan.target.ir_format == target_ir.ir_format
    assert first_result.report.defaulted == ("/data/pothole_size",)
    assert first_result.report.dropped == (
        "/agent_response",
        "/data/pothole_size",
        "/payload/latest_input",
    )
    first_result.report.verify(
        plan=plan,
        source_state=source_state,
        target_state=first_result.state,
    )

    first_result.state.data["service"]["id"] = "changed"
    assert source_state.data["service"] == source_ir.to_document()["service"]
    with pytest.raises(StateMigrationError, match="does not match"):
        first_result.report.verify(
            plan=plan,
            source_state=source_state,
            target_state=first_result.state,
        )


def test_plan_digest_is_independent_of_declaration_order(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    plan = _complete_plan(source_ir, target_ir)
    reordered_plan = StateMigrationPlan(
        source=plan.source,
        target=plan.target,
        copies=tuple(reversed(plan.copies)),
        defaults=tuple(reversed(plan.defaults)),
        drops=tuple(reversed(plan.drops)),
    )

    assert reordered_plan.digest == plan.digest
    assert reordered_plan.canonical_json() == plan.canonical_json()

    mutable_default = {"nested": ["original"]}
    immutable_default = StateDefault(StatePath("data", "service"), mutable_default)
    mutable_default["nested"].append("mutated")
    first_projection = immutable_default.default_value
    first_projection["nested"].append("locally-mutated")
    assert immutable_default.default_value == {"nested": ["original"]}


async def test_migrated_state_restores_only_against_its_exact_target_runtime(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_state = _state_for_ir(source_ir, data={"pothole_type": "Crater"})
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(
            StateCopy(
                StatePath("data", "pothole_type"),
                StatePath("internal", "pothole_type"),
            ),
        ),
    )
    migrated_state = migrate_service_state(
        source_state,
        source_ir=source_ir,
        target_ir=target_ir,
        plan=plan,
        clock=lambda: _MIGRATED_AT,
    ).state

    accepted_state = await FlowRuntime(target_ir.to_document()).execute(
        migrated_state.model_copy(deep=True),
        {},
    )
    rejected_state = await FlowRuntime(source_ir.to_document()).execute(
        migrated_state.model_copy(deep=True),
        {},
    )

    assert accepted_state.status == "progress"
    assert rejected_state.status == "error"
    assert rejected_state.agent_response is not None
    assert "flow_version" in (rejected_state.agent_response.error_message or "")


@pytest.mark.parametrize(
    ("metadata_field", "invalid_value"),
    [
        ("flow_version", "9.9.9"),
        ("flow_ir_digest", "0" * 64),
        ("dependency_digest", "1" * 64),
        ("profile_digest", "2" * 64),
    ],
)
def test_migration_rejects_stale_source_provenance(
    pothole_document: dict[str, Any],
    metadata_field: str,
    invalid_value: str,
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_state = _state_for_ir(source_ir)
    setattr(source_state.metadata, metadata_field, invalid_value)
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
    )

    with pytest.raises(StateMigrationError, match=metadata_field):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_rejects_plan_contract_that_does_not_pin_supplied_ir(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_contract = replace(FlowStateContract.from_ir(source_ir), source_digest="0" * 64)
    plan = StateMigrationPlan(
        source=source_contract,
        target=FlowStateContract.from_ir(target_ir),
    )

    with pytest.raises(StateMigrationError, match="source_digest"):
        migrate_service_state(
            _state_for_ir(source_ir),
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )

    schema_contract = replace(
        FlowStateContract.from_ir(source_ir),
        state_schema_digest="1" * 64,
    )
    schema_plan = StateMigrationPlan(
        source=schema_contract,
        target=FlowStateContract.from_ir(target_ir),
    )
    with pytest.raises(StateMigrationError, match="state_schema_digest"):
        migrate_service_state(
            _state_for_ir(source_ir),
            source_ir=source_ir,
            target_ir=target_ir,
            plan=schema_plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_source_state_schema_uses_format_checker(streetlight_document: dict[str, Any]) -> None:
    flow_ir = build_flow_ir(streetlight_document)
    source_state = _state_for_ir(flow_ir, data={"email": "not-an-email"})
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(flow_ir),
        target=FlowStateContract.from_ir(flow_ir),
        drops=(StatePath("data", "email"),),
    )

    with pytest.raises(StateMigrationError, match="source state at /data/email"):
        migrate_service_state(
            source_state,
            source_ir=flow_ir,
            target_ir=flow_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )

    invalid_target_default = StateMigrationPlan(
        source=FlowStateContract.from_ir(flow_ir),
        target=FlowStateContract.from_ir(flow_ir),
        defaults=(StateDefault(StatePath("data", "email"), "still-not-an-email"),),
    )
    with pytest.raises(StateMigrationError, match="violates its target schema"):
        migrate_service_state(
            _state_for_ir(flow_ir),
            source_ir=flow_ir,
            target_ir=flow_ir,
            plan=invalid_target_default,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_rejects_missing_and_unaccounted_source_paths(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    missing_plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(
            StateCopy(
                StatePath("data", "pothole_type"),
                StatePath("internal", "pothole_type"),
            ),
        ),
    )
    with pytest.raises(StateMigrationError, match="missing from the active state"):
        migrate_service_state(
            _state_for_ir(source_ir),
            source_ir=source_ir,
            target_ir=target_ir,
            plan=missing_plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_rejects_unknown_target_path_in_closed_partition_schema(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    target_state_schema = target_ir.state_schema()
    target_state_schema["properties"]["internal"]["additionalProperties"] = False
    closed_target_ir = replace(
        target_ir,
        state_schema_json=json.dumps(
            target_state_schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    source_state = _state_for_ir(source_ir, internal={"_started": True})
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(closed_target_ir),
        copies=(
            StateCopy(
                StatePath("internal", "_started"),
                StatePath("internal", "unknown_marker"),
            ),
        ),
    )

    with pytest.raises(StateMigrationError, match="forbidden by the generated state schema"):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=closed_target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )

    unaccounted_state = _state_for_ir(source_ir, internal={"_started": True})
    empty_plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
    )
    with pytest.raises(StateMigrationError, match="explicit copy or drop"):
        migrate_service_state(
            unaccounted_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=empty_plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_rejects_schema_incompatible_copy_even_when_value_would_fit(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_state = _state_for_ir(source_ir, data={"pothole_size": "Small"})
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(
            StateCopy(
                StatePath("data", "pothole_size"),
                StatePath("data", "ticket_data_confirmed"),
            ),
        ),
    )

    with pytest.raises(StateMigrationError, match="not proven schema-compatible"):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_does_not_treat_inclusive_bound_as_exclusive_proof(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_ir = _with_member_schema(source_ir, "score", {"type": "number", "minimum": 0})
    target_ir = _with_member_schema(
        target_ir,
        "score",
        {"type": "number", "exclusiveMinimum": 0},
    )
    source_state = _state_for_ir(source_ir, data={"score": 1})
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(StateCopy(StatePath("data", "score"), StatePath("data", "score")),),
    )

    with pytest.raises(StateMigrationError, match="not proven schema-compatible"):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_proves_target_all_of_sibling_constraints(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_ir = _with_member_schema(source_ir, "label", {"type": "string"})
    target_ir = _with_member_schema(
        target_ir,
        "label",
        {"allOf": [{"type": "string"}], "minLength": 5},
    )
    source_state = _state_for_ir(source_ir, data={"label": "long enough"})
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(StateCopy(StatePath("data", "label"), StatePath("data", "label")),),
    )

    with pytest.raises(StateMigrationError, match="not proven schema-compatible"):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_checks_named_source_properties_against_target_additional_schema(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_ir = _with_member_schema(
        source_ir,
        "record",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"field": {"enum": [1, "currently-compatible"]}},
        },
    )
    target_ir = _with_member_schema(
        target_ir,
        "record",
        {"type": "object", "additionalProperties": {"type": "string"}},
    )
    source_state = _state_for_ir(
        source_ir,
        data={"record": {"field": "currently-compatible"}},
    )
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(StateCopy(StatePath("data", "record"), StatePath("data", "record")),),
    )

    with pytest.raises(StateMigrationError, match="not proven schema-compatible"):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )


def test_migration_rejects_invalid_default_and_conflicting_plan_paths(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    invalid_default_plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        defaults=(StateDefault(StatePath("data", "pothole_size"), "Gigante"),),
    )
    with pytest.raises(StateMigrationError, match="violates its target schema"):
        migrate_service_state(
            _state_for_ir(source_ir),
            source_ir=source_ir,
            target_ir=target_ir,
            plan=invalid_default_plan,
            clock=lambda: _MIGRATED_AT,
        )

    duplicate_target = StatePath("data", "pothole_size")
    with pytest.raises(StateMigrationError, match="one producer"):
        StateMigrationPlan(
            source=FlowStateContract.from_ir(source_ir),
            target=FlowStateContract.from_ir(target_ir),
            defaults=(
                StateDefault(duplicate_target, "Small"),
                StateDefault(duplicate_target, "Large"),
            ),
        )


def test_migration_preserves_safe_local_reference_contracts(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    referenced_schema = {
        "$id": "urn:flowspec2:migration:contact",
        "$defs": {
            "contact": {
                "type": "object",
                "additionalProperties": False,
                "required": ["email"],
                "properties": {"email": {"type": "string", "format": "email"}},
            }
        },
        "$ref": "#/$defs/contact",
    }
    source_ir = _with_member_schema(source_ir, "contact", referenced_schema)
    target_ir = _with_member_schema(
        target_ir,
        "contact",
        {**referenced_schema, "$id": "urn:flowspec2:migration:contact-v2"},
    )
    source_state = _state_for_ir(
        source_ir,
        data={"contact": {"email": "citizen@example.com"}},
    )
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
        copies=(StateCopy(StatePath("data", "contact"), StatePath("data", "contact")),),
    )

    migrated = migrate_service_state(
        source_state,
        source_ir=source_ir,
        target_ir=target_ir,
        plan=plan,
        clock=lambda: _MIGRATED_AT,
    )

    assert migrated.state.data == {"contact": {"email": "citizen@example.com"}}


def test_migration_rejects_ambiguous_accepted_resume_state(
    pothole_document: dict[str, Any],
) -> None:
    source_ir, target_ir = _flow_ir_pair(pothole_document)
    source_state = _state_for_ir(source_ir)
    source_state.metadata.await_resume = AwaitResumeProvenance(
        step="authenticate",
        version="1",
        digest="a" * 64,
        correlation_path="$token.id",
        correlation_value="request-1",
    )
    plan = StateMigrationPlan(
        source=FlowStateContract.from_ir(source_ir),
        target=FlowStateContract.from_ir(target_ir),
    )

    with pytest.raises(StateMigrationError, match="external-resume"):
        migrate_service_state(
            source_state,
            source_ir=source_ir,
            target_ir=target_ir,
            plan=plan,
            clock=lambda: _MIGRATED_AT,
        )


def _with_member_schema(flow_ir: FlowIR, member: str, member_schema: dict[str, Any]) -> FlowIR:
    state_schema = flow_ir.state_schema()
    state_schema["properties"]["data"]["properties"][member] = member_schema
    return replace(
        flow_ir,
        state_schema_json=json.dumps(
            state_schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
