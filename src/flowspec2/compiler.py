"""Compile a flowspec/2 document into an executable LangGraph ``StateGraph``.

The compiler:

1. builds a :class:`~flowspec2.nodes.FlowContext` (domains, slots, config, tools),
   discovers subflow-exposed slots, validates every state reference, and builds
   the dependency indexes (transitive ``requires`` → ``dependents``,
   ``derive.from`` → ``derive_readers``);
2. walks ``path`` into a flat ordered list of nodes, expanding each ``use`` into
   its subflow's spliced nodes and inserting ``derive`` nodes at their ``after``
   anchor;
3. registers every node, then synthesizes the routers — a node returns ``END``
   (pause), a literal target (correction back-edge / subflow branch), or the
   ``NEXT`` sentinel which the compiler resolves to the following node;
4. returns a :class:`CompiledFlow` the runtime invokes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Final, Optional, cast

from langgraph.graph import END as LANGGRAPH_END
from langgraph.graph import StateGraph

from .clock import UtcClock, system_utc_now
from .compiler_contracts import (
    bind_await_external,
    validate_await_external_definition,
    validate_await_external_targets,
    validate_entry_args_schema,
    validate_flow_tool_contracts,
)
from .domains import interactive_options_for_domain
from .interactive import (
    BUTTON_ID_MAX,
    BUTTON_TITLE_MAX,
    LIST_ROWS_TOTAL_MAX,
    MAX_BUTTONS,
    ROW_DESC_MAX,
    ROW_ID_MAX,
    ROW_TITLE_MAX,
    interactive_option_identifier,
)
from .models import ServiceState
from .nodes import (
    NEXT,
    FlowContext,
    NodeDesc,
    make_await_external_node,
    make_bool_confirm_node,
    make_collect_node,
    make_derive_node,
    make_hub_confirm_node,
    make_init_node,
    make_summary_confirm_node,
    make_terminal_node,
)
from .observability import SnowflakeIdGenerator, default_log_id_generator
from .subflows import SubflowDefinition, SubflowRegistry, default_subflows
from .tools import ToolRegistry, default_tool_registry

END: Final[str] = LANGGRAPH_END
_CONTRACT_INVALID: Final[int] = -1
_CONTRACT_UNKNOWN: Final[int] = 0
_CONTRACT_VALID: Final[int] = 1
_RESUME_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\$token(?:\.[A-Za-z][A-Za-z0-9_]*)+$"
)


@dataclass
class CompiledFlow:
    graph: Any  # compiled langgraph
    ctx: FlowContext
    _doc: dict[str, Any]
    terminal_id: Optional[str]
    entry_node_id: str

    @property
    def doc(self) -> dict[str, Any]:
        """Return an owned copy of the immutable compiler source snapshot."""

        return copy.deepcopy(self._doc)


def _transitive_dependents(slots: dict[str, Any]) -> dict[str, set[str]]:
    """slot -> every slot that (transitively) lists it in ``requires``."""
    direct: dict[str, set[str]] = {}
    for slot, cfg in slots.items():
        for req in cfg.get("requires", []) or []:
            direct.setdefault(req, set()).add(slot)
    dependents: dict[str, set[str]] = {}
    # Iterate the union of declared slots and every slot named as a `requires`
    # target: subflow-contributed slots (e.g. `address`) are required by top-level
    # slots but are not keys of the document's `slots` block.
    for slot in set(slots) | set(direct):
        seen: set[str] = set()
        stack = list(direct.get(slot, set()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(direct.get(cur, set()))
        dependents[slot] = seen
    return dependents


def _derive_readers(derives: list[dict[str, Any]]) -> dict[str, list[str]]:
    direct_readers: dict[str, list[str]] = {}
    for derive_definition in derives:
        for source_slot in derive_definition.get("from", []):
            direct_readers.setdefault(source_slot, []).append(derive_definition["writes"])

    transitive_readers: dict[str, list[str]] = {}
    for source_slot in direct_readers:
        pending_targets = list(direct_readers[source_slot])
        seen_targets: set[str] = set()
        ordered_targets: list[str] = []
        while pending_targets:
            derived_target = pending_targets.pop(0)
            if derived_target in seen_targets:
                continue
            seen_targets.add(derived_target)
            ordered_targets.append(derived_target)
            pending_targets.extend(direct_readers.get(derived_target, ()))
        transitive_readers[source_slot] = ordered_targets
    return transitive_readers


def _derive_node_identifiers(doc: dict[str, Any]) -> dict[str, str]:
    path_identifiers = {
        cast(str, path_step["derive"]): cast(str, path_step["id"])
        for path_step in doc["path"]
        if "derive" in path_step
    }
    return {
        cast(str, derive_definition["writes"]): path_identifiers.get(
            cast(str, derive_definition["writes"]),
            f"derive_{derive_definition['writes']}",
        )
        for derive_definition in doc.get("derive", [])
    }


def _validate_derive_execution_order(
    doc: dict[str, Any],
    ctx: FlowContext,
    sequence: list[NodeDesc],
) -> None:
    node_positions = {descriptor.id: node_index for node_index, descriptor in enumerate(sequence)}
    derive_node_identifiers = _derive_node_identifiers(doc)
    for derive_index, derive_definition in enumerate(doc.get("derive", [])):
        target = cast(str, derive_definition["writes"])
        target_node = derive_node_identifiers[target]
        target_position = node_positions[target_node]
        for source_index, source_slot in enumerate(derive_definition.get("from", [])):
            producer_node = derive_node_identifiers.get(source_slot) or ctx.node_for_slot.get(
                source_slot
            )
            if producer_node is None:
                continue
            producer_position = node_positions.get(producer_node)
            if producer_position is None or producer_position >= target_position:
                raise ValueError(
                    f"$.derive[{derive_index}].from[{source_index}] is produced by "
                    f"{producer_node!r} after derive node {target_node!r} would execute"
                )


def _validate_terminal_derive_execution_order(
    doc: dict[str, Any],
    sequence: list[NodeDesc],
) -> None:
    terminal_definition = cast(dict[str, Any] | None, doc.get("terminal"))
    if terminal_definition is None:
        return
    node_positions = {descriptor.id: node_index for node_index, descriptor in enumerate(sequence)}
    terminal_position = node_positions.get(cast(str, terminal_definition["step"]))
    if terminal_position is None:
        return
    derive_nodes = _derive_node_identifiers(doc)
    for input_index, input_binding in enumerate(terminal_definition.get("input", [])):
        source_name = cast(str, input_binding["slot"])
        derive_node = derive_nodes.get(source_name)
        if derive_node is None:
            continue
        derive_position = node_positions.get(derive_node)
        if derive_position is None or derive_position >= terminal_position:
            raise ValueError(
                f"$.terminal.input[{input_index}] reads derived value {source_name!r} before "
                f"its producer node {derive_node!r} executes"
            )


def _gate_for(step: dict[str, Any], gates: dict[str, Any]) -> Optional[dict[str, Any]]:
    if "ask_when" in step:
        return cast(dict[str, Any], step["ask_when"])
    return cast(Optional[dict[str, Any]], gates.get(step["id"]))


def _compiled_path_step_identifier(
    path_step: dict[str, Any],
    terminal_identifier: str,
) -> str | None:
    if "terminal" in path_step:
        return terminal_identifier
    if any(step_kind in path_step for step_kind in ("slot", "confirm", "derive", "await_external")):
        return cast(str, path_step["id"])
    return None


def _validate_auto_flow_target(
    doc: dict[str, Any],
    *,
    field_path: str,
    target: str,
    submission_slots: frozenset[str],
) -> None:
    matching_indexes = [
        path_index
        for path_index, path_step in enumerate(doc["path"])
        if path_step.get("id") == target
    ]
    if len(matching_indexes) != 1:
        raise ValueError(f"{field_path} must reference exactly one native path step: {target!r}")
    prefix = doc["path"][: matching_indexes[0]]
    native_slot_definitions = cast(dict[str, dict[str, Any]], doc.get("slots", {}))
    override_gates = cast(
        dict[str, Any],
        cast(dict[str, Any], doc.get("overrides", {})).get("gates", {}),
    )
    is_submission_target = field_path == "auto_flow.resume_at"
    for path_index, path_step in enumerate(prefix):
        if any(
            step_kind in path_step for step_kind in ("use", "derive", "await_external", "terminal")
        ):
            raise ValueError(
                f"{field_path} cannot bypass a subflow, derivation, external wait, "
                f"or terminal step at $.path[{path_index}]"
            )
        if "confirm" in path_step and path_step.get("correctable") is True:
            raise ValueError(
                f"{field_path} cannot bypass a correction-hub confirmation at $.path[{path_index}]"
            )
        conditional_contract = (
            path_step.get("ask_when")
            or path_step.get("skip_when")
            or override_gates.get(path_step.get("id"))
        )
        if (slot_name := path_step.get("slot")) is not None:
            slot_definition = native_slot_definitions[slot_name]
            if not slot_definition.get("required"):
                continue
            if is_submission_target and (
                slot_name in submission_slots or conditional_contract is not None
            ):
                continue
            raise ValueError(f"{field_path} cannot bypass unsatisfied required slot {slot_name!r}")
        if (confirmation_slot := path_step.get("confirm")) is None:
            continue
        if is_submission_target and (
            confirmation_slot in submission_slots or conditional_contract is not None
        ):
            continue
        raise ValueError(
            f"{field_path} cannot bypass confirmation {confirmation_slot!r} while it is unanswered"
        )


def _validate_auto_flow_resume(doc: dict[str, Any]) -> None:
    auto_flow = cast(dict[str, Any], doc.get("auto_flow") or {})
    native_slot_definitions = cast(dict[str, dict[str, Any]], doc.get("slots", {}))
    native_slots = set(native_slot_definitions)
    prefill_slot_list = cast(list[str], auto_flow.get("prefill_from", []))
    prefill_slots = frozenset(prefill_slot_list)
    for prefill_index, prefill_slot in enumerate(prefill_slot_list):
        if prefill_slot not in native_slots:
            raise ValueError(
                f"auto_flow.prefill_from[{prefill_index}] must reference a top-level slot"
            )
        if native_slot_definitions[prefill_slot].get("persist", "data") == "internal":
            raise ValueError(
                f"auto_flow.prefill_from[{prefill_index}] cannot expose internal slot "
                f"{prefill_slot!r}"
            )
    alias_destinations: set[str] = set()
    for flow_field, mapping in auto_flow.get("alias_map", {}).items():
        candidate_mappings = (
            [mapping]
            if all(not isinstance(value, dict) for value in mapping.values())
            else [value for value in mapping.values() if isinstance(value, dict)]
        )
        for candidate_mapping in candidate_mappings:
            for destination_slot in candidate_mapping:
                if destination_slot not in native_slots:
                    raise ValueError(
                        "auto_flow alias destinations must be top-level slots: "
                        f"{flow_field!r} -> {destination_slot!r}"
                    )
                alias_destinations.add(destination_slot)
    resume_target = auto_flow.get("resume_at")
    if resume_target is not None:
        _validate_auto_flow_target(
            doc,
            field_path="auto_flow.resume_at",
            target=cast(str, resume_target),
            submission_slots=prefill_slots | alias_destinations,
        )
    recovery = cast(dict[str, Any], auto_flow.get("recovery") or {})
    if (fallback_target := recovery.get("fallback_at")) is not None:
        _validate_auto_flow_target(
            doc,
            field_path="auto_flow.recovery.fallback_at",
            target=cast(str, fallback_target),
            submission_slots=frozenset(),
        )


def _validate_gate_bindings(doc: dict[str, Any], gates: dict[str, Any]) -> None:
    gateable_steps = {
        step["id"]: step for step in doc["path"] if "slot" in step or "confirm" in step
    }
    for gate_node_id in gates:
        if gate_node_id not in gateable_steps:
            raise ValueError(
                f"$.overrides.gates[{gate_node_id!r}] does not resolve to a collect or confirm step"
            )
        path_step = gateable_steps[gate_node_id]
        if "ask_when" in path_step:
            raise ValueError(
                f"$.overrides.gates[{gate_node_id!r}] duplicates the inline ask_when gate"
            )
        if "confirm" in path_step and (
            path_step.get("correctable") is True or "on_reject" in path_step
        ):
            raise ValueError(
                f"$.overrides.gates[{gate_node_id!r}] targets a confirmation variant "
                "that does not support gating"
            )


def _validate_confirmation_domains(doc: dict[str, Any], ctx: FlowContext) -> None:
    for path_index, path_step in enumerate(doc["path"]):
        confirmation_slot = path_step.get("confirm")
        if confirmation_slot is None or confirmation_slot not in ctx.slots:
            continue
        domain_name = ctx.slots[confirmation_slot]["domain"]
        domain = ctx.domains.get(domain_name)
        if domain is not None and domain.get("type", "categorical") != "bool":
            raise ValueError(
                f"$.path[{path_index}].confirm slot {confirmation_slot!r} must use a bool domain"
            )


def _validate_native_path_slot_references(doc: dict[str, Any], ctx: FlowContext) -> None:
    declared_slot_names = set(ctx.slots)
    for step_index, step in enumerate(doc["path"]):
        reference_kind = "slot" if "slot" in step else "confirm" if "confirm" in step else None
        if reference_kind is None:
            continue
        slot_name = cast(str, step[reference_kind])
        if slot_name not in declared_slot_names:
            raise ValueError(
                f"$.path[{step_index}].{reference_kind} does not resolve to a declared slot: "
                f"{slot_name!r}"
            )


def _validate_subflow_bindings(
    doc: dict[str, Any],
    subflows: SubflowRegistry,
) -> dict[str, dict[str, Any]]:
    path_locations: dict[str, str] = {}
    for path_index, path_step in enumerate(doc["path"]):
        subflow_reference = path_step.get("use")
        if subflow_reference is None:
            continue
        path_location = f"$.path[{path_index}].use"
        if subflow_reference in path_locations:
            raise ValueError(
                f"{path_location} repeats subflow anchor {subflow_reference!r}; "
                f"first anchored at {path_locations[subflow_reference]}"
            )
        path_locations[subflow_reference] = path_location

    declarations: dict[str, tuple[int, dict[str, Any]]] = {}
    for declaration_index, declaration in enumerate(doc.get("uses", []) or []):
        subflow_reference = cast(str, declaration["ref"])
        declaration_location = f"$.uses[{declaration_index}].ref"
        if subflow_reference in declarations:
            first_declaration_index = declarations[subflow_reference][0]
            raise ValueError(
                f"{declaration_location} duplicates subflow declaration {subflow_reference!r}; "
                f"first declared at $.uses[{first_declaration_index}].ref"
            )
        if not subflows.has(subflow_reference):
            raise ValueError(
                f"{declaration_location} subflow is not registered: {subflow_reference!r}"
            )
        exposed_slots = subflows.definition(subflow_reference).exposed_slots
        if exposed_slots is not None and (collisions := set(doc.get("slots", {})) & exposed_slots):
            raise ValueError(
                f"{declaration_location} subflow-owned slots collide with top-level "
                f"declarations: {', '.join(sorted(collisions))}"
            )
        declarations[subflow_reference] = (declaration_index, declaration)

    for subflow_reference, path_location in path_locations.items():
        if not subflows.has(subflow_reference):
            raise ValueError(f"{path_location} subflow is not registered: {subflow_reference!r}")
        if subflow_reference not in declarations:
            raise ValueError(
                f"{path_location} has no matching declaration in $.uses: {subflow_reference!r}"
            )

    configurations: dict[str, dict[str, Any]] = {}
    for subflow_reference, (declaration_index, declaration) in declarations.items():
        if subflow_reference not in path_locations:
            raise ValueError(
                f"$.uses[{declaration_index}].ref is an orphan declaration without a path "
                f"anchor: {subflow_reference!r}"
            )
        configuration = cast(dict[str, Any], declaration.get("with") or {})
        subflows.validate_configuration(
            subflow_reference,
            configuration,
            location=f"$.uses[{declaration_index}].with",
        )
        configurations[subflow_reference] = configuration
    return configurations


def _register_subflow_exposures(
    ctx: FlowContext,
    subflow_reference: str,
    definition: SubflowDefinition,
    entry_id: str,
    first_descriptor_id: str,
    exposed_slots: dict[str, str],
    subflow_node_ids: set[str],
) -> None:
    if entry_id not in subflow_node_ids:
        raise ValueError(f"subflow {subflow_reference!r} declares unknown entry node {entry_id!r}")
    if entry_id != first_descriptor_id:
        raise ValueError(
            f"subflow {subflow_reference!r} entry node {entry_id!r} must be its first "
            f"descriptor {first_descriptor_id!r}"
        )
    if definition.exposed_slots is not None and set(exposed_slots) != definition.exposed_slots:
        declared_slots = ", ".join(sorted(definition.exposed_slots)) or "<none>"
        built_slots = ", ".join(sorted(exposed_slots)) or "<none>"
        raise ValueError(
            f"subflow {subflow_reference!r} build exposes slots [{built_slots}], but its "
            f"manifest declares [{declared_slots}]"
        )
    for slot_name, node_id in exposed_slots.items():
        if node_id not in subflow_node_ids:
            raise ValueError(
                f"subflow {subflow_reference!r} exposes slot {slot_name!r} through unknown "
                f"node {node_id!r}"
            )
        existing_node_id = ctx.node_for_slot.get(slot_name)
        if existing_node_id is not None and existing_node_id != node_id:
            raise ValueError(
                f"subflow {subflow_reference!r} exposes slot {slot_name!r} through {node_id!r}, "
                f"but it is already bound to {existing_node_id!r}"
            )
        ctx.node_for_slot[slot_name] = node_id


def _validate_compiled_slot_references(doc: dict[str, Any], ctx: FlowContext) -> None:
    collectable_slot_names = set(ctx.node_for_slot)
    derived_slot_names = {
        derive_definition["writes"] for derive_definition in doc.get("derive", [])
    }
    readable_slot_names = set(ctx.slots) | collectable_slot_names | derived_slot_names

    for slot_name, slot_declaration in ctx.slots.items():
        for requirement_index, required_slot_name in enumerate(
            slot_declaration.get("requires", []) or []
        ):
            if required_slot_name not in collectable_slot_names:
                raise ValueError(
                    f"$.slots.{slot_name}.requires[{requirement_index}] does not resolve to a "
                    f"collectable slot: {required_slot_name!r}"
                )

    for derive_index, derive_definition in enumerate(doc.get("derive", [])):
        for source_index, source_slot_name in enumerate(derive_definition.get("from", [])):
            if source_slot_name not in readable_slot_names:
                raise ValueError(
                    f"$.derive[{derive_index}].from[{source_index}] does not resolve to a slot: "
                    f"{source_slot_name!r}"
                )

    confirm_definition = doc.get("confirm")
    if confirm_definition:
        confirmation_slot = confirm_definition["slot"]
        if confirmation_slot not in ctx.slots:
            raise ValueError(
                f"$.confirm.slot does not resolve to a declared slot: {confirmation_slot!r}"
            )
        for correctable_index, correctable_slot_name in enumerate(
            confirm_definition["correctable"]
        ):
            if correctable_slot_name not in collectable_slot_names:
                raise ValueError(
                    f"$.confirm.correctable[{correctable_index}] does not resolve to a "
                    f"collectable slot: {correctable_slot_name!r}"
                )

    terminal_definition = doc.get("terminal")
    if terminal_definition:
        for input_index, input_binding in enumerate(terminal_definition.get("input", [])):
            input_slot_name = input_binding["slot"]
            if input_slot_name not in readable_slot_names:
                raise ValueError(
                    f"$.terminal.input[{input_index}].slot does not resolve to a slot or "
                    f"derived value: {input_slot_name!r}"
                )


def _register_interactive_binding(
    field_bindings: dict[str, tuple[str, str]],
    field_name: str,
    slot_name: str,
    source_location: str,
) -> None:
    existing_binding = field_bindings.get(field_name)
    if existing_binding is not None and existing_binding[0] != slot_name:
        raise ValueError(
            f"{source_location}.field binds payload field {field_name!r} to slot {slot_name!r}, "
            f"but {existing_binding[1]}.field already binds it to {existing_binding[0]!r}"
        )
    field_bindings[field_name] = (slot_name, source_location)


def _validate_interactive_domain_binding(
    ctx: FlowContext,
    interactive: dict[str, Any],
    slot_name: str,
    source_location: str,
) -> None:
    if slot_name not in ctx.slots:
        raise ValueError(f"{source_location} targets an undeclared slot: {slot_name!r}")
    interactive_domain_name = interactive.get("from_domain")
    if interactive_domain_name is None:
        if interactive.get("options_when"):
            raise ValueError(f"{source_location}.options_when requires from_domain")
        return
    if interactive_domain_name not in ctx.domains:
        raise ValueError(
            f"{source_location}.from_domain does not resolve to a domain: "
            f"{interactive_domain_name!r}"
        )
    slot_domain_name = ctx.slots[slot_name]["domain"]
    if interactive_domain_name != slot_domain_name:
        raise ValueError(
            f"{source_location}.from_domain must match $.slots.{slot_name}.domain: "
            f"{interactive_domain_name!r} != {slot_domain_name!r}"
        )

    domain_specification = ctx.domains[interactive_domain_name]
    domain_kind = domain_specification.get("type", "categorical")
    if domain_kind not in {"categorical", "bool"}:
        raise ValueError(
            f"{source_location}.from_domain must reference a categorical or bool domain"
        )
    domain_values: set[Any] = {
        domain_option.value
        for domain_option in interactive_options_for_domain(domain_specification)
    }
    if None in domain_specification.get("values", []):
        domain_values.add(None)
    renderable_options = interactive_options_for_domain(domain_specification)
    if not renderable_options:
        raise ValueError(f"{source_location}.from_domain has no renderable options")
    interactive_kind = interactive["kind"]
    option_identifiers = [
        interactive_option_identifier(domain_option.value) for domain_option in renderable_options
    ]
    if len(option_identifiers) != len(set(option_identifiers)):
        raise ValueError(f"{source_location} contains duplicate rendered option identifiers")
    identifier_maximum = BUTTON_ID_MAX if interactive_kind == "buttons" else ROW_ID_MAX
    if any(
        not option_identifier.strip() or len(option_identifier) > identifier_maximum
        for option_identifier in option_identifiers
    ):
        raise ValueError(f"{source_location} contains an invalid rendered option identifier")
    if interactive_kind == "buttons":
        if len(renderable_options) > MAX_BUTTONS:
            raise ValueError(f"{source_location} exceeds the buttons option limit")
        if any(len(domain_option.title) > BUTTON_TITLE_MAX for domain_option in renderable_options):
            raise ValueError(f"{source_location} contains a button title that is too long")
    else:
        if len(renderable_options) > LIST_ROWS_TOTAL_MAX:
            raise ValueError(f"{source_location} exceeds the list option limit")
        if any(len(domain_option.title) > ROW_TITLE_MAX for domain_option in renderable_options):
            raise ValueError(f"{source_location} contains a list row title that is too long")
        if any(
            len(domain_option.description) > ROW_DESC_MAX for domain_option in renderable_options
        ):
            raise ValueError(f"{source_location} contains a list row description that is too long")
    configured_values: set[Any] = set()
    for option_index, conditional_option in enumerate(interactive.get("options_when", [])):
        option_value = conditional_option["value"]
        if option_value not in domain_values:
            raise ValueError(
                f"{source_location}.options_when[{option_index}].value is not in domain "
                f"{interactive_domain_name!r}: {option_value!r}"
            )
        if option_value in configured_values:
            raise ValueError(
                f"{source_location}.options_when repeats domain value {option_value!r}"
            )
        configured_values.add(option_value)


def _validate_interactive_bindings(doc: dict[str, Any], ctx: FlowContext) -> None:
    field_bindings: dict[str, tuple[str, str]] = {}

    for step_index, step in enumerate(doc["path"]):
        interactive = step.get("interactive")
        if not interactive:
            continue
        slot_name = step.get("slot", step.get("confirm"))
        if slot_name is not None:
            source_location = f"$.path[{step_index}].interactive"
            _validate_interactive_domain_binding(ctx, interactive, slot_name, source_location)
            _register_interactive_binding(
                field_bindings,
                interactive["field"],
                slot_name,
                source_location,
            )

    confirm_definition = doc.get("confirm")
    if confirm_definition and confirm_definition.get("interactive"):
        _validate_interactive_domain_binding(
            ctx,
            confirm_definition["interactive"],
            confirm_definition["slot"],
            "$.confirm.interactive",
        )
        _register_interactive_binding(
            field_bindings,
            confirm_definition["interactive"]["field"],
            confirm_definition["slot"],
            "$.confirm.interactive",
        )


def _validate_confirmation_hub_binding(doc: dict[str, Any]) -> None:
    correctable_steps = [step for step in doc["path"] if step.get("correctable") is True]
    confirm_definition = doc.get("confirm")
    if confirm_definition is None and correctable_steps:
        raise ValueError("a correctable path confirmation requires the top-level confirm block")
    if len(correctable_steps) > 1:
        raise ValueError("the top-level confirm block cannot bind multiple correctable path steps")
    if confirm_definition is None or confirm_definition.get("on_confirm") is None:
        return
    terminal_identifier = cast(
        str,
        cast(dict[str, Any], doc.get("terminal", {})).get("step", "terminal"),
    )
    path_positions = {
        step_identifier: path_index
        for path_index, path_step in enumerate(doc["path"])
        if (
            step_identifier := _compiled_path_step_identifier(
                path_step,
                terminal_identifier,
            )
        )
        is not None
    }
    confirmation_step = cast(str, confirm_definition["step"])
    confirmation_target = cast(str, confirm_definition["on_confirm"])
    if (
        confirmation_step in path_positions
        and confirmation_target in path_positions
        and path_positions[confirmation_target] <= path_positions[confirmation_step]
    ):
        raise ValueError("$.confirm.on_confirm must target a later path step")


def _effective_hub_confirm_definition(
    step: dict[str, Any],
    confirm_definition: dict[str, Any],
) -> dict[str, Any]:
    if step["id"] != confirm_definition["step"]:
        raise ValueError(
            "correctable path confirmation step must match $.confirm.step: "
            f"{step['id']!r} != {confirm_definition['step']!r}"
        )
    if step["confirm"] != confirm_definition["slot"]:
        raise ValueError(
            "correctable path confirmation slot must match $.confirm.slot: "
            f"{step['confirm']!r} != {confirm_definition['slot']!r}"
        )

    effective_definition = copy.deepcopy(confirm_definition)
    for presentation_key in ("prompt", "interactive"):
        path_presentation = step.get(presentation_key)
        confirm_presentation = confirm_definition.get(presentation_key)
        if (
            path_presentation is not None
            and confirm_presentation is not None
            and path_presentation != confirm_presentation
        ):
            raise ValueError(
                f"correctable path {presentation_key} conflicts with $.confirm.{presentation_key}"
            )
        if path_presentation is not None:
            effective_definition[presentation_key] = copy.deepcopy(path_presentation)
    return effective_definition


def compile_flow(
    doc: dict[str, Any],
    *,
    tools: Optional[ToolRegistry] = None,
    subflows: Optional[SubflowRegistry] = None,
    log_id_generator: Optional[SnowflakeIdGenerator] = None,
    clock: Optional[UtcClock] = None,
    validate_semantics: bool = True,
) -> CompiledFlow:
    tools = tools or default_tool_registry()
    subflows = subflows or default_subflows()
    log_id_generator = log_id_generator or default_log_id_generator()
    clock = clock or system_utc_now

    # Work on a private copy: the compiler annotates path steps with synthesized
    # node ids, and must never mutate the caller's document (which is re-validated
    # against an additionalProperties:false schema).
    doc = copy.deepcopy(doc)

    if validate_semantics:
        from .profiles import reference_profile
        from .semantics import FlowLinkError, semantic_diagnostics

        linking_diagnostics = semantic_diagnostics(
            doc,
            profile=reference_profile(tools=tools, subflows=subflows),
        )
        if linking_diagnostics:
            raise FlowLinkError(linking_diagnostics)

    subflow_configurations = _validate_subflow_bindings(doc, subflows)
    validate_flow_tool_contracts(doc, tools, subflows)
    await_external = bind_await_external(doc)

    slots = dict(doc.get("slots", {}))
    ctx = FlowContext(
        domains=dict(doc.get("domains", {})),
        slots=slots,
        config=dict(doc.get("config", {})),
        tools=tools,
        log_id_generator=log_id_generator,
        clock=clock,
        flow_name=cast(str, doc["flow"]),
        flow_revision=(
            f"{doc['version']}:"
            + hashlib.sha256(
                json.dumps(
                    doc,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        ),
        await_external=await_external,
    )
    await_external_path_step = next(
        (step for step in doc["path"] if step.get("await_external") is True),
        None,
    )
    validate_await_external_definition(
        doc,
        await_external,
        tools,
        subflows,
        await_external_path_step,
    )

    terminal = doc.get("terminal")
    terminal_id = terminal["step"] if terminal else None
    gates = (doc.get("overrides", {}) or {}).get("gates", {}) or {}
    confirm_block = doc.get("confirm")

    seq: list[NodeDesc] = []

    # init node (service seed + best-effort entry tool) is always the entry point
    auto_flow = cast(dict[str, Any], doc.get("auto_flow") or {})
    auto_flow_resume_targets = frozenset(
        target
        for target in (
            auto_flow.get("resume_at"),
            cast(dict[str, Any], auto_flow.get("recovery") or {}).get("fallback_at"),
        )
        if isinstance(target, str)
    )
    seq.append(
        make_init_node(
            ctx,
            doc.get("entry"),
            dict(doc.get("service", {})),
            auto_flow_resume_targets=auto_flow_resume_targets,
        )
    )

    # Pre-pass: assign step ids and register node_for_slot so the correction hub
    # (built later in path order) can resolve every correctable target.
    for step in doc["path"]:
        if "slot" in step:
            step["id"] = step.get("step", step["slot"])
            ctx.node_for_slot.setdefault(step["slot"], step["id"])
        elif "confirm" in step:
            step["id"] = step.get("step", step["confirm"])
            ctx.node_for_slot.setdefault(step["confirm"], step["id"])
        elif "terminal" in step:
            step["id"] = terminal_id or "terminal"
        elif "derive" in step:
            step["id"] = step.get("step", f"derive_{step['derive']}")
        elif "await_external" in step:
            step["id"] = step["step"]
        # `use` ids come from the subflow

    _validate_auto_flow_resume(doc)
    _validate_gate_bindings(doc, gates)
    _validate_native_path_slot_references(doc, ctx)
    _validate_confirmation_domains(doc, ctx)
    _validate_confirmation_hub_binding(doc)
    _validate_interactive_bindings(doc, ctx)

    # Main pass: build nodes in path order.
    for step in doc["path"]:
        if "use" in step:
            ref = step["use"]
            with_cfg = subflow_configurations[ref]
            build = subflows.get(ref).build(ctx, with_cfg)
            if not build.descriptors:
                raise ValueError(f"subflow {ref!r} build must contain at least one descriptor")
            _register_subflow_exposures(
                ctx,
                ref,
                subflows.definition(ref),
                build.entry_id,
                build.descriptors[0].id,
                build.node_for_slot,
                {descriptor.id for descriptor in build.descriptors},
            )
            seq.extend(build.descriptors)
            continue
        if "slot" in step:
            seq.append(make_collect_node(ctx, step, _gate_for(step, gates)))
        elif "confirm" in step:
            if step.get("correctable") and confirm_block:
                effective_confirm = _effective_hub_confirm_definition(step, confirm_block)
                seq.append(make_hub_confirm_node(ctx, effective_confirm))
            elif step.get("on_reject"):
                seq.append(make_summary_confirm_node(ctx, step))
            else:
                seq.append(make_bool_confirm_node(ctx, step, _gate_for(step, gates)))
        elif "derive" in step:
            derive_def = next(d for d in doc.get("derive", []) if d["writes"] == step["derive"])
            seq.append(make_derive_node(ctx, derive_def, node_id=step["id"]))
        elif "await_external" in step:
            if await_external is None:  # defended by bind_await_external
                raise ValueError("path await_external requires capabilities.await_external")
            seq.append(make_await_external_node(ctx, await_external, step))
        elif "terminal" in step:
            if terminal:
                seq.append(make_terminal_node(ctx, terminal))

    _validate_compiled_slot_references(doc, ctx)
    validate_entry_args_schema(doc, ctx, subflows)

    # Compute dependency indexes now that subflow slots are registered.
    ctx.dependents = _transitive_dependents(ctx.slots)
    ctx.derive_readers = _derive_readers(doc.get("derive", []))

    # Insert derive[] nodes at their `after` anchor (those not already placed as path steps).
    placed = {s["derive"] for s in doc["path"] if "derive" in s}
    last_inserted_for_anchor: dict[str, str] = {}
    for derive_index, derive_def in enumerate(doc.get("derive", [])):
        if derive_def["writes"] in placed:
            continue
        after = derive_def.get("after")
        node = make_derive_node(ctx, derive_def)
        if after is None:
            raise ValueError(
                f"$.derive[{derive_index}] must be placed in path or declare an after anchor"
            )
        effective_anchor = last_inserted_for_anchor.get(after, after)
        idx = next((i for i, d in enumerate(seq) if d.id == effective_anchor), None)
        if idx is None:
            raise ValueError(
                f"$.derive[{derive_index}].after does not resolve to a compiled node: {after!r}"
            )
        seq.insert(idx + 1, node)
        last_inserted_for_anchor[after] = node.id

    _validate_derive_execution_order(doc, ctx, seq)
    _validate_terminal_derive_execution_order(doc, seq)
    node_ids = [descriptor.id for descriptor in seq]
    if len(node_ids) != len(set(node_ids)):
        duplicate_node_ids = sorted(
            node_id for node_id in set(node_ids) if node_ids.count(node_id) > 1
        )
        raise ValueError(
            f"compiled flow contains duplicate node ids: {', '.join(duplicate_node_ids)}"
        )
    validate_await_external_targets(
        await_external,
        set(node_ids),
        ctx.await_external_nodes,
    )

    # ── wire the StateGraph ──────────────────────────────────────────────
    graph = StateGraph(ServiceState)
    for desc in seq:
        graph.add_node(desc.id, cast(Any, desc.fn))
    graph.set_entry_point(seq[0].id)

    for i, desc in enumerate(seq):
        next_id = seq[i + 1].id if i + 1 < len(seq) else END
        path_map = list(dict.fromkeys([*desc.targets, next_id, END]))

        def make_router(
            router: Callable[[ServiceState], str],
            resolved_next: str,
        ) -> Callable[[ServiceState], str]:
            def routed(state: ServiceState) -> str:
                target = router(state)
                return resolved_next if target == NEXT else target

            return routed

        graph.add_conditional_edges(desc.id, make_router(desc.router, next_id), path_map)

    compiled = graph.compile()
    return CompiledFlow(
        graph=compiled,
        ctx=ctx,
        _doc=copy.deepcopy(doc),
        terminal_id=terminal_id,
        entry_node_id=seq[0].id,
    )
