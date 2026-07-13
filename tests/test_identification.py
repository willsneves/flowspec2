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
                    "methods": methods or ["cpf", "govbr", "anonimo"],
                    "max_attempts": max_attempts,
                    "on_exhaust": on_exhaust,
                },
            }
        ],
    }


def test_is_skip_matches_exact_tokens_only() -> None:
    # explicit skip words (accent/case-insensitive)
    assert _is_skip("pular")
    assert _is_skip("PULAR")
    assert _is_skip("não")  # normalizes to "nao"
    assert _is_skip("nenhum")
    assert _is_skip("passar")


def test_is_skip_never_triggers_on_substrings() -> None:
    # a real value that merely *contains* a skip token must NOT be read as a skip
    assert not _is_skip("skipper@example.com")  # contains "skip"
    assert not _is_skip("ana@nenhum.com")  # contains "nenhum"
    assert not _is_skip("Maria Aparecida")
    assert not _is_skip("11144477735")


async def test_pular_skips_optional_email_and_name(luminaria: FlowRuntime) -> None:
    st = await step(luminaria, None, {})  # flow_sent
    st = await step(
        luminaria,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Apagada",
            "qty_pattern": "uma",
            "location": "Rua",
        },
    )
    st = await step(luminaria, st, {"address": "Rua das Flores, 100, Centro"})
    st = await step(luminaria, st, {"confirmacao": "sim"})  # confirm address
    st = await step(luminaria, st, {"ponto_referencia": "em frente à padaria"})
    st = await step(luminaria, st, {"identification_method": "cpf"})
    st = await step(luminaria, st, {"cpf": "11144477735"})

    # at the e-mail step: "pular" must skip it deterministically, not validate
    assert "e-mail" in require_agent_response(st).description.lower()
    st = await step(luminaria, st, {"email": "pular"})
    assert st.data.get("email_processed") is True
    assert "email" not in st.data  # nothing stored on skip

    # and the name step likewise
    assert "nome" in require_agent_response(st).description.lower()
    st = await step(luminaria, st, {"name": "pular"})
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
        "máximo de tentativas — vamos tentar de novo"
    )


async def test_required_identification_rejects_anonymous_method() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(required=True, max_attempts=2, on_exhaust="reask")
    )

    state = await runtime.execute(
        runtime.new_state("required-identification"),
        {"identification_method": "anonimo"},
    )

    assert state.status == "progress"
    assert "identification_method" not in state.data
    assert state.data["_attempts_method"] == 1
    response = require_agent_response(state)
    assert response.interactive is not None
    assert {button["id"] for button in response.interactive["buttons"]} == {"cpf", "govbr"}


async def test_skip_exhaustion_resolves_to_anonymous() -> None:
    runtime = FlowRuntime(_identification_flow_document(required=False, on_exhaust="skip"))

    state = await runtime.execute(
        runtime.new_state("identification-skip"),
        {"identification_method": "invalid"},
    )

    assert state.status == "completed"
    assert state.data["identification_method"] == "anonimo"
    assert state.data["identificacao_pulada"] is True
    assert "_attempts_method" not in state.data


async def test_default_exhaustion_uses_the_first_configured_method() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="default"))

    state = await runtime.execute(
        runtime.new_state("identification-method-default"),
        {"identification_method": "invalid"},
    )

    assert state.status == "progress"
    assert state.data["identification_method"] == "cpf"
    assert "identificacao_pulada" not in state.data
    assert "cpf" in require_agent_response(state).description.lower()


async def test_default_exhaustion_resolves_invalid_cpf_to_anonymous() -> None:
    runtime = FlowRuntime(_identification_flow_document(required=False, on_exhaust="default"))
    state = await runtime.execute(
        runtime.new_state("identification-cpf-default"),
        {"identification_method": "cpf"},
    )

    state = await runtime.execute(state, {"cpf": "invalid"})

    assert state.status == "completed"
    assert state.data["identification_method"] == "anonimo"
    assert state.data["identificacao_pulada"] is True


async def test_handoff_exhaustion_pauses_cpf_collection() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="handoff"))
    state = await runtime.execute(
        runtime.new_state("identification-cpf-handoff"),
        {"identification_method": "cpf"},
    )

    state = await runtime.execute(state, {"cpf": "invalid"})

    assert state.status == "progress"
    assert require_agent_response(state).description == (
        "Vou te encaminhar para um atendente da Central 1746."
    )
    assert state.data["identification_method"] == "cpf"
    assert "identificacao_pulada" not in state.data


async def test_handoff_exhaustion_pauses_optional_contact_collection() -> None:
    runtime = FlowRuntime(_identification_flow_document(on_exhaust="handoff"))
    state = await runtime.execute(
        runtime.new_state("identification-email-handoff"),
        {"identification_method": "cpf"},
    )
    state = await runtime.execute(state, {"cpf": "52998224725"})

    state = await runtime.execute(state, {"email": "invalid"})

    assert state.status == "progress"
    assert require_agent_response(state).description == (
        "Vou te encaminhar para um atendente da Central 1746."
    )
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
    assert (
        response.description == "Não consegui concluir a identificação. Tente novamente mais tarde."
    )
    matching_records = [
        record
        for record in caplog.records
        if record.message == "Flow stopped after identification attempts were exhausted"
    ]
    assert len(matching_records) == 1
    assert getattr(matching_records[0], "log_id", None) == response.log_id


