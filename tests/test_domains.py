"""Deterministic normalizers: the rail the LLM cannot widen."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from flowspec2.domains import make_slot_model, parse_affirmation

DOMAINS = {
    "LuminariaDefeito": {
        "type": "categorical",
        "values": ["Apagada", "Piscando", "Acesa de dia", "Pendurada", "Danificada", "Com ruído"],
        "normalize": {
            "accent_fold": True,
            "number_words": True,
            "synonyms": {"sem luz": "Apagada", "ruido": "Com ruído"},
        },
    },
    "LuminariaLocalizacao": {
        "type": "categorical",
        "values": ["Calçada", "Praça", "Rua", None],
        "normalize": {"accent_fold": True, "synonyms": {"praca": "Praça"}},
    },
    "SimNao": {"type": "bool", "normalize": {"affirmation": True, "emoji_veto": True}},
    "CPF": {"type": "cpf"},
    "Email": {"type": "email"},
}


def _coerce(slot, domain, raw, nullable=False):
    model = make_slot_model(slot, domain, DOMAINS, nullable=nullable)
    return getattr(model.model_validate({slot: raw}), slot)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("apagada", "Apagada"),
        ("APAGADA", "Apagada"),
        ("sem luz", "Apagada"),  # synonym
        ("1", "Apagada"),  # positional number
        ("acesa de dia", "Acesa de dia"),
        ("ruido", "Com ruído"),  # synonym + accent target
        ("com ruído", "Com ruído"),  # accent-fold match
    ],
)
def test_categorical_normalizes_to_closed_token(raw, expected):
    assert _coerce("luminaria_defeito", "LuminariaDefeito", raw) == expected


def test_categorical_rejects_out_of_domain():
    with pytest.raises(ValidationError):
        _coerce("luminaria_defeito", "LuminariaDefeito", "explodiu")


def test_nullable_member():
    model = make_slot_model("loc", "LuminariaLocalizacao", DOMAINS, nullable=True)
    assert model.model_validate({"loc": None}).model_dump()["loc"] is None
    assert model.model_validate({"loc": "praca"}).model_dump()["loc"] == "Praça"


def test_payload_schema_has_enum():
    model = make_slot_model("luminaria_defeito", "LuminariaDefeito", DOMAINS)
    js = model.model_json_schema()
    enum = js["properties"]["luminaria_defeito"]["enum"]
    assert "Apagada" in enum and "Com ruído" in enum


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sim", True),
        ("não", False),
        ("isso", True),
        ("ok", True),
        ("👍", True),
        ("👎", False),
        ("nao quero", False),
    ],
)
def test_affirmation(raw, expected):
    assert _coerce("ok", "SimNao", raw) is expected


def test_emoji_veto_overrides_words():
    # negative emoji vetoes even alongside an affirmative word
    assert parse_affirmation("sim 👎") is False


def test_cpf_checksum():
    assert _coerce("cpf", "CPF", "529.982.247-25") == "52998224725"  # valid
    with pytest.raises(ValidationError):
        _coerce("cpf", "CPF", "111.111.111-11")  # repeated digits


def test_email():
    assert _coerce("email", "Email", "Foo@Bar.com") == "foo@bar.com"
    with pytest.raises(ValidationError):
        _coerce("email", "Email", "not-an-email")
