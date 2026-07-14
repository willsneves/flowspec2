"""Engine-side routing and extraction through subscription-authenticated Codex."""

from __future__ import annotations

import copy
import json
from typing import Any

from jsonschema import Draft202012Validator

from .codex_transport import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    CodexProvider,
    CodexTransportError,
    create_codex_provider,
    run_codex_structured_output,
    validate_codex_configuration,
)
from .json_codec import StrictJsonError, strict_json_loads
from .llm import StructuredOutputAgent


class CodexAgentError(RuntimeError):
    """The Codex engine boundary could not return valid structured output."""


def _nullable_property_schema(property_schema: dict[str, Any]) -> dict[str, Any]:
    nullable_schema = copy.deepcopy(property_schema)
    if Draft202012Validator(nullable_schema).is_valid(None):
        return nullable_schema
    if isinstance(nullable_schema.get("enum"), list):
        nullable_schema["enum"] = [*nullable_schema["enum"], None]
        return nullable_schema
    property_type = nullable_schema.get("type")
    if isinstance(property_type, str):
        nullable_schema["type"] = [property_type, "null"]
        return nullable_schema
    if isinstance(property_type, list):
        nullable_schema["type"] = [*property_type, "null"]
        return nullable_schema
    return {"anyOf": [nullable_schema, {"type": "null"}]}


def _codex_response_contract(
    response_schema: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Project the correction union into the structured-output subset Codex accepts."""

    if "oneOf" not in response_schema or "correcao" not in response_schema.get("properties", {}):
        return response_schema, False
    provider_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            property_name: _nullable_property_schema(property_schema)
            for property_name, property_schema in response_schema["properties"].items()
        },
        "required": list(response_schema["properties"]),
    }
    Draft202012Validator.check_schema(provider_schema)
    return provider_schema, True


def _lower_codex_correction_response(
    response_document: dict[str, Any],
) -> dict[str, Any]:
    correction_target = response_document.get("correcao")
    if correction_target is not None:
        return {"correcao": correction_target}
    return {
        property_name: property_value
        for property_name, property_value in response_document.items()
        if property_name != "correcao"
    }


class CodexAgent(StructuredOutputAgent):
    """Route and extract with isolated Codex structured output."""

    def __init__(
        self,
        model: str = DEFAULT_CODEX_MODEL,
        *,
        effort: str = DEFAULT_CODEX_EFFORT,
        timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
        provider: CodexProvider | None = None,
    ) -> None:
        validate_codex_configuration(model, effort, timeout_seconds)
        if provider is None:
            try:
                resolved_provider, _sdk_version = create_codex_provider(
                    model=model,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                )
            except CodexTransportError as transport_error:
                raise CodexAgentError(str(transport_error)) from transport_error
        else:
            if provider.model != model:
                raise ValueError("injected Codex provider model does not match the agent model")
            resolved_provider = provider
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds
        self._provider = resolved_provider

    def _json(
        self,
        system: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        provider_schema, lowers_correction_union = _codex_response_contract(response_schema)
        provider_prompt = (
            f"{prompt}\n"
            "Codex transport rule: return every response-schema property and use null for "
            "the inactive correction branch."
            if lowers_correction_union
            else prompt
        )
        try:
            provider_turn = run_codex_structured_output(
                self._provider,
                system=system,
                prompt=provider_prompt,
                response_schema=provider_schema,
                timeout_seconds=self.timeout_seconds,
            )
        except CodexTransportError as transport_error:
            raise CodexAgentError("Codex structured-output execution failed") from transport_error
        if not isinstance(provider_turn.text, str) or not provider_turn.text:
            return {}
        try:
            response_document = strict_json_loads(provider_turn.text)
        except (json.JSONDecodeError, StrictJsonError, TypeError, ValueError):
            return {}
        if isinstance(response_document, dict) and lowers_correction_union:
            response_document = _lower_codex_correction_response(response_document)
        if isinstance(response_document, dict) and Draft202012Validator(response_schema).is_valid(
            response_document
        ):
            return response_document
        return {}
