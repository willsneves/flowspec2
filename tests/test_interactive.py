"""End-to-end format fixtures for interactive rendering and payload binding."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from flowspec2 import FlowRuntime, load_flow
from flowspec2.compiler import compile_flow
from flowspec2.interactive import (
    BODY_MAX,
    BUTTON_ID_MAX,
    BUTTON_TITLE_MAX,
    ROW_DESC_MAX,
    ROW_ID_MAX,
    ROW_TITLE_MAX,
    build_buttons,
    build_cta_url,
    build_flow,
    build_list,
    options_from_domain,
)
from flowspec2.llm import _extraction_response_schema
from flowspec2.models import CORRECTION_REQUESTED_INTERNAL_KEY, CORRECTION_TARGETS_SCHEMA_KEY

INTERACTIVE_FIXTURES = Path(__file__).with_name("fixtures") / "interactive"
CONDITIONAL_OPTIONS_FLOW = INTERACTIVE_FIXTURES / "conditional_options.flow.json"
BOOLEAN_OPTIONS_FLOW = INTERACTIVE_FIXTURES / "boolean_options.flow.json"
BOOLEAN_BUTTONS = [
    {"id": "true", "title": "Yes"},
    {"id": "false", "title": "No"},
]


def _button_identifiers(state_interactive: dict[str, Any] | None) -> set[str]:
    assert state_interactive is not None
    return {button["id"] for button in state_interactive["buttons"]}


def _buttons(state_interactive: dict[str, Any] | None) -> list[dict[str, str]]:
    assert state_interactive is not None
    return state_interactive["buttons"]


async def test_options_when_fixture_renders_only_gate_visible_buttons() -> None:
    optional_runtime = FlowRuntime.from_path(str(CONDITIONAL_OPTIONS_FLOW))
    optional_state = await optional_runtime.execute(optional_runtime.new_state("optional"), {})

    assert optional_state.agent_response is not None
    assert _button_identifiers(optional_state.agent_response.interactive) == {
        "identified",
        "anonymous",
    }

    required_document = load_flow(CONDITIONAL_OPTIONS_FLOW)
    required_document["config"]["identification_required"] = True
    required_runtime = FlowRuntime(required_document)
    required_state = await required_runtime.execute(required_runtime.new_state("required"), {})

    assert required_state.agent_response is not None
    assert _button_identifiers(required_state.agent_response.interactive) == {"identified"}


async def test_interactive_field_is_bound_to_the_enclosing_slot() -> None:
    runtime = FlowRuntime.from_path(str(CONDITIONAL_OPTIONS_FLOW))

    state = await runtime.execute(
        runtime.new_state("field-binding"),
        {"identification_choice_button": "identified"},
    )

    assert state.status == "completed"
    assert state.data["identification_choice"] == "identified"


async def test_boolean_domain_renders_collection_and_all_confirmation_buttons() -> None:
    runtime = FlowRuntime.from_path(str(BOOLEAN_OPTIONS_FLOW))

    state = await runtime.execute(runtime.new_state("boolean-buttons"), {})
    assert state.agent_response is not None
    assert _buttons(state.agent_response.interactive) == BOOLEAN_BUTTONS

    state = await runtime.execute(state, {"boolean_choice_button": "false"})
    assert state.data["boolean_choice"] is False
    assert state.agent_response is not None
    assert _buttons(state.agent_response.interactive) == BOOLEAN_BUTTONS

    state = await runtime.execute(state, {"summary_confirmation_button": "true"})
    assert state.data["summary_confirmed"] is True
    assert state.agent_response is not None
    assert _buttons(state.agent_response.interactive) == BOOLEAN_BUTTONS

    state = await runtime.execute(state, {"simple_confirmation_button": "true"})
    assert state.data["simple_confirmed"] is True
    assert state.agent_response is not None
    hub_interactive = state.agent_response.interactive
    assert _buttons(hub_interactive) == BOOLEAN_BUTTONS
    assert hub_interactive is not None
    assert hub_interactive["field"] == "hub_confirmation_button"


async def test_boolean_options_when_uses_boolean_domain_tokens() -> None:
    flow_document = load_flow(BOOLEAN_OPTIONS_FLOW)
    flow_document["config"]["identification_required"] = True
    runtime = FlowRuntime(flow_document)

    state = await runtime.execute(runtime.new_state("boolean-visibility"), {})

    assert state.agent_response is not None
    assert _buttons(state.agent_response.interactive) == [{"id": "true", "title": "Yes"}]


async def test_summary_without_interactive_exposes_and_enforces_boolean_schema() -> None:
    flow_document = load_flow(BOOLEAN_OPTIONS_FLOW)
    flow_document["path"][1].pop("interactive")
    runtime = FlowRuntime(flow_document)
    state = runtime.new_state("summary-schema", data={"boolean_choice": True})
    state.metadata.saved = True

    state = await runtime.execute(state, {})

    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    assert state.agent_response.payload_schema["properties"]["summary_confirmed"]["type"] == (
        "boolean"
    )

    state = await runtime.execute(state, {"summary_confirmed": "banana"})

    assert state.status == "progress"
    assert state.agent_response is not None
    assert state.agent_response.error_message is not None


async def test_hub_without_interactive_offers_exclusive_boolean_or_correction_schema() -> None:
    flow_document = load_flow(BOOLEAN_OPTIONS_FLOW)
    flow_document["path"][3].pop("interactive")
    runtime = FlowRuntime(flow_document)
    state = runtime.new_state(
        "hub-schema",
        data={
            "boolean_choice": True,
            "summary_confirmed": True,
            "simple_confirmed": True,
        },
    )
    state.metadata.saved = True

    state = await runtime.execute(state, {})

    assert state.agent_response is not None
    assert state.agent_response.payload_schema is not None
    assert state.agent_response.payload_schema[CORRECTION_TARGETS_SCHEMA_KEY] == ["boolean_choice"]
    response_schema = _extraction_response_schema(state.agent_response)
    assert response_schema["properties"]["correction"] == {"enum": ["boolean_choice"]}
    assert {tuple(branch["required"]) for branch in response_schema["oneOf"]} == {
        ("hub_confirmed",),
        ("correction",),
    }


async def test_correction_hub_rejects_fuzzy_target_and_accepts_exact_slot_id() -> None:
    runtime = FlowRuntime.from_path(str(BOOLEAN_OPTIONS_FLOW))
    state = runtime.new_state(
        "exact-correction-target",
        data={
            "boolean_choice": True,
            "summary_confirmed": True,
            "simple_confirmed": True,
        },
    )
    state.metadata.saved = True

    state = await runtime.execute(state, {"correction": "choice"})

    assert state.agent_response is not None
    assert state.data["boolean_choice"] is True
    assert CORRECTION_REQUESTED_INTERNAL_KEY not in state.internal

    state = await runtime.execute(state, {"correction": "boolean_choice"})

    assert state.agent_response is not None
    assert "boolean_choice" not in state.data
    assert CORRECTION_REQUESTED_INTERNAL_KEY not in state.internal


def test_compiler_rejects_one_interactive_field_bound_to_different_slots() -> None:
    flow_document = load_flow(CONDITIONAL_OPTIONS_FLOW)
    flow_document["domains"]["SecondChoice"] = {
        "type": "categorical",
        "values": ["second"],
    }
    flow_document["slots"]["second_choice"] = {
        "domain": "SecondChoice",
        "required": True,
    }
    second_step = copy.deepcopy(flow_document["path"][0])
    second_step["step"] = "collect_second_choice"
    second_step["slot"] = "second_choice"
    second_step["interactive"]["from_domain"] = "SecondChoice"
    second_step["interactive"].pop("options_when")
    flow_document["path"].append(second_step)

    with pytest.raises(
        ValueError,
        match="already binds it to 'identification_choice'",
    ):
        compile_flow(flow_document)


def test_compiler_rejects_interactive_domain_different_from_slot_domain() -> None:
    flow_document = load_flow(CONDITIONAL_OPTIONS_FLOW)
    flow_document["domains"]["DifferentChoice"] = {
        "type": "categorical",
        "values": ["different"],
    }
    flow_document["path"][0]["interactive"]["from_domain"] = "DifferentChoice"

    with pytest.raises(ValueError, match="must match .*identification_choice.*domain"):
        compile_flow(flow_document)


def test_compiler_rejects_options_when_value_outside_domain() -> None:
    flow_document = load_flow(CONDITIONAL_OPTIONS_FLOW)
    flow_document["path"][0]["interactive"]["options_when"][0]["value"] = "unknown"

    with pytest.raises(ValueError, match=r"options_when\[0\].value is not in domain"):
        compile_flow(flow_document)


async def test_conflicting_slot_and_interactive_payload_fields_fail_with_log_id() -> None:
    runtime = FlowRuntime.from_path(str(CONDITIONAL_OPTIONS_FLOW))

    state = await runtime.execute(
        runtime.new_state("field-conflict"),
        {
            "identification_choice": "anonymous",
            "identification_choice_button": "identified",
        },
    )

    assert state.status == "error"
    assert state.agent_response is not None
    assert state.agent_response.error_message is not None
    assert state.agent_response.log_id is not None


def test_button_builder_preserves_unicode_at_limits_and_never_truncates() -> None:
    body = "★" * BODY_MAX
    title = "●" * BUTTON_TITLE_MAX

    envelope = build_buttons(body, [{"id": "choice", "title": title}])

    assert envelope["status"] == "ok"
    assert envelope["interactive"]["body"]["text"] == body
    assert envelope["interactive"]["action"]["buttons"][0]["reply"]["title"] == title
    assert build_buttons(f"{body}★", [{"id": "choice", "title": title}])["status"] == ("error")
    assert build_buttons(body, [{"id": "choice", "title": f"{title}●"}])["status"] == ("error")


@pytest.mark.parametrize("invalid_identifier", ["", "   ", "x" * (BUTTON_ID_MAX + 1)])
def test_button_builder_rejects_invalid_identifiers(invalid_identifier: str) -> None:
    assert (
        build_buttons("Choose", [{"id": invalid_identifier, "title": "One"}])["status"] == "error"
    )


def test_button_builder_rejects_duplicate_identifiers() -> None:
    envelope = build_buttons(
        "Choose",
        [
            {"id": "same", "title": "One"},
            {"id": "same", "title": "Two"},
        ],
    )

    assert envelope == {"status": "error", "error": "duplicate button id: same"}


def test_list_builder_preserves_unicode_at_limits_and_never_truncates() -> None:
    body = "★" * BODY_MAX
    title = "●" * ROW_TITLE_MAX
    description = "◆" * ROW_DESC_MAX
    sections = [
        {
            "title": "Options",
            "rows": [{"id": "choice", "title": title, "description": description}],
        }
    ]

    envelope = build_list(body, sections)

    assert envelope["status"] == "ok"
    assert envelope["interactive"]["body"]["text"] == body
    assert envelope["interactive"]["action"]["sections"] == sections
    overlong_title = copy.deepcopy(sections)
    overlong_title[0]["rows"][0]["title"] += "●"
    assert build_list(body, overlong_title)["status"] == "error"
    overlong_description = copy.deepcopy(sections)
    overlong_description[0]["rows"][0]["description"] += "◆"
    assert build_list(body, overlong_description)["status"] == "error"


@pytest.mark.parametrize("invalid_identifier", ["", "   ", "x" * (ROW_ID_MAX + 1)])
def test_list_builder_rejects_invalid_row_identifiers(invalid_identifier: str) -> None:
    sections = [{"rows": [{"id": invalid_identifier, "title": "Choice"}]}]

    assert build_list("Choose", sections)["status"] == "error"


def test_list_builder_rejects_duplicate_row_identifiers_across_sections() -> None:
    sections = [
        {"title": "First", "rows": [{"id": "same", "title": "One"}]},
        {"title": "Second", "rows": [{"id": "same", "title": "Two"}]},
    ]

    assert build_list("Choose", sections) == {
        "status": "error",
        "error": "duplicate row id: same",
    }


@pytest.mark.parametrize(
    ("url", "display_text"),
    [
        ("http://example.test", "Open"),
        ("https://", "Open"),
        ("https://example.test/with space", "Open"),
        ("https://example.test", ""),
        ("https://example.test", "x" * (BUTTON_TITLE_MAX + 1)),
    ],
)
def test_cta_builder_rejects_invalid_fields(url: str, display_text: str) -> None:
    assert build_cta_url("Continue", url, display_text)["status"] == "error"


def test_flow_builder_rejects_invalid_fields_without_truncating() -> None:
    assert build_flow("", "Continue")["status"] == "error"
    assert build_flow("flow", "Continue", cta="x" * (BUTTON_TITLE_MAX + 1))["status"] == "error"
    assert build_flow("flow", "x" * (BODY_MAX + 1))["status"] == "error"


def test_domain_projection_rejects_duplicate_values_and_rendered_identifiers() -> None:
    interactive = {"kind": "list", "field": "choice", "from_domain": "Choice"}

    with pytest.raises(ValueError, match="duplicate interactive option value"):
        options_from_domain(
            interactive,
            {"Choice": {"type": "categorical", "values": ["same", "same"]}},
        )
    with pytest.raises(ValueError, match="duplicate interactive option identifier"):
        options_from_domain(
            interactive,
            {"Choice": {"type": "categorical", "values": ["Same", "same"]}},
        )


def test_compiler_validates_unicode_list_row_title_and_description_lengths() -> None:
    boundary_document = load_flow(CONDITIONAL_OPTIONS_FLOW)
    boundary_interactive = boundary_document["path"][0]["interactive"]
    boundary_interactive["kind"] = "list"
    boundary_interactive.pop("options_when")
    boundary_document["domains"]["IdentificationChoice"] = {
        "type": "categorical",
        "values": ["★" * ROW_TITLE_MAX],
        "rows": [
            {
                "value": "★" * ROW_TITLE_MAX,
                "description": "●" * ROW_DESC_MAX,
            }
        ],
    }

    compile_flow(boundary_document)

    overlong_title_document = copy.deepcopy(boundary_document)
    overlong_title_document["domains"]["IdentificationChoice"]["values"] = [
        "★" * (ROW_TITLE_MAX + 1)
    ]
    overlong_title_document["domains"]["IdentificationChoice"]["rows"][0]["value"] = "★" * (
        ROW_TITLE_MAX + 1
    )
    with pytest.raises(ValueError, match="list row title that is too long"):
        compile_flow(overlong_title_document)

    overlong_description_document = copy.deepcopy(boundary_document)
    overlong_description_document["domains"]["IdentificationChoice"]["rows"][0]["description"] += (
        "●"
    )
    with pytest.raises(ValueError, match="list row description that is too long"):
        compile_flow(overlong_description_document)
