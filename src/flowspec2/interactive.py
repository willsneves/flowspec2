"""WhatsApp interactive envelope builders and closed-domain projections.

These materialize button and list envelopes from the closed options provided by
``domains.py``. Categorical options retain their declared values/rows; boolean
options use canonical ``true``/``false`` identifiers with ``Yes``/``No``
titles. Limits mirror the Meta Cloud API (and the production
``whatsapp_interactive``).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from .domains import DomainInteractiveOption, interactive_options_for_domain
from .models import ServiceState
from .predicates import evaluate

BUTTON_TITLE_MAX = 20
BUTTON_ID_MAX = 256
MAX_BUTTONS = 3
LIST_ROWS_TOTAL_MAX = 10
ROW_ID_MAX = 200
ROW_TITLE_MAX = 24
ROW_DESC_MAX = 72
BODY_MAX = 1024
CTA_URL_MAX = 2000


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def _body_error(body: str) -> dict[str, Any] | None:
    if not isinstance(body, str):
        return _err("body must be a string")
    if len(body) > BODY_MAX:
        return _err(f"body exceeds {BODY_MAX} characters")
    return None


def _valid_identifier(identifier: Any, maximum_length: int) -> bool:
    return (
        isinstance(identifier, str)
        and bool(identifier.strip())
        and len(identifier) <= maximum_length
    )


def interactive_option_identifier(value: str | bool) -> str:
    """Return the exact payload identifier used for a rendered domain option."""

    return str(value).lower()


def build_buttons(body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
    if body_error := _body_error(body):
        return body_error
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        return _err(f"buttons must be 1..{MAX_BUTTONS}")
    seen_identifiers: set[str] = set()
    normalized_buttons: list[dict[str, str]] = []
    for button in buttons:
        identifier, title = button.get("id"), button.get("title")
        if not _valid_identifier(identifier, BUTTON_ID_MAX):
            return _err(f"button id must be non-empty and at most {BUTTON_ID_MAX} characters")
        assert isinstance(identifier, str)
        if not isinstance(title, str) or not title.strip():
            return _err("button needs id+title")
        if len(title) > BUTTON_TITLE_MAX:
            return _err(f"button title >{BUTTON_TITLE_MAX}: {title!r}")
        if identifier in seen_identifiers:
            return _err(f"duplicate button id: {identifier}")
        seen_identifiers.add(identifier)
        normalized_buttons.append({"id": identifier, "title": title})
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [{"type": "reply", "reply": button} for button in normalized_buttons]
            },
        },
    }


def build_list(
    body: str, sections: list[dict[str, Any]], button_label: str = "Escolher"
) -> dict[str, Any]:
    if body_error := _body_error(body):
        return body_error
    if not isinstance(button_label, str) or not button_label.strip():
        return _err("button_label must be non-empty")
    if len(button_label) > BUTTON_TITLE_MAX:
        return _err("button_label too long")

    row_count = 0
    seen_identifiers: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            return _err("each list section must be an object")
        section_title = section.get("title")
        if section_title is not None and (
            not isinstance(section_title, str)
            or not section_title.strip()
            or len(section_title) > ROW_TITLE_MAX
        ):
            return _err(f"section title must be non-empty and at most {ROW_TITLE_MAX} characters")
        rows = section.get("rows")
        if not isinstance(rows, list):
            return _err("each list section needs rows")
        row_count += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                return _err("each list row must be an object")
            identifier = row.get("id")
            title = row.get("title")
            description = row.get("description", "")
            if not _valid_identifier(identifier, ROW_ID_MAX):
                return _err(f"row id must be non-empty and at most {ROW_ID_MAX} characters")
            assert isinstance(identifier, str)
            if identifier in seen_identifiers:
                return _err(f"duplicate row id: {identifier}")
            if not isinstance(title, str) or not title.strip():
                return _err("row title must be non-empty")
            if len(title) > ROW_TITLE_MAX:
                return _err(f"row title >{ROW_TITLE_MAX}: {title!r}")
            if not isinstance(description, str):
                return _err("row description must be a string")
            if len(description) > ROW_DESC_MAX:
                return _err(f"row description >{ROW_DESC_MAX}: {description!r}")
            seen_identifiers.add(identifier)

    if not 1 <= row_count <= LIST_ROWS_TOTAL_MAX:
        return _err(f"list rows total must be 1..{LIST_ROWS_TOTAL_MAX}")
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_label, "sections": sections},
        },
    }


def build_flow(
    flow_id: str, body: str, cta: str = "Preencher", flow_token: str = ""
) -> dict[str, Any]:
    if body_error := _body_error(body):
        return body_error
    if not _valid_identifier(flow_id, BUTTON_ID_MAX):
        return _err(f"flow_id must be non-empty and at most {BUTTON_ID_MAX} characters")
    if not isinstance(cta, str) or not cta.strip():
        return _err("flow cta must be non-empty")
    if len(cta) > BUTTON_TITLE_MAX:
        return _err(f"flow cta exceeds {BUTTON_TITLE_MAX} characters")
    if not isinstance(flow_token, str):
        return _err("flow_token must be a string")
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": body},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_id": flow_id,
                    "flow_cta": cta,
                    "flow_token": flow_token,
                    "flow_action": "navigate",
                },
            },
        },
    }


def build_cta_url(body: str, url: str, display_text: str) -> dict[str, Any]:
    if body_error := _body_error(body):
        return body_error
    if not isinstance(display_text, str) or not display_text.strip():
        return _err("cta display_text must be non-empty")
    if len(display_text) > BUTTON_TITLE_MAX:
        return _err(f"cta display_text exceeds {BUTTON_TITLE_MAX} characters")
    if (
        not isinstance(url, str)
        or len(url) > CTA_URL_MAX
        or any(character.isspace() for character in url)
    ):
        return _err("cta url must be a bounded HTTPS URL without whitespace")
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        return _err("cta url must be a valid HTTPS URL")
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        return _err("cta url must be a valid HTTPS URL")
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": display_text, "url": url},
            },
        },
    }


def _visible_options(
    domain_specification: dict[str, Any],
    options_when: Optional[list[dict[str, Any]]],
    state: Optional[ServiceState],
    config: dict[str, Any],
) -> list[DomainInteractiveOption]:
    domain_options = list(interactive_options_for_domain(domain_specification))
    if not options_when:
        return domain_options
    gates = {
        conditional_option["value"]: conditional_option["gate"]
        for conditional_option in options_when
    }
    visible_options: list[DomainInteractiveOption] = []
    for domain_option in domain_options:
        gate = gates.get(domain_option.value)
        if gate is None or (state is not None and evaluate(gate, state, config)):
            visible_options.append(domain_option)
    return visible_options


def options_from_domain(
    interactive: dict[str, Any],
    domains: dict[str, Any],
    *,
    state: Optional[ServiceState] = None,
    config: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Build the citizen-facing interactive spec attached to ``AgentResponse``."""
    config = config or {}
    kind = interactive["kind"]
    field = interactive["field"]
    body = interactive.get("body", "")
    spec_name = interactive.get("from_domain")

    if kind in ("buttons", "list") and spec_name:
        spec = domains[spec_name]
        domain_options = _visible_options(
            spec,
            interactive.get("options_when"),
            state,
            config,
        )
        if not domain_options:
            return None
        seen_values: set[tuple[type[str] | type[bool], str | bool]] = set()
        seen_identifiers: set[str] = set()
        for domain_option in domain_options:
            typed_value = (type(domain_option.value), domain_option.value)
            if typed_value in seen_values:
                raise ValueError(f"duplicate interactive option value: {domain_option.value!r}")
            identifier = interactive_option_identifier(domain_option.value)
            identifier_maximum = BUTTON_ID_MAX if kind == "buttons" else ROW_ID_MAX
            if not _valid_identifier(identifier, identifier_maximum):
                raise ValueError(f"invalid {kind} option identifier: {identifier!r}")
            if identifier in seen_identifiers:
                raise ValueError(f"duplicate interactive option identifier: {identifier!r}")
            seen_values.add(typed_value)
            seen_identifiers.add(identifier)
        if kind == "buttons":
            buttons = [
                {
                    "id": interactive_option_identifier(domain_option.value),
                    "title": domain_option.title,
                }
                for domain_option in domain_options
            ]
            return {"body": body, "field": field, "buttons": buttons}
        rows = [
            {
                "id": interactive_option_identifier(domain_option.value),
                "title": domain_option.title,
                "description": domain_option.description,
            }
            for domain_option in domain_options
        ]
        return {"body": body, "field": field, "sections": [{"title": "Options", "rows": rows}]}

    if interactive.get("out_of_band"):
        return {
            "body": body,
            "field": field,
            "out_of_band_sent": True,
            "next_step": interactive.get("next_step"),
        }

    return {"body": body, "field": field}
