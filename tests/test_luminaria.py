"""Drive the luminária flow turn-by-turn through the compiled LangGraph graph."""

from __future__ import annotations

from conftest import require_agent_response, step


async def test_auto_flow_then_submission_fills_via_alias_map(luminaria):
    st = await step(luminaria, None, {})
    assert (require_agent_response(st).interactive or {}).get("status") == "flow_sent"

    st = await step(
        luminaria,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Apagada",
            "qty_pattern": "bloco",
            "location": "Rua",
        },
    )
    # alias_map fan-out: qty_pattern=bloco -> quantidade=grupo + intercaladas=bloco
    assert st.data["luminaria_defeito"] == "Apagada"
    assert st.data["luminaria_quantidade"] == "grupo"
    assert st.data["luminaria_intercaladas_bloco"] == "bloco"
    # derived lookup table (matches _classifica_defeito)
    assert st.data["luminaria_defeito_classificado"] == "Bloco ou grupo de luminárias apagadas"
    # summary was satisfied by its explicit skip_when without inventing consent
    assert "service_confirmed" not in st.data
    assert st.internal["_slot_skipped:service_confirmed"] is True
    # now collecting the address
    assert "endereço" in require_agent_response(st).description.lower()


async def test_full_happy_path_opens_ticket_and_resets(luminaria):
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
    # quadra is gated OFF for a Rua address -> we should now be at reference point
    st = await step(luminaria, st, {"ponto_referencia": "em frente à padaria"})
    st = await step(luminaria, st, {"identification_method": "anonimo"})
    st = await step(luminaria, st, {"confirmacao": "sim"})  # confirm ticket -> open

    assert st.status == "completed"
    assert st.data.get("protocol_id", "").startswith("SGRC-")
    assert st.data.get("ticket_created") is True
    assert st.data.get("_reset_on_next_call") is True

    # next turn wipes and starts fresh (auto_flow fires again)
    st = await step(luminaria, st, {})
    assert (require_agent_response(st).interactive or {}).get("status") == "flow_sent"
    assert "luminaria_defeito" not in st.data


async def test_praca_address_opens_the_quadra_gate(luminaria):
    st = await step(luminaria, None, {})
    st = await step(
        luminaria,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Danificada", "location": "Praça"},
    )
    st = await step(luminaria, st, {"address": "Praça Mauá, Centro"})  # fake geocode -> kind=praca
    st = await step(luminaria, st, {"confirmacao": "sim"})  # confirm address
    # gate {or: address.kind==praca, localizacao==Praça} is TRUE -> quadra is asked
    assert "quadra" in require_agent_response(st).description.lower()


async def test_non_visual_defect_skips_quantity_gate(luminaria):
    st = await step(luminaria, None, {})
    # Pendurada is non-visual: quantidade/intercaladas gates are closed
    st = await step(
        luminaria, st, {"_source": "whatsapp_flow", "defect_type": "Pendurada", "location": "Rua"}
    )
    assert st.data["luminaria_defeito"] == "Pendurada"
    assert "luminaria_quantidade" not in st.data
    assert st.data["luminaria_defeito_classificado"] == "Pendurada"
    assert "endereço" in require_agent_response(st).description.lower()


async def test_correction_clears_slot_and_dependents(luminaria):
    st = await step(luminaria, None, {})
    st = await step(
        luminaria,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Apagada",
            "qty_pattern": "bloco",
            "location": "Rua",
        },
    )
    st = await step(luminaria, st, {"address": "Rua das Flores, 100, Centro"})
    st = await step(luminaria, st, {"confirmacao": "sim"})
    st = await step(luminaria, st, {"ponto_referencia": "padaria"})
    st = await step(luminaria, st, {"identification_method": "anonimo"})
    # at confirm_ticket_data -> request a correction of the defect
    st = await step(luminaria, st, {"correcao": "luminaria_defeito"})
    # defect + its requires-dependents + the derived classification are cleared
    assert "luminaria_defeito" not in st.data
    assert "luminaria_quantidade" not in st.data
    assert "luminaria_intercaladas_bloco" not in st.data
    assert "luminaria_defeito_classificado" not in st.data
    assert "ticket_data_confirmed" not in st.data
    # ...and the address it did NOT touch is preserved
    assert st.data.get("address_confirmed") is True


async def test_govbr_await_external_resume(luminaria):
    st = await step(luminaria, None, {})
    st = await step(
        luminaria, st, {"_source": "whatsapp_flow", "defect_type": "Danificada", "location": "Rua"}
    )
    st = await step(luminaria, st, {"address": "Rua A, 1, Centro"})
    st = await step(luminaria, st, {"confirmacao": "sim"})
    st = await step(luminaria, st, {"ponto_referencia": "esquina"})
    st = await step(luminaria, st, {"identification_method": "govbr"})
    # out-of-band CTA sent; turn ends waiting for the external signal
    assert (require_agent_response(st).interactive or {}).get("out_of_band_sent") is True
    # the external signal arrives -> slots populated from the token
    st = await step(
        luminaria,
        st,
        {
            "govbr_token": {
                "cpf": "52998224725",
                "nome": "Maria Silva",
                "email": "maria@example.com",
            }
        },
    )
    assert st.data.get("govbr_authenticated") is True
    assert st.data.get("cpf") == "52998224725"
    assert st.data.get("name") == "Maria Silva"
    assert st.data.get("email") == "maria@example.com"
    assert st.data.get("phone")  # enriched via get_user_info


async def test_govbr_external_switch_routes_to_cpf(luminaria):
    st = await step(luminaria, None, {})
    st = await step(
        luminaria,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Danificada",
            "location": "Rua",
        },
    )
    st = await step(luminaria, st, {"address": "Rua A, 1, Centro"})
    st = await step(luminaria, st, {"confirmacao": "sim"})
    st = await step(luminaria, st, {"ponto_referencia": "esquina"})
    st = await step(luminaria, st, {"identification_method": "govbr"})

    st = await step(luminaria, st, {"_external_event": "switch"})

    assert st.data["identification_method"] == "cpf"
    assert "cpf" in require_agent_response(st).description.lower()
