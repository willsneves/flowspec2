"""Deterministic normalizers: the rail the LLM cannot widen."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from flowspec2.domains import make_slot_model, parse_affirmation

DOMAINS = {
    "StreetlightIssue": {
        "type": "categorical",
        "values": [
            "Not working",
            "Flickering",
            "On during daylight",
            "Hanging",
            "Damaged",
            "Noisy",
        ],
        "normalize": {
            "accent_fold": True,
            "number_words": True,
            "synonyms": {"no light": "Not working", "humming": "Noisy"},
        },
    },
    "StreetlightLocation": {
        "type": "categorical",
        "values": ["Sidewalk", "Public square", "Street", None],
        "normalize": {"accent_fold": True, "synonyms": {"square": "Public square"}},
    },
    "YesNo": {"type": "bool", "normalize": {"affirmation": True, "emoji_veto": True}},
    "Brazilian tax ID": {"type": "brazilian_tax_id"},
    "Email": {"type": "email"},
}


def _coerce(slot, domain, raw, nullable=False):
    model = make_slot_model(slot, domain, DOMAINS, nullable=nullable)
    return getattr(model.model_validate({slot: raw}), slot)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("not working", "Not working"),
        ("NOT WORKING", "Not working"),
        ("no light", "Not working"),  # synonym
        ("1", "Not working"),  # positional number
        ("on during daylight", "On during daylight"),
        ("humming", "Noisy"),  # synonym
    ],
)
def test_categorical_normalizes_to_closed_token(raw, expected):
    assert _coerce("streetlight_issue", "StreetlightIssue", raw) == expected


def test_categorical_rejects_out_of_domain():
    with pytest.raises(ValidationError):
        _coerce("streetlight_issue", "StreetlightIssue", "exploded")


def test_nullable_member():
    model = make_slot_model("loc", "StreetlightLocation", DOMAINS, nullable=True)
    assert model.model_validate({"loc": None}).model_dump()["loc"] is None
    assert model.model_validate({"loc": "square"}).model_dump()["loc"] == "Public square"


def test_categorical_null_requires_slot_nullable_contract() -> None:
    model = make_slot_model("loc", "StreetlightLocation", DOMAINS, nullable=False)

    with pytest.raises(ValidationError):
        model.model_validate({"loc": None})
    assert None not in model.model_json_schema()["properties"]["loc"]["enum"]


def test_nullable_flag_applies_to_non_categorical_domains():
    model = make_slot_model("email", "Email", DOMAINS, nullable=True)

    assert model.model_validate({"email": None}).model_dump()["email"] is None
    assert {
        branch.get("type") for branch in model.model_json_schema()["properties"]["email"]["anyOf"]
    } == {
        "string",
        "null",
    }


def test_payload_schema_has_enum():
    model = make_slot_model("streetlight_issue", "StreetlightIssue", DOMAINS)
    js = model.model_json_schema()
    enum = js["properties"]["streetlight_issue"]["enum"]
    assert "Not working" in enum and "Noisy" in enum


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("yes", True),
        ("no", False),
        ("correct", True),
        ("ok", True),
        ("👍", True),
        ("👎", False),
        ("I disagree", False),
    ],
)
def test_affirmation(raw, expected):
    assert _coerce("ok", "YesNo", raw) is expected


def test_emoji_veto_overrides_words():
    # negative emoji vetoes even alongside an affirmative word
    assert parse_affirmation("yes 👎") is False


def test_boolean_aliases_honor_accent_fold() -> None:
    accent_sensitive_domains = {
        "Boolean": {
            "type": "bool",
            "normalize": {
                "accent_fold": False,
                "synonyms": {"r\u00e9sum\u00e9": False},
            },
        }
    }
    model = make_slot_model("accepted", "Boolean", accent_sensitive_domains)

    assert model.model_validate({"accepted": "r\u00e9sum\u00e9"}).model_dump()["accepted"] is False
    with pytest.raises(ValidationError):
        model.model_validate({"accepted": "resume"})


def test_brazilian_tax_id_checksum():
    assert (
        _coerce("brazilian_tax_id", "Brazilian tax ID", "529.982.247-25") == "52998224725"
    )  # valid
    with pytest.raises(ValidationError):
        _coerce("brazilian_tax_id", "Brazilian tax ID", "111.111.111-11")  # repeated digits


def test_email():
    assert _coerce("email", "Email", "Foo@Bar.com") == "foo@bar.com"
    with pytest.raises(ValidationError):
        _coerce("email", "Email", "not-an-email")
