"""Closed value-domains — the spine and the deterministic/non-deterministic boundary.

One ``domains.<X>`` declaration materializes (here) two of its four artifacts:

1. a Pydantic model (per *slot*, since a domain is reused across slots) whose
   ``@field_validator(mode="before")`` deterministically coerces free
   speech/number/emoji into a **closed token** — the rail the LLM cannot widen;
2. ``Model.model_json_schema()`` — the JSON Schema (with the ``enum``) handed to
   constrained decoding at each pause, so the LLM extracts onto a closed token at
   exactly one place.

The other two (interactive button titles + list rows) are built in
``interactive.py`` from the same ``values``/``rows`` arrays, so the affordance
and the recognizer can never drift.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, create_model, field_validator

# ── normalization primitives ────────────────────────────────────────────────

_NUMBER_WORDS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
}

_AFFIRM_POS = {
    "sim", "s", "yes", "y", "isso", "ok", "okay", "claro", "quero", "correto",
    "certo", "positivo", "pode", "confirmo", "aham", "uhum", "exato", "verdade",
    "afirmativo", "true", "1", "blz", "beleza", "isso mesmo", "com certeza",
}
_AFFIRM_NEG = {
    "nao", "n", "no", "errado", "negativo", "incorreto", "discordo", "nunca",
    "false", "0", "nem", "jamais",
}
_POS_EMOJI = ("👍", "✅", "👌", "🙂", "😊", "🆗")
_NEG_EMOJI = ("👎", "❌", "🚫", "🙅")


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def normalize_text(value: Any, *, accent_fold: bool = True) -> str:
    text = str(value if value is not None else "").strip().lower()
    if accent_fold:
        text = strip_accents(text)
    return re.sub(r"\s+", " ", text)


def parse_affirmation(value: Any) -> Optional[bool]:
    """sim/yes/isso/👍 -> True, não/no/👎 -> False, ambiguous -> None.

    Emoji veto first: a negative emoji forces False regardless of words.
    """
    raw = str(value if value is not None else "")
    if any(e in raw for e in _NEG_EMOJI):
        return False
    if any(e in raw for e in _POS_EMOJI):
        return True
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
    return None


# ── per-domain validators ───────────────────────────────────────────────────

DomainValidator = Callable[[Any], Any]


def _categorical_validator(spec: dict[str, Any]) -> DomainValidator:
    values: list[Any] = spec["values"]
    norm = spec.get("normalize", {}) or {}
    use_numbers = norm.get("number_words", False)
    synonyms = {normalize_text(k): t for k, t in (norm.get("synonyms") or {}).items()}
    by_norm = {normalize_text(v): v for v in values if v is not None}
    has_null = any(v is None for v in values)

    def validate(raw: Any) -> Any:
        if raw is None:
            if has_null:
                return None
            raise ValueError("valor obrigatório")
        key = normalize_text(raw)
        if not key:
            if has_null:
                return None
            raise ValueError("valor vazio")
        if use_numbers:
            idx = int(key) if key.isdigit() else _NUMBER_WORDS.get(key)
            if idx is not None and 1 <= idx <= len(values):
                return values[idx - 1]
        if key in synonyms:
            return synonyms[key]
        if key in by_norm:
            return by_norm[key]
        raise ValueError(f"valor fora do domínio: {raw!r}")

    return validate


def _bool_validator(spec: dict[str, Any]) -> DomainValidator:
    norm = spec.get("normalize", {}) or {}
    use_affirm = norm.get("affirmation", False)
    synonyms = {normalize_text(k): t for k, t in (norm.get("synonyms") or {}).items()}

    def validate(raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        key = normalize_text(raw)
        if key in synonyms and isinstance(synonyms[key], bool):
            return synonyms[key]
        if use_affirm:
            result = parse_affirmation(raw)
            if result is None:
                raise ValueError(f"resposta ambígua: {raw!r}. Use sim/não.")
            return result
        if key in {"true", "1", "sim", "s"}:
            return True
        if key in {"false", "0", "nao", "n"}:
            return False
        raise ValueError(f"esperado sim/não: {raw!r}")

    return validate


_CPF_RE = re.compile(r"\D")


def _cpf_valid(digits: str) -> bool:
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for length in (9, 10):
        weights = range(length + 1, 1, -1)
        total = sum(int(d) * w for d, w in zip(digits, weights))
        check = (total * 10) % 11 % 10
        if check != int(digits[length]):
            return False
    return True


def _cpf_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        digits = _CPF_RE.sub("", str(raw or ""))
        if not _cpf_valid(digits):
            raise ValueError("CPF inválido")
        return digits

    return validate


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        value = str(raw or "").strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("e-mail inválido")
        return value

    return validate


def _name_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        value = str(raw or "").strip()
        if len(value) < 2:
            raise ValueError("nome inválido")
        return value

    return validate


def _free_text_validator(_spec: dict[str, Any]) -> DomainValidator:
    def validate(raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            raise ValueError("texto vazio")
        return value

    return validate


_VALIDATOR_FACTORIES: dict[str, Callable[[dict[str, Any]], DomainValidator]] = {
    "categorical": _categorical_validator,
    "bool": _bool_validator,
    "cpf": _cpf_validator,
    "email": _email_validator,
    "name": _name_validator,
    "free_text": _free_text_validator,
}


def make_validator(spec: dict[str, Any]) -> DomainValidator:
    return _VALIDATOR_FACTORIES[spec.get("type", "categorical")](spec)


# ── per-slot Pydantic model (the payload_schema source) ──────────────────────

def _field_type(spec: dict[str, Any], nullable: bool) -> Any:
    dtype = spec.get("type", "categorical")
    if dtype == "categorical":
        non_null = [v for v in spec["values"] if v is not None]
        base = Literal[tuple(non_null)]  # type: ignore[valid-type]
        if nullable or any(v is None for v in spec["values"]):
            return Optional[base]
        return base
    if dtype == "bool":
        return bool
    return str


def _field_description(slot_name: str, spec: dict[str, Any], extract_hint: Optional[str]) -> str:
    parts: list[str] = []
    if spec.get("type", "categorical") == "categorical":
        tokens = ", ".join("null" if v is None else str(v) for v in spec["values"])
        parts.append(
            "Interprete a fala do usuário e devolva SOMENTE um valor fechado: "
            f"{tokens}."
        )
    elif spec.get("type") == "bool":
        parts.append("Interprete como booleano: true para sim/afirmativo, false para não.")
    if extract_hint:
        parts.append(extract_hint)
    return " ".join(parts) or f"Valor para {slot_name}."


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
    description = _field_description(slot_name, spec, extract_hint)

    def _run_validator(cls, value):  # noqa: ANN001
        return validator(value)

    validators = {
        f"_validate_{slot_name}": field_validator(slot_name, mode="before")(
            classmethod(_run_validator)
        )
    }
    model = create_model(  # type: ignore[call-overload]
        f"Slot_{slot_name}",
        __validators__=validators,
        **{slot_name: (field_type, Field(..., description=description))},
    )
    return model
