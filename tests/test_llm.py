"""LLM driver tests.

Helper tests (schema → field spec) always run offline. Integration tests hit a
real Gemini and only run when GEMINI_API_KEY is set AND FLOWSPEC2_RUN_LLM_TESTS=1
(so the default `uv run pytest` never makes network calls / costs).
"""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flowspec2 import FlowRuntime
from flowspec2.llm import (
    DEFAULT_EXTRACTION_SYSTEM_PROMPT,
    DEFAULT_ROUTE_SYSTEM_PROMPT,
    GeminiAgent,
    StructuredOutputAgent,
    _enum_of,
    _extraction_response_schema,
    _fields_spec,
    _route_response_schema,
    build_extraction_request,
    build_route_request,
)
from flowspec2.models import CORRECTION_TARGETS_SCHEMA_KEY, AgentResponse

# ── offline unit tests for the schema→field-spec mapping ─────────────────────


def test_enum_of_plain_and_anyof_nullable():
    assert _enum_of({"enum": ["a", "b"]}) == ["a", "b"]
    assert _enum_of({"anyOf": [{"enum": ["x"]}, {"type": "null"}]}) == ["x", None]
    assert _enum_of({"type": "string"}) is None


def test_fields_spec_from_payload_schema():
    ar = AgentResponse(
        description="?",
        payload_schema={
            "type": "object",
            "properties": {"streetlight_issue": {"enum": ["Not working", "Flickering"]}},
            "required": ["streetlight_issue"],
        },
    )
    assert _fields_spec(ar) == [
        ("streetlight_issue", "closed", ["Not working", "Flickering"], False)
    ]


def test_fields_spec_preserves_boolean_kind_for_nullable_schema():
    response = AgentResponse(
        description="?",
        payload_schema={
            "type": "object",
            "properties": {"optional_confirmation": {"type": ["boolean", "null"]}},
            "required": ["optional_confirmation"],
        },
    )

    assert _fields_spec(response) == [("optional_confirmation", "bool", None, True)]


def test_fields_spec_preserves_numeric_kinds_for_nullable_schemas() -> None:
    response = AgentResponse(
        description="?",
        payload_schema={
            "type": "object",
            "properties": {
                "quantity": {"type": "integer"},
                "distance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            },
            "required": ["quantity", "distance"],
        },
    )

    assert _fields_spec(response) == [
        ("quantity", "integer", None, False),
        ("distance", "number", None, True),
    ]


def test_fields_spec_falls_back_to_interactive_buttons():
    ar = AgentResponse(
        description="?",
        interactive={
            "field": "confirmation",
            "buttons": [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}],
        },
    )
    assert _fields_spec(ar) == [("confirmation", "bool", None, False)]
    ar2 = AgentResponse(
        description="?",
        interactive={
            "field": "identification_method",
            "buttons": [
                {"id": "brazilian_tax_id", "title": "Brazilian tax ID"},
                {"id": "govbr", "title": "Gov.br"},
            ],
        },
    )
    assert _fields_spec(ar2) == [
        ("identification_method", "closed", ["brazilian_tax_id", "govbr"], False)
    ]


def _capture_extraction_prompt(agent_response: AgentResponse) -> str:
    return build_extraction_request("answer", agent_response).prompt


def test_extraction_prompt_uses_exact_json_scalar_types_and_null() -> None:
    prompt = _capture_extraction_prompt(
        AgentResponse(
            description="Provide the values.",
            payload_schema={
                "type": "object",
                "properties": {
                    "quantity": {"type": "integer"},
                    "distance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "label": {"type": ["string", "null"]},
                    "enabled": {"type": ["boolean", "null"]},
                    "choice": {"enum": ["alpha", None]},
                },
                "required": ["quantity", "distance", "label", "enabled", "choice"],
            },
        )
    )

    assert '"quantity": a JSON integer without quotes' in prompt
    assert '"distance": a finite JSON number without quotes or null' in prompt
    assert '"label": a JSON string containing the supplied text or null' in prompt
    assert '"enabled": true (yes/affirmative), false (no/negative) or null' in prompt
    assert '"choice": one of these EXACT values: ["alpha", null]' in prompt


