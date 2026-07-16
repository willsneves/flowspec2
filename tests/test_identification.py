"""identification@2 — skip handling for the optional contact slots."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import require_agent_response, step

from flowspec2 import FlowRuntime, default_tool_registry
from flowspec2.models import CORRECTION_REQUESTED_INTERNAL_KEY
from flowspec2.subflows.identification import _is_skip


def _govbr_resume_contract() -> dict[str, Any]:
    return {
        "version": "1",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "brazilian_tax_id": {"type": "string", "pattern": "^[0-9]{11}$"},
                "name": {"type": "string", "minLength": 2},
                "email": {"type": "string", "format": "email"},
            },
            "required": ["brazilian_tax_id"],
        },
        "correlation": "$token.brazilian_tax_id",
        "duplicate": "ignore",
        "late": "reject",
    }


def _identification_flow_document(
    *,
    required: bool = True,
    methods: list[str] | None = None,
    max_attempts: int = 1,
    on_exhaust: str = "reask",
) -> dict[str, Any]:
    return {
        "schema": "flowspec/2",
        "flow": "identification_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise identification exhaustion."},
        "domains": {"Placeholder": {"type": "categorical", "values": ["unused"]}},
        "path": [{"use": "identification@2"}],
        "uses": [
            {
                "ref": "identification@2",
                "with": {
                    "required": required,
                    "methods": methods or ["brazilian_tax_id", "govbr", "anonymous"],
                    "max_attempts": max_attempts,
                    "on_exhaust": on_exhaust,
                },
            }
        ],
    }


def test_is_skip_matches_exact_tokens_only() -> None:
    # Explicit skip words are case-insensitive.
    assert _is_skip("skip")
    assert _is_skip("SKIP")
    assert _is_skip("no")
    assert _is_skip("none")
    assert _is_skip("pass")


def test_is_skip_never_triggers_on_substrings() -> None:
    # a real value that merely *contains* a skip token must NOT be read as a skip
    assert not _is_skip("skipper@example.com")  # contains "skip"
    assert not _is_skip("none@example.com")  # contains "none"
    assert not _is_skip("Mary Smith")
    assert not _is_skip("11144477735")


async def test_skip_omits_optional_email_and_name(streetlight: FlowRuntime) -> None:
    st = await step(streetlight, None, {})  # flow_sent
    st = await step(
        streetlight,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "single",
            "location": "Street",
        },
    )
    st = await step(streetlight, st, {"address": "Flower Street, 100, Downtown"})
    st = await step(streetlight, st, {"confirmation": "yes"})  # confirm address
    st = await step(streetlight, st, {"reference_point": "across from the bakery"})
    st = await step(streetlight, st, {"identification_method": "brazilian_tax_id"})
    st = await step(streetlight, st, {"brazilian_tax_id": "11144477735"})

    # At the email step, "skip" omits it deterministically instead of validating it.
    assert "email" in require_agent_response(st).description.lower()
    st = await step(streetlight, st, {"email": "skip"})
    assert st.data.get("email_processed") is True
    assert "email" not in st.data  # nothing stored on skip

    # and the name step likewise
    assert "name" in require_agent_response(st).description.lower()
    st = await step(streetlight, st, {"name": "skip"})
    assert st.data.get("name_processed") is True
    assert "name" not in st.data


async def test_reask_exhaustion_resets_method_attempt_budget() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="reask"))

    state = await runtime.execute(
        runtime.new_state("identification-reask"),
        {"identification_method": "invalid"},
    )

    assert state.status == "progress"
    assert "_attempts_method" not in state.data
    assert require_agent_response(state).error_message == (
        "maximum attempts reached; let us try again"
    )


async def test_required_identification_rejects_anonymous_method() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(required=True, max_attempts=2, on_exhaust="reask")
    )

    state = await runtime.execute(
        runtime.new_state("required-identification"),
        {"identification_method": "anonymous"},
    )

    assert state.status == "progress"
    assert "identification_method" not in state.data
    assert state.data["_attempts_method"] == 1
    response = require_agent_response(state)
    assert response.interactive is not None
    assert {button["id"] for button in response.interactive["buttons"]} == {
        "brazilian_tax_id",
        "govbr",
    }


async def test_skip_exhaustion_resolves_to_anonymous() -> None:
    runtime = FlowRuntime(_identification_flow_document(required=False, on_exhaust="skip"))

    state = await runtime.execute(
        runtime.new_state("identification-skip"),
        {"identification_method": "invalid"},
    )

    assert state.status == "completed"
    assert state.data["identification_method"] == "anonymous"
    assert state.data["identification_skipped"] is True
    assert "_attempts_method" not in state.data


async def test_default_exhaustion_uses_the_first_configured_method() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="default"))

    state = await runtime.execute(
        runtime.new_state("identification-method-default"),
        {"identification_method": "invalid"},
    )

    assert state.status == "progress"
    assert state.data["identification_method"] == "brazilian_tax_id"
    assert "identification_skipped" not in state.data
    assert "brazilian tax id" in require_agent_response(state).description.lower()


async def test_default_exhaustion_resolves_invalid_brazilian_tax_id_to_anonymous() -> None:
    runtime = FlowRuntime(_identification_flow_document(required=False, on_exhaust="default"))
    state = await runtime.execute(
        runtime.new_state("identification-brazilian_tax_id-default"),
        {"identification_method": "brazilian_tax_id"},
    )

    state = await runtime.execute(state, {"brazilian_tax_id": "invalid"})

    assert state.status == "completed"
    assert state.data["identification_method"] == "anonymous"
    assert state.data["identification_skipped"] is True


async def test_handoff_exhaustion_pauses_brazilian_tax_id_collection() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="handoff"))
    state = await runtime.execute(
        runtime.new_state("identification-brazilian_tax_id-handoff"),
        {"identification_method": "brazilian_tax_id"},
    )

    state = await runtime.execute(state, {"brazilian_tax_id": "invalid"})

    assert state.status == "progress"
    assert require_agent_response(state).description == ("I will transfer you to a support agent.")
    assert state.data["identification_method"] == "brazilian_tax_id"
    assert "identification_skipped" not in state.data


async def test_handoff_exhaustion_pauses_optional_contact_collection() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="handoff"))
    state = await runtime.execute(
        runtime.new_state("identification-email-handoff"),
        {"identification_method": "brazilian_tax_id"},
    )
    state = await runtime.execute(state, {"brazilian_tax_id": "52998224725"})

    state = await runtime.execute(state, {"email": "invalid"})

    assert state.status == "progress"
    assert require_agent_response(state).description == ("I will transfer you to a support agent.")
    assert "email_processed" not in state.data


async def test_end_exhaustion_completes_with_a_correlated_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="END"))

    with caplog.at_level(logging.WARNING, logger="flowspec2.subflows.identification"):
        state = await runtime.execute(
            runtime.new_state("identification-end"),
            {"identification_method": "invalid"},
        )

    assert state.status == "completed"
    response = require_agent_response(state)
    assert response.log_id is not None
    assert response.description == "I could not complete identification. Try again later."
    matching_records = [
        record
        for record in caplog.records
        if record.message == "Flow stopped after identification attempts were exhausted"
    ]
    assert len(matching_records) == 1
    assert getattr(matching_records[0], "log_id", None) == response.log_id


async def test_correcting_brazilian_tax_id_invalidates_contacts_derived_from_the_prior_lookup() -> (
    None
):
    registry = default_tool_registry()

    async def brazilian_tax_id_lookup(brazilian_tax_id: str) -> dict[str, Any]:
        if brazilian_tax_id == "52998224725":
            return {
                "status": "ok",
                "email": "registry@example.com",
                "name": "Registry Citizen",
                "phones": [],
            }
        return {"status": "ok", "email": "", "name": "", "phones": []}

    registry.register("brazilian_tax_id_lookup", brazilian_tax_id_lookup)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("derived-contact-correction"),
        {"identification_method": "brazilian_tax_id"},
    )
    state = await runtime.execute(state, {"brazilian_tax_id": "52998224725"})

    assert state.data["email"] == "registry@example.com"
    assert state.data["name"] == "Registry Citizen"
    assert state.internal["_brazilian_tax_id_lookup_derived:email"] is True
    assert state.internal["_brazilian_tax_id_lookup_derived:name"] is True

    state.internal[CORRECTION_REQUESTED_INTERNAL_KEY] = "brazilian_tax_id"
    state = await runtime.execute(state, {"brazilian_tax_id": "11144477735"})

    assert state.data["brazilian_tax_id"] == "11144477735"
    assert "email" not in state.data
    assert "email_processed" not in state.data
    assert "name" not in state.data
    assert "name_processed" not in state.data
    assert "_brazilian_tax_id_lookup_derived:email" not in state.internal
    assert "_brazilian_tax_id_lookup_derived:name" not in state.internal
    assert "email" in require_agent_response(state).description.lower()


async def test_correcting_brazilian_tax_id_preserves_contacts_supplied_by_the_citizen() -> None:
    registry = default_tool_registry()

    async def brazilian_tax_id_lookup(brazilian_tax_id: str) -> dict[str, Any]:
        if brazilian_tax_id == "11144477735":
            return {
                "status": "ok",
                "email": "replacement@example.com",
                "name": "Replacement Citizen",
                "phones": [],
            }
        return {"status": "ok", "email": "", "name": "", "phones": []}

    registry.register("brazilian_tax_id_lookup", brazilian_tax_id_lookup)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("citizen-contact-correction"),
        {"identification_method": "brazilian_tax_id"},
    )
    state = await runtime.execute(state, {"brazilian_tax_id": "52998224725"})
    state = await runtime.execute(state, {"email": "citizen@example.com"})
    state = await runtime.execute(state, {"name": "Citizen Provided"})

    state.internal[CORRECTION_REQUESTED_INTERNAL_KEY] = "brazilian_tax_id"
    state = await runtime.execute(state, {"brazilian_tax_id": "11144477735"})

    assert state.data["brazilian_tax_id"] == "11144477735"
    assert state.data["email"] == "citizen@example.com"
    assert state.data["email_processed"] is True
    assert state.data["name"] == "Citizen Provided"
    assert state.data["name_processed"] is True
    assert "_brazilian_tax_id_lookup_derived:email" not in state.internal
    assert "_brazilian_tax_id_lookup_derived:name" not in state.internal


@pytest.mark.parametrize(
    "invalid_token",
    [
        {"brazilian_tax_id": "invalid", "name": "Citizen Name", "email": "citizen@example.com"},
        {"brazilian_tax_id": "52998224725", "name": "A", "email": "citizen@example.com"},
        {"brazilian_tax_id": "52998224725", "name": "Citizen Name", "email": "invalid"},
    ],
)
async def test_legacy_govbr_token_validates_every_identity_before_atomic_commit(
    invalid_token: dict[str, str],
) -> None:
    registry = default_tool_registry()
    enrichment_calls = 0

    async def get_user_info(brazilian_tax_id: str) -> dict[str, Any]:
        nonlocal enrichment_calls
        enrichment_calls += 1
        return {"status": "ok", "phones": [brazilian_tax_id]}

    registry.register("get_user_info", get_user_info)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("legacy-govbr-validation"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(state, {"govbr_token": invalid_token})

    assert state.status == "progress"
    assert state.data["identification_method"] == "brazilian_tax_id"
    assert enrichment_calls == 0
    assert "brazilian_tax_id" not in state.data
    assert "name" not in state.data
    assert "email" not in state.data
    assert "govbr_authenticated" not in state.data
    assert require_agent_response(state).log_id is not None

    state = await runtime.execute(state, {"brazilian_tax_id": "52998224725"})

    assert state.data["brazilian_tax_id"] == "52998224725"
    assert state.status == "progress"


async def test_legacy_govbr_token_commits_normalized_identity_together() -> None:
    runtime = FlowRuntime(_identification_flow_document())
    state = await runtime.execute(
        runtime.new_state("legacy-govbr-success"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(
        state,
        {
            "govbr_token": {
                "brazilian_tax_id": "529.982.247-25",
                "name": "  Citizen Name  ",
                "email": "  CITIZEN@EXAMPLE.COM  ",
            }
        },
    )

    assert state.status == "completed"
    assert state.data["brazilian_tax_id"] == "52998224725"
    assert state.data["name"] == "Citizen Name"
    assert state.data["email"] == "citizen@example.com"
    assert state.data["name_processed"] is True
    assert state.data["email_processed"] is True
    assert state.data["govbr_authenticated"] is True
    assert state.data["identity_verified"] is True


@pytest.mark.parametrize(
    ("lookup_contacts", "accepted_slot", "accepted_value", "rejected_slot"),
    [
        (
            {"email": "  REGISTRY@EXAMPLE.COM  ", "name": "A"},
            "email",
            "registry@example.com",
            "name",
        ),
    ],
)
async def test_brazilian_tax_id_lookup_validates_each_optional_contact_before_commit(
    lookup_contacts: dict[str, str],
    accepted_slot: str,
    accepted_value: str,
    rejected_slot: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = default_tool_registry()

    async def brazilian_tax_id_lookup(brazilian_tax_id: str) -> dict[str, Any]:
        del brazilian_tax_id
        return {
            "status": "ok",
            "phones": [],
            **lookup_contacts,
        }

    registry.register("brazilian_tax_id_lookup", brazilian_tax_id_lookup)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("lookup-contact-validation"),
        {"identification_method": "brazilian_tax_id"},
    )

    with caplog.at_level(logging.WARNING, logger="flowspec2.subflows.identification"):
        state = await runtime.execute(state, {"brazilian_tax_id": "52998224725"})

    assert state.data["brazilian_tax_id"] == "52998224725"
    assert state.data[accepted_slot] == accepted_value
    assert state.data[f"{accepted_slot}_processed"] is True
    assert rejected_slot not in state.data
    assert f"{rejected_slot}_processed" not in state.data
    assert state.data["identity_verified"] is True
    matching_records = [
        log_record
        for log_record in caplog.records
        if log_record.message == "Brazilian tax ID lookup contact failed slot validation"
        and getattr(log_record, "slot", None) == rejected_slot
    ]
    assert len(matching_records) == 1
    assert getattr(matching_records[0], "log_id", None) is not None


def test_required_identification_rejects_anonymous_only_default_configuration() -> None:
    flow_document = _identification_flow_document(
        required=False,
        methods=["anonymous"],
        on_exhaust="default",
    )
    flow_document["config"] = {"identification_required": True}
    del flow_document["uses"][0]["with"]["required"]

    with pytest.raises(ValueError, match="requires an eligible method"):
        FlowRuntime(flow_document)


async def test_optional_identification_without_anonymous_method_never_selects_it() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(
            required=False,
            methods=["govbr", "brazilian_tax_id"],
            on_exhaust="default",
        )
    )

    state = await runtime.execute(runtime.new_state("no-anonymous-method"), {})

    response = require_agent_response(state)
    assert response.interactive is not None
    assert {button["id"] for button in response.interactive["buttons"]} == {
        "govbr",
        "brazilian_tax_id",
    }
    assert "identification_method" not in state.data

    state = await runtime.execute(state, {"identification_method": "anonymous"})

    assert state.data["identification_method"] == "govbr"
    assert "identification_skipped" not in state.data
    assert (require_agent_response(state).interactive or {}).get("out_of_band_sent") is True


async def test_legacy_invalid_govbr_token_without_brazilian_tax_id_method_retries_govbr() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(
            methods=["govbr"],
            on_exhaust="default",
        )
    )
    state = await runtime.execute(
        runtime.new_state("govbr-only-invalid-token"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(
        state,
        {"govbr_token": {"brazilian_tax_id": "invalid", "name": "Citizen Name"}},
    )

    assert state.status == "progress"
    assert state.data["identification_method"] == "govbr"
    assert "brazilian_tax_id" not in state.data
    assert "identification_skipped" not in state.data
    response = require_agent_response(state)
    assert response.log_id is not None
    assert "try again" in response.description.lower()

    state = await runtime.execute(state, {})

    assert state.data["identification_method"] == "govbr"
    assert (require_agent_response(state).interactive or {}).get("out_of_band_sent") is True


async def test_legacy_brazilian_tax_id_request_is_rejected_when_method_is_disabled() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(
            methods=["govbr"],
            max_attempts=2,
            on_exhaust="reask",
        )
    )
    state = await runtime.execute(
        runtime.new_state("govbr-only-text-recovery"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(state, {"message": "I prefer brazilian_tax_id"})

    assert state.data["identification_method"] == "govbr"
    assert "brazilian_tax_id" not in state.data
    response = require_agent_response(state)
    assert response.interactive is not None
    assert [button["id"] for button in response.interactive["buttons"]] == ["govbr"]


async def test_brazilian_tax_id_skip_without_anonymous_method_uses_configured_default() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(
            required=False,
            methods=["govbr", "brazilian_tax_id"],
            on_exhaust="default",
        )
    )
    state = await runtime.execute(
        runtime.new_state("brazilian_tax_id-skip-without-anonymous"),
        {"identification_method": "brazilian_tax_id"},
    )

    state = await runtime.execute(state, {"brazilian_tax_id": "skip"})

    assert state.data["identification_method"] == "govbr"
    assert "identification_skipped" not in state.data
    assert "brazilian_tax_id" not in state.data
    assert (require_agent_response(state).interactive or {}).get("out_of_band_sent") is True


def test_await_recovery_rejects_disabled_brazilian_tax_id_target() -> None:
    flow_document = _identification_flow_document(methods=["govbr"])
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "step": "authenticate_govbr",
            "resume_on": "govbr_token",
            "resume": _govbr_resume_contract(),
            "recovery": {
                "switch": {"goto": "collect_tax_id"},
            },
        }
    }

    with pytest.raises(ValueError, match="routes to disabled method 'brazilian_tax_id'"):
        FlowRuntime(flow_document)


async def test_await_recovery_without_brazilian_tax_id_can_return_to_method_selection() -> None:
    flow_document = _identification_flow_document(methods=["govbr"])
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "step": "authenticate_govbr",
            "resume_on": "govbr_token",
            "resume": _govbr_resume_contract(),
            "recovery": {
                "switch": {"goto": "select_identification_method"},
            },
        }
    }
    runtime = FlowRuntime(flow_document)
    state = await runtime.execute(
        runtime.new_state("govbr-neutral-recovery"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(state, {"govbr_token": {"name": "Untrusted"}})

    assert "identification_method" not in state.data
    assert "brazilian_tax_id" not in state.data
    response = require_agent_response(state)
    assert response.interactive is not None
    assert [button["id"] for button in response.interactive["buttons"]] == ["govbr"]
