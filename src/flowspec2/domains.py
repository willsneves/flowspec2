"""Closed value-domains — the spine and the deterministic/non-deterministic boundary.

One ``domains.<X>`` declaration materializes shared artifacts:

1. a Pydantic model (per *slot*, since a domain is reused across slots) whose
   ``@field_validator(mode="before")`` deterministically coerces free
   speech/number/emoji into a **closed token** — the rail the LLM cannot widen;
2. ``Model.model_json_schema()`` — the JSON Schema (with the ``enum``) handed to
   constrained decoding at each pause, so the LLM extracts onto a closed token at
   exactly one place;
3. ordered interactive values, titles, and descriptions. Categorical domains
   derive them from ``values``/``rows``; boolean domains expose the canonical
   ``true``/``false`` tokens with localized titles.

``interactive.py`` turns those shared options into button and list envelopes,
so the affordance and the recognizer cannot drift.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Final, Literal, Mapping, Optional, cast

from pydantic import BaseModel, Field, create_model, field_validator

# ── normalization primitives ────────────────────────────────────────────────

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_AFFIRM_POS = {
    "yes",
    "y",
    "ok",
    "okay",
    "sure",
    "correct",
    "positive",
    "confirm",
    "confirmed",
    "exactly",
    "affirmative",
    "true",
    "1",
    "of course",
}
_AFFIRM_NEG = {
    "n",
    "no",
    "wrong",
    "negative",
    "incorrect",
    "disagree",
    "never",
    "false",
    "0",
}
_POS_EMOJI = ("👍", "✅", "👌", "🙂", "😊", "🆗")
_NEG_EMOJI = ("👎", "❌", "🚫", "🙅")


@dataclass(frozen=True)
class DomainInteractiveOption:
    """One closed domain token and its citizen-facing presentation."""

    value: str | bool
    title: str
    description: str = ""


_BOOLEAN_INTERACTIVE_OPTIONS: Final[tuple[DomainInteractiveOption, ...]] = (
    DomainInteractiveOption(value=True, title="Yes"),
    DomainInteractiveOption(value=False, title="No"),
)


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def normalize_text(value: Any, *, accent_fold: bool = True) -> str:
    text = str(value if value is not None else "").strip().lower()
    if accent_fold:
        text = strip_accents(text)
    return re.sub(r"\s+", " ", text)


def categorical_number_bindings(values: list[Any]) -> dict[str, Any]:
    """Return every positional digit/word input accepted for categorical values."""

    bindings = {
        str(position): domain_value for position, domain_value in enumerate(values, start=1)
    }
    bindings.update(
        {
            number_word: values[position - 1]
            for number_word, position in _NUMBER_WORDS.items()
            if position <= len(values)
        }
    )
    return bindings


def parse_affirmation(value: Any, *, emoji_veto: bool = True) -> Optional[bool]:
    """yes/confirm/👍 -> True, no/negative/👎 -> False, ambiguous -> None.

    A negative emoji overrides every other signal only when ``emoji_veto`` is
    enabled. Without the veto, recognized words take precedence over emoji so
    mixed input stays deterministic; emoji-only answers remain supported.
    """
    raw = str(value if value is not None else "")
    has_negative_emoji = any(emoji in raw for emoji in _NEG_EMOJI)
    has_positive_emoji = any(emoji in raw for emoji in _POS_EMOJI)
    if emoji_veto and has_negative_emoji:
        return False
    text = normalize_text(raw)
    if not text:
        return None
    if text in _AFFIRM_POS:
        return True
    if text in _AFFIRM_NEG:
        return False
    # natural phrases: token membership
    tokens = set(text.split())
    if tokens & _AFFIRM_NEG:
        return False
    if tokens & _AFFIRM_POS:
        return True
    if has_negative_emoji and has_positive_emoji:
        return None
    if has_negative_emoji:
        return False
    if has_positive_emoji:
        return True
    return None


def interactive_options_for_domain(
    domain_specification: Mapping[str, Any],
) -> tuple[DomainInteractiveOption, ...]:
    """Return the ordered renderable options for a closed domain."""
    domain_type = domain_specification.get("type", "categorical")
    if domain_type == "bool":
        return _BOOLEAN_INTERACTIVE_OPTIONS
    if domain_type != "categorical":
        return ()

    row_descriptions = {
        row_definition["value"]: row_definition.get("description", "")
        for row_definition in domain_specification.get("rows", [])
        if row_definition["value"] is not None
    }
    return tuple(
        DomainInteractiveOption(
            value=domain_value,
            title=domain_value,
            description=row_descriptions.get(domain_value, ""),
        )
        for domain_value in domain_specification.get("values", [])
        if domain_value is not None
    )


# ── per-domain validators ───────────────────────────────────────────────────

DomainValidator = Callable[[Any], Any]


def _categorical_validator(spec: dict[str, Any]) -> DomainValidator:
    values: list[Any] = spec["values"]
    norm = spec.get("normalize", {}) or {}
    accent_fold = bool(norm.get("accent_fold", False))
    use_numbers = norm.get("number_words", False)
    synonyms = {
        normalize_text(alias, accent_fold=accent_fold): target
        for alias, target in (norm.get("synonyms") or {}).items()
    }
    by_norm = {
        normalize_text(domain_value, accent_fold=accent_fold): domain_value
        for domain_value in values
        if domain_value is not None
    }
    has_null = any(v is None for v in values)

    def validate(raw: Any) -> Any:
        if raw is None:
            if has_null:
                return None
            raise ValueError("value is required")
        key = normalize_text(raw, accent_fold=accent_fold)
        if not key:
            if has_null:
                return None
            raise ValueError("value is empty")
        if use_numbers:
            idx = int(key) if key.isdigit() else _NUMBER_WORDS.get(key)
            if idx is not None and 1 <= idx <= len(values):
                return values[idx - 1]
        if key in synonyms:
            return synonyms[key]
        if key in by_norm:
            return by_norm[key]
        raise ValueError(f"value outside the domain: {raw!r}")

    return validate


def _bool_validator(spec: dict[str, Any]) -> DomainValidator:
    norm = spec.get("normalize", {}) or {}
    use_affirm = norm.get("affirmation", False)
    emoji_veto = bool(norm.get("emoji_veto", False))
    accent_fold = bool(norm.get("accent_fold", False))
    synonyms = {
        normalize_text(alias, accent_fold=accent_fold): target
        for alias, target in (norm.get("synonyms") or {}).items()
    }

    def validate(raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        if (
            use_affirm
            and emoji_veto
            and any(emoji in str(raw if raw is not None else "") for emoji in _NEG_EMOJI)
        ):
            return False
        key = normalize_text(raw, accent_fold=accent_fold)
        synonym = synonyms.get(key)
        if isinstance(synonym, bool):
            return synonym
        if use_affirm:
            result = parse_affirmation(raw, emoji_veto=emoji_veto)
            if result is None:
                raise ValueError(f"ambiguous response: {raw!r}. Use yes/no.")
            return result
        if key in {"true", "1", "yes", "y"}:
            return True
        if key in {"false", "0", "no", "n"}:
            return False
        raise ValueError(f"expected yes/no: {raw!r}")

    return validate


_BRAZILIAN_TAX_ID_RE = re.compile(r"[^0-9]")


def _brazilian_tax_id_valid(digits: str) -> bool:
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for length in (9, 10):
        weights = range(length + 1, 1, -1)
        total = sum(
            int(digit) * weight for digit, weight in zip(digits[:length], weights, strict=True)
        )
        check = (total * 10) % 11 % 10
        if check != int(digits[length]):
            return False
    return True


def _brazilian_tax_id_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        digits = _BRAZILIAN_TAX_ID_RE.sub("", str(raw or ""))
        if not _brazilian_tax_id_valid(digits):
            raise ValueError("invalid Brazilian tax ID")
        return digits

    return validate


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        value = str(raw or "").strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("invalid email address")
        return value

    return validate


def _name_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        value = str(raw or "").strip()
        if len(value) < 2:
            raise ValueError("invalid name")
        return value

    return validate


def _free_text_validator(spec: dict[str, Any]) -> DomainValidator:
    optional = bool(spec.get("optional"))

    def validate(raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            if optional:  # empty is a valid skip for an optional free-text slot
                return ""
            raise ValueError("text is empty")
        return value

    return validate


def _bounded_numeric_value(value: Decimal, spec: dict[str, Any]) -> Decimal:
    if "minimum" in spec and value < Decimal(str(spec["minimum"])):
        raise ValueError(f"value below minimum: {spec['minimum']}")
    if "maximum" in spec and value > Decimal(str(spec["maximum"])):
        raise ValueError(f"value above maximum: {spec['maximum']}")
    return value


def _integer_validator(spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> int:
        if isinstance(raw, bool):
            raise ValueError("invalid integer")
        text = str(raw if raw is not None else "").strip()
        if re.fullmatch(r"[+-]?\d+", text) is None:
            raise ValueError("invalid integer")
        return int(_bounded_numeric_value(Decimal(text), spec))

    return validate


def _number_validator(spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> float:
        if isinstance(raw, bool):
            raise ValueError("invalid number")
        try:
            value = Decimal(str(raw if raw is not None else "").strip())
        except InvalidOperation as error:
            raise ValueError("invalid number") from error
        if not value.is_finite():
            raise ValueError("number must be finite")
        bounded_value = _bounded_numeric_value(value, spec)
        floating_value = float(bounded_value)
        if not math.isfinite(floating_value):
            raise ValueError("number outside the representable range")
        return floating_value

    return validate


_VALIDATOR_FACTORIES: dict[str, Callable[[dict[str, Any]], DomainValidator]] = {
    "categorical": _categorical_validator,
    "bool": _bool_validator,
    "brazilian_tax_id": _brazilian_tax_id_validator,
    "email": _email_validator,
    "name": _name_validator,
    "free_text": _free_text_validator,
    "integer": _integer_validator,
    "number": _number_validator,
}


def make_validator(spec: dict[str, Any]) -> DomainValidator:
    return _VALIDATOR_FACTORIES[spec.get("type", "categorical")](spec)


# ── per-slot Pydantic model (the payload_schema source) ──────────────────────


def _field_type(spec: dict[str, Any], nullable: bool) -> Any:
    dtype = spec.get("type", "categorical")
    if dtype == "categorical":
        non_null = [v for v in spec["values"] if v is not None]
        base = Literal[tuple(non_null)]  # type: ignore[valid-type]
        return Optional[base] if nullable else base
    if dtype == "bool":
        return Optional[bool] if nullable else bool
    if dtype == "integer":
        return Optional[int] if nullable else int
    if dtype == "number":
        return Optional[float] if nullable else float
    return Optional[str] if nullable else str


def _field_description(
    slot_name: str,
    spec: dict[str, Any],
    extract_hint: Optional[str],
    *,
    nullable: bool,
) -> str:
    parts: list[str] = []
    if spec.get("type", "categorical") == "categorical":
        tokens = ", ".join(
            "null" if value is None else str(value)
            for value in spec["values"]
            if value is not None or nullable
        )
        parts.append(f"Interpret the user's input and return ONLY one closed value: {tokens}.")
    elif spec.get("type") == "bool":
        parts.append("Interpret as a boolean: true for yes/affirmative, false for no/negative.")
    elif spec.get("type") == "integer":
        parts.append("Interpret as an integer.")
    elif spec.get("type") == "number":
        parts.append("Interpret as a finite number.")
    if extract_hint:
        parts.append(extract_hint)
    return " ".join(parts) or f"Value for {slot_name}."


def make_slot_model(
    slot_name: str,
    domain_name: str,
    domains: dict[str, Any],
    *,
    nullable: bool = False,
    extract_hint: Optional[str] = None,
) -> type[BaseModel]:
    """A one-field Pydantic model whose before-validator enforces the domain.

    The field name == the slot name so the generated ``model_json_schema()`` (the
    ``payload_schema``) teaches the LLM exactly which key to extract.
    """
    spec = domains[domain_name]
    validator = make_validator(spec)
    field_type = _field_type(spec, nullable)
    description = _field_description(slot_name, spec, extract_hint, nullable=nullable)

    def _run_validator(_model_class: type[BaseModel], value: Any) -> Any:
        if value is None:
            if nullable:
                return None
            raise ValueError("null requires slots.<name>.nullable:true")
        return validator(value)

    validators: dict[str, Any] = {
        f"_validate_{slot_name}": field_validator(slot_name, mode="before")(
            classmethod(_run_validator)
        )
    }
    domain_type = spec.get("type", "categorical")
    field_constraints: dict[str, Any] = {}
    if domain_type in {"integer", "number"}:
        field_constraints.update(ge=spec.get("minimum"), le=spec.get("maximum"))
    elif domain_type == "brazilian_tax_id":
        field_constraints["pattern"] = r"^[0-9]{11}$"
    elif domain_type == "email":
        field_constraints["pattern"] = _EMAIL_RE.pattern
        field_constraints["json_schema_extra"] = {"format": "email"}
    elif domain_type == "name":
        field_constraints["min_length"] = 2
    elif domain_type == "free_text" and not spec.get("optional"):
        field_constraints["min_length"] = 1
    field_definitions: dict[str, Any] = {
        slot_name: (
            field_type,
            Field(
                ...,
                description=description,
                **{
                    constraint_name: constraint_value
                    for constraint_name, constraint_value in field_constraints.items()
                    if constraint_value is not None
                },
            ),
        )
    }
    model = cast(
        type[BaseModel],
        create_model(
            f"Slot_{slot_name}",
            __validators__=validators,
            **field_definitions,
        ),
    )
    return model
