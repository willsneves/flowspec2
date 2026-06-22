"""identification@2 — skip handling for the optional contact slots."""

from __future__ import annotations

from conftest import step
from flowspec2.subflows.identification import _is_skip


def test_is_skip_matches_exact_tokens_only():
    # explicit skip words (accent/case-insensitive)
    assert _is_skip("pular")
    assert _is_skip("PULAR")
    assert _is_skip("não")  # normalizes to "nao"
    assert _is_skip("nenhum")
    assert _is_skip("passar")


def test_is_skip_never_triggers_on_substrings():
    # a real value that merely *contains* a skip token must NOT be read as a skip
    assert not _is_skip("skipper@example.com")  # contains "skip"
    assert not _is_skip("ana@nenhum.com")  # contains "nenhum"
    assert not _is_skip("Maria Aparecida")
    assert not _is_skip("11144477735")


async def test_pular_skips_optional_email_and_name(luminaria):
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
    assert "e-mail" in st.agent_response.description.lower()
    st = await step(luminaria, st, {"email": "pular"})
    assert st.data.get("email_processed") is True
    assert "email" not in st.data  # nothing stored on skip

    # and the name step likewise
    assert "nome" in st.agent_response.description.lower()
    st = await step(luminaria, st, {"name": "pular"})
    assert st.data.get("name_processed") is True
    assert "name" not in st.data