def test_extraction_prompt_only_mentions_correction_for_correction_hub() -> None:
    ordinary_response = AgentResponse(
        description="Provide an answer.",
        payload_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )
    correction_response = ordinary_response.model_copy(
        update={
            "payload_schema": {
                **(ordinary_response.payload_schema or {}),
                CORRECTION_TARGETS_SCHEMA_KEY: ["address", "brazilian_tax_id"],
            }
        }
    )

    assert "CORRECT" not in _capture_extraction_prompt(ordinary_response)
    assert "CORRECT" in _capture_extraction_prompt(correction_response)


def test_route_response_schema_closes_service_to_catalog() -> None:
    response_schema = _route_response_schema(
        [
            {"flow": "repair_light"},
            {"flow": "repair_road"},
            {"flow": "repair_light"},
        ]
    )

    assert response_schema["additionalProperties"] is False
    assert response_schema["properties"]["service"]["enum"] == [
        "repair_light",
        "repair_road",
        None,
    ]


def test_route_request_renders_description_first_and_trigger_phrases_as_examples() -> None:
    request = build_route_request(
        "the streetlight on the corner is out",
        [
            {
                "flow": "repair_light",
                "route": {
                    "description": "Report public streetlight issues.",
                    "trigger_phrases": ["streetlight out", "flickering light"],
                },
            },
            {
                "flow": "repair_road",
                "route": {"description": "Report road-surface issues."},
            },
        ],
    )

    assert request.system == DEFAULT_ROUTE_SYSTEM_PROMPT
    assert request.prompt == (
        "Service catalog as JSON:\n"
        '[{"description":"Report public streetlight issues.","service":"repair_light",'
        '"trigger_phrases":["streetlight out","flickering light"]},{"description":"Report '
        'road-surface issues.","service":"repair_road","trigger_phrases":[]}]\n\n'
        "Use description as each service's primary definition. trigger_phrases contains only "
        "examples of compatible messages; do not treat them as an exclusive list or a match "
        "guarantee.\n\n"
        'User message: "the streetlight on the corner is out"\n\n'
        'Return {"service": "<service name>"} if one applies, or {"service": null} if none '
        "applies."
    )
    assert request.response_schema["properties"]["service"]["enum"] == [
        "repair_light",
        "repair_road",
        None,
    ]


def test_extraction_request_preserves_extract_hint_in_closed_response_schema() -> None:
    request = build_extraction_request(
        "everything is dark",
        AgentResponse(
            description="What is the issue?",
            payload_schema={
                "type": "object",
                "properties": {
                    "defect": {
                        "description": "Map darkness to Off.",
                        "enum": ["Off", "Flashing"],
                    }
                },
                "required": ["defect"],
            },
        ),
    )

    assert request.system == DEFAULT_EXTRACTION_SYSTEM_PROMPT
    assert request.prompt == (
        'System question: "What is the issue?"\n'
        'Fields to extract:\n- "defect": one of these EXACT values: ["Off", "Flashing"]\n\n'
        'User response: "everything is dark"\n\n'
        "Rules:\n- Use ONLY permitted values from closed lists.\n"
        "- Return only JSON with the requested fields."
    )
    assert request.response_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "defect": {
                "description": "Map darkness to Off.",
                "enum": ["Off", "Flashing"],
            }
        },
        "required": ["defect"],
    }


def test_default_system_prompts_are_provider_and_deployment_neutral() -> None:
    combined_prompts = f"{DEFAULT_ROUTE_SYSTEM_PROMPT} {DEFAULT_EXTRACTION_SYSTEM_PROMPT}"

    assert "municipality" not in combined_prompts.lower()
    assert "Rio" not in combined_prompts


