"""WhatsApp interactive envelope builders (buttons / list / flow / cta).

These materialize the *other two* domain artifacts — button titles and list
rows — from the same ``values``/``rows`` arrays the validator uses, so the
affordance and the recognizer can never drift. Limits mirror the Meta Cloud API
(and the production ``whatsapp_interactive``).
"""

from __future__ import annotations

from typing import Any, Optional

from .models import ServiceState
from .predicates import evaluate

BUTTON_TITLE_MAX = 20
MAX_BUTTONS = 3
LIST_ROWS_TOTAL_MAX = 10
ROW_TITLE_MAX = 24
ROW_DESC_MAX = 72
BODY_MAX = 1024


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def build_buttons(body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        return _err(f"buttons must be 1..{MAX_BUTTONS}")
    seen: set[str] = set()
    norm: list[dict[str, str]] = []
    for btn in buttons:
        bid, title = btn.get("id", ""), btn.get("title", "")
        if not bid or not title:
            return _err("button needs id+title")
        if len(title) > BUTTON_TITLE_MAX:
            return _err(f"button title >{BUTTON_TITLE_MAX}: {title!r}")
        if bid in seen:
            return _err(f"duplicate button id: {bid}")
        seen.add(bid)
        norm.append({"id": bid, "title": title})
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:BODY_MAX]},
            "action": {"buttons": [{"type": "reply", "reply": b} for b in norm]},
        },
    }


def build_list(
    body: str, sections: list[dict[str, Any]], button_label: str = "Escolher"
) -> dict[str, Any]:
    total = sum(len(s.get("rows", [])) for s in sections)
    if not 1 <= total <= LIST_ROWS_TOTAL_MAX:
        return _err(f"list rows total must be 1..{LIST_ROWS_TOTAL_MAX}")
    if len(button_label) > BUTTON_TITLE_MAX:
        return _err("button_label too long")
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:BODY_MAX]},
            "action": {"button": button_label, "sections": sections},
        },
    }


def build_flow(
    flow_id: str, body: str, cta: str = "Preencher", flow_token: str = ""
) -> dict[str, Any]:
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": body[:BODY_MAX]},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_id": flow_id,
                    "flow_cta": cta[:BUTTON_TITLE_MAX],
                    "flow_token": flow_token,
                    "flow_action": "navigate",
                },
            },
        },
    }


def build_cta_url(body: str, url: str, display_text: str) -> dict[str, Any]:
    if not url.startswith("https://"):
        return _err("cta url must be https://")
    return {
        "status": "ok",
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body[:BODY_MAX]},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": display_text[:BUTTON_TITLE_MAX], "url": url},
            },
        },
    }


def _visible_values(
    spec: dict[str, Any],
    options_when: Optional[list[dict[str, Any]]],
    state: Optional[ServiceState],
    config: dict[str, Any],
) -> list[Any]:
    values = [v for v in spec.get("values", []) if v is not None]
    if not options_when:
        return values
    gates = {o["value"]: o["gate"] for o in options_when}
    out = []
    for v in values:
        gate = gates.get(v)
        if gate is None or (state is not None and evaluate(gate, state, config)):
            out.append(v)
    return out


def options_from_domain(
    interactive: dict[str, Any],
    domains: dict[str, Any],
    *,
    state: Optional[ServiceState] = None,
    config: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Build the citizen-facing interactive spec a node attaches to AgentResponse.

    Returns the production ``AgentResponse.interactive`` shape:
    ``{"body", "field", "buttons"|"sections", ...}``. The wrapper layer turns
    this into a real envelope when ``ENABLE_INTERACTIVE_CONFIRM`` is on; the
    deterministic ``description`` is the fallback when it is off.
    """
    config = config or {}
    kind = interactive["kind"]
    field = interactive["field"]
    body = interactive.get("body", "")
    spec_name = interactive.get("from_domain")

    if kind in ("buttons", "list") and spec_name:
        spec = domains[spec_name]
        values = _visible_values(spec, interactive.get("options_when"), state, config)
        if kind == "buttons":
            buttons = [{"id": str(v).lower(), "title": str(v)} for v in values]
            return {"body": body, "field": field, "buttons": buttons}
        rows_meta = {r["value"]: r.get("description", "") for r in spec.get("rows", [])}
        rows = [
            {
                "id": str(v).lower(),
                "title": str(v)[:ROW_TITLE_MAX],
                "description": rows_meta.get(v, "")[:ROW_DESC_MAX],
            }
            for v in values
        ]
        return {"body": body, "field": field, "sections": [{"title": "Opções", "rows": rows}]}

    if kind == "flow":
        out: dict[str, Any] = {"body": body, "field": field, "flow": True}
        if interactive.get("meta_flow_ref"):
            out["meta_flow_ref"] = interactive["meta_flow_ref"]
        if interactive.get("prefill_from"):
            out["prefill_from"] = interactive["prefill_from"]
        return out

    if interactive.get("out_of_band"):
        return {
            "body": body,
            "field": field,
            "out_of_band_sent": True,
            "next_step": interactive.get("next_step"),
        }

    return {"body": body, "field": field}