async def test_correcting_cpf_invalidates_contacts_derived_from_the_prior_lookup() -> None:
    registry = default_tool_registry()

    async def cpf_lookup(cpf: str) -> dict[str, Any]:
        if cpf == "52998224725":
            return {
                "status": "ok",
                "email": "registry@example.com",
                "name": "Registry Citizen",
                "phones": [],
            }
        return {"status": "ok", "email": "", "name": "", "phones": []}

    registry.register("cpf_lookup", cpf_lookup)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("derived-contact-correction"),
        {"identification_method": "cpf"},
    )
    state = await runtime.execute(state, {"cpf": "52998224725"})

    assert state.data["email"] == "registry@example.com"
    assert state.data["name"] == "Registry Citizen"
    assert state.internal["_cpf_lookup_derived:email"] is True
    assert state.internal["_cpf_lookup_derived:name"] is True

    state.internal[CORRECTION_REQUESTED_INTERNAL_KEY] = "cpf"
    state = await runtime.execute(state, {"cpf": "11144477735"})

    assert state.data["cpf"] == "11144477735"
    assert "email" not in state.data
    assert "email_processed" not in state.data
    assert "name" not in state.data
    assert "name_processed" not in state.data
    assert "_cpf_lookup_derived:email" not in state.internal
    assert "_cpf_lookup_derived:name" not in state.internal
    assert "e-mail" in require_agent_response(state).description.lower()


async def test_correcting_cpf_preserves_contacts_supplied_by_the_citizen() -> None:
    registry = default_tool_registry()

    async def cpf_lookup(cpf: str) -> dict[str, Any]:
        if cpf == "11144477735":
            return {
                "status": "ok",
                "email": "replacement@example.com",
                "name": "Replacement Citizen",
                "phones": [],
            }
        return {"status": "ok", "email": "", "name": "", "phones": []}

    registry.register("cpf_lookup", cpf_lookup)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("citizen-contact-correction"),
        {"identification_method": "cpf"},
    )
    state = await runtime.execute(state, {"cpf": "52998224725"})
    state = await runtime.execute(state, {"email": "citizen@example.com"})
    state = await runtime.execute(state, {"name": "Citizen Provided"})

    state.internal[CORRECTION_REQUESTED_INTERNAL_KEY] = "cpf"
    state = await runtime.execute(state, {"cpf": "11144477735"})

    assert state.data["cpf"] == "11144477735"
    assert state.data["email"] == "citizen@example.com"
    assert state.data["email_processed"] is True
    assert state.data["name"] == "Citizen Provided"
    assert state.data["name_processed"] is True
    assert "_cpf_lookup_derived:email" not in state.internal
    assert "_cpf_lookup_derived:name" not in state.internal


