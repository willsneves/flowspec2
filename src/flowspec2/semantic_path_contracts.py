"""Path, domain, and slot semantic contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .diagnostics import DiagnosticLocation, FlowDiagnostic
from .domains import categorical_number_bindings, normalize_text, parse_affirmation
from .semantic_support import json_pointer, semantic_diagnostic


def step_identifier(path_step: Mapping[str, Any], terminal_identifier: str) -> str | None:
    if "slot" in path_step:
        return cast(str, path_step["step"])
    if "confirm" in path_step:
        return cast(str, path_step["step"])
    if "derive" in path_step:
        return cast(str, path_step["step"])
    if "terminal" in path_step:
        return terminal_identifier
    if "await_external" in path_step:
        return cast(str, path_step["step"])
    return None


def path_contracts(
    document: dict[str, Any],
) -> tuple[list[FlowDiagnostic], dict[str, str], dict[str, int]]:
    diagnostics: list[FlowDiagnostic] = []
    terminal = cast(dict[str, Any], document.get("terminal", {}))
    terminal_identifier = cast(str, terminal.get("step", "terminal"))
    identifiers: dict[str, str] = {"__init__": ""}
    slot_positions: dict[str, int] = {}
    terminal_markers: list[int] = []
    subflow_anchors: dict[str, list[int]] = {}
    external_wait_anchors: list[int] = []

    for path_index, path_step in enumerate(document["path"]):
        source_path = json_pointer("path", path_index)
        if "terminal" in path_step:
            terminal_markers.append(path_index)
        if "use" in path_step:
            subflow_anchors.setdefault(path_step["use"], []).append(path_index)
        if "await_external" in path_step:
            external_wait_anchors.append(path_index)
        if "slot" in path_step or "confirm" in path_step:
            slot_name = cast(str, path_step.get("slot", path_step.get("confirm")))
            slot_positions[slot_name] = path_index
        identifier = step_identifier(path_step, terminal_identifier)
        if identifier is None:
            continue
        if (first_path := identifiers.get(identifier)) is not None:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_STEP_ID",
                    source_path,
                    f"step id {identifier!r} is already bound",
                    related=(DiagnosticLocation(path=first_path, message="First binding."),),
                    suggested_fix="Assign a unique stable step id.",
                )
            )
        else:
            identifiers[identifier] = source_path

    if terminal_markers and not terminal:
        diagnostics.extend(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_TERMINAL_DEFINITION_MISSING",
                json_pointer("path", path_index, "terminal"),
                "terminal path marker has no top-level terminal definition",
                suggested_fix="Declare the terminal action or remove its path marker.",
            )
            for path_index in terminal_markers
        )
    if terminal and not terminal_markers:
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_TERMINAL_MARKER_MISSING",
                "/terminal",
                "terminal action is unreachable because path has no terminal marker",
                suggested_fix='Add {"terminal": true} at the intended path position.',
            )
        )
    if len(terminal_markers) > 1:
        diagnostics.extend(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_DUPLICATE_TERMINAL_MARKER",
                json_pointer("path", path_index, "terminal"),
                "only one terminal path marker is executable",
                related=(
                    DiagnosticLocation(
                        path=json_pointer("path", terminal_markers[0], "terminal"),
                        message="First marker.",
                    ),
                ),
                suggested_fix="Keep a single terminal marker.",
            )
            for path_index in terminal_markers[1:]
        )
    if terminal_markers and terminal_markers[0] != len(document["path"]) - 1:
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_UNREACHABLE_PATH_AFTER_TERMINAL",
                json_pointer("path", terminal_markers[0], "terminal"),
                "path entries after the terminal marker can never execute",
                suggested_fix="Move the terminal marker to the end or remove unreachable entries.",
            )
        )

    use_declarations: dict[str, list[int]] = {}
    for use_index, use_definition in enumerate(document.get("uses", [])):
        use_declarations.setdefault(use_definition["ref"], []).append(use_index)
    for reference, declaration_indexes in use_declarations.items():
        for duplicate_index in declaration_indexes[1:]:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_USE_DECLARATION",
                    json_pointer("uses", duplicate_index, "ref"),
                    f"subflow {reference!r} is declared more than once",
                    related=(
                        DiagnosticLocation(
                            path=json_pointer("uses", declaration_indexes[0], "ref"),
                            message="First declaration.",
                        ),
                    ),
                    suggested_fix="Keep one declaration and one configuration per subflow.",
                )
            )
        if reference not in subflow_anchors:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_ORPHAN_USE_DECLARATION",
                    json_pointer("uses", declaration_indexes[0], "ref"),
                    f"subflow {reference!r} is declared but has no path anchor",
                    suggested_fix="Add its use step or remove the unused declaration.",
                )
            )
    for reference, anchor_indexes in subflow_anchors.items():
        if reference not in use_declarations:
            diagnostics.extend(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_USE_DECLARATION_MISSING",
                    json_pointer("path", path_index, "use"),
                    f"subflow anchor {reference!r} has no uses declaration",
                    suggested_fix="Declare the versioned subflow and its configuration in uses.",
                )
                for path_index in anchor_indexes
            )
        for duplicate_index in anchor_indexes[1:]:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_USE_ANCHOR",
                    json_pointer("path", duplicate_index, "use"),
                    f"subflow {reference!r} is anchored more than once and would duplicate node ids",
                    related=(
                        DiagnosticLocation(
                            path=json_pointer("path", anchor_indexes[0], "use"),
                            message="First anchor.",
                        ),
                    ),
                    suggested_fix="Use one anchor per declared subflow instance.",
                )
            )

    await_definition = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external"),
    )
    if external_wait_anchors and await_definition is None:
        diagnostics.append(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_AWAIT_DEFINITION_MISSING",
                json_pointer("path", external_wait_anchors[0], "await_external"),
                "external-wait marker has no capabilities.await_external contract",
            )
        )
    if len(external_wait_anchors) > 1:
        diagnostics.extend(
            semantic_diagnostic(
                "FLOWSPEC_SEMANTIC_DUPLICATE_AWAIT_MARKER",
                json_pointer("path", path_index, "await_external"),
                "the format supports one external-wait binding per flow",
            )
            for path_index in external_wait_anchors[1:]
        )

    return diagnostics, identifiers, slot_positions


def domain_contracts(document: dict[str, Any]) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    domains = cast(dict[str, dict[str, Any]], document.get("domains", {}))
    for domain_name, domain_definition in domains.items():
        domain_path = json_pointer("domains", domain_name)
        if (
            "minimum" in domain_definition
            and "maximum" in domain_definition
            and domain_definition["minimum"] > domain_definition["maximum"]
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_INVALID_NUMERIC_RANGE",
                    json_pointer("domains", domain_name, "minimum"),
                    "numeric domain minimum exceeds maximum",
                    related=(
                        DiagnosticLocation(path=json_pointer("domains", domain_name, "maximum")),
                    ),
                )
            )
        values = cast(list[Any], domain_definition.get("values", []))
        for value_index, domain_value in enumerate(values):
            if domain_value in values[:value_index]:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_DUPLICATE_DOMAIN_VALUE",
                        json_pointer("domains", domain_name, "values", value_index),
                        f"domain value {domain_value!r} is duplicated",
                        suggested_fix="Keep each canonical value once.",
                    )
                )

        if domain_definition.get("type") == "categorical":
            normalization = cast(dict[str, Any], domain_definition.get("normalize", {}))
            accent_fold = bool(normalization.get("accent_fold", False))
            normalized_inputs: dict[str, tuple[Any, str]] = {}
            for value_index, domain_value in enumerate(values):
                if domain_value is None:
                    continue
                normalized_value = normalize_text(domain_value, accent_fold=accent_fold)
                if not normalized_value:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_EMPTY_NORMALIZED_VALUE",
                            json_pointer("domains", domain_name, "values", value_index),
                            "canonical categorical value normalizes to an empty input",
                            suggested_fix="Use a non-whitespace canonical value.",
                        )
                    )
                    continue
                if (first_binding := normalized_inputs.get(normalized_value)) is not None:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_VALUE",
                            json_pointer("domains", domain_name, "values", value_index),
                            "canonical values collapse to the same normalized input",
                            related=(
                                DiagnosticLocation(
                                    path=first_binding[1],
                                    message="Conflicting canonical value.",
                                ),
                            ),
                            suggested_fix="Choose canonical values that remain distinct after configured normalization.",
                        )
                    )
                else:
                    normalized_inputs[normalized_value] = (
                        domain_value,
                        json_pointer("domains", domain_name, "values", value_index),
                    )

            if normalization.get("number_words"):
                for number_input, target in categorical_number_bindings(values).items():
                    normalized_number_input = normalize_text(number_input, accent_fold=accent_fold)
                    number_words_path = json_pointer(
                        "domains", domain_name, "normalize", "number_words"
                    )
                    first_binding = normalized_inputs.get(normalized_number_input)
                    if first_binding is not None and (
                        type(first_binding[0]) is not type(target) or first_binding[0] != target
                    ):
                        diagnostics.append(
                            semantic_diagnostic(
                                "FLOWSPEC_SEMANTIC_NUMBER_INPUT_COLLISION",
                                number_words_path,
                                "positional number input collides with a different canonical value",
                                related=(DiagnosticLocation(path=first_binding[1]),),
                            )
                        )
                    else:
                        normalized_inputs.setdefault(
                            normalized_number_input,
                            (target, number_words_path),
                        )

            for synonym, target in cast(dict[str, Any], normalization.get("synonyms", {})).items():
                synonym_path = json_pointer(
                    "domains", domain_name, "normalize", "synonyms", synonym
                )
                normalized_synonym = normalize_text(synonym, accent_fold=accent_fold)
                if not normalized_synonym:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_EMPTY_NORMALIZED_ALIAS",
                            synonym_path,
                            "categorical alias normalizes to an empty input and is unreachable",
                        )
                    )
                    continue
                first_binding = normalized_inputs.get(normalized_synonym)
                if first_binding is not None and (
                    type(first_binding[0]) is not type(target) or first_binding[0] != target
                ):
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_ALIAS",
                            synonym_path,
                            "alias collides with an input bound to a different canonical value",
                            related=(DiagnosticLocation(path=first_binding[1]),),
                        )
                    )
                else:
                    normalized_inputs.setdefault(normalized_synonym, (target, synonym_path))

        if domain_definition.get("type") == "bool":
            normalization = cast(dict[str, Any], domain_definition.get("normalize", {}))
            accent_fold = bool(normalization.get("accent_fold", False))
            normalized_aliases: dict[str, tuple[bool, str]] = {}
            for synonym, target in cast(dict[str, bool], normalization.get("synonyms", {})).items():
                synonym_path = json_pointer(
                    "domains", domain_name, "normalize", "synonyms", synonym
                )
                normalized_synonym = normalize_text(synonym, accent_fold=accent_fold)
                if not normalized_synonym:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_EMPTY_NORMALIZED_ALIAS",
                            synonym_path,
                            "boolean alias normalizes to an empty input and is unreachable",
                        )
                    )
                    continue
                if (
                    first_alias := normalized_aliases.get(normalized_synonym)
                ) is not None and first_alias[0] is not target:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_ALIAS",
                            synonym_path,
                            "boolean aliases normalize to the same input with different targets",
                            related=(DiagnosticLocation(path=first_alias[1]),),
                        )
                    )
                else:
                    normalized_aliases.setdefault(normalized_synonym, (target, synonym_path))
                built_in_target = (
                    parse_affirmation(
                        synonym,
                        emoji_veto=bool(normalization.get("emoji_veto", False)),
                    )
                    if normalization.get("affirmation")
                    else {
                        "true": True,
                        "1": True,
                        "sim": True,
                        "s": True,
                        "false": False,
                        "0": False,
                        "nao": False,
                        "não": False,
                        "n": False,
                    }.get(normalized_synonym)
                )
                if built_in_target is not None and built_in_target is not target:
                    diagnostics.append(
                        semantic_diagnostic(
                            "FLOWSPEC_SEMANTIC_ALIAS_CONFLICTS_WITH_BOOLEAN_NORMALIZATION",
                            synonym_path,
                            "boolean alias conflicts with the configured built-in interpretation",
                        )
                    )

        row_values: dict[Any, int] = {}
        for row_index, row_definition in enumerate(domain_definition.get("rows", [])):
            row_value = row_definition["value"]
            if row_value not in values:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_ROW_VALUE_OUTSIDE_DOMAIN",
                        json_pointer("domains", domain_name, "rows", row_index, "value"),
                        f"row value {row_value!r} is not a canonical domain value",
                    )
                )
            if (first_row := row_values.get(row_value)) is not None:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_DUPLICATE_DOMAIN_ROW",
                        json_pointer("domains", domain_name, "rows", row_index, "value"),
                        f"row value {row_value!r} has more than one presentation row",
                        related=(
                            DiagnosticLocation(
                                path=json_pointer(
                                    "domains", domain_name, "rows", first_row, "value"
                                )
                            ),
                        ),
                    )
                )
            else:
                row_values[row_value] = row_index

        synonyms = cast(
            dict[str, Any],
            cast(dict[str, Any], domain_definition.get("normalize", {})).get("synonyms", {}),
        )
        allowed_targets = {True, False} if domain_definition.get("type") == "bool" else set(values)
        for synonym, target in synonyms.items():
            if target not in allowed_targets:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_SYNONYM_TARGET_OUTSIDE_DOMAIN",
                        json_pointer("domains", domain_name, "normalize", "synonyms", synonym),
                        f"synonym target {target!r} is not accepted by domain {domain_name!r}",
                        related=(DiagnosticLocation(path=domain_path),),
                    )
                )
    return diagnostics


def slot_contracts(
    document: dict[str, Any],
    slot_positions: dict[str, int],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    slots = cast(dict[str, dict[str, Any]], document.get("slots", {}))
    domains = cast(dict[str, Any], document.get("domains", {}))
    available_slots = set(slots)
    if external_slots is not None:
        available_slots.update(external_slots)
    derive_targets = {
        cast(str, derive_definition["writes"]) for derive_definition in document.get("derive", [])
    }

    for slot_name, slot_definition in slots.items():
        if slot_definition["domain"] not in domains:
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_DOMAIN",
                    json_pointer("slots", slot_name, "domain"),
                    f"domain {slot_definition['domain']!r} is not declared",
                )
            )
        if (
            slot_definition.get("required", True)
            and slot_name not in slot_positions
            and slot_name not in derive_targets
        ):
            diagnostics.append(
                semantic_diagnostic(
                    "FLOWSPEC_SEMANTIC_REQUIRED_SLOT_WITHOUT_PRODUCER",
                    json_pointer("slots", slot_name),
                    f"required slot {slot_name!r} has no collection or derive producer",
                    suggested_fix=(
                        "Add its collection step, derive it deterministically, or make it optional."
                    ),
                )
            )
        for requirement_index, requirement in enumerate(slot_definition.get("requires", [])):
            requirement_path = json_pointer("slots", slot_name, "requires", requirement_index)
            if requirement == slot_name:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_SELF_DEPENDENCY",
                        requirement_path,
                        f"slot {slot_name!r} cannot require itself",
                    )
                )
            elif external_slots is not None and requirement not in available_slots:
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_REQUIRED_SLOT",
                        requirement_path,
                        f"required slot {requirement!r} is not declared or exposed by the profile",
                    )
                )
            if (
                requirement in slot_positions
                and slot_name in slot_positions
                and slot_positions[requirement] >= slot_positions[slot_name]
            ):
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_REQUIREMENT_ORDER",
                        requirement_path,
                        f"required slot {requirement!r} is not collected before {slot_name!r}",
                        suggested_fix="Move the required slot earlier in path.",
                    )
                )

    dependency_graph = {
        slot_name: [
            requirement
            for requirement in slot_definition.get("requires", [])
            if requirement in slots
        ]
        for slot_name, slot_definition in slots.items()
    }
    active: list[str] = []
    completed: set[str] = set()
    reported_cycles: set[frozenset[str]] = set()

    def visit(slot_name: str) -> None:
        if slot_name in completed:
            return
        if slot_name in active:
            cycle = active[active.index(slot_name) :] + [slot_name]
            cycle_members = frozenset(cycle)
            if cycle_members not in reported_cycles:
                reported_cycles.add(cycle_members)
                diagnostics.append(
                    semantic_diagnostic(
                        "FLOWSPEC_SEMANTIC_SLOT_DEPENDENCY_CYCLE",
                        json_pointer("slots", slot_name, "requires"),
                        f"slot dependency cycle: {' -> '.join(cycle)}",
                        suggested_fix="Make requires a directed acyclic precedence relation.",
                    )
                )
            return
        active.append(slot_name)
        for requirement in dependency_graph[slot_name]:
            visit(requirement)
        active.pop()
        completed.add(slot_name)

    for slot_name in sorted(slots):
        visit(slot_name)
    return diagnostics
