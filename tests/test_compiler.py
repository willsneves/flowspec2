"""Compiler-level referential integrity after subflow expansion."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from flowspec2 import FlowLinkError
from flowspec2.compiler import compile_flow
from flowspec2.models import ServiceState
from flowspec2.nodes import NEXT, FlowContext, NodeDesc
from flowspec2.subflows import SubflowBuild, SubflowDefinition, SubflowRegistry


async def _finish_test_subflow(state: ServiceState) -> ServiceState:
    state.agent_response = None
    return state


def _route_test_subflow(state: ServiceState) -> str:
    del state
    return NEXT


class _BrokenExposureSubflow:
    name = "broken_exposure"
    major = 1

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild:
        del ctx, with_cfg
        return SubflowBuild(
            descriptors=[
                NodeDesc("actual_node", _finish_test_subflow, _route_test_subflow),
            ],
            entry_id="actual_node",
            node_for_slot={"external_slot": "missing_node"},
        )


def test_compiler_discovers_slots_exposed_by_subflows(
    luminaria_doc: dict[str, Any],
) -> None:
    compiled_flow = compile_flow(luminaria_doc)

    assert {
        "address": "collect_address",
        "cpf": "collect_cpf",
        "email": "collect_email",
        "name": "collect_name",
    }.items() <= compiled_flow.ctx.node_for_slot.items()


def test_compiler_rejects_unknown_native_path_slot(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["path"][1]["slot"] = "unknown_slot"

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_NATIVE_PATH_SLOT"):
        compile_flow(invalid_flow)


def test_compiler_rejects_unknown_required_slot(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["slots"]["ponto_referencia"]["requires"] = ["unknown_slot"]

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_REQUIRED_SLOT"):
        compile_flow(invalid_flow)


def test_compiler_rejects_unknown_derive_source(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["derive"][0]["from"][0] = "unknown_slot"

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_SOURCE"):
        compile_flow(invalid_flow)


def test_compiler_rejects_unknown_correctable_slot(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["confirm"]["correctable"][2] = "unknown_slot"

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_CORRECTABLE_SLOT"):
        compile_flow(invalid_flow)


def test_compiler_rejects_unknown_terminal_input(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["terminal"]["input"][1]["slot"] = "unknown_slot"

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_TERMINAL_INPUT"):
        compile_flow(invalid_flow)


def test_compiler_rejects_conflicting_correctable_confirmation_definitions(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["confirm"]["interactive"]["field"] = "different_confirmation"

    with pytest.raises(
        ValueError,
        match=r"correctable path interactive conflicts with \$\.confirm\.interactive",
    ):
        compile_flow(invalid_flow)


def test_compiler_rejects_subflow_exposure_bound_to_unknown_node() -> None:
    subflow_registry = SubflowRegistry()
    subflow_registry.register(
        _BrokenExposureSubflow(),
        definition=SubflowDefinition(
            ref="broken_exposure@1",
            description="Expose a deliberately misbound test slot.",
            configuration_schema={"type": "object", "additionalProperties": False},
            exposed_slots=frozenset({"external_slot"}),
            exposed_slot_schemas={"external_slot": {}},
        ),
    )
    flow_document = {
        "schema": "flowspec/2",
        "flow": "broken_exposure_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise invalid subflow exposure."},
        "domains": {"Unused": {"type": "free_text"}},
        "path": [{"use": "broken_exposure@1"}],
        "uses": [{"ref": "broken_exposure@1"}],
    }

    with pytest.raises(ValueError, match="through unknown node 'missing_node'"):
        compile_flow(flow_document, subflows=subflow_registry)


def test_compiler_requires_confirm_block_for_correctable_path(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow.pop("confirm")

    with pytest.raises(ValueError, match="requires the top-level confirm block"):
        compile_flow(invalid_flow)


def test_compiler_rejects_unknown_override_gate(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["overrides"]["gates"]["missing_step"] = {"eq": ["slots.x", True]}

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_GATE_STEP"):
        compile_flow(invalid_flow)


def test_compiler_rejects_unknown_derive_anchor(
    luminaria_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(luminaria_doc)
    invalid_flow["derive"][0]["after"] = "missing_step"

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_ANCHOR"):
        compile_flow(invalid_flow)


def test_compiler_rejects_duplicate_inline_and_override_gate(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["path"][0]["ask_when"] = {"is_present": "slots.buraco_tamanho"}
    invalid_flow["overrides"] = {"gates": {"collect_tipo": {"is_present": "slots.buraco_tamanho"}}}

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_DUPLICATE_GATE_SOURCE"):
        compile_flow(invalid_flow)


def test_compiler_preserves_declaration_order_for_shared_derive_anchor(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["derive"].append(
        {
            "writes": "second_classification",
            "from": ["luminaria_defeito"],
            "after": "collect_localizacao",
            "lookup": {"Apagada": "secondary"},
            "default": "$from[0]",
        }
    )

    graph_node_ids = list(compile_flow(flow_document).graph.get_graph().nodes)

    first_derive_index = graph_node_ids.index("derive_luminaria_defeito_classificado")
    second_derive_index = graph_node_ids.index("derive_second_classification")
    assert first_derive_index < second_derive_index


def test_compiler_honors_custom_path_derive_step_id(
    luminaria_doc: dict[str, Any],
) -> None:
    flow_document = copy.deepcopy(luminaria_doc)
    flow_document["path"].insert(
        -1,
        {"step": "custom_classification", "derive": "luminaria_defeito_classificado"},
    )

    compiled_flow = compile_flow(flow_document)

    assert "custom_classification" in compiled_flow.graph.get_graph().nodes
    assert "derive_luminaria_defeito_classificado" not in compiled_flow.graph.get_graph().nodes


def test_compiler_rejects_derive_before_its_derived_source() -> None:
    flow_document = {
        "schema": "flowspec/2",
        "flow": "derive_order_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise derive execution ordering."},
        "domains": {"Source": {"type": "free_text"}},
        "slots": {"source": {"domain": "Source"}},
        "path": [
            {"step": "collect_source", "slot": "source"},
            {"step": "derive_second", "derive": "second"},
            {"step": "derive_first", "derive": "first"},
        ],
        "derive": [
            {"writes": "second", "from": ["first"], "lookup": {"bar": "wrong"}},
            {"writes": "first", "from": ["source"], "lookup": {"foo": "bar"}},
        ],
    }

    with pytest.raises(FlowLinkError, match="FLOWSPEC_SEMANTIC_DERIVE_EXECUTION_ORDER"):
        compile_flow(flow_document)


def test_compiler_builds_transitive_derive_invalidation_index() -> None:
    flow_document = {
        "schema": "flowspec/2",
        "flow": "derive_invalidation_contract",
        "version": "1.0.0",
        "route": {"description": "Exercise transitive derive invalidation."},
        "domains": {"Source": {"type": "free_text"}},
        "slots": {"source": {"domain": "Source"}},
        "path": [{"step": "collect_source", "slot": "source"}],
        "derive": [
            {
                "writes": "first",
                "from": ["source"],
                "after": "collect_source",
                "lookup": {"foo": "bar"},
            },
            {
                "writes": "second",
                "from": ["first"],
                "after": "derive_first",
                "lookup": {"bar": "baz"},
            },
        ],
    }

    compiled_flow = compile_flow(flow_document)

    assert compiled_flow.ctx.derive_readers["source"] == ["first", "second"]


def test_compiler_rejects_non_boolean_confirmation_domain(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["slots"]["ticket_data_confirmed"]["domain"] = "Tamanho"

    with pytest.raises(ValueError, match="must use a bool domain"):
        compile_flow(invalid_flow)


def test_compiler_rejects_non_renderable_interactive_domain(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["domains"]["Tamanho"] = {"type": "integer"}
    invalid_flow["terminal"]["input"] = [
        input_binding
        for input_binding in invalid_flow["terminal"]["input"]
        if input_binding["slot"] != "buraco_tamanho"
    ]

    with pytest.raises(
        ValueError,
        match="from_domain must reference a categorical or bool domain",
    ):
        compile_flow(invalid_flow)


def test_compiler_validates_embedded_entry_args_schema(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["route"]["entry_args_schema"] = {"type": 42}

    with pytest.raises(ValueError, match="not a valid Draft 2020-12 schema"):
        compile_flow(invalid_flow)
