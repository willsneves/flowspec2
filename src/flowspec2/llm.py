"""An LLM driver — the *non-deterministic execution* side of the boundary.

flowspec2 pins the rails (states, closed value-domains, transitions). This module
is the agent that reasons *within* them: given a flow's ``route.description`` and
non-exclusive ``route.trigger_phrases`` examples it decides whether to enter,
and at each pause it reads the user's free text/voice and the node's
``payload_schema`` (or the interactive options) and
extracts the **closed token** for the slot. The flowspec2 validators then enforce
the rail — an out-of-domain extraction is rejected and the node re-asks.

Default provider: Google Gemini (``gemini-2.5-flash``), via ``GEMINI_API_KEY``.
The driver is a thin protocol —
``route`` + ``extract`` — so any provider can implement it.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from jsonschema import Draft202012Validator

from .json_codec import strict_json_loads
from .models import CORRECTION_TARGETS_SCHEMA_KEY, AgentResponse

DEFAULT_ROUTE_SYSTEM_PROMPT = (
    "Você roteia mensagens para um catálogo de serviços. "
    "Dada a mensagem da pessoa usuária, decida qual serviço (se algum) atende ao pedido. "
    "Responda APENAS com JSON."
)

DEFAULT_EXTRACTION_SYSTEM_PROMPT = (
    "Você extrai dados estruturados para um fluxo conversacional. "
    "O sistema fez uma pergunta à pessoa usuária; converta a resposta em texto "
    "livre num objeto JSON com os campos pedidos, usando SOMENTE os valores permitidos "
    "quando houver lista fechada. Interprete sinônimos, gírias, números e emojis. "
    "Responda APENAS com JSON, sem comentários."
)


@dataclass(frozen=True)
class StructuredOutputRequest:
    """Exact provider-neutral inputs for one closed structured-output turn."""

    system: str
    prompt: str
    response_schema: dict[str, Any]


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


def build_route_request(
    text: str,
    flows: list[dict[str, Any]],
    *,
    system_prompt: str = DEFAULT_ROUTE_SYSTEM_PROMPT,
) -> StructuredOutputRequest:
    """Render the exact routing request, including non-exclusive trigger examples."""

    catalog = [
        {
            "service": flow["flow"],
            "description": flow["route"]["description"],
            "trigger_phrases": list(flow["route"].get("trigger_phrases", [])),
        }
        for flow in flows
    ]
    serialized_catalog = json.dumps(
        catalog,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    prompt = (
        f"Catálogo de serviços em JSON:\n{serialized_catalog}\n\n"
        "Use description como a definição principal de cada serviço. "
        "trigger_phrases contém apenas exemplos de mensagens compatíveis; "
        "não trate esses exemplos como lista exclusiva nem como garantia de correspondência.\n\n"
        f'Mensagem da pessoa usuária: "{text}"\n\n'
        'Devolva {"service": "<nome do serviço>"} se algum atende, '
        'ou {"service": null} se nenhum atende.'
    )
    return StructuredOutputRequest(
        system=system_prompt,
        prompt=prompt,
        response_schema=_route_response_schema(flows),
    )


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


def build_extraction_request(
    text: str,
    agent_response: AgentResponse,
    *,
    system_prompt: str = DEFAULT_EXTRACTION_SYSTEM_PROMPT,
) -> StructuredOutputRequest:
    """Render the exact extraction request, preserving payload-schema guidance."""

    spec = _fields_spec(agent_response)
    lines: list[str] = []
    for field, kind, allowed, nullable in spec:
        null_alternative = " ou null" if nullable and kind != "closed" else ""
        if kind == "closed":
            values = ", ".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                for value in (allowed or [])
            )
            lines.append(f'- "{field}": um destes valores EXATOS: [{values}]')
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
    options = _interactive_options(agent_response)
    options_hint = f"\nOpções oferecidas à pessoa usuária: {', '.join(options)}." if options else ""
    correction_targets = (agent_response.payload_schema or {}).get(CORRECTION_TARGETS_SCHEMA_KEY)
    correction_rule = (
        "- Se a pessoa usuária quer CORRIGIR algo já informado, devolva somente "
        '{"correcao": "<identificador>"}, usando um destes identificadores EXATOS: '
        f"{json.dumps(correction_targets, ensure_ascii=False)}.\n"
        if isinstance(correction_targets, list) and correction_targets
        else ""
    )
    prompt = (
        f'Pergunta do sistema: "{agent_response.description}"\n'
        f"Campos a extrair:\n" + "\n".join(lines) + options_hint + "\n\n"
        f'Resposta da pessoa usuária: "{text}"\n\n'
        "Regras:\n"
        "- Use SOMENTE os valores permitidos nas listas fechadas.\n"
        f"{correction_rule}"
        "- Devolva apenas o JSON com os campos pedidos."
    )
    return StructuredOutputRequest(
        system=system_prompt,
        prompt=prompt,
        response_schema=_extraction_response_schema(agent_response),
    )


class StructuredOutputAgent:
    """Provider-neutral routing and extraction over one closed JSON completion."""

    def __init__(
        self,
        *,
        route_system_prompt: str = DEFAULT_ROUTE_SYSTEM_PROMPT,
        extraction_system_prompt: str = DEFAULT_EXTRACTION_SYSTEM_PROMPT,
    ) -> None:
        self.route_system_prompt = route_system_prompt
        self.extraction_system_prompt = extraction_system_prompt

    def _json(
        self,
        system: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def route(self, text: str, flows: list[dict[str, Any]]) -> Optional[str]:
        request = build_route_request(text, flows, system_prompt=self.route_system_prompt)
        return self._json(request.system, request.prompt, request.response_schema).get("service")

    def extract(self, text: str, ar: AgentResponse) -> dict[str, Any]:
        request = build_extraction_request(
            text,
            ar,
            system_prompt=self.extraction_system_prompt,
        )
        return self._json(request.system, request.prompt, request.response_schema)


class GeminiAgent(StructuredOutputAgent):
    """Engine-side driver backed by Google Gemini structured output."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: Optional[str] = None,
        *,
        route_system_prompt: str = DEFAULT_ROUTE_SYSTEM_PROMPT,
        extraction_system_prompt: str = DEFAULT_EXTRACTION_SYSTEM_PROMPT,
    ) -> None:
        from google import genai

        super().__init__(
            route_system_prompt=route_system_prompt,
            extraction_system_prompt=extraction_system_prompt,
        )
        self.model = model
        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])

    def _json(
        self,
        system: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        from google.genai import types

        response = self.client.models.generate_content(
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
            response_document = strict_json_loads(response.text or "{}")
            if isinstance(response_document, dict) and Draft202012Validator(
                response_schema
            ).is_valid(response_document):
                return response_document
            return {}
        except (ValueError, TypeError):
            return {}
