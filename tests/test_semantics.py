"""Aggregate semantic-linking regressions."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from flowspec2 import FlowRuntime
from flowspec2.derive import encode_derive_key
from flowspec2.profiles import reference_profile
from flowspec2.semantics import semantic_diagnostics


def _codes(flow_document: dict[str, Any]) -> set[str]:
    return {diagnostic.code for diagnostic in semantic_diagnostics(flow_document)}


def test_semantic_linker_aggregates_independent_reference_errors(
    streetlight_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(streetlight_document)
    invalid_flow["overrides"]["gates"]["missing_step"] = {"eq": ["slots.missing", True]}
    invalid_flow["derive"][0]["after"] = "missing_anchor"
    invalid_flow["auto_flow"]["resume_at"] = "missing_resume"

    diagnostics = semantic_diagnostics(invalid_flow, external_slots=frozenset())

    assert {
        "FLOWSPEC_SEMANTIC_UNKNOWN_GATE_STEP",
        "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_ANCHOR",
        "FLOWSPEC_SEMANTIC_UNKNOWN_AUTO_FLOW_RESUME_STEP",
        "FLOWSPEC_SEMANTIC_UNKNOWN_PREDICATE_SLOT",
    } <= {diagnostic.code for diagnostic in diagnostics}
    assert tuple(diagnostics) == semantic_diagnostics(invalid_flow, external_slots=frozenset())


def test_semantic_linker_rejects_orphan_and_duplicate_subflow_contracts(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["uses"].append(copy.deepcopy(invalid_flow["uses"][0]))
    invalid_flow["uses"].append({"ref": "unused@1"})

    assert {
        "FLOWSPEC_SEMANTIC_DUPLICATE_USE_DECLARATION",
        "FLOWSPEC_SEMANTIC_ORPHAN_USE_DECLARATION",
    } <= _codes(invalid_flow)


def test_semantic_linker_rejects_terminal_marker_inconsistency(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["path"].insert(0, invalid_flow["path"].pop())

    assert "FLOWSPEC_SEMANTIC_UNREACHABLE_PATH_AFTER_TERMINAL" in _codes(invalid_flow)


def test_semantic_linker_validates_embedded_entry_json_schema(
    pothole_document: dict[str, Any],
) -> None:
    invalid_schema_flow = copy.deepcopy(pothole_document)
    invalid_schema_flow["route"]["entry_args_schema"] = {"type": 42}
    open_schema_flow = copy.deepcopy(pothole_document)
    open_schema_flow["route"]["entry_args_schema"] = {
        "type": "object",
        "properties": {"pothole_type": {"type": "string"}},
    }

    assert "FLOWSPEC_SEMANTIC_INVALID_ENTRY_SCHEMA" in _codes(invalid_schema_flow)
    assert "FLOWSPEC_SEMANTIC_ENTRY_SCHEMA_OPEN" in _codes(open_schema_flow)


def test_semantic_linker_rejects_domain_and_dependency_ambiguity(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["domains"]["PotholeType"]["values"].append("ASPHALT POTHOLE")
    invalid_flow["domains"]["PotholeType"]["normalize"]["accent_fold"] = True
    invalid_flow["slots"]["pothole_type"]["requires"] = ["pothole_size"]
    invalid_flow["slots"]["pothole_size"]["requires"] = ["pothole_type"]

    assert {
        "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_VALUE",
        "FLOWSPEC_SEMANTIC_SLOT_DEPENDENCY_CYCLE",
        "FLOWSPEC_SEMANTIC_REQUIREMENT_ORDER",
    } <= _codes(invalid_flow)


def test_semantic_linker_rejects_aliases_that_shadow_canonical_inputs(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["domains"]["PotholeType"]["normalize"]["synonyms"]["asphalt pothole"] = "Crater"

    assert "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_ALIAS" in _codes(invalid_flow)


def test_semantic_linker_rejects_normalized_alias_and_number_word_collisions(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    size_normalization = invalid_flow["domains"]["Size"]["normalize"]
    size_normalization["synonyms"]["one"] = "Large"
    size_normalization["synonyms"]["tiny"] = "Small"
    size_normalization["synonyms"]["tiny"] = "Large"

    assert {
        "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_ALIAS",
    } <= _codes(invalid_flow)


def test_semantic_linker_rejects_boolean_alias_conflicting_with_veto(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    normalization = invalid_flow["domains"]["YesNo"]["normalize"]
    normalization["emoji_veto"] = True
    normalization["synonyms"] = {"👎": True}

    assert "FLOWSPEC_SEMANTIC_ALIAS_CONFLICTS_WITH_BOOLEAN_NORMALIZATION" in _codes(invalid_flow)


def test_semantic_linker_rejects_wrong_predicate_partition(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["slots"]["pothole_type"]["persist"] = "internal"
    invalid_flow["path"][0]["ask_when"] = {"is_present": "slots.pothole_type"}

    assert "FLOWSPEC_SEMANTIC_WRONG_PREDICATE_PARTITION" in _codes(invalid_flow)


def test_semantic_linker_rejects_duplicate_gate_sources(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["path"][0]["ask_when"] = {"is_present": "slots.pothole_size"}
    invalid_flow["overrides"] = {"gates": {"collect_type": {"is_present": "slots.pothole_size"}}}

    assert "FLOWSPEC_SEMANTIC_DUPLICATE_GATE_SOURCE" in _codes(invalid_flow)


def test_semantic_linker_does_not_treat_literal_objects_as_predicates(
    pothole_document: dict[str, Any],
) -> None:
    valid_flow = copy.deepcopy(pothole_document)
    valid_flow["terminal"]["outcomes"]["success"]["set"] = {"metadata": {"is_present": "literal"}}

    assert semantic_diagnostics(valid_flow) == ()


def test_semantic_linker_requires_boolean_confirmation_domain(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["slots"]["ticket_data_confirmed"]["domain"] = "Size"

    assert "FLOWSPEC_SEMANTIC_CONFIRM_DOMAIN_NOT_BOOLEAN" in _codes(invalid_flow)


def test_semantic_linker_rejects_invalid_derive_contracts(
    streetlight_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(streetlight_document)
    invalid_flow["derive"][0]["default"] = "$from[8]"
    invalid_flow["derive"][0]["lookup"]["wrong|arity"] = "invalid"

    assert {
        "FLOWSPEC_SEMANTIC_INVALID_DERIVE_DEFAULT_REFERENCE",
        "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_ARITY",
    } <= _codes(invalid_flow)


def test_semantic_linker_rejects_auto_flow_alias_to_subflow_exposure(
    streetlight_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(streetlight_document)
    invalid_flow["auto_flow"]["alias_map"]["external_address"] = {"address": "$value"}

    assert "FLOWSPEC_SEMANTIC_NON_NATIVE_AUTO_FLOW_DESTINATION" in _codes(invalid_flow)


def test_semantic_linker_requires_explicit_derive_placement(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["derive"] = [
        {
            "writes": "classification",
            "from": ["pothole_type"],
            "lookup": {"Asphalt pothole": "mapped"},
            "default": "$from[0]",
        }
    ]

    assert "FLOWSPEC_SEMANTIC_UNPLACED_DERIVE" in _codes(invalid_flow)


def test_semantic_linker_rejects_ambiguous_multi_source_derive_encoding(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["derive"] = [
        {
            "writes": "classification",
            "from": ["pothole_type", "address"],
            "after": "confirm_address",
            "lookup": {"Asphalt pothole|street": "mapped"},
        }
    ]

    assert "FLOWSPEC_SEMANTIC_NON_INJECTIVE_DERIVE_KEY" in _codes(invalid_flow)


def test_semantic_linker_rejects_derive_before_its_producer() -> None:
    invalid_flow = {
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

    assert "FLOWSPEC_SEMANTIC_DERIVE_EXECUTION_ORDER" in _codes(invalid_flow)


def test_semantic_linker_rejects_required_slot_without_a_producer(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["slots"]["orphan_required_slot"] = {
        "domain": "TextoLivre",
        "required": True,
    }

    assert "FLOWSPEC_SEMANTIC_REQUIRED_SLOT_WITHOUT_PRODUCER" in _codes(invalid_flow)


def test_semantic_linker_rejects_derive_target_owned_by_profile(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["derive"] = [
        {
            "writes": "address",
            "from": ["pothole_type"],
            "after": "collect_type",
            "lookup": {"Asphalt pothole": "derived"},
            "default": "$from[0]",
        }
    ]

    diagnostics = semantic_diagnostics(
        invalid_flow,
        external_slots=frozenset({"address", "brazilian_tax_id", "email", "name"}),
    )

    assert "FLOWSPEC_SEMANTIC_DERIVE_OVERWRITES_PROFILE_SLOT" in {
        diagnostic.code for diagnostic in diagnostics
    }


def test_semantic_linker_validates_derive_lookup_and_target_domains(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["domains"]["Classification"] = {
        "type": "categorical",
        "values": ["mapped"],
    }
    invalid_flow["slots"]["classification"] = {
        "domain": "Classification",
        "required": True,
    }
    invalid_flow["derive"] = [
        {
            "writes": "classification",
            "from": ["pothole_type"],
            "lookup": {"unreachable": "outside-target-domain"},
        }
    ]
    invalid_flow["path"].insert(
        1,
        {"step": "derive_classification", "derive": "classification"},
    )

    assert {
        "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_VALUE_OUT_OF_DOMAIN",
        "FLOWSPEC_SEMANTIC_DERIVE_OUTPUT_OUT_OF_DOMAIN",
        "FLOWSPEC_SEMANTIC_REQUIRED_DERIVE_NOT_TOTAL",
    } <= _codes(invalid_flow)


def test_semantic_linker_accepts_total_closed_required_derive(
    pothole_document: dict[str, Any],
) -> None:
    valid_flow = copy.deepcopy(pothole_document)
    valid_flow["domains"]["Classification"] = {
        "type": "categorical",
        "values": ["mapped"],
    }
    valid_flow["slots"]["classification"] = {
        "domain": "Classification",
        "required": True,
    }
    valid_flow["derive"] = [
        {
            "writes": "classification",
            "from": ["pothole_type"],
            "lookup": {
                source_value: "mapped"
                for source_value in valid_flow["domains"]["PotholeType"]["values"]
            },
        }
    ]
    valid_flow["path"].insert(
        1,
        {"step": "derive_classification", "derive": "classification"},
    )

    assert "FLOWSPEC_SEMANTIC_REQUIRED_DERIVE_NOT_TOTAL" not in _codes(valid_flow)


@pytest.mark.asyncio
async def test_single_source_derive_preserves_delimiter_inside_closed_token() -> None:
    flow_document = {
        "schema": "flowspec/2",
        "flow": "single_source_delimiter",
        "version": "1.0.0",
        "route": {"description": "Preserve a delimiter in one source token."},
        "domains": {
            "Source": {"type": "categorical", "values": ["north|south"]},
            "Target": {"type": "categorical", "values": ["mapped"]},
        },
        "slots": {
            "source": {"domain": "Source"},
            "target": {"domain": "Target"},
        },
        "path": [
            {"step": "collect_source", "slot": "source"},
            {"step": "derive_target", "derive": "target"},
        ],
        "derive": [
            {
                "writes": "target",
                "from": ["source"],
                "lookup": {"north|south": "mapped"},
            }
        ],
    }

    assert semantic_diagnostics(flow_document) == ()
    runtime = FlowRuntime(flow_document)
    updated_state = await runtime.execute(
        runtime.new_state(user_id="derive-delimiter-user"),
        {"source": "north|south"},
    )
    assert updated_state.data["target"] == "mapped"


def test_derive_key_encoding_uses_canonical_json_boolean_and_null_tokens() -> None:
    assert encode_derive_key([True, False, None]) == "true|false|null"


def test_semantic_linker_rejects_python_specific_boolean_derive_key() -> None:
    invalid_flow = {
        "schema": "flowspec/2",
        "flow": "boolean_derive_key",
        "version": "1.0.0",
        "route": {"description": "Reject Python-specific lookup tokens."},
        "domains": {
            "Boolean": {"type": "bool"},
            "Target": {"type": "categorical", "values": ["mapped"]},
        },
        "slots": {
            "source": {"domain": "Boolean"},
            "target": {"domain": "Target", "required": False},
        },
        "path": [
            {"step": "collect_source", "slot": "source"},
            {"step": "derive_target", "derive": "target"},
        ],
        "derive": [
            {
                "writes": "target",
                "from": ["source"],
                "lookup": {"True": "mapped"},
            }
        ],
    }

    assert "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_VALUE_OUT_OF_DOMAIN" in _codes(invalid_flow)


def test_semantic_linker_rejects_incompatible_source_fallback_domain() -> None:
    invalid_flow = {
        "schema": "flowspec/2",
        "flow": "incompatible_derive_fallback",
        "version": "1.0.0",
        "route": {"description": "Reject a fallback outside its target domain."},
        "domains": {
            "Boolean": {"type": "bool"},
            "Target": {"type": "categorical", "values": ["mapped"]},
        },
        "slots": {
            "source": {"domain": "Boolean"},
            "target": {"domain": "Target"},
        },
        "path": [
            {"step": "collect_source", "slot": "source"},
            {"step": "derive_target", "derive": "target"},
        ],
        "derive": [
            {
                "writes": "target",
                "from": ["source"],
                "lookup": {"true": "mapped"},
                "default": "$from[0]",
            }
        ],
    }

    assert "FLOWSPEC_SEMANTIC_DERIVE_DEFAULT_SOURCE_INCOMPATIBLE" in _codes(invalid_flow)


def test_predicate_literals_are_checked_against_closed_slot_domains(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["path"][0]["ask_when"] = {
        "in": ["slots.pothole_type", ["Asphalt pothole", "not-a-token"]]
    }

    diagnostics = semantic_diagnostics(invalid_flow)

    assert any(
        diagnostic.code == "FLOWSPEC_SEMANTIC_PREDICATE_LITERAL_OUT_OF_DOMAIN"
        and diagnostic.path == "/path/0/ask_when/in/1/1"
        for diagnostic in diagnostics
    )


def test_predicate_rejects_disjoint_declared_slot_references(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["path"][0]["ask_when"] = {
        "eq": ["slots.pothole_type", "slots.ticket_data_confirmed"]
    }

    diagnostics = semantic_diagnostics(invalid_flow)

    assert any(
        diagnostic.code == "FLOWSPEC_SEMANTIC_PREDICATE_REFERENCE_TYPE_MISMATCH"
        and diagnostic.path == "/path/0/ask_when/eq/1"
        for diagnostic in diagnostics
    )


def test_predicate_validates_config_and_address_contracts(
    pothole_document: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(pothole_document)
    invalid_flow["path"][0]["ask_when"] = {
        "and": [
            {"eq": ["config.identification_required", "false"]},
            {"eq": ["address.kind", 42]},
            {"eq": ["address.unknown_field", "value"]},
        ]
    }

    diagnostics = semantic_diagnostics(
        invalid_flow,
        profile=reference_profile(),
    )

    assert {
        "FLOWSPEC_SEMANTIC_PREDICATE_LITERAL_OUT_OF_DOMAIN",
        "FLOWSPEC_SEMANTIC_UNKNOWN_ADDRESS_REFERENCE",
    } <= {diagnostic.code for diagnostic in diagnostics}
    assert any(diagnostic.path == "/path/0/ask_when/and/0/eq/1" for diagnostic in diagnostics)
    assert any(diagnostic.path == "/path/0/ask_when/and/2/eq/0" for diagnostic in diagnostics)


def test_predicate_config_contract_is_not_narrowed_to_configured_value(
    pothole_document: dict[str, Any],
) -> None:
    valid_flow = copy.deepcopy(pothole_document)
    valid_flow["config"]["identification_required"] = True
    valid_flow["path"][0]["ask_when"] = {"eq": ["config.identification_required", False]}

    diagnostics = semantic_diagnostics(
        valid_flow,
        profile=reference_profile(),
    )

    assert not any(
        diagnostic.code == "FLOWSPEC_SEMANTIC_PREDICATE_LITERAL_OUT_OF_DOMAIN"
        and diagnostic.path == "/path/0/ask_when/eq/1"
        for diagnostic in diagnostics
    )
