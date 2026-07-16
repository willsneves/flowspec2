"""Derived-value semantic contracts and execution-order checks."""

from __future__ import annotations

import re
from itertools import product
from typing import Any, cast

from .derive import decode_derive_key, encode_derive_component, encode_derive_key
from .diagnostics import DiagnosticLocation, FlowDiagnostic
from .domains import make_slot_model
from .semantic_path_contracts import step_identifier
from .semantic_schema_contracts import slot_model_accepts
from .semantic_support import json_pointer, semantic_diagnostic


def derive_contracts(
    document: dict[str, Any],
    identifiers: dict[str, str],
    *,
    external_slots: frozenset[str] | None,
    external_steps: frozenset[str],
) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    declared_slots = set(document.get("slots", {}))
    native_collected_slots = {
        cast(str, path_step.get("slot", path_step.get("confirm")))
        for path_step in document["path"]
        if "slot" in path_step or "confirm" in path_step
    }
    slots = set(declared_slots)
    if external_slots is not None:
        slots.update(external_slots)
    definitions: dict[str, int] = {}
    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        target = derive_definition["writes"]
        if (first_index := definitions.get(target)) is not None:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_DERIVE_TARGET",
                    json_pointer("derive", derive_index, "writes"),
                    f"derive target {target!r} has more than one definition",
                    related=(
                        DiagnosticLocation(path=json_pointer("derive", first_index, "writes")),
                    ),
                )
            )
        else:
            definitions[target] = derive_index
        if target in native_collected_slots:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DERIVE_OVERWRITES_DECLARED_SLOT",
                    json_pointer("derive", derive_index, "writes"),
                    f"derive target {target!r} collides with a collected slot",
                    suggested_fix="Use a distinct derived state key.",
                )
            )
        if external_slots is not None and target in external_slots:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DERIVE_OVERWRITES_PROFILE_SLOT",
                    json_pointer("derive", derive_index, "writes"),
                    f"derive target {target!r} is owned by a profile subflow",
                    suggested_fix="Use a distinct derived state key.",
                )
            )

    derive_targets = set(definitions)
    explicit_derive_nodes = {
        cast(str, path_step["derive"]): cast(
            str,
            path_step.get("step", f"derive_{path_step['derive']}"),
        )
        for path_step in document["path"]
        if "derive" in path_step
    }
    derive_node_identifiers = {
        target: explicit_derive_nodes.get(target, f"derive_{target}") for target in derive_targets
    }
    slot_definitions = cast(dict[str, dict[str, Any]], document.get("slots", {}))
    domain_definitions = cast(dict[str, dict[str, Any]], document.get("domains", {}))
    gatedstep_identifiers = set(
        cast(dict[str, Any], cast(dict[str, Any], document.get("overrides", {})).get("gates", {}))
    )

    def source_can_be_absent(source_slot: str, slot_definition: dict[str, Any]) -> bool:
        source_path_step: dict[str, Any] = next(
            (
                path_step
                for path_step in document["path"]
                if path_step.get("slot", path_step.get("confirm")) == source_slot
            ),
            {},
        )
        return bool(
            not slot_definition.get("required", True)
            or slot_definition.get("nullable", False)
            or slot_definition.get("on_exhaust") == "skip"
            or "ask_when" in source_path_step
            or "skip_when" in source_path_step
            or source_path_step.get("step") in gatedstep_identifiers
        )

    def closed_source_values(source_slot: str) -> tuple[Any, ...] | None:
        slot_definition = slot_definitions.get(source_slot)
        if slot_definition is None:
            return None
        domain_definition = domain_definitions.get(cast(str, slot_definition["domain"]))
        if domain_definition is None:
            return None
        domain_kind = domain_definition.get("type", "categorical")
        source_values: list[Any]
        if domain_kind == "categorical":
            source_values = list(domain_definition.get("values", []))
        elif domain_kind == "bool":
            source_values = [True, False]
        else:
            return None
        if source_can_be_absent(source_slot, slot_definition) and None not in source_values:
            source_values.append(None)
        return tuple(source_values)

    def closed_source_tokens(source_slot: str) -> tuple[str, ...] | None:
        source_values = closed_source_values(source_slot)
        return (
            tuple(encode_derive_component(source_value) for source_value in source_values)
            if source_values is not None
            else None
        )

    def open_source_is_compatible_with_target(
        source_slot: str,
        target_slot: str,
    ) -> bool | None:
        source_slot_definition = slot_definitions.get(source_slot)
        target_slot_definition = slot_definitions.get(target_slot)
        if source_slot_definition is None or target_slot_definition is None:
            return None
        if source_can_be_absent(source_slot, source_slot_definition) and not bool(
            target_slot_definition.get("nullable", False)
        ):
            return False
        source_domain = domain_definitions.get(cast(str, source_slot_definition["domain"]))
        target_domain = domain_definitions.get(cast(str, target_slot_definition["domain"]))
        if source_domain is None or target_domain is None:
            return None
        source_kind = cast(str, source_domain.get("type", "categorical"))
        target_kind = cast(str, target_domain.get("type", "categorical"))
        if source_kind in {"integer", "number"}:
            if target_kind not in {"integer", "number"}:
                return False
            if source_kind == "number" and target_kind == "integer":
                return False
            source_minimum = source_domain.get("minimum")
            target_minimum = target_domain.get("minimum")
            source_maximum = source_domain.get("maximum")
            target_maximum = target_domain.get("maximum")
            if target_minimum is not None and (
                source_minimum is None or source_minimum < target_minimum
            ):
                return False
            if target_maximum is not None and (
                source_maximum is None or source_maximum > target_maximum
            ):
                return False
            return True
        string_kinds = {"brazilian_tax_id", "email", "name", "free_text"}
        if source_kind not in string_kinds or target_kind not in string_kinds:
            return False if source_kind != target_kind else None
        if target_kind == "free_text":
            if target_domain.get("optional"):
                return True
            return source_kind != "free_text" or not source_domain.get("optional")
        if source_kind == target_kind:
            return True
        if source_kind == "brazilian_tax_id" and target_kind == "name":
            return True
        return False

    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        derive_target = cast(str, derive_definition["writes"])
        if derive_target not in explicit_derive_nodes and "after" not in derive_definition:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNPLACED_DERIVE",
                    json_pointer("derive", derive_index),
                    "derive must be placed in path or declare an after anchor",
                    suggested_fix="Add a derive path step or an explicit after step id.",
                )
            )
        for source_index, source_slot in enumerate(derive_definition["from"]):
            if (
                external_slots is not None
                and source_slot not in slots
                and source_slot not in derive_targets
            ):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_SOURCE",
                        json_pointer("derive", derive_index, "from", source_index),
                        f"derive source {source_slot!r} is not declared or exposed",
                    )
                )
            if len(derive_definition["from"]) > 1:
                slot_definition = slot_definitions.get(source_slot)
                domain_definition = (
                    domain_definitions.get(slot_definition["domain"])
                    if slot_definition is not None
                    else None
                )
                domain_kind = (
                    domain_definition.get("type", "categorical")
                    if domain_definition is not None
                    else None
                )
                categorical_values = (
                    domain_definition.get("values", [])
                    if domain_kind == "categorical" and domain_definition is not None
                    else []
                )
                if domain_kind not in {"categorical", "bool"} or any(
                    isinstance(domain_value, str) and "|" in domain_value
                    for domain_value in categorical_values
                ):
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_NON_INJECTIVE_DERIVE_KEY",
                            json_pointer("derive", derive_index, "from", source_index),
                            "multi-source lookup keys require closed categorical or bool "
                            "sources whose values do not contain '|'",
                            suggested_fix=(
                                "Use closed sources or compose single-source derives so the "
                                "lookup key is unambiguous."
                            ),
                        )
                    )
        if (
            (anchor := derive_definition.get("after")) is not None
            and anchor not in identifiers
            and anchor not in external_steps
            and anchor not in derive_node_identifiers.values()
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_ANCHOR",
                    json_pointer("derive", derive_index, "after"),
                    f"derive anchor {anchor!r} is not a path or profile step",
                    suggested_fix="Use a stable executable step id.",
                )
            )
        if isinstance(
            default_value := derive_definition.get("default"), str
        ) and default_value.startswith("$from["):
            default_match = re.fullmatch(r"\$from\[(\d+)\]", default_value)
            if default_match is None or int(default_match.group(1)) >= len(
                derive_definition["from"]
            ):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_INVALID_DERIVE_DEFAULT_REFERENCE",
                        json_pointer("derive", derive_index, "default"),
                        f"derive default {default_value!r} does not reference an existing source",
                    )
                )
        expected_arity = len(derive_definition["from"])
        for lookup_key in derive_definition["lookup"]:
            lookup_components = decode_derive_key(lookup_key, expected_arity)
            if len(lookup_components) != expected_arity:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_ARITY",
                        json_pointer("derive", derive_index, "lookup", lookup_key),
                        f"lookup key has a different arity than the {expected_arity} derive sources",
                    )
                )
                continue
            for source_index, (source_slot, lookup_component) in enumerate(
                zip(derive_definition["from"], lookup_components, strict=True)
            ):
                accepted_tokens = closed_source_tokens(source_slot)
                if accepted_tokens is not None and lookup_component not in accepted_tokens:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_VALUE_OUT_OF_DOMAIN",
                            json_pointer("derive", derive_index, "lookup", lookup_key),
                            f"lookup component {lookup_component!r} is not reachable from "
                            f"source {source_slot!r}",
                            related=(
                                DiagnosticLocation(
                                    path=json_pointer("derive", derive_index, "from", source_index),
                                    message="Source slot for this lookup component.",
                                ),
                            ),
                        )
                    )

        target_slot_definition = slot_definitions.get(derive_target)
        if target_slot_definition is not None:
            target_domain_name = cast(str, target_slot_definition["domain"])
            if target_domain_name in domain_definitions:
                target_model = make_slot_model(
                    derive_target,
                    target_domain_name,
                    domain_definitions,
                    nullable=bool(target_slot_definition.get("nullable", False)),
                )

                authored_outputs = [
                    *(
                        (
                            json_pointer("derive", derive_index, "lookup", lookup_key),
                            lookup_value,
                        )
                        for lookup_key, lookup_value in derive_definition["lookup"].items()
                    ),
                    *(
                        [
                            (
                                json_pointer("derive", derive_index, "default"),
                                derive_definition["default"],
                            )
                        ]
                        if "default" in derive_definition
                        and not str(derive_definition["default"]).startswith("$from[")
                        else []
                    ),
                ]
                for output_path, output_value in authored_outputs:
                    try:
                        target_model.model_validate({derive_target: output_value})
                    except Exception:
                        diagnostics.append(
                            semantic_diagnostic(
                                "FLOWSPEC_SEMANTIC_DERIVE_OUTPUT_OUT_OF_DOMAIN",
                                output_path,
                                f"derive output {output_value!r} is not accepted by target "
                                f"slot {derive_target!r}",
                            )
                        )

                if isinstance(default_value, str) and (
                    default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value)
                ):
                    source_index = int(default_match.group(1))
                    if source_index < len(derive_definition["from"]):
                        source_slot = cast(str, derive_definition["from"][source_index])
                        source_value_sets = [
                            closed_source_values(candidate_source)
                            for candidate_source in derive_definition["from"]
                        ]
                        fallback_values: tuple[Any, ...] | None = None
                        if all(source_values is not None for source_values in source_value_sets):
                            fallback_values = tuple(
                                source_values[source_index]
                                for source_values in product(
                                    *cast(list[tuple[Any, ...]], source_value_sets)
                                )
                                if encode_derive_key(source_values)
                                not in derive_definition["lookup"]
                            )
                        fallback_is_compatible = (
                            all(
                                slot_model_accepts(
                                    target_model,
                                    derive_target,
                                    fallback_value,
                                )
                                for fallback_value in fallback_values
                            )
                            if fallback_values is not None
                            else open_source_is_compatible_with_target(
                                source_slot,
                                derive_target,
                            )
                        )
                        if fallback_is_compatible is False:
                            diagnostics.append(
                                semantic_diagnostic(
                                    "FLOWSPEC_SEMANTIC_DERIVE_DEFAULT_SOURCE_INCOMPATIBLE",
                                    json_pointer("derive", derive_index, "default"),
                                    f"derive fallback source {source_slot!r} is not accepted by "
                                    f"target slot {derive_target!r}",
                                )
                            )

            if target_slot_definition.get("required", True) and "default" not in derive_definition:
                source_token_sets = [
                    closed_source_tokens(source_slot) for source_slot in derive_definition["from"]
                ]
                lookup_keys = set(derive_definition["lookup"])
                is_total = all(token_set is not None for token_set in source_token_sets) and all(
                    encode_derive_key(source_tokens) in lookup_keys
                    for source_tokens in product(*cast(list[tuple[str, ...]], source_token_sets))
                )
                if not is_total:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_REQUIRED_DERIVE_NOT_TOTAL",
                            json_pointer("derive", derive_index),
                            f"derive for required slot {derive_target!r} does not cover every "
                            "reachable source value and has no default",
                            suggested_fix="Add a domain-valid default or complete the lookup.",
                        )
                    )

    dependency_graph = {
        target: [
            source
            for source in document["derive"][derive_index]["from"]
            if source in derive_targets
        ]
        for target, derive_index in definitions.items()
    }
    active_targets: list[str] = []
    completed_targets: set[str] = set()
    reported_cycles: set[frozenset[str]] = set()

    def visit(target: str) -> None:
        if target in completed_targets:
            return
        if target in active_targets:
            cycle = active_targets[active_targets.index(target) :] + [target]
            cycle_members = frozenset(cycle)
            if cycle_members not in reported_cycles:
                reported_cycles.add(cycle_members)
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_DEPENDENCY_CYCLE",
                        json_pointer("derive", definitions[target], "from"),
                        f"derive dependency cycle: {' -> '.join(cycle)}",
                    )
                )
            return
        active_targets.append(target)
        for source in dependency_graph[target]:
            visit(source)
        active_targets.pop()
        completed_targets.add(target)

    for target in sorted(derive_targets):
        visit(target)

    terminal_identifier = cast(
        str,
        cast(dict[str, Any], document.get("terminal", {})).get("step", "terminal"),
    )
    ordered_node_identifiers = ["__init__"]
    native_slot_nodes: dict[str, str] = {}
    for path_step in document["path"]:
        node_identifier = step_identifier(path_step, terminal_identifier)
        if node_identifier is not None:
            ordered_node_identifiers.append(node_identifier)
        if "slot" in path_step or "confirm" in path_step:
            native_slot_nodes[cast(str, path_step.get("slot", path_step.get("confirm")))] = cast(
                str, node_identifier
            )

    last_inserted_for_anchor: dict[str, str] = {}
    unresolved_placement_targets: set[str] = set()
    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        target = cast(str, derive_definition["writes"])
        if target in explicit_derive_nodes:
            continue
        derive_node_identifier = derive_node_identifiers[target]
        anchor = derive_definition.get("after")
        if anchor is None:
            terminal_position = (
                ordered_node_identifiers.index(terminal_identifier)
                if terminal_identifier in ordered_node_identifiers
                else len(ordered_node_identifiers)
            )
            ordered_node_identifiers.insert(terminal_position, derive_node_identifier)
            continue
        effective_anchor = last_inserted_for_anchor.get(anchor, anchor)
        if effective_anchor not in ordered_node_identifiers:
            if anchor not in external_steps:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_ANCHOR_ORDER",
                        json_pointer("derive", derive_index, "after"),
                        f"derive anchor {anchor!r} is not executable before this declaration",
                        suggested_fix="Declare an anchored producer before derives that target it.",
                    )
                )
            unresolved_placement_targets.add(target)
            continue
        anchor_index = ordered_node_identifiers.index(effective_anchor)
        ordered_node_identifiers.insert(anchor_index + 1, derive_node_identifier)
        last_inserted_for_anchor[cast(str, anchor)] = derive_node_identifier

    node_positions = {
        node_identifier: node_index
        for node_index, node_identifier in enumerate(ordered_node_identifiers)
    }
    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        target = cast(str, derive_definition["writes"])
        if target in unresolved_placement_targets:
            continue
        target_node = derive_node_identifiers[target]
        target_position = node_positions.get(target_node)
        if target_position is None:
            continue
        for source_index, source_slot in enumerate(derive_definition["from"]):
            producer_node = derive_node_identifiers.get(source_slot) or native_slot_nodes.get(
                source_slot
            )
            if producer_node is None:
                continue
            producer_position = node_positions.get(producer_node)
            if producer_position is None or producer_position >= target_position:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_EXECUTION_ORDER",
                        json_pointer("derive", derive_index, "from", source_index),
                        f"derive source {source_slot!r} is produced after {target!r} would execute",
                        suggested_fix="Move the derive after every source producer.",
                    )
                )

    for path_index, path_step in enumerate(document["path"]):
        if "derive" in path_step and path_step["derive"] not in definitions:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_TARGET",
                    json_pointer("path", path_index, "derive"),
                    f"derive step {path_step['derive']!r} has no derive definition",
                )
            )
    return diagnostics
