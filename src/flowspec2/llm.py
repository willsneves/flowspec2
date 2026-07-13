"""An LLM driver — the *non-deterministic execution* side of the boundary.

flowspec2 pins the rails (states, closed value-domains, transitions). This module
is the agent that reasons *within* them: given a flow's ``route.description`` it
decides whether to enter, and at each pause it reads the citizen's free
text/voice and the node's ``payload_schema`` (or the interactive options) and
extracts the **closed token** for the slot. The flowspec2 validators then enforce
the rail — an out-of-domain extraction is rejected and the node re-asks.

Default provider: Google Gemini (``gemini-2.5-flash``, the model the production
Prefeitura bot uses), via ``GEMINI_API_KEY``. The driver is a thin protocol —
``route`` + ``extract`` — so any provider can implement it.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Optional

from jsonschema import Draft202012Validator

from .json_codec import strict_json_loads
from .models import CORRECTION_TARGETS_SCHEMA_KEY, AgentResponse

ROUTE_SYS = (
    "Você é o roteador de um bot de serviços da Prefeitura do Rio. "
    "Dada a mensagem do cidadão, decida qual serviço (se algum) atende ao pedido. "
    "Responda APENAS com JSON."
)

EXTRACT_SYS = (
    "Você é o motor de extração de um bot de serviços da Prefeitura do Rio. "
    "O bot fez uma pergunta ao cidadão; sua tarefa é converter a resposta em texto "
    "livre num objeto JSON com os campos pedidos, usando SOMENTE os valores permitidos "
    "quando houver lista fechada. Interprete sinônimos, gírias, números e emojis. "
    "Responda APENAS com JSON, sem comentários."
)


def _enum_of(prop: dict[str, Any]) -> Optional[list[Any]]:
    if "enum" in prop:
        return list(prop["enum"])
    if "const" in prop:
        return [prop["const"]]
    if "anyOf" in prop:
        values: list[Any] = []
        nullable = False
        for sub in prop["anyOf"]:
            if "enum" in sub:
                values.extend(sub["enum"])
            if sub.get("type") == "null":
                nullable = True
        if values:
            return values + ([None] if nullable else [])
    return None


def _property_types(property_schema: dict[str, Any]) -> set[str]:
    property_type = property_schema.get("type")
    property_types: set[str] = set()
    if isinstance(property_type, str):
        property_types.add(property_type)
    elif isinstance(property_type, list):
        property_types.update(
            candidate_type for candidate_type in property_type if isinstance(candidate_type, str)
        )
    for union_keyword in ("anyOf", "oneOf"):
        property_types.update(
            nested_schema["type"]
            for nested_schema in property_schema.get(union_keyword, [])
            if isinstance(nested_schema, dict) and isinstance(nested_schema.get("type"), str)
        )
    return property_types


def _fields_spec(
    ar: AgentResponse,
) -> list[tuple[str, str, Optional[list[Any]], bool]]:
    """Return ``(field, kind, allowed, nullable)`` for the current extraction."""
    schema = ar.payload_schema or {}
    props = schema.get("properties", {})
    spec: list[tuple[str, str, Optional[list[Any]], bool]] = []
    for name, prop in props.items():
        allowed = _enum_of(prop)
        property_types = _property_types(prop)
        nullable = "null" in property_types or (allowed is not None and None in allowed)
        if allowed is not None:
            spec.append((name, "closed", allowed, nullable))
        elif "boolean" in property_types:
            spec.append((name, "bool", None, nullable))
        elif "integer" in property_types:
            spec.append((name, "integer", None, nullable))
        elif "number" in property_types:
            spec.append((name, "number", None, nullable))
        elif property_types == {"null"}:
            spec.append((name, "null", None, True))
        else:
            spec.append((name, "text", None, nullable))
    if not spec and ar.interactive and ar.interactive.get("field"):
        field = ar.interactive["field"]
        buttons = ar.interactive.get("buttons") or []
        ids = [b.get("id") for b in buttons if b.get("id")]
        if ids and set(ids) <= {"sim", "nao"}:
            spec.append((field, "bool", None, False))
        elif ids:
            spec.append((field, "closed", ids, False))
        else:
            spec.append((field, "text", None, False))
    return spec


def _interactive_options(ar: AgentResponse) -> list[str]:
    iv = ar.interactive or {}
    if iv.get("buttons"):
        return [b["title"] for b in iv["buttons"]]
    if iv.get("sections"):
        return [r["title"] for s in iv["sections"] for r in s["rows"]]
    return []


def _route_response_schema(flows: list[dict[str, Any]]) -> dict[str, Any]:
    flow_identifiers = list(dict.fromkeys(flow["flow"] for flow in flows))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["service"],
        "properties": {"service": {"enum": [*flow_identifiers, None]}},
    }


def _extraction_response_schema(agent_response: AgentResponse) -> dict[str, Any]:
    payload_schema = agent_response.payload_schema or {}
    properties = copy.deepcopy(payload_schema.get("properties", {}))
    required = list(payload_schema.get("required", []))
    if not properties:
        for field, kind, allowed, nullable in _fields_spec(agent_response):
            if kind == "closed":
                properties[field] = {"enum": allowed or []}
            elif kind == "bool":
                properties[field] = {"type": ["boolean", "null"] if nullable else "boolean"}
            elif kind in {"integer", "number"}:
                properties[field] = {"type": [kind, "null"] if nullable else kind}
            elif kind == "null":
                properties[field] = {"type": "null"}
            else:
                properties[field] = {"type": ["string", "null"] if nullable else "string"}
            required.append(field)
    correction_targets = payload_schema.get(CORRECTION_TARGETS_SCHEMA_KEY)
    allows_correction = (
        isinstance(correction_targets, list)
        and bool(correction_targets)
        and all(isinstance(target, str) for target in correction_targets)
    )
    response_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        response_schema["required"] = required
    if allows_correction:
        properties["correcao"] = {"enum": correction_targets}
        payload_branch = {
            "required": required,
            "not": {"required": ["correcao"]},
        }
        correction_branch = {
            "required": ["correcao"],
            "not": {"anyOf": [{"required": [field_name]} for field_name in required]},
        }
        response_schema.pop("required", None)
        response_schema["oneOf"] = [payload_branch, correction_branch]
    Draft202012Validator.check_schema(response_schema)
    return response_schema


class GeminiAgent:
    """Engine-side driver backed by Google Gemini structured (JSON) output."""

    def __init__(self, model: str = "gemini-2.5-flash", api_key: Optional[str] = None) -> None:
        from google import genai

        self.model = model
        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])

    def _json(
        self,
        system: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        from google.genai import types

        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=response_schema,
                temperature=0.0,
            ),
        )
        try:
            data = strict_json_loads(resp.text or "{}")
            if isinstance(data, dict) and Draft202012Validator(response_schema).is_valid(data):
                return data
            return {}
        except (ValueError, TypeError):
            return {}

    def route(self, text: str, flows: list[dict[str, Any]]) -> Optional[str]:
        catalog = "\n".join(f"- {f['flow']}: {f['route']['description']}" for f in flows)
        prompt = (
            f"Serviços disponíveis:\n{catalog}\n\n"
            f'Mensagem do cidadão: "{text}"\n\n'
            'Devolva {"service": "<nome do serviço>"} se algum atende, '
            'ou {"service": null} se nenhum atende.'
        )
        return self._json(ROUTE_SYS, prompt, _route_response_schema(flows)).get("service")

    def extract(self, text: str, ar: AgentResponse) -> dict[str, Any]:
        spec = _fields_spec(ar)
        lines: list[str] = []
        for field, kind, allowed, nullable in spec:
            null_alternative = " ou null" if nullable and kind != "closed" else ""
            if kind == "closed":
                vals = ", ".join(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    for value in (allowed or [])
                )
                lines.append(f'- "{field}": um destes valores EXATOS: [{vals}]')
            elif kind == "bool":
                lines.append(
                    f'- "{field}": true (sim/afirmativo), false (não/negativo){null_alternative}'
                )
            elif kind == "integer":
                lines.append(f'- "{field}": número inteiro em JSON, sem aspas{null_alternative}')
            elif kind == "number":
                lines.append(f'- "{field}": número finito em JSON, sem aspas{null_alternative}')
            elif kind == "null":
                lines.append(f'- "{field}": null')
            else:
                lines.append(f'- "{field}": string JSON com o texto informado{null_alternative}')
        options = _interactive_options(ar)
        opt_hint = f"\nOpções oferecidas ao cidadão: {', '.join(options)}." if options else ""
        correction_targets = (ar.payload_schema or {}).get(CORRECTION_TARGETS_SCHEMA_KEY)
        correction_rule = (
            "- Se o cidadão quer CORRIGIR algo já informado, devolva somente "
            '{"correcao": "<identificador>"}, usando um destes identificadores EXATOS: '
            f"{json.dumps(correction_targets, ensure_ascii=False)}.\n"
            if isinstance(correction_targets, list) and correction_targets
            else ""
        )
        prompt = (
            f'Pergunta do bot: "{ar.description}"\n'
            f"Campos a extrair:\n" + "\n".join(lines) + opt_hint + "\n\n"
            f'Resposta do cidadão: "{text}"\n\n'
            "Regras:\n"
            "- Use SOMENTE os valores permitidos nas listas fechadas.\n"
            f"{correction_rule}"
            "- Devolva apenas o JSON com os campos pedidos."
        )
        return self._json(EXTRACT_SYS, prompt, _extraction_response_schema(ar))
