"""Regression tests for correction round-trips across node/subflow boundaries.

These lock the three bugs an adversarial review found: corrections that cross a
subflow boundary used to drop the re-answer or submit stale downstream data.
"""

from __future__ import annotations

from conftest import require_agent_response, step


async def _to_confirm_praca(luminaria):
    """Drive to the confirm-ticket hub with a Praça address (quadra asked)."""
    st = await step(luminaria, None, {})
    st = await step(
        luminaria,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Danificada", "location": "Praça"},
    )
    st = await step(luminaria, st, {"address": "Praça Mauá, Centro"})  # kind=praca
    st = await step(luminaria, st, {"confirmacao": "sim"})  # confirm address
    st = await step(
        luminaria, st, {"reparo_luminaria_quadra_esportes": "sim"}
    )  # quadra (gated open)
    st = await step(luminaria, st, {"ponto_referencia": "perto da quadra de tênis"})
    st = await step(luminaria, st, {"identification_method": "anonimo"})
    return st  # now at confirm_ticket_data


async def test_bug_a_address_correction_clears_dependents(luminaria):
    st = await _to_confirm_praca(luminaria)
    assert st.data.get("reparo_luminaria_quadra_esportes") is True
    assert st.data.get("ponto_referencia")

    # correct the address -> address + its requires-dependents must be cleared,
    # so no stale quadra flag / reference point reaches the ticket.
    st = await step(luminaria, st, {"correcao": "endereço"})
    assert "address" not in st.data
    assert "reparo_luminaria_quadra_esportes" not in st.data
    assert "ponto_referencia" not in st.data
    assert "ticket_data_confirmed" not in st.data


async def test_bug_b_text_reanswer_after_correction_is_not_swallowed(luminaria):
    st = await step(luminaria, None, {})
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
    st = await step(luminaria, st, {"address": "Rua A, 1"})
    st = await step(luminaria, st, {"confirmacao": "sim"})
    st = await step(luminaria, st, {"ponto_referencia": "esquina"})
    st = await step(luminaria, st, {"identification_method": "anonimo"})
    st = await step(luminaria, st, {"correcao": "defeito"})  # clears luminaria_defeito
    assert "luminaria_defeito" not in st.data

    # the citizen TYPES the new defect — auto_flow must NOT re-fire and swallow it
    st = await step(luminaria, st, {"luminaria_defeito": "Piscando"})
    assert (require_agent_response(st).interactive or {}).get("status") != "flow_sent"
    assert st.data["luminaria_defeito"] == "Piscando"


async def test_bug_c_correct_cpf_after_anonimo_reenters_subflow(luminaria):
    st = await step(luminaria, None, {})
    st = await step(
        luminaria, st, {"_source": "whatsapp_flow", "defect_type": "Danificada", "location": "Rua"}
    )
    st = await step(luminaria, st, {"address": "Rua A, 1"})
    st = await step(luminaria, st, {"confirmacao": "sim"})
    st = await step(luminaria, st, {"ponto_referencia": "esquina"})
    st = await step(luminaria, st, {"identification_method": "anonimo"})
    # at confirm: now the citizen wants to identify after all
    st = await step(luminaria, st, {"correcao": "cpf"})
    st = await step(luminaria, st, {"cpf": "52998224725"})
    assert st.data.get("cpf") == "52998224725"