def test_structured_output_agent_uses_custom_system_prompts() -> None:
    captured_system_prompts: list[str] = []

    class CapturingAgent(StructuredOutputAgent):
        def _json(
            self,
            system: str,
            prompt: str,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            captured_system_prompts.append(system)
            return {"service": None} if "service" in response_schema["properties"] else {}

    agent = CapturingAgent(
        route_system_prompt="custom route",
        extraction_system_prompt="custom extraction",
    )
    agent.route(
        "hello",
        [{"flow": "support", "route": {"description": "Provides support."}}],
    )
    agent.extract(
        "hello",
        AgentResponse(
            description="What happened?",
            payload_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
    )

    assert captured_system_prompts == ["custom route", "custom extraction"]


def test_extraction_response_schema_supports_payload_or_correction() -> None:
    response_schema = _extraction_response_schema(
        AgentResponse(
            description="?",
            payload_schema={
                "type": "object",
                "properties": {"defect": {"enum": ["off", "flashing"]}},
                "required": ["defect"],
                CORRECTION_TARGETS_SCHEMA_KEY: ["defect", "address"],
            },
        )
    )

    assert response_schema["additionalProperties"] is False
    assert response_schema["properties"]["defect"] == {"enum": ["off", "flashing"]}
    assert response_schema["properties"]["correction"] == {"enum": ["defect", "address"]}
    assert {tuple(branch["required"]) for branch in response_schema["oneOf"]} == {
        ("defect",),
        ("correction",),
    }


def test_extraction_response_schema_does_not_offer_correction_outside_hub() -> None:
    response_schema = _extraction_response_schema(
        AgentResponse(
            description="?",
            payload_schema={
                "type": "object",
                "properties": {"defect": {"enum": ["off", "flashing"]}},
                "required": ["defect"],
            },
        )
    )

    assert response_schema["required"] == ["defect"]
    assert "correction" not in response_schema["properties"]


def test_gemini_request_uses_json_schema_and_rejects_invalid_provider_output() -> None:
    captured_config: list[Any] = []

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> SimpleNamespace:
            captured_config.append(kwargs["config"])
            return SimpleNamespace(text='{"service":"unknown"}')

    agent = object.__new__(GeminiAgent)
    agent.model = "test-model"
    cast(Any, agent).client = SimpleNamespace(models=FakeModels())
    response_schema = _route_response_schema([{"flow": "repair_light"}])

    assert agent._json("system", "prompt", response_schema) == {}
    assert captured_config[0].response_json_schema == response_schema


# ── gated integration tests (real Gemini) ───────────────────────────────────

_RUN_LLM = (
    bool(os.environ.get("GEMINI_API_KEY")) and os.environ.get("FLOWSPEC2_RUN_LLM_TESTS") == "1"
)
pytestmark_integration = pytest.mark.skipif(
    not _RUN_LLM, reason="set GEMINI_API_KEY + FLOWSPEC2_RUN_LLM_TESTS=1"
)


def _conversational(doc: dict) -> dict:
    d = copy.deepcopy(doc)
    d.pop("auto_flow", None)
    d["path"] = [s for s in d["path"] if s.get("confirm") != "service_confirmed"]
    return d


@pytestmark_integration
def test_gemini_routes_in_and_out(streetlight_document):
    from flowspec2.llm import GeminiAgent

    agent = GeminiAgent()
    assert agent.route("the streetlight on my street is out", [streetlight_document]) == (
        "streetlight_repair"
    )
    assert agent.route("I want to pay an overdue tax bill", [streetlight_document]) != (
        "streetlight_repair"
    )


@pytestmark_integration
def test_gemini_extracts_closed_token():
    from flowspec2.llm import GeminiAgent

    agent = GeminiAgent()
    ar = AgentResponse(
        description="What is the streetlight issue?",
        payload_schema={
            "type": "object",
            "properties": {
                "streetlight_issue": {
                    "enum": [
                        "Not working",
                        "Flickering",
                        "On during daylight",
                        "Hanging",
                        "Damaged",
                        "Noisy",
                    ]
                }
            },
            "required": ["streetlight_issue"],
        },
    )
    out = agent.extract("everything is dark; the light has been out for days", ar)
    assert out.get("streetlight_issue") == "Not working"


@pytestmark_integration
async def test_gemini_drives_full_conversation(streetlight_document):
    import asyncio

    from flowspec2.llm import GeminiAgent

    agent = GeminiAgent()
    rt = FlowRuntime(_conversational(streetlight_document))
    state = rt.new_state("llm-e2e")
    state = await rt.execute(state, {})  # enter -> asks defect
    for msg in [
        "the light is out",  # issue -> Not working
        "it is a single streetlight",  # count -> single (no outage-pattern branch)
        "it is on the street",  # location -> Street (sports-court gate is closed)
        "Acacia Street, 50, Downtown",  # address
        "yes, confirm it",  # confirm address
        "near the school",  # reference point
        "I prefer to remain anonymous",  # identification
        "open the service request",  # confirm ticket -> open
    ]:
        if state.status == "completed":
            break
        payload = await asyncio.to_thread(agent.extract, msg, state.agent_response)
        state = await rt.execute(state, payload)
    assert state.status == "completed"
    assert state.data.get("protocol_id", "").startswith("REQ-")