@pytest.mark.parametrize(
    "invalid_token",
    [
        {"cpf": "invalid", "nome": "Citizen Name", "email": "citizen@example.com"},
        {"cpf": "52998224725", "nome": "A", "email": "citizen@example.com"},
        {"cpf": "52998224725", "nome": "Citizen Name", "email": "invalid"},
    ],
)
async def test_legacy_govbr_token_validates_every_identity_before_atomic_commit(
    invalid_token: dict[str, str],
) -> None:
    registry = default_tool_registry()
    enrichment_calls = 0

    async def get_user_info(cpf: str) -> dict[str, Any]:
        nonlocal enrichment_calls
        enrichment_calls += 1
        return {"status": "ok", "phones": [cpf]}

    registry.register("get_user_info", get_user_info)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("legacy-govbr-validation"),
        {"identification_method": "govbr"},
    )

    state = await runtime.execute(state, {"govbr_token": invalid_token})

    assert state.status == "progress"
    assert state.data["identification_method"] == "cpf"
    assert enrichment_calls == 0
    assert "cpf" not in state.data
    assert "name" not in state.data
    assert "email" not in state.data
    assert "govbr_authenticated" not in state.data
    assert require_agent_response(state).log_id is not None

    state = await runtime.execute(state, {"cpf": "52998224725"})

    assert state.data["cpf"] == "52998224725"
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
                "cpf": "529.982.247-25",
                "nome": "  Citizen Name  ",
                "email": "  CITIZEN@EXAMPLE.COM  ",
            }
        },
    )

    assert state.status == "completed"
    assert state.data["cpf"] == "52998224725"
    assert state.data["name"] == "Citizen Name"
    assert state.data["email"] == "citizen@example.com"
    assert state.data["name_processed"] is True
    assert state.data["email_processed"] is True
    assert state.data["govbr_authenticated"] is True
    assert state.data["cadastro_verificado"] is True


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
async def test_cpf_lookup_validates_each_optional_contact_before_commit(
    lookup_contacts: dict[str, str],
    accepted_slot: str,
    accepted_value: str,
    rejected_slot: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = default_tool_registry()

    async def cpf_lookup(cpf: str) -> dict[str, Any]:
        del cpf
        return {
            "status": "ok",
            "phones": [],
            **lookup_contacts,
        }

    registry.register("cpf_lookup", cpf_lookup)
    runtime = FlowRuntime(_identification_flow_document(), tools=registry)
    state = await runtime.execute(
        runtime.new_state("lookup-contact-validation"),
        {"identification_method": "cpf"},
    )

    with caplog.at_level(logging.WARNING, logger="flowspec2.subflows.identification"):
        state = await runtime.execute(state, {"cpf": "52998224725"})

    assert state.data["cpf"] == "52998224725"
    assert state.data[accepted_slot] == accepted_value
    assert state.data[f"{accepted_slot}_processed"] is True
    assert rejected_slot not in state.data
    assert f"{rejected_slot}_processed" not in state.data
    assert state.data["cadastro_verificado"] is True
    matching_records = [
        log_record
        for log_record in caplog.records
        if log_record.message == "CPF lookup contact failed slot validation"
        and getattr(log_record, "slot", None) == rejected_slot
    ]
    assert len(matching_records) == 1
    assert getattr(matching_records[0], "log_id", None) is not None


def test_required_identification_rejects_anonymous_only_default_configuration() -> None:
    flow_document = _identification_flow_document(
        required=False,
        methods=["anonimo"],
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
            methods=["govbr", "cpf"],
            on_exhaust="default",
        )
    )

    state = await runtime.execute(runtime.new_state("no-anonymous-method"), {})

    response = require_agent_response(state)
    assert response.interactive is not None
    assert {button["id"] for button in response.interactive["buttons"]} == {
        "govbr",
        "cpf",
    }
    assert "identification_method" not in state.data

    state = await runtime.execute(state, {"identification_method": "anonimo"})

    assert state.data["identification_method"] == "govbr"
    assert "identificacao_pulada" not in state.data
    assert (require_agent_response(state).interactive or {}).get("out_of_band_sent") is True


async def test_legacy_invalid_govbr_token_without_cpf_method_retries_govbr() -> None:
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
        {"govbr_token": {"cpf": "invalid", "nome": "Citizen Name"}},
    )

    assert state.status == "progress"
    assert state.data["identification_method"] == "govbr"
    assert "cpf" not in state.data
    assert "identificacao_pulada" not in state.data
    response = require_agent_response(state)
    assert response.log_id is not None
    assert "tente novamente" in response.description.lower()

    state = await runtime.execute(state, {})

    assert state.data["identification_method"] == "govbr"
    assert (require_agent_response(state).interactive or {}).get("out_of_band_sent") is True


async def test_legacy_cpf_request_is_rejected_when_method_is_disabled() -> None:
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

    state = await runtime.execute(state, {"message": "prefiro cpf"})

    assert state.data["identification_method"] == "govbr"
    assert "cpf" not in state.data
    response = require_agent_response(state)
    assert response.interactive is not None
    assert [button["id"] for button in response.interactive["buttons"]] == ["govbr"]


async def test_cpf_skip_without_anonymous_method_uses_configured_default() -> None:
    runtime = FlowRuntime(
        _identification_flow_document(
            required=False,
            methods=["govbr", "cpf"],
            on_exhaust="default",
        )
    )
    state = await runtime.execute(
        runtime.new_state("cpf-skip-without-anonymous"),
        {"identification_method": "cpf"},
    )

    state = await runtime.execute(state, {"cpf": "pular"})

    assert state.data["identification_method"] == "govbr"
    assert "identificacao_pulada" not in state.data
    assert "cpf" not in state.data
    assert (require_agent_response(state).interactive or {}).get("out_of_band_sent") is True


def test_await_recovery_rejects_disabled_cpf_target() -> None:
    flow_document = _identification_flow_document(methods=["govbr"])
    flow_document["capabilities"] = {
        "await_external": {
            "kind": "cta_url",
            "step": "authenticate_govbr",
            "resume_on": "govbr_token",
            "resume": _govbr_resume_contract(),
            "recovery": {
                "switch": {"goto": "collect_cpf"},
            },
        }
    }

    with pytest.raises(ValueError, match="routes to disabled method 'cpf'"):
        FlowRuntime(flow_document)


async def test_await_recovery_without_cpf_can_return_to_method_selection() -> None:
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

    state = await runtime.execute(state, {"govbr_token": {"nome": "Untrusted"}})

    assert "identification_method" not in state.data
    assert "cpf" not in state.data
    response = require_agent_response(state)
    assert response.interactive is not None
    assert [button["id"] for button in response.interactive["buttons"]] == ["govbr"]
