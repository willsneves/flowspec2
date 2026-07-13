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

import json
import os
from typing import Any, Optional

from .models import AgentResponse

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
            return values + (["null"] if nullable else [])
    return None


def _fields_spec(ar: AgentResponse) -> list[tuple[str, str, Optional[list[Any]]]]:
    """(field, kind, allowed) for each key the LLM should produce this turn."""
    schema = ar.payload_schema or {}
    props = schema.get("properties", {})
    spec: list[tuple[str, str, Optional[list[Any]]]] = []
    for name, prop in props.items():
        allowed = _enum_of(prop)
        if allowed is not None:
            spec.append((name, "closed", allowed))
        elif prop.get("type") == "boolean":
            spec.append((name, "bool", None))
        else:
            spec.append((name, "text", None))
    if not spec and ar.interactive and ar.interactive.get("field"):
        field = ar.interactive["field"]
        buttons = ar.interactive.get("buttons") or []
        ids = [b.get("id") for b in buttons if b.get("id")]
        if ids and set(ids) <= {"sim", "nao"}:
            spec.append((field, "bool", None))
        elif ids:
            spec.append((field, "closed", ids))
        else:
            spec.append((field, "text", None))
    return spec


def _interactive_options(ar: AgentResponse) -> list[str]:
    iv = ar.interactive or {}
    if iv.get("buttons"):
        return [b["title"] for b in iv["buttons"]]
    if iv.get("sections"):
        return [r["title"] for s in iv["sections"] for r in s["rows"]]
    return []


class GeminiAgent:
    """Engine-side driver backed by Google Gemini structured (JSON) output."""

    def __init__(self, model: str = "gemini-2.5-flash", api_key: Optional[str] = None) -> None:
        from google import genai

        self.model = model
        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])

    def _json(self, system: str, prompt: str) -> dict[str, Any]:
        from google.genai import types

        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        try:
            data = json.loads(resp.text or "{}")
            return data if isinstance(data, dict) else {}
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
        return self._json(ROUTE_SYS, prompt).get("service")

    def extract(self, text: str, ar: AgentResponse) -> dict[str, Any]:
        spec = _fields_spec(ar)
        lines: list[str] = []
        for field, kind, allowed in spec:
            if kind == "closed":
                vals = ", ".join("null" if v is None else str(v) for v in (allowed or []))
                lines.append(f'- "{field}": um destes valores EXATOS: [{vals}]')
            elif kind == "bool":
                lines.append(f'- "{field}": true (sim/afirmativo) ou false (não/negativo)')
            else:
                lines.append(f'- "{field}": texto livre (transcreva o que o cidadão informou)')
        options = _interactive_options(ar)
        opt_hint = f"\nOpções oferecidas ao cidadão: {', '.join(options)}." if options else ""
        prompt = (
            f'Pergunta do bot: "{ar.description}"\n'
            f"Campos a extrair:\n" + "\n".join(lines) + opt_hint + "\n\n"
            f'Resposta do cidadão: "{text}"\n\n'
            "Regras:\n"
            "- Use SOMENTE os valores permitidos nas listas fechadas.\n"
            '- Se o cidadão quer CORRIGIR algo já informado (ex.: "o endereço está errado"), '
            'devolva {"correcao": "<o que corrigir: defeito, endereço, cpf, etc>"} em vez do campo.\n'
            "- Devolva apenas o JSON com os campos pedidos."
        )
        return self._json(EXTRACT_SYS, prompt)
