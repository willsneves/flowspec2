"""Aggregate semantic linking for structurally valid FlowSpec2 documents."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from itertools import product
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .derive import decode_derive_key, encode_derive_component, encode_derive_key
from .diagnostics import DiagnosticLocation, FlowDiagnostic
from .domains import (
    categorical_number_bindings,
    make_slot_model,
    normalize_text,
    parse_affirmation,
)
from .ir import normalize_flow
from .profiles import FlowProfile, capability_is_requested
from .schema import schema as flowspec_schema

_PREDICATE_OPERATORS = frozenset({"in", "eq", "ne", "is_present", "and", "or", "not"})
_PREDICATE_NAMESPACES = frozenset({"slots", "internal", "payload", "config", "address"})
_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_CONFIG_PROPERTY_SCHEMAS = cast(
    Mapping[str, Mapping[str, Any]],
    flowspec_schema()["properties"]["config"]["properties"],
)


class FlowLinkError(ValueError):
    """Raised when a structurally valid flow fails semantic or profile linking."""

    def __init__(self, diagnostics: tuple[FlowDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        rendered_diagnostics = "; ".join(
            f"{diagnostic.code} at {diagnostic.path or '/'}: {diagnostic.message}"
            for diagnostic in diagnostics
        )
        super().__init__(f"FlowSpec2 linking failed: {rendered_diagnostics}")


def _pointer(*segments: str | int) -> str:
    return "".join(f"/{str(segment).replace('~', '~0').replace('/', '~1')}" for segment in segments)


def _diagnostic(
    code: str,
    path: str,
    message: str,
    *,
    related: tuple[DiagnosticLocation, ...] = (),
    suggested_fix: str | None = None,
) -> FlowDiagnostic:
    return FlowDiagnostic(
        code=code,
        severity="error",
        path=path,
        message=message,
        related_locations=related,
        suggested_fix=suggested_fix,
    )


def _slot_model_accepts(model: Any, slot_name: str, slot_value: Any) -> bool:
    try:
        model.model_validate({slot_name: slot_value})
    except Exception:
        return False
    return True


def _json_instance_type(instance: Any) -> str:
    if instance is None:
        return "null"
    if isinstance(instance, bool):
        return "boolean"
    if isinstance(instance, int):
        return "integer"
    if isinstance(instance, float):
        return "number"
    if isinstance(instance, str):
        return "string"
    if isinstance(instance, list):
        return "array"
    return "object"


def _schema_possible_types(schema: Any) -> frozenset[str]:
    if schema is False:
        return frozenset()
    if schema is True or not isinstance(schema, Mapping):
        return _JSON_SCHEMA_TYPES
    if "const" in schema:
        return frozenset({_json_instance_type(schema["const"])})
    if isinstance((enum_values := schema.get("enum")), (list, tuple)):
        return frozenset(_json_instance_type(enum_value) for enum_value in enum_values)
    schema_type = schema.get("type")
    possible_types = (
        frozenset({schema_type})
        if isinstance(schema_type, str)
        else frozenset(schema_type)
        if isinstance(schema_type, (list, tuple))
        else _JSON_SCHEMA_TYPES
    )
    if "number" in possible_types:
        possible_types = possible_types | {"integer"}
    for union_keyword in ("anyOf", "oneOf"):
        if isinstance((branches := schema.get(union_keyword)), (list, tuple)):
            possible_types &= frozenset().union(
                *(_schema_possible_types(branch) for branch in branches)
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            possible_types &= _schema_possible_types(branch)
    return possible_types


def _schema_closed_values(schema: Any) -> tuple[Any, ...] | None:
    if not isinstance(schema, Mapping):
        return () if schema is False else None
    if "const" in schema:
        return (schema["const"],)
    if isinstance((enum_values := schema.get("enum")), (list, tuple)):
        return tuple(enum_values)
    for union_keyword in ("anyOf", "oneOf"):
        if isinstance((branches := schema.get(union_keyword)), (list, tuple)):
            branch_values = tuple(_schema_closed_values(branch) for branch in branches)
            if all(values is not None for values in branch_values):
                return tuple(
                    value for values in branch_values for value in cast(tuple[Any, ...], values)
                )
    return None


def _schema_accepts(schema: Mapping[str, Any], instance: Any) -> bool:
    return Draft202012Validator(
        _mutable_json(schema),
        format_checker=FormatChecker(),
    ).is_valid(instance)


def _schemas_are_obviously_disjoint(
    first_schema: Mapping[str, Any],
    second_schema: Mapping[str, Any],
) -> bool:
    first_values = _schema_closed_values(first_schema)
    if first_values is not None:
        return not any(_schema_accepts(second_schema, value) for value in first_values)
    second_values = _schema_closed_values(second_schema)
    if second_values is not None:
        return not any(_schema_accepts(first_schema, value) for value in second_values)
    return not bool(_schema_possible_types(first_schema) & _schema_possible_types(second_schema))


def _schema_property(schema: Mapping[str, Any], property_path: str) -> Mapping[str, Any] | None:
    candidates: tuple[Any, ...] = (schema,)
    for property_name in property_path.split("."):
        next_candidates: list[Any] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            branches = tuple(
                branch
                for keyword in ("anyOf", "oneOf", "allOf")
                for branch in candidate.get(keyword, ())
                if isinstance(candidate.get(keyword), (list, tuple))
            )
            candidate_schemas = (candidate, *branches)
            for candidate_schema in candidate_schemas:
                if not isinstance(candidate_schema, Mapping):
                    continue
                properties = candidate_schema.get("properties")
                if isinstance(properties, Mapping) and property_name in properties:
                    next_candidates.append(properties[property_name])
        candidates = tuple(next_candidates)
        if not candidates:
            return None
    mutable_candidates = tuple(_mutable_json(candidate) for candidate in candidates)
    return cast(
        Mapping[str, Any],
        mutable_candidates[0]
        if len(mutable_candidates) == 1
        else {"allOf": list(mutable_candidates)},
    )


def _step_identifier(path_step: Mapping[str, Any], terminal_identifier: str) -> str | None:
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


def _path_contracts(
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
        source_path = _pointer("path", path_index)
        if "terminal" in path_step:
            terminal_markers.append(path_index)
        if "use" in path_step:
            subflow_anchors.setdefault(path_step["use"], []).append(path_index)
        if "await_external" in path_step:
            external_wait_anchors.append(path_index)
        if "slot" in path_step or "confirm" in path_step:
            slot_name = cast(str, path_step.get("slot", path_step.get("confirm")))
            slot_positions[slot_name] = path_index
        identifier = _step_identifier(path_step, terminal_identifier)
        if identifier is None:
            continue
        if (first_path := identifiers.get(identifier)) is not None:
            diagnostics.append(
                _diagnostic(
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
            _diagnostic(
                "FLOWSPEC_SEMANTIC_TERMINAL_DEFINITION_MISSING",
                _pointer("path", path_index, "terminal"),
                "terminal path marker has no top-level terminal definition",
                suggested_fix="Declare the terminal action or remove its path marker.",
            )
            for path_index in terminal_markers
        )
    if terminal and not terminal_markers:
        diagnostics.append(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_TERMINAL_MARKER_MISSING",
                "/terminal",
                "terminal action is unreachable because path has no terminal marker",
                suggested_fix='Add {"terminal": true} at the intended path position.',
            )
        )
    if len(terminal_markers) > 1:
        diagnostics.extend(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_DUPLICATE_TERMINAL_MARKER",
                _pointer("path", path_index, "terminal"),
                "only one terminal path marker is executable",
                related=(
                    DiagnosticLocation(
                        path=_pointer("path", terminal_markers[0], "terminal"),
                        message="First marker.",
                    ),
                ),
                suggested_fix="Keep a single terminal marker.",
            )
            for path_index in terminal_markers[1:]
        )
    if terminal_markers and terminal_markers[0] != len(document["path"]) - 1:
        diagnostics.append(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_UNREACHABLE_PATH_AFTER_TERMINAL",
                _pointer("path", terminal_markers[0], "terminal"),
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
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_USE_DECLARATION",
                    _pointer("uses", duplicate_index, "ref"),
                    f"subflow {reference!r} is declared more than once",
                    related=(
                        DiagnosticLocation(
                            path=_pointer("uses", declaration_indexes[0], "ref"),
                            message="First declaration.",
                        ),
                    ),
                    suggested_fix="Keep one declaration and one configuration per subflow.",
                )
            )
        if reference not in subflow_anchors:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_ORPHAN_USE_DECLARATION",
                    _pointer("uses", declaration_indexes[0], "ref"),
                    f"subflow {reference!r} is declared but has no path anchor",
                    suggested_fix="Add its use step or remove the unused declaration.",
                )
            )
    for reference, anchor_indexes in subflow_anchors.items():
        if reference not in use_declarations:
            diagnostics.extend(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_USE_DECLARATION_MISSING",
                    _pointer("path", path_index, "use"),
                    f"subflow anchor {reference!r} has no uses declaration",
                    suggested_fix="Declare the versioned subflow and its configuration in uses.",
                )
                for path_index in anchor_indexes
            )
        for duplicate_index in anchor_indexes[1:]:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_USE_ANCHOR",
                    _pointer("path", duplicate_index, "use"),
                    f"subflow {reference!r} is anchored more than once and would duplicate node ids",
                    related=(
                        DiagnosticLocation(
                            path=_pointer("path", anchor_indexes[0], "use"),
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
            _diagnostic(
                "FLOWSPEC_SEMANTIC_AWAIT_DEFINITION_MISSING",
                _pointer("path", external_wait_anchors[0], "await_external"),
                "external-wait marker has no capabilities.await_external contract",
            )
        )
    if len(external_wait_anchors) > 1:
        diagnostics.extend(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_DUPLICATE_AWAIT_MARKER",
                _pointer("path", path_index, "await_external"),
                "the format supports one external-wait binding per flow",
            )
            for path_index in external_wait_anchors[1:]
        )

    return diagnostics, identifiers, slot_positions


def _domain_contracts(document: dict[str, Any]) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    domains = cast(dict[str, dict[str, Any]], document.get("domains", {}))
    for domain_name, domain_definition in domains.items():
        domain_path = _pointer("domains", domain_name)
        if (
            "minimum" in domain_definition
            and "maximum" in domain_definition
            and domain_definition["minimum"] > domain_definition["maximum"]
        ):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_INVALID_NUMERIC_RANGE",
                    _pointer("domains", domain_name, "minimum"),
                    "numeric domain minimum exceeds maximum",
                    related=(DiagnosticLocation(path=_pointer("domains", domain_name, "maximum")),),
                )
            )
        values = cast(list[Any], domain_definition.get("values", []))
        for value_index, domain_value in enumerate(values):
            if domain_value in values[:value_index]:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_DUPLICATE_DOMAIN_VALUE",
                        _pointer("domains", domain_name, "values", value_index),
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
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_EMPTY_NORMALIZED_VALUE",
                            _pointer("domains", domain_name, "values", value_index),
                            "canonical categorical value normalizes to an empty input",
                            suggested_fix="Use a non-whitespace canonical value.",
                        )
                    )
                    continue
                if (first_binding := normalized_inputs.get(normalized_value)) is not None:
                    diagnostics.append(
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_AMBIGUOUS_NORMALIZED_VALUE",
                            _pointer("domains", domain_name, "values", value_index),
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
                        _pointer("domains", domain_name, "values", value_index),
                    )

            if normalization.get("number_words"):
                for number_input, target in categorical_number_bindings(values).items():
                    normalized_number_input = normalize_text(number_input, accent_fold=accent_fold)
                    number_words_path = _pointer(
                        "domains", domain_name, "normalize", "number_words"
                    )
                    first_binding = normalized_inputs.get(normalized_number_input)
                    if first_binding is not None and (
                        type(first_binding[0]) is not type(target) or first_binding[0] != target
                    ):
                        diagnostics.append(
                            _diagnostic(
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
                synonym_path = _pointer("domains", domain_name, "normalize", "synonyms", synonym)
                normalized_synonym = normalize_text(synonym, accent_fold=accent_fold)
                if not normalized_synonym:
                    diagnostics.append(
                        _diagnostic(
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
                        _diagnostic(
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
                synonym_path = _pointer("domains", domain_name, "normalize", "synonyms", synonym)
                normalized_synonym = normalize_text(synonym, accent_fold=accent_fold)
                if not normalized_synonym:
                    diagnostics.append(
                        _diagnostic(
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
                        _diagnostic(
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
                        _diagnostic(
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_ROW_VALUE_OUTSIDE_DOMAIN",
                        _pointer("domains", domain_name, "rows", row_index, "value"),
                        f"row value {row_value!r} is not a canonical domain value",
                    )
                )
            if (first_row := row_values.get(row_value)) is not None:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_DUPLICATE_DOMAIN_ROW",
                        _pointer("domains", domain_name, "rows", row_index, "value"),
                        f"row value {row_value!r} has more than one presentation row",
                        related=(
                            DiagnosticLocation(
                                path=_pointer("domains", domain_name, "rows", first_row, "value")
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_SYNONYM_TARGET_OUTSIDE_DOMAIN",
                        _pointer("domains", domain_name, "normalize", "synonyms", synonym),
                        f"synonym target {target!r} is not accepted by domain {domain_name!r}",
                        related=(DiagnosticLocation(path=domain_path),),
                    )
                )
    return diagnostics


def _slot_contracts(
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
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_DOMAIN",
                    _pointer("slots", slot_name, "domain"),
                    f"domain {slot_definition['domain']!r} is not declared",
                )
            )
        if (
            slot_definition.get("required", True)
            and slot_name not in slot_positions
            and slot_name not in derive_targets
        ):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_REQUIRED_SLOT_WITHOUT_PRODUCER",
                    _pointer("slots", slot_name),
                    f"required slot {slot_name!r} has no collection or derive producer",
                    suggested_fix=(
                        "Add its collection step, derive it deterministically, or make it optional."
                    ),
                )
            )
        for requirement_index, requirement in enumerate(slot_definition.get("requires", [])):
            requirement_path = _pointer("slots", slot_name, "requires", requirement_index)
            if requirement == slot_name:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_SELF_DEPENDENCY",
                        requirement_path,
                        f"slot {slot_name!r} cannot require itself",
                    )
                )
            elif external_slots is not None and requirement not in available_slots:
                diagnostics.append(
                    _diagnostic(
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
                    _diagnostic(
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_SLOT_DEPENDENCY_CYCLE",
                        _pointer("slots", slot_name, "requires"),
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


def _derive_contracts(
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
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_DERIVE_TARGET",
                    _pointer("derive", derive_index, "writes"),
                    f"derive target {target!r} has more than one definition",
                    related=(DiagnosticLocation(path=_pointer("derive", first_index, "writes")),),
                )
            )
        else:
            definitions[target] = derive_index
        if target in native_collected_slots:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DERIVE_OVERWRITES_DECLARED_SLOT",
                    _pointer("derive", derive_index, "writes"),
                    f"derive target {target!r} collides with a collected slot",
                    suggested_fix="Use a distinct derived state key.",
                )
            )
        if external_slots is not None and target in external_slots:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DERIVE_OVERWRITES_PROFILE_SLOT",
                    _pointer("derive", derive_index, "writes"),
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
    gated_step_identifiers = set(
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
            or source_path_step.get("step") in gated_step_identifiers
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
        string_kinds = {"cpf", "email", "name", "free_text"}
        if source_kind not in string_kinds or target_kind not in string_kinds:
            return False if source_kind != target_kind else None
        if target_kind == "free_text":
            if target_domain.get("optional"):
                return True
            return source_kind != "free_text" or not source_domain.get("optional")
        if source_kind == target_kind:
            return True
        if source_kind == "cpf" and target_kind == "name":
            return True
        return False

    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        derive_target = cast(str, derive_definition["writes"])
        if derive_target not in explicit_derive_nodes and "after" not in derive_definition:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNPLACED_DERIVE",
                    _pointer("derive", derive_index),
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_SOURCE",
                        _pointer("derive", derive_index, "from", source_index),
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
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_NON_INJECTIVE_DERIVE_KEY",
                            _pointer("derive", derive_index, "from", source_index),
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
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_ANCHOR",
                    _pointer("derive", derive_index, "after"),
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_INVALID_DERIVE_DEFAULT_REFERENCE",
                        _pointer("derive", derive_index, "default"),
                        f"derive default {default_value!r} does not reference an existing source",
                    )
                )
        expected_arity = len(derive_definition["from"])
        for lookup_key in derive_definition["lookup"]:
            lookup_components = decode_derive_key(lookup_key, expected_arity)
            if len(lookup_components) != expected_arity:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_ARITY",
                        _pointer("derive", derive_index, "lookup", lookup_key),
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
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_DERIVE_LOOKUP_VALUE_OUT_OF_DOMAIN",
                            _pointer("derive", derive_index, "lookup", lookup_key),
                            f"lookup component {lookup_component!r} is not reachable from "
                            f"source {source_slot!r}",
                            related=(
                                DiagnosticLocation(
                                    path=_pointer("derive", derive_index, "from", source_index),
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
                            _pointer("derive", derive_index, "lookup", lookup_key),
                            lookup_value,
                        )
                        for lookup_key, lookup_value in derive_definition["lookup"].items()
                    ),
                    *(
                        [
                            (
                                _pointer("derive", derive_index, "default"),
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
                            _diagnostic(
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
                                _slot_model_accepts(
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
                                _diagnostic(
                                    "FLOWSPEC_SEMANTIC_DERIVE_DEFAULT_SOURCE_INCOMPATIBLE",
                                    _pointer("derive", derive_index, "default"),
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
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_REQUIRED_DERIVE_NOT_TOTAL",
                            _pointer("derive", derive_index),
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_DEPENDENCY_CYCLE",
                        _pointer("derive", definitions[target], "from"),
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
        node_identifier = _step_identifier(path_step, terminal_identifier)
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_ANCHOR_ORDER",
                        _pointer("derive", derive_index, "after"),
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_DERIVE_EXECUTION_ORDER",
                        _pointer("derive", derive_index, "from", source_index),
                        f"derive source {source_slot!r} is produced after {target!r} would execute",
                        suggested_fix="Move the derive after every source producer.",
                    )
                )

    for path_index, path_step in enumerate(document["path"]):
        if "derive" in path_step and path_step["derive"] not in definitions:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_DERIVE_TARGET",
                    _pointer("path", path_index, "derive"),
                    f"derive step {path_step['derive']!r} has no derive definition",
                )
            )
    return diagnostics


def _entry_schema_contracts(
    document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    entry_schema = cast(dict[str, Any] | None, document["route"].get("entry_args_schema"))
    if entry_schema is None:
        return []
    try:
        Draft202012Validator.check_schema(entry_schema)
    except SchemaError as schema_error:
        return [
            _diagnostic(
                "FLOWSPEC_SEMANTIC_INVALID_ENTRY_SCHEMA",
                "/route/entry_args_schema",
                f"entry_args_schema is not a valid Draft 2020-12 schema: {schema_error.message}",
            )
        ]

    diagnostics: list[FlowDiagnostic] = []
    if entry_schema.get("type") != "object":
        diagnostics.append(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_ENTRY_SCHEMA_NOT_OBJECT",
                "/route/entry_args_schema/type",
                "entry_args_schema must describe one object of initial slot values",
                suggested_fix="Set type to object and declare the permitted slot properties.",
            )
        )
    if entry_schema.get("additionalProperties") is not False:
        diagnostics.append(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_ENTRY_SCHEMA_OPEN",
                "/route/entry_args_schema/additionalProperties",
                "entry_args_schema must reject undeclared entry fields",
                suggested_fix="Set additionalProperties to false.",
            )
        )
    available_slots = set(document.get("slots", {}))
    if external_slots is not None:
        available_slots.update(external_slots)
    for property_name in cast(dict[str, Any], entry_schema.get("properties", {})):
        if property_name not in available_slots:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_ENTRY_SLOT",
                    _pointer("route", "entry_args_schema", "properties", property_name),
                    f"entry argument {property_name!r} is not a declared or exposed slot",
                )
            )
    return diagnostics


def _state_writer_contracts(
    document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    """Reject state keys whose ownership or write phase is ambiguous."""

    diagnostics: list[FlowDiagnostic] = []
    declared_slots = set(document.get("slots", {}))
    state_slots = declared_slots | set(external_slots or ())
    derive_locations = {
        cast(str, derive_definition["writes"]): _pointer("derive", derive_index, "writes")
        for derive_index, derive_definition in enumerate(document.get("derive", []))
    }
    await_locations: dict[str, str] = {}
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    if await_definition:
        on_resume = cast(dict[str, Any], await_definition.get("on_resume", {}))
        for state_key in cast(dict[str, Any], on_resume.get("set", {})):
            await_locations[state_key] = _pointer(
                "capabilities", "await_external", "on_resume", "set", state_key
            )
        enrichment = on_resume.get("enrich")
        if isinstance(enrichment, dict):
            for state_key in cast(dict[str, Any], enrichment.get("set", {})):
                await_locations[state_key] = _pointer(
                    "capabilities",
                    "await_external",
                    "on_resume",
                    "enrich",
                    "set",
                    state_key,
                )
        for transition_name, transition in {
            "timeout": await_definition.get("timeout"),
            **cast(dict[str, Any], await_definition.get("recovery", {})),
        }.items():
            if not isinstance(transition, dict):
                continue
            for state_key in cast(dict[str, Any], transition.get("set", {})):
                await_locations.setdefault(
                    state_key,
                    _pointer(
                        "capabilities",
                        "await_external",
                        transition_name if transition_name == "timeout" else "recovery",
                        *(() if transition_name == "timeout" else (transition_name,)),
                        "set",
                        state_key,
                    ),
                )

    terminal_locations: dict[str, str] = {}
    terminal = cast(dict[str, Any], document.get("terminal", {}))
    for state_key in terminal.get("outputs", {}):
        terminal_locations[state_key] = _pointer("terminal", "outputs", state_key)
    success_set = cast(
        dict[str, Any],
        cast(dict[str, Any], terminal.get("outcomes", {})).get("success", {}).get("set", {}),
    )
    for state_key in success_set:
        if state_key in terminal_locations:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_TERMINAL_WRITE",
                    _pointer("terminal", "outcomes", "success", "set", state_key),
                    f"terminal writes state key {state_key!r} through outputs and success.set",
                    related=(DiagnosticLocation(path=terminal_locations[state_key]),),
                )
            )
        terminal_locations.setdefault(
            state_key,
            _pointer("terminal", "outcomes", "success", "set", state_key),
        )

    entry = cast(dict[str, Any], document.get("entry", {}))
    entry_key = cast(str | None, entry.get("writes"))
    protected_keys = (
        state_slots | set(derive_locations) | set(await_locations) | set(terminal_locations)
    )
    reserved_service_key = {"service"} if document.get("service") else set()
    if entry_key is not None and entry_key in protected_keys | reserved_service_key:
        diagnostics.append(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_ENTRY_WRITE_COLLISION",
                "/entry/writes",
                f"entry result key {entry_key!r} has another state owner",
            )
        )

    if document.get("service") and "service" in state_slots | set(derive_locations) | set(
        await_locations
    ) | set(terminal_locations):
        diagnostics.append(
            _diagnostic(
                "FLOWSPEC_SEMANTIC_SERVICE_STATE_COLLISION",
                "/service",
                "state key 'service' is reserved for namespaced service metadata",
            )
        )

    for derived_key, derive_path in derive_locations.items():
        conflicting_path = await_locations.get(derived_key) or terminal_locations.get(derived_key)
        if conflicting_path is not None:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DERIVE_WRITE_COLLISION",
                    derive_path,
                    f"derived key {derived_key!r} is also written by another phase",
                    related=(DiagnosticLocation(path=conflicting_path),),
                )
            )

    for terminal_key, terminal_path in terminal_locations.items():
        if terminal_key in state_slots:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_TERMINAL_SLOT_COLLISION",
                    terminal_path,
                    f"terminal write {terminal_key!r} would bypass its slot domain",
                    suggested_fix="Write a distinct result key.",
                )
            )
        if terminal_key in await_locations:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_PHASE_WRITE_COLLISION",
                    terminal_path,
                    f"state key {terminal_key!r} is also written by await_external",
                    related=(DiagnosticLocation(path=await_locations[terminal_key]),),
                )
            )
    return diagnostics


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
                yield predicate, _pointer("path", path_index, predicate_key)
        interactive = cast(dict[str, Any], path_step.get("interactive") or {})
        for option_index, conditional_option in enumerate(interactive.get("options_when", [])):
            if isinstance(predicate := conditional_option.get("gate"), dict):
                yield (
                    predicate,
                    _pointer(
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
                _pointer("confirm", "interactive", "options_when", option_index, "gate"),
            )

    gates = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("overrides", {})).get("gates", {}),
    )
    for gate_identifier, predicate in gates.items():
        if isinstance(predicate, dict):
            yield predicate, _pointer("overrides", "gates", gate_identifier)

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
        return _schema_property(address_schema, state_key), True
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
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_ADDRESS_REFERENCE",
                    f"{operator_path}/0",
                    f"predicate references unknown address field {reference!r}",
                )
            )
        if reference_schema is not None and isinstance(operand[1], list):
            for literal_index, literal in enumerate(operand[1]):
                if not _schema_accepts(reference_schema, literal):
                    diagnostics.append(
                        _diagnostic(
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
                _diagnostic(
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
            and _schemas_are_obviously_disjoint(first_schema, second_schema)
        ):
            diagnostics.append(
                _diagnostic(
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
        if reference_schema is not None and not _schema_accepts(reference_schema, literal):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_PREDICATE_LITERAL_OUT_OF_DOMAIN",
                    _predicate_literal_path(
                        literal_operand,
                        f"{operator_path}/{literal_index}",
                    ),
                    f"predicate literal {literal!r} is not accepted by {reference!r}",
                )
            )
    return diagnostics


def _predicate_contracts(
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
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIG_REFERENCE",
                        reference_path,
                        f"predicate references unknown config key {key!r}",
                    )
                )
            elif namespace == "address" and not uses_address:
                diagnostics.append(
                    _diagnostic(
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
                        _diagnostic(
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
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_UNKNOWN_PREDICATE_SLOT",
                            reference_path,
                            f"predicate references unknown state key {reference!r}",
                        )
                    )
    return diagnostics


def _rail_reference_contracts(
    document: dict[str, Any],
    identifiers: dict[str, str],
    slot_positions: dict[str, int],
    *,
    external_slots: frozenset[str] | None,
) -> list[FlowDiagnostic]:
    diagnostics: list[FlowDiagnostic] = []
    declared_slots = set(document.get("slots", {}))
    available_slots = set(declared_slots)
    if external_slots is not None:
        available_slots.update(external_slots)
    derive_targets = {definition["writes"] for definition in document.get("derive", [])}
    available_inputs = available_slots | derive_targets

    for path_index, path_step in enumerate(document["path"]):
        for step_kind in ("slot", "confirm"):
            if step_kind in path_step and path_step[step_kind] not in declared_slots:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_NATIVE_PATH_SLOT",
                        _pointer("path", path_index, step_kind),
                        f"native {step_kind} step must reference a top-level declared slot",
                    )
                )
        if "confirm" in path_step and path_step["confirm"] in declared_slots:
            confirmation_slot = cast(dict[str, Any], document["slots"][path_step["confirm"]])
            confirmation_domain = cast(
                dict[str, Any] | None,
                cast(dict[str, Any], document.get("domains", {})).get(confirmation_slot["domain"]),
            )
            if confirmation_domain is not None and confirmation_domain["type"] != "bool":
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_DOMAIN_NOT_BOOLEAN",
                        _pointer("path", path_index, "confirm"),
                        f"confirmation slot {path_step['confirm']!r} must use a bool domain",
                    )
                )
    gates = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("overrides", {})).get("gates", {}),
    )
    gateable_identifiers = {
        cast(str, path_step["step"])
        for path_step in document["path"]
        if "slot" in path_step or "confirm" in path_step
    }
    for gate_identifier in gates:
        if gate_identifier not in gateable_identifiers:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_GATE_STEP",
                    _pointer("overrides", "gates", gate_identifier),
                    f"gate target {gate_identifier!r} is not a native collect or confirm step",
                )
            )
            continue
        gated_step = next(
            path_step
            for path_step in document["path"]
            if path_step.get("step") == gate_identifier
            and ("slot" in path_step or "confirm" in path_step)
        )
        if "ask_when" in gated_step:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_DUPLICATE_GATE_SOURCE",
                    _pointer("overrides", "gates", gate_identifier),
                    "step has both inline ask_when and an override gate",
                    related=(
                        DiagnosticLocation(
                            path=_pointer("path", document["path"].index(gated_step), "ask_when")
                        ),
                    ),
                    suggested_fix="Keep one gate source for the step.",
                )
            )
        if "confirm" in gated_step and (
            gated_step.get("correctable") is True or "on_reject" in gated_step
        ):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_GATE_UNSUPPORTED_FOR_CONFIRMATION_VARIANT",
                    _pointer("overrides", "gates", gate_identifier),
                    "summary and correction-hub confirmations do not support override gates",
                )
            )

    if (confirmation := document.get("confirm")) is not None:
        if confirmation["step"] not in identifiers:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIRM_STEP",
                    "/confirm/step",
                    f"confirmation hub step {confirmation['step']!r} is not in path",
                )
            )
        if confirmation["slot"] not in declared_slots:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIRM_SLOT",
                    "/confirm/slot",
                    f"confirmation hub slot {confirmation['slot']!r} is not declared",
                )
            )
        else:
            confirmation_slot = cast(dict[str, Any], document["slots"][confirmation["slot"]])
            confirmation_domain = cast(
                dict[str, Any] | None,
                cast(dict[str, Any], document.get("domains", {})).get(confirmation_slot["domain"]),
            )
            if confirmation_domain is not None and confirmation_domain["type"] != "bool":
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_DOMAIN_NOT_BOOLEAN",
                        "/confirm/slot",
                        f"confirmation slot {confirmation['slot']!r} must use a bool domain",
                    )
                )
        matching_confirmation_steps = [
            path_step
            for path_step in document["path"]
            if path_step.get("step") == confirmation["step"] and "confirm" in path_step
        ]
        if matching_confirmation_steps:
            matching_step = matching_confirmation_steps[0]
            if matching_step["confirm"] != confirmation["slot"]:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_SLOT_MISMATCH",
                        "/confirm/slot",
                        "top-level confirmation slot differs from its path step",
                    )
                )
            if matching_step.get("correctable") is not True:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_CONFIRM_HUB_NOT_CORRECTABLE",
                        "/confirm/step",
                        "top-level confirmation must bind a path step marked correctable",
                    )
                )
        if (
            on_confirm := confirmation.get("on_confirm")
        ) is not None and on_confirm not in identifiers:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_CONFIRM_TARGET",
                    "/confirm/on_confirm",
                    f"confirmation target {on_confirm!r} is not an executable step",
                )
            )
        elif on_confirm is not None:
            confirmation_path = identifiers.get(confirmation["step"], "")
            target_path = identifiers[on_confirm]
            if confirmation_path.startswith("/path/") and target_path.startswith("/path/"):
                confirmation_index = int(confirmation_path.split("/")[2])
                target_index = int(target_path.split("/")[2])
                if target_index <= confirmation_index:
                    diagnostics.append(
                        _diagnostic(
                            "FLOWSPEC_SEMANTIC_CONFIRM_TARGET_NOT_FORWARD",
                            "/confirm/on_confirm",
                            "confirmation target must execute after the confirmation hub",
                            suggested_fix="Target a later path step, normally the terminal step.",
                        )
                    )
        for correctable_index, correctable_slot in enumerate(confirmation["correctable"]):
            if external_slots is not None and correctable_slot not in available_slots:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_CORRECTABLE_SLOT",
                        _pointer("confirm", "correctable", correctable_index),
                        f"correctable slot {correctable_slot!r} is not declared or exposed",
                    )
                )
            confirmation_source = identifiers.get(confirmation["step"], "")
            if (
                confirmation_source.startswith("/path/")
                and correctable_slot in slot_positions
                and slot_positions[correctable_slot] >= int(confirmation_source.split("/")[2])
            ):
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_CORRECTABLE_SLOT_ORDER",
                        _pointer("confirm", "correctable", correctable_index),
                        f"correctable slot {correctable_slot!r} is not produced before the hub",
                        suggested_fix="Move its producer before the confirmation hub.",
                    )
                )

    if (terminal := document.get("terminal")) is not None:
        for binding_index, binding in enumerate(terminal.get("input", [])):
            if external_slots is not None and binding["slot"] not in available_inputs:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_UNKNOWN_TERMINAL_INPUT",
                        _pointer("terminal", "input", binding_index, "slot"),
                        f"terminal input slot {binding['slot']!r} is not declared, derived, or exposed",
                    )
                )

    if (auto_flow := document.get("auto_flow")) is not None:
        native_path_identifiers = {
            identifier
            for identifier, source_path in identifiers.items()
            if source_path.startswith("/path/")
        }
        if auto_flow.get("resume_at") not in {None, *native_path_identifiers}:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_SEMANTIC_UNKNOWN_AUTO_FLOW_RESUME_STEP",
                    "/auto_flow/resume_at",
                    f"auto_flow.resume_at {auto_flow['resume_at']!r} is not a native path step",
                )
            )
        for prefill_index, prefill_slot in enumerate(auto_flow.get("prefill_from", [])):
            if prefill_slot not in declared_slots:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_SEMANTIC_NON_NATIVE_AUTO_FLOW_PREFILL_SLOT",
                        _pointer("auto_flow", "prefill_from", prefill_index),
                        f"auto-flow prefill slot {prefill_slot!r} is not a top-level slot",
                    )
                )
        for flow_field, mapping in auto_flow.get("alias_map", {}).items():
            candidate_mappings = (
                [mapping]
                if all(not isinstance(value, dict) for value in mapping.values())
                else [value for value in mapping.values() if isinstance(value, dict)]
            )
            for candidate_mapping in candidate_mappings:
                for destination_slot in candidate_mapping:
                    if destination_slot not in declared_slots:
                        diagnostics.append(
                            _diagnostic(
                                "FLOWSPEC_SEMANTIC_NON_NATIVE_AUTO_FLOW_DESTINATION",
                                _pointer("auto_flow", "alias_map", flow_field),
                                f"auto-flow alias destination {destination_slot!r} is not a "
                                "top-level slot",
                            )
                        )
    return diagnostics


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(nested_value) for nested_value in value]
    if isinstance(value, list):
        return [_mutable_json(nested_value) for nested_value in value]
    return value


def _profile_contracts(
    document: dict[str, Any],
    profile: FlowProfile,
) -> tuple[list[FlowDiagnostic], frozenset[str] | None, dict[str, int]]:
    diagnostics: list[FlowDiagnostic] = []
    exposed_slots: set[str] = set()
    exposed_slot_positions: dict[str, int] = {}
    exposed_slot_owners: dict[str, tuple[str, int]] = {}
    state_key_owners: dict[str, tuple[str, int, bool]] = {}
    has_open_manifest = False
    required_capabilities: set[tuple[str, str]] = set()
    use_anchor_positions = {
        cast(str, path_step["use"]): path_index
        for path_index, path_step in enumerate(document["path"])
        if "use" in path_step
    }
    authored_state_writers: dict[str, list[tuple[str, str]]] = {}

    def add_authored_state_writer(state_key: str, path: str, writer_kind: str) -> None:
        authored_state_writers.setdefault(state_key, []).append((path, writer_kind))

    for slot_name in document.get("slots", {}):
        add_authored_state_writer(slot_name, _pointer("slots", slot_name), "slot")
    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        add_authored_state_writer(
            cast(str, derive_definition["writes"]),
            _pointer("derive", derive_index, "writes"),
            "derive",
        )
    if (entry_definition := document.get("entry")) is not None and entry_definition.get("writes"):
        add_authored_state_writer(
            cast(str, entry_definition["writes"]),
            "/entry/writes",
            "entry",
        )
    if (terminal_definition := document.get("terminal")) is not None:
        for output_key in terminal_definition.get("outputs", {}):
            add_authored_state_writer(
                output_key,
                _pointer("terminal", "outputs", output_key),
                "terminal",
            )
        success_set = cast(
            dict[str, Any],
            cast(dict[str, Any], terminal_definition.get("outcomes", {}))
            .get("success", {})
            .get("set", {}),
        )
        for state_key in success_set:
            add_authored_state_writer(
                state_key,
                _pointer("terminal", "outcomes", "success", "set", state_key),
                "terminal",
            )

    for domain_name, domain_definition in document.get("domains", {}).items():
        domain_type = domain_definition["type"]
        if domain_type not in profile.domain_types:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_DOMAIN_UNSUPPORTED",
                    _pointer("domains", domain_name, "type"),
                    f"profile {profile.identifier!r} does not implement domain type {domain_type!r}",
                )
            )

    for use_index, use_definition in enumerate(document.get("uses", [])):
        reference = use_definition["ref"]
        if not profile.subflows.has(reference):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_SUBFLOW_UNAVAILABLE",
                    _pointer("uses", use_index, "ref"),
                    f"profile {profile.identifier!r} does not register subflow {reference!r}",
                )
            )
            has_open_manifest = True
            continue
        definition = profile.subflows.definition(reference)
        if definition.legacy_manifest and not profile.allow_legacy_contracts:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_LEGACY_SUBFLOW_MANIFEST_FORBIDDEN",
                    _pointer("uses", use_index, "ref"),
                    f"profile {profile.identifier!r} requires a typed manifest for "
                    f"subflow {reference!r}",
                )
            )
        if definition.exposed_slots is None:
            has_open_manifest = True
        else:
            exposed_slots.update(definition.exposed_slots)
            for exposed_slot in definition.exposed_slots:
                if (first_owner := exposed_slot_owners.get(exposed_slot)) is not None:
                    diagnostics.append(
                        _diagnostic(
                            "FLOWSPEC_PROFILE_SUBFLOW_SLOT_COLLISION",
                            _pointer("uses", use_index, "ref"),
                            f"subflow slot {exposed_slot!r} is also owned by {first_owner[0]!r}",
                            related=(
                                DiagnosticLocation(
                                    path=_pointer("uses", first_owner[1], "ref"),
                                    message="First owning subflow declaration.",
                                ),
                            ),
                        )
                    )
                else:
                    exposed_slot_owners[exposed_slot] = (reference, use_index)
                if reference in use_anchor_positions:
                    exposed_slot_positions[exposed_slot] = use_anchor_positions[reference]
            for colliding_slot in sorted(set(document.get("slots", {})) & definition.exposed_slots):
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_SLOT_COLLISION",
                        _pointer("slots", colliding_slot),
                        f"slot {colliding_slot!r} is owned by subflow {reference!r}",
                        related=(
                            DiagnosticLocation(
                                path=_pointer("uses", use_index, "ref"),
                                message="Owning subflow declaration.",
                            ),
                        ),
                        suggested_fix="Remove the duplicate top-level slot declaration.",
                    )
                )
        for state_key in sorted(definition.owned_state_keys):
            state_key_is_exposed = (
                definition.exposed_slots is not None and state_key in definition.exposed_slots
            )
            if (first_state_owner := state_key_owners.get(state_key)) is not None:
                if not (state_key_is_exposed and first_state_owner[2]):
                    diagnostics.append(
                        _diagnostic(
                            "FLOWSPEC_PROFILE_SUBFLOW_STATE_COLLISION",
                            _pointer("uses", use_index, "ref"),
                            f"subflow state key {state_key!r} is also owned by "
                            f"{first_state_owner[0]!r}",
                            related=(
                                DiagnosticLocation(
                                    path=_pointer("uses", first_state_owner[1], "ref"),
                                    message="First owning subflow declaration.",
                                ),
                            ),
                        )
                    )
            else:
                state_key_owners[state_key] = (
                    reference,
                    use_index,
                    state_key_is_exposed,
                )
            for writer_path, writer_kind in authored_state_writers.get(state_key, []):
                if writer_kind == "slot" and state_key_is_exposed:
                    continue
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_STATE_COLLISION",
                        writer_path,
                        f"{writer_kind} writes state key {state_key!r}, which is owned by "
                        f"subflow {reference!r}",
                        related=(
                            DiagnosticLocation(
                                path=_pointer("uses", use_index, "ref"),
                                message="Owning subflow declaration.",
                            ),
                        ),
                        suggested_fix="Remove the duplicate writer or use a distinct state key.",
                    )
                )
        required_capabilities.update(
            (capability, _pointer("uses", use_index, "ref"))
            for capability in definition.capabilities
        )
        for tool_name, required_version in definition.required_tools.items():
            tool_path = _pointer("uses", use_index, "ref")
            if not profile.tools.has(tool_name):
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_TOOL_UNAVAILABLE",
                        tool_path,
                        f"subflow {reference!r} requires tool {tool_name!r} at version "
                        f"{required_version!r}, but the profile does not register it",
                    )
                )
                continue
            actual_version = profile.tools.definition(tool_name).version
            if (
                profile.tools.definition(tool_name).legacy_contract
                and not profile.allow_legacy_contracts
            ):
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_PROFILE_LEGACY_TOOL_CONTRACT_FORBIDDEN",
                        tool_path,
                        f"profile {profile.identifier!r} requires a typed contract for "
                        f"tool {tool_name!r}",
                    )
                )
            if actual_version != required_version:
                diagnostics.append(
                    _diagnostic(
                        "FLOWSPEC_PROFILE_SUBFLOW_TOOL_VERSION_MISMATCH",
                        tool_path,
                        f"subflow {reference!r} requires tool {tool_name!r} at version "
                        f"{required_version!r}, but the profile provides {actual_version!r}",
                    )
                )
        configuration_schema = _mutable_json(definition.configuration_schema)
        configuration_validator = Draft202012Validator(
            configuration_schema,
            format_checker=FormatChecker(),
        )
        for configuration_error in sorted(
            configuration_validator.iter_errors(use_definition.get("with", {})),
            key=lambda error: tuple(str(segment) for segment in error.absolute_path),
        ):
            error_path = _pointer(
                "uses",
                use_index,
                "with",
                *tuple(configuration_error.absolute_path),
            )
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_SUBFLOW_CONFIGURATION_INVALID",
                    error_path,
                    f"configuration for {definition.ref!r}: {configuration_error.message}",
                )
            )

    tool_references: list[tuple[str, str]] = []
    if (entry := document.get("entry")) is not None:
        tool_references.append((entry["tool"], "/entry/tool"))
    if (terminal := document.get("terminal")) is not None:
        tool_references.append((terminal["tool"], "/terminal/tool"))
    await_external = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external"),
    )
    if await_external is not None:
        enrichment = cast(
            str | dict[str, Any] | None,
            cast(dict[str, Any], await_external.get("on_resume", {})).get("enrich"),
        )
        if not profile.allow_legacy_contracts and (
            not isinstance(await_external.get("resume"), dict) or isinstance(enrichment, str)
        ):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_LEGACY_AWAIT_CONTRACT_FORBIDDEN",
                    "/capabilities/await_external",
                    f"profile {profile.identifier!r} requires a versioned typed resume "
                    "contract and object-form enrichment",
                )
            )
        if isinstance(enrichment, str):
            tool_references.append((enrichment, "/capabilities/await_external/on_resume/enrich"))
        elif isinstance(enrichment, dict):
            tool_references.append(
                (
                    enrichment["tool"],
                    "/capabilities/await_external/on_resume/enrich/tool",
                )
            )
    for tool_name, tool_path in tool_references:
        if not profile.tools.has(tool_name):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_TOOL_UNAVAILABLE",
                    tool_path,
                    f"tool {tool_name!r} is not registered in profile {profile.identifier!r}",
                )
            )
        elif (
            profile.tools.definition(tool_name).legacy_contract
            and not profile.allow_legacy_contracts
        ):
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_LEGACY_TOOL_CONTRACT_FORBIDDEN",
                    tool_path,
                    f"profile {profile.identifier!r} requires a typed contract for "
                    f"tool {tool_name!r}",
                )
            )

    for capability_name, capability_value in document.get("capabilities", {}).items():
        if capability_is_requested(capability_value):
            required_capabilities.add((capability_name, _pointer("capabilities", capability_name)))
    if "auto_flow" in document:
        required_capabilities.add(("auto_flow", "/auto_flow"))
    for capability_name, capability_path in sorted(required_capabilities):
        if capability_name not in profile.capabilities:
            diagnostics.append(
                _diagnostic(
                    "FLOWSPEC_PROFILE_CAPABILITY_UNAVAILABLE",
                    capability_path,
                    f"profile {profile.identifier!r} does not provide capability {capability_name!r}",
                )
            )

    return (
        diagnostics,
        None if has_open_manifest else frozenset(exposed_slots),
        exposed_slot_positions,
    )


def semantic_diagnostics(
    flow_document: dict[str, Any],
    *,
    external_slots: frozenset[str] | None = None,
    external_steps: frozenset[str] = frozenset(),
    profile: FlowProfile | None = None,
) -> tuple[FlowDiagnostic, ...]:
    """Link all source contracts and return every deterministic finding.

    ``external_slots=None`` defers profile-owned references to compilation.  A
    profile-aware caller passes the complete exposed-slot set to make unknown
    references an aggregate semantic error instead.
    """

    document = normalize_flow(flow_document)
    profile_diagnostics: list[FlowDiagnostic] = []
    profile_slot_positions: dict[str, int] = {}
    if profile is not None:
        profile_diagnostics, profile_slots, profile_slot_positions = _profile_contracts(
            document, profile
        )
        if profile_slots is not None:
            external_slots = profile_slots
    diagnostics, identifiers, slot_positions = _path_contracts(document)
    slot_positions = {**profile_slot_positions, **slot_positions}
    diagnostics.extend(profile_diagnostics)
    diagnostics.extend(_domain_contracts(document))
    diagnostics.extend(_slot_contracts(document, slot_positions, external_slots=external_slots))
    diagnostics.extend(
        _derive_contracts(
            document,
            identifiers,
            external_slots=external_slots,
            external_steps=external_steps,
        )
    )
    diagnostics.extend(_entry_schema_contracts(document, external_slots=external_slots))
    diagnostics.extend(_state_writer_contracts(document, external_slots=external_slots))
    diagnostics.extend(
        _predicate_contracts(
            document,
            external_slots=external_slots,
            profile=profile,
        )
    )
    diagnostics.extend(
        _rail_reference_contracts(
            document,
            identifiers,
            slot_positions,
            external_slots=external_slots,
        )
    )
    return tuple(
        sorted(
            set(diagnostics),
            key=lambda diagnostic: (
                diagnostic.path,
                diagnostic.code,
                diagnostic.message,
            ),
        )
    )
