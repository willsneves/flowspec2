"""Predicate reference and type semantic contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from .diagnostics import DiagnosticLocation, FlowDiagnostic
from .domains import make_slot_model
from .profiles import FlowProfile
from .schema import schema as flowspec_schema
from .semantic_schema_contracts import (
    schema_accepts,
    schema_property,
    schemas_are_obviously_disjoint,
)
from .semantic_support import json_pointer, semantic_diagnostic

_PREDICATE_NAMESPACES = frozenset({"slots", "internal", "payload", "config", "address"})
_CONFIG_PROPERTY_SCHEMAS = cast(
    Mapping[str, Mapping[str, Any]],
    flowspec_schema()["properties"]["config"]["properties"],
)


def _predicate_references(
    predicate: Any,
    path: str,
) -> Iterable[tuple[str, str]]:
    if not isinstance(predicate, dict) or len(predicate) != 1:
        return ()
    operator, operand = next(iter(predicate.items()))
    operator_path = f"{path}/{operator}"
    if operator in {"eq", "ne"} and isinstance(operand, list):
        return tuple(
            (candidate, f"{operator_path}/{operand_index}")
            for operand_index, candidate in enumerate(operand)
            if isinstance(candidate, str)
            and "." in candidate
            and candidate.split(".", 1)[0] in _PREDICATE_NAMESPACES
        )
    if operator == "in" and isinstance(operand, list):
        candidate = operand[0]
        if (
            isinstance(candidate, str)
            and "." in candidate
            and candidate.split(".", 1)[0] in _PREDICATE_NAMESPACES
        ):
            return ((candidate, f"{operator_path}/0"),)
        return ()
    if operator == "is_present" and isinstance(operand, str):
        if "." in operand and operand.split(".", 1)[0] in _PREDICATE_NAMESPACES:
            return ((operand, operator_path),)
        return ()
    if operator in {"and", "or"} and isinstance(operand, list):
        return tuple(
            reference
            for predicate_index, nested_predicate in enumerate(operand)
            for reference in _predicate_references(
                nested_predicate, f"{operator_path}/{predicate_index}"
            )
        )
    if operator == "not":
        return tuple(_predicate_references(operand, operator_path))
    return ()


def _declared_predicates(document: dict[str, Any]) -> Iterable[tuple[dict[str, Any], str]]:
    for path_index, path_step in enumerate(document["path"]):
        for predicate_key in ("ask_when", "skip_when"):
            if isinstance(predicate := path_step.get(predicate_key), dict):
                yield predicate, json_pointer("path", path_index, predicate_key)
        interactive = cast(dict[str, Any], path_step.get("interactive") or {})
        for option_index, conditional_option in enumerate(interactive.get("options_when", [])):
            if isinstance(predicate := conditional_option.get("gate"), dict):
                yield (
                    predicate,
                    json_pointer(
                        "path", path_index, "interactive", "options_when", option_index, "gate"
                    ),
                )

    confirmation_interactive = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("confirm", {})).get("interactive", {}),
    )
    for option_index, conditional_option in enumerate(
        confirmation_interactive.get("options_when", [])
    ):
        if isinstance(predicate := conditional_option.get("gate"), dict):
            yield (
                predicate,
                json_pointer("confirm", "interactive", "options_when", option_index, "gate"),
            )

    gates = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("overrides", {})).get("gates", {}),
    )
    for gate_identifier, predicate in gates.items():
        if isinstance(predicate, dict):
            yield predicate, json_pointer("overrides", "gates", gate_identifier)

    auto_flow = cast(dict[str, Any], document.get("auto_flow") or {})
    if isinstance(predicate := auto_flow.get("send_when"), dict):
        yield predicate, "/auto_flow/send_when"


def _is_predicate_reference(operand: Any) -> bool:
    return (
        isinstance(operand, str)
        and "." in operand
        and operand.split(".", 1)[0] in _PREDICATE_NAMESPACES
    )


def _predicate_literal(operand: Any) -> Any:
    return (
        operand["literal"] if isinstance(operand, dict) and set(operand) == {"literal"} else operand
    )


def _predicate_literal_path(operand: Any, operand_path: str) -> str:
    return f"{operand_path}/literal" if isinstance(operand, dict) else operand_path


def _predicate_reference_schema(
    document: dict[str, Any],
    reference: str,
    profile: FlowProfile | None,
) -> tuple[Mapping[str, Any] | None, bool]:
    namespace, state_key = reference.split(".", 1)
    slots = cast(dict[str, dict[str, Any]], document.get("slots", {}))
    if namespace in {"slots", "internal"} and state_key in slots:
        slot_definition = slots[state_key]
        domain_name = cast(str, slot_definition["domain"])
        domain_definitions = cast(dict[str, Any], document.get("domains", {}))
        if domain_name not in domain_definitions:
            return None, True
        slot_model_schema = make_slot_model(
            state_key,
            domain_name,
            domain_definitions,
            nullable=bool(slot_definition.get("nullable", False)),
        ).model_json_schema()
        return cast(Mapping[str, Any], slot_model_schema["properties"][state_key]), True
    if namespace == "config" and state_key in document.get("config", {}):
        return _CONFIG_PROPERTY_SCHEMAS.get(state_key), True
    if namespace == "payload":
        return None, False
    if profile is None:
        return None, False
    if namespace == "slots":
        for use_definition in document.get("uses", []):
            reference_definition = cast(str, use_definition["ref"])
            if not profile.subflows.has(reference_definition):
                continue
            subflow_definition = profile.subflows.definition(reference_definition)
            if (
                subflow_definition.exposed_slots is not None
                and state_key in subflow_definition.exposed_slots
            ):
                return subflow_definition.exposed_slot_schemas[state_key], True
        return None, True
    if namespace == "address" and profile.subflows.has("address@1"):
        address_definition = profile.subflows.definition("address@1")
        address_schema = address_definition.owned_state_keys["address"].schema
        return schema_property(address_schema, state_key), True
    return None, False


def _predicate_type_diagnostics(
    predicate: Any,
    predicate_path: str,
    document: dict[str, Any],
    profile: FlowProfile | None,
) -> list[FlowDiagnostic]:
    if not isinstance(predicate, dict) or len(predicate) != 1:
        return []
    operator, operand = next(iter(predicate.items()))
    operator_path = f"{predicate_path}/{operator}"
    if operator in {"and", "or"} and isinstance(operand, list):
        return [
            diagnostic
            for predicate_index, nested_predicate in enumerate(operand)
            for diagnostic in _predicate_type_diagnostics(
                nested_predicate,
                f"{operator_path}/{predicate_index}",
                document,
                profile,
            )
        ]
    if operator == "not":
        return _predicate_type_diagnostics(operand, operator_path, document, profile)

    diagnostics: list[FlowDiagnostic] = []
    if operator == "in" and isinstance(operand, list) and len(operand) == 2:
        reference = cast(str, operand[0])
        reference_schema, schema_is_expected = _predicate_reference_schema(
            document,
            reference,
            profile,
        )
        if schema_is_expected and reference_schema is None and reference.startswith("address."):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_ADDRESS_REFERENCE",
                    f"{operator_path}/0",
                    f"predicate references unknown address field {reference!r}",
                )
            )
        if reference_schema is not None and isinstance(operand[1], list):
            for literal_index, literal in enumerate(operand[1]):
                if not schema_accepts(reference_schema, literal):
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_PREDICATE_LITERAL_OUT_OF_DOMAIN",
                            f"{operator_path}/1/{literal_index}",
                            f"predicate literal {literal!r} is not accepted by {reference!r}",
                        )
                    )
        return diagnostics

    if operator not in {"eq", "ne"} or not isinstance(operand, list):
        return diagnostics
    operand_contracts: list[tuple[str, str, Mapping[str, Any] | None]] = []
    for operand_index, candidate in enumerate(operand):
        if not _is_predicate_reference(candidate):
            continue
        candidate_path = f"{operator_path}/{operand_index}"
        candidate_schema, schema_is_expected = _predicate_reference_schema(
            document,
            candidate,
            profile,
        )
        if schema_is_expected and candidate_schema is None and candidate.startswith("address."):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_ADDRESS_REFERENCE",
                    candidate_path,
                    f"predicate references unknown address field {candidate!r}",
                )
            )
        operand_contracts.append((candidate, candidate_path, candidate_schema))

    if len(operand_contracts) == 2:
        first_reference, first_path, first_schema = operand_contracts[0]
        second_reference, second_path, second_schema = operand_contracts[1]
        if (
            first_schema is not None
            and second_schema is not None
            and schemas_are_obviously_disjoint(first_schema, second_schema)
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_PREDICATE_REFERENCE_TYPE_MISMATCH",
                    second_path,
                    f"predicate references {first_reference!r} and {second_reference!r} "
                    "with disjoint contracts",
                    related=(DiagnosticLocation(path=first_path),),
                )
            )
        return diagnostics

    if len(operand_contracts) == 1:
        reference, _, reference_schema = operand_contracts[0]
        reference_index = next(
            index for index, candidate in enumerate(operand) if candidate == reference
        )
        literal_index = 1 - reference_index
        literal_operand = operand[literal_index]
        literal = _predicate_literal(literal_operand)
        if reference_schema is not None and not schema_accepts(reference_schema, literal):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_PREDICATE_LITERAL_OUT_OF_DOMAIN",
                    _predicate_literal_path(
                        literal_operand,
                        f"{operator_path}/{literal_index}",
                    ),
                    f"predicate literal {literal!r} is not accepted by {reference!r}",
                )
            )
    return diagnostics


def predicate_contracts(
    document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None,
    profile: FlowProfile | None,
) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    slots = cast(dict[str, dict[str, Any]], document.get("slots", {}))
    available_slots = set(slots)
    if external_slots is not None:
        available_slots.update(external_slots)
    config_keys = set(cast(dict[str, Any], document.get("config", {})))
    uses_address = any(use["ref"] == "address@1" for use in document.get("uses", []))

    for predicate, predicate_path in _declared_predicates(document):
        diagnostics.extend(
            _predicate_type_diagnostics(
                predicate,
                predicate_path,
                document,
                profile,
            )
        )
        for reference, reference_path in _predicate_references(predicate, predicate_path):
            namespace, key = reference.split(".", 1)
            if namespace == "config" and key not in config_keys:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIG_REFERENCE",
                        reference_path,
                        f"predicate references unknown config key {key!r}",
                    )
                )
            elif namespace == "address" and not uses_address:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_ADDRESS_REFERENCE_WITHOUT_SUBFLOW",
                        reference_path,
                        "address predicate namespace requires the address@1 subflow",
                    )
                )
            elif namespace in {"slots", "internal", "payload"}:
                expected_partition = {
                    "slots": "data",
                    "internal": "internal",
                    "payload": "payload",
                }[namespace]
                if key in slots and slots[key]["persist"] != expected_partition:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_WRONG_PREDICATE_PARTITION",
                            reference_path,
                            f"{reference!r} addresses a slot persisted in {slots[key]['persist']!r}",
                            suggested_fix=f"Use the {slots[key]['persist']} namespace.",
                        )
                    )
                elif namespace != "payload" and (
                    external_slots is not None
                    and key not in available_slots
                    and not key.startswith("_")
                ):
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_UNKNOWN_PREDICATE_SLOT",
                            reference_path,
                            f"predicate references unknown state key {reference!r}",
                        )
                    )
    return diagnostics
