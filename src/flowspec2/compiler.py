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
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import product
from typing import Any, Callable, Final, Optional, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from langgraph.graph import END as LANGGRAPH_END
from langgraph.graph import StateGraph

from .clock import UtcClock, system_utc_now
from .domains import interactive_options_for_domain, make_slot_model
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
from .schema_contracts import (
    schema_reference_target as _schema_reference_target,
)
from .schema_contracts import (
    validate_resume_token_schema as _validate_resume_token_schema,
)
from .schema_contracts import (
    validate_safe_local_schema_references as _validate_safe_local_schema_references,
)
from .subflows import SubflowDefinition, SubflowRegistry, default_subflows
from .tools import ToolDefinition, ToolRegistry, default_tool_registry

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


def _validate_entry_args_schema(
    doc: dict[str, Any],
    ctx: FlowContext,
    subflows: SubflowRegistry,
) -> None:
    entry_args_schema = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], doc.get("route", {})).get("entry_args_schema"),
    )
    if entry_args_schema is None:
        return
    try:
        Draft202012Validator.check_schema(entry_args_schema)
    except SchemaError as schema_error:
        raise ValueError(
            f"$.route.entry_args_schema is not a valid Draft 2020-12 schema: {schema_error.message}"
        ) from schema_error
    _validate_safe_local_schema_references(
        entry_args_schema,
        location="$.route.entry_args_schema",
    )
    if entry_args_schema.get("type") != "object":
        raise ValueError("$.route.entry_args_schema.type must be 'object'")
    if entry_args_schema.get("additionalProperties") is not False:
        raise ValueError("$.route.entry_args_schema.additionalProperties must be false")
    exposed_contracts = _exposed_slot_value_contracts(
        doc,
        subflows,
        include_absence=False,
    )
    for property_name, property_schema in cast(
        dict[str, Any], entry_args_schema.get("properties", {})
    ).items():
        target_contract = _declared_slot_value_contract(
            doc,
            property_name,
            include_absence=False,
        ) or exposed_contracts.get(property_name)
        if target_contract is None or property_name not in set(ctx.node_for_slot) | set(ctx.slots):
            raise ValueError(
                "$.route.entry_args_schema.properties"
                f"[{property_name!r}] does not resolve to a declared or exposed slot"
            )
        if _schema_is_proven_subset(
            property_schema,
            entry_args_schema,
            target_contract.schema,
            target_contract.root_schema,
        ):
            continue
        property_contract_text = json.dumps(
            _mutable_contract_json(property_schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        target_contract_text = json.dumps(
            _mutable_contract_json(target_contract.schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        raise ValueError(
            "$.route.entry_args_schema.properties"
            f"[{property_name!r}] contract {property_contract_text} is not proven to be a "
            f"subset of {target_contract.origin} contract {target_contract_text}"
        )


def _uses_identification_v2(doc: dict[str, Any]) -> bool:
    return any(use.get("ref") == "identification@2" for use in doc.get("uses", []))


def _bind_await_external(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    capability = cast(
        Optional[dict[str, Any]],
        (doc.get("capabilities") or {}).get("await_external"),
    )
    path_steps = [step for step in doc["path"] if step.get("await_external") is True]
    if len(path_steps) > 1:
        raise ValueError("flowspec/2 supports one capabilities.await_external binding")
    if path_steps and capability is None:
        raise ValueError("path await_external requires capabilities.await_external")
    if capability is None:
        return None

    if path_steps:
        path_step = path_steps[0]
        unsupported_fields = {
            "ask_when",
            "skip_when",
            "on_reject",
            "correctable",
        } & path_step.keys()
        if unsupported_fields:
            unsupported = ", ".join(sorted(unsupported_fields))
            raise ValueError(f"path await_external does not support these fields: {unsupported}")
        path_node_id = path_step.get("step")
        capability_node_id = capability.get("step")
        if path_node_id and capability_node_id and path_node_id != capability_node_id:
            raise ValueError(
                "path await_external step conflicts with capabilities.await_external.step"
            )
        bound_node_id = path_node_id or capability_node_id or "await_external"
        path_step["step"] = bound_node_id
        capability["step"] = bound_node_id
        return capability

    if not capability.get("step") and _uses_identification_v2(doc):
        # Backward compatibility for documents authored before the explicit
        # subflow binding existed.
        capability["step"] = "authenticate_govbr"
    if not capability.get("step"):
        raise ValueError(
            "capabilities.await_external must declare step or have a path await_external anchor"
        )
    return capability


def _validate_binding_map(
    bindings: dict[str, Any],
    *,
    namespace: str,
    location: str,
) -> None:
    if not isinstance(bindings, dict):
        raise ValueError(f"{location} must be an object")
    prefix = f"${namespace}."
    for target, binding in bindings.items():
        if not isinstance(target, str) or not target:
            raise ValueError(f"{location} contains an empty destination")
        if binding is not None and not isinstance(binding, (str, int, float, bool)):
            raise ValueError(f"{location}.{target} must be a JSON scalar")
        if isinstance(binding, str) and binding.startswith("$"):
            reference_path = binding.removeprefix(prefix)
            if (
                not binding.startswith(prefix)
                or not reference_path
                or any(not segment for segment in reference_path.split("."))
            ):
                raise ValueError(f"{location}.{target} must use {prefix}path")


def _resume_reference_segments(reference: str, *, location: str) -> tuple[str, ...]:
    if _RESUME_REFERENCE_PATTERN.fullmatch(reference) is None:
        raise ValueError(
            f"{location} must be an exact $token property path with identifier segments"
        )
    return tuple(reference.removeprefix("$token.").split("."))


def _schema_array_index_contract(
    schema: Any,
    array_index: int,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> Any:
    if schema is False:
        return False
    if schema is True or not isinstance(schema, Mapping):
        return True

    contract_fragments: list[Any] = []
    prefix_items = schema.get("prefixItems")
    if isinstance(prefix_items, (list, tuple)) and array_index < len(prefix_items):
        contract_fragments.append(prefix_items[array_index])
    elif "items" in schema:
        contract_fragments.append(schema["items"])

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        contract_fragments.append(
            True
            if reference_text in reference_stack
            else _schema_array_index_contract(
                _schema_reference_target(root_schema, reference_text),
                array_index,
                root_schema,
                (*reference_stack, reference_text),
            )
        )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        contract_fragments.extend(
            _schema_array_index_contract(branch, array_index, root_schema, reference_stack)
            for branch in all_of
        )
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, (list, tuple)):
            contract_fragments.append(
                _combined_contract(
                    [
                        _schema_array_index_contract(
                            branch,
                            array_index,
                            root_schema,
                            reference_stack,
                        )
                        for branch in branches
                    ],
                    "anyOf",
                )
            )
    return _combined_contract(contract_fragments)


def _schema_path_value_contract(
    schema: Any,
    path_segments: tuple[str, ...],
    root_schema: Any,
) -> Any:
    current_contract = schema
    for path_segment in path_segments:
        if path_segment.isdigit():
            current_contract = _schema_array_index_contract(
                current_contract,
                int(path_segment),
                root_schema,
            )
        else:
            current_contract = _schema_property_contract(
                current_contract,
                path_segment,
                root_schema,
                frozenset(_required_schema_properties(current_contract, root_schema)),
            )
    return current_contract


def _resume_token_path_contract(
    token_schema: dict[str, Any],
    reference: str,
    *,
    location: str,
    require_presence: bool,
) -> _FlowValueContract:
    path_segments = _resume_reference_segments(reference, location=location)
    path_status = _schema_path_status(token_schema, path_segments)
    if path_status != _CONTRACT_VALID:
        raise ValueError(f"{location} does not resolve exactly in resume.schema: {reference!r}")
    if require_presence and not _schema_requires_path_in_every_alternative(
        token_schema,
        path_segments,
        token_schema,
    ):
        raise ValueError(f"{location} must be required by every resume.schema alternative")
    path_contract = _schema_path_value_contract(token_schema, path_segments, token_schema)
    return _FlowValueContract(
        schema=path_contract,
        root_schema=token_schema,
        origin=f"resume token path {reference!r}",
        total=require_presence,
    )


def _mutable_contract_json(contract_fragment: Any) -> Any:
    if isinstance(contract_fragment, Mapping):
        return {
            property_name: _mutable_contract_json(property_value)
            for property_name, property_value in contract_fragment.items()
        }
    if isinstance(contract_fragment, (list, tuple)):
        return [_mutable_contract_json(element) for element in contract_fragment]
    return contract_fragment


def _standalone_schema(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> Any:
    """Inline safe local references so contracts from different roots can be composed."""

    if isinstance(schema, Mapping):
        standalone_mapping = {
            keyword: _standalone_schema(keyword_value, root_schema, reference_stack)
            for keyword, keyword_value in schema.items()
            if keyword != "$ref"
        }
        if (reference := schema.get("$ref")) is None:
            return standalone_mapping
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            raise ValueError("cyclic local schema reference cannot be materialized")
        referenced_schema = _standalone_schema(
            _schema_reference_target(root_schema, reference_text),
            root_schema,
            (*reference_stack, reference_text),
        )
        return _combined_contract([standalone_mapping or True, referenced_schema])
    if isinstance(schema, (list, tuple)):
        return [_standalone_schema(element, root_schema, reference_stack) for element in schema]
    return schema


def _schema_accepts_known_instance(
    schema: Any,
    root_schema: Any,
    instance: object,
) -> bool:
    mutable_root_schema = _mutable_contract_json(root_schema)
    validator = Draft202012Validator(
        mutable_root_schema,
        format_checker=FormatChecker(),
    )
    return validator.evolve(schema=_mutable_contract_json(schema)).is_valid(cast(Any, instance))


def _combined_contract(contract_fragments: list[Any], keyword: str = "allOf") -> Any:
    effective_fragments = [
        contract_fragment
        for contract_fragment in contract_fragments
        if contract_fragment is not True
    ]
    if not effective_fragments:
        return True
    if len(effective_fragments) == 1:
        return effective_fragments[0]
    return {keyword: effective_fragments}


def _schema_property_contract(
    schema: Any,
    property_name: str,
    root_schema: Any,
    bound_keys: frozenset[str],
    reference_stack: tuple[str, ...] = (),
) -> Any:
    if schema is False:
        return False
    if schema is True or not isinstance(schema, Mapping):
        return True

    property_contracts: list[Any] = []
    properties = schema.get("properties")
    pattern_properties = schema.get("patternProperties")
    has_property_dispatch = (
        isinstance(properties, Mapping)
        or isinstance(pattern_properties, Mapping)
        or "additionalProperties" in schema
    )
    if has_property_dispatch:
        matching_contracts: list[Any] = []
        if isinstance(properties, Mapping) and property_name in properties:
            matching_contracts.append(properties[property_name])
        if isinstance(pattern_properties, Mapping):
            matching_contracts.extend(
                property_contract
                for property_pattern, property_contract in pattern_properties.items()
                if re.search(property_pattern, property_name) is not None
            )
        if not matching_contracts:
            matching_contracts.append(schema.get("additionalProperties", True))
        property_contracts.append(_combined_contract(matching_contracts))

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        property_contracts.append(
            True
            if reference_text in reference_stack
            else _schema_property_contract(
                _schema_reference_target(root_schema, reference_text),
                property_name,
                root_schema,
                bound_keys,
                (*reference_stack, reference_text),
            )
        )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        property_contracts.extend(
            _schema_property_contract(
                branch,
                property_name,
                root_schema,
                bound_keys,
                reference_stack,
            )
            for branch in all_of
        )
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, (list, tuple)):
            property_contracts.append(
                _combined_contract(
                    [
                        _schema_property_contract(
                            branch,
                            property_name,
                            root_schema,
                            bound_keys,
                            reference_stack,
                        )
                        for branch in branches
                    ],
                    "anyOf",
                )
            )
    if "if" in schema:
        property_contracts.append(
            _combined_contract(
                [
                    _schema_property_contract(
                        schema.get("then", True),
                        property_name,
                        root_schema,
                        bound_keys,
                        reference_stack,
                    ),
                    _schema_property_contract(
                        schema.get("else", True),
                        property_name,
                        root_schema,
                        bound_keys,
                        reference_stack,
                    ),
                ],
                "anyOf",
            )
        )
    dependent_schemas = schema.get("dependentSchemas")
    if isinstance(dependent_schemas, Mapping):
        property_contracts.extend(
            _schema_property_contract(
                dependent_schema,
                property_name,
                root_schema,
                bound_keys,
                reference_stack,
            )
            for trigger_property, dependent_schema in dependent_schemas.items()
            if trigger_property in bound_keys
        )
    return _combined_contract(property_contracts)


_VALUE_TYPE_ATOMS: Final[frozenset[str]] = frozenset(
    {
        "array",
        "boolean",
        "integer",
        "non_integer_number",
        "null",
        "object",
        "string",
    }
)


def _instance_type_atom(schema_instance: Any) -> str:
    if isinstance(schema_instance, Mapping):
        return "object"
    if isinstance(schema_instance, list):
        return "array"
    if isinstance(schema_instance, bool):
        return "boolean"
    if schema_instance is None:
        return "null"
    if isinstance(schema_instance, int) or (
        isinstance(schema_instance, float) and schema_instance.is_integer()
    ):
        return "integer"
    if isinstance(schema_instance, float):
        return "non_integer_number"
    return "string"


def _atoms_for_schema_type(schema_type: str) -> frozenset[str]:
    if schema_type == "number":
        return frozenset({"integer", "non_integer_number"})
    if schema_type == "integer":
        return frozenset({"integer"})
    return frozenset({schema_type})


def _schema_type_atoms(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> frozenset[str]:
    if schema is False:
        return frozenset()
    if schema is True or not isinstance(schema, Mapping):
        return _VALUE_TYPE_ATOMS

    possible_atoms = _VALUE_TYPE_ATOMS
    if (schema_types := schema.get("type")) is not None:
        declared_types = (schema_types,) if isinstance(schema_types, str) else tuple(schema_types)
        possible_atoms &= frozenset().union(
            *(_atoms_for_schema_type(schema_type) for schema_type in declared_types)
        )
    if "const" in schema:
        possible_atoms &= frozenset({_instance_type_atom(schema["const"])})
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        possible_atoms &= frozenset(
            _instance_type_atom(enum_member) for enum_member in enum_members
        )
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            possible_atoms &= _schema_type_atoms(
                _schema_reference_target(root_schema, reference_text),
                root_schema,
                (*reference_stack, reference_text),
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            possible_atoms &= _schema_type_atoms(branch, root_schema, reference_stack)
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, (list, tuple)):
            branch_atoms = frozenset().union(
                *(_schema_type_atoms(branch, root_schema, reference_stack) for branch in branches)
            )
            possible_atoms &= branch_atoms
    if "if" in schema:
        conditional_atoms = _schema_type_atoms(
            schema.get("then", True), root_schema, reference_stack
        ) | _schema_type_atoms(schema.get("else", True), root_schema, reference_stack)
        possible_atoms &= conditional_atoms
    return possible_atoms


def _canonical_contract_instance(schema_instance: Any) -> str:
    return json.dumps(
        schema_instance,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _finite_schema_instances(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if schema is False:
        return {}
    if schema is True or not isinstance(schema, Mapping):
        return None

    finite_instances: dict[str, Any] | None = None

    def intersect_instances(candidate_instances: dict[str, Any]) -> None:
        nonlocal finite_instances
        finite_instances = (
            candidate_instances
            if finite_instances is None
            else {
                serialized_instance: schema_instance
                for serialized_instance, schema_instance in finite_instances.items()
                if serialized_instance in candidate_instances
            }
        )

    if "const" in schema:
        constant_instance = schema["const"]
        intersect_instances({_canonical_contract_instance(constant_instance): constant_instance})
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        intersect_instances(
            {_canonical_contract_instance(enum_member): enum_member for enum_member in enum_members}
        )
    if (schema_types := schema.get("type")) is not None:
        declared_types = {schema_types} if isinstance(schema_types, str) else set(schema_types)
        if declared_types and declared_types <= {"boolean", "null"}:
            finite_type_instances = [
                *([None] if "null" in declared_types else []),
                *([False, True] if "boolean" in declared_types else []),
            ]
            intersect_instances(
                {
                    _canonical_contract_instance(type_instance): type_instance
                    for type_instance in finite_type_instances
                }
            )
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if (
            reference_text not in reference_stack
            and (
                referenced_instances := _finite_schema_instances(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    (*reference_stack, reference_text),
                )
            )
            is not None
        ):
            intersect_instances(referenced_instances)
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            if (
                branch_instances := _finite_schema_instances(branch, root_schema, reference_stack)
            ) is not None:
                intersect_instances(branch_instances)
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        branch_instance_sets = [
            _finite_schema_instances(branch, root_schema, reference_stack) for branch in branches
        ]
        if all(branch_instances is not None for branch_instances in branch_instance_sets):
            intersect_instances(
                {
                    serialized_instance: schema_instance
                    for branch_instances in branch_instance_sets
                    if branch_instances is not None
                    for serialized_instance, schema_instance in branch_instances.items()
                }
            )
    if "if" in schema:
        conditional_instance_sets = [
            _finite_schema_instances(
                schema.get(conditional_keyword, True),
                root_schema,
                reference_stack,
            )
            for conditional_keyword in ("then", "else")
        ]
        if all(
            conditional_instances is not None for conditional_instances in conditional_instance_sets
        ):
            intersect_instances(
                {
                    serialized_instance: schema_instance
                    for conditional_instances in conditional_instance_sets
                    if conditional_instances is not None
                    for serialized_instance, schema_instance in conditional_instances.items()
                }
            )

    if finite_instances is None:
        return None
    return {
        serialized_instance: schema_instance
        for serialized_instance, schema_instance in finite_instances.items()
        if _schema_accepts_known_instance(schema, root_schema, schema_instance)
    }


_NumericBoundary = tuple[Decimal, bool]
_NumericInterval = tuple[_NumericBoundary | None, _NumericBoundary | None]


def _stronger_lower_boundary(
    current_boundary: _NumericBoundary | None,
    candidate_boundary: _NumericBoundary,
) -> _NumericBoundary:
    if current_boundary is None or candidate_boundary[0] > current_boundary[0]:
        return candidate_boundary
    if candidate_boundary[0] == current_boundary[0]:
        return candidate_boundary[0], current_boundary[1] and candidate_boundary[1]
    return current_boundary


def _stronger_upper_boundary(
    current_boundary: _NumericBoundary | None,
    candidate_boundary: _NumericBoundary,
) -> _NumericBoundary:
    if current_boundary is None or candidate_boundary[0] < current_boundary[0]:
        return candidate_boundary
    if candidate_boundary[0] == current_boundary[0]:
        return candidate_boundary[0], current_boundary[1] and candidate_boundary[1]
    return current_boundary


def _schema_numeric_interval(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> _NumericInterval:
    if not isinstance(schema, Mapping):
        return None, None
    lower_boundary: _NumericBoundary | None = None
    upper_boundary: _NumericBoundary | None = None
    if isinstance((minimum := schema.get("minimum")), (int, float)):
        lower_boundary = _stronger_lower_boundary(lower_boundary, (Decimal(str(minimum)), True))
    if isinstance((exclusive_minimum := schema.get("exclusiveMinimum")), (int, float)):
        lower_boundary = _stronger_lower_boundary(
            lower_boundary, (Decimal(str(exclusive_minimum)), False)
        )
    if isinstance((maximum := schema.get("maximum")), (int, float)):
        upper_boundary = _stronger_upper_boundary(upper_boundary, (Decimal(str(maximum)), True))
    if isinstance((exclusive_maximum := schema.get("exclusiveMaximum")), (int, float)):
        upper_boundary = _stronger_upper_boundary(
            upper_boundary, (Decimal(str(exclusive_maximum)), False)
        )

    nested_schemas: list[Any] = []
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            nested_schemas.append(_schema_reference_target(root_schema, reference_text))
            reference_stack = (*reference_stack, reference_text)
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        nested_schemas.extend(all_of)
    for nested_schema in nested_schemas:
        nested_lower, nested_upper = _schema_numeric_interval(
            nested_schema, root_schema, reference_stack
        )
        if nested_lower is not None:
            lower_boundary = _stronger_lower_boundary(lower_boundary, nested_lower)
        if nested_upper is not None:
            upper_boundary = _stronger_upper_boundary(upper_boundary, nested_upper)
    return lower_boundary, upper_boundary


def _numeric_intervals_overlap(
    source_interval: _NumericInterval,
    parameter_interval: _NumericInterval,
    *,
    integer_only: bool,
) -> bool:
    lower_boundary: _NumericBoundary | None = None
    upper_boundary: _NumericBoundary | None = None
    for candidate_lower in (source_interval[0], parameter_interval[0]):
        if candidate_lower is not None:
            lower_boundary = _stronger_lower_boundary(lower_boundary, candidate_lower)
    for candidate_upper in (source_interval[1], parameter_interval[1]):
        if candidate_upper is not None:
            upper_boundary = _stronger_upper_boundary(upper_boundary, candidate_upper)
    if lower_boundary is not None and upper_boundary is not None:
        if lower_boundary[0] > upper_boundary[0]:
            return False
        if lower_boundary[0] == upper_boundary[0] and not (lower_boundary[1] and upper_boundary[1]):
            return False
    if not integer_only:
        return True

    minimum_integer = (
        None
        if lower_boundary is None
        else int(lower_boundary[0].to_integral_value(rounding=ROUND_CEILING))
        + (
            1
            if not lower_boundary[1] and lower_boundary[0] == lower_boundary[0].to_integral_value()
            else 0
        )
    )
    maximum_integer = (
        None
        if upper_boundary is None
        else int(upper_boundary[0].to_integral_value(rounding=ROUND_FLOOR))
        - (
            1
            if not upper_boundary[1] and upper_boundary[0] == upper_boundary[0].to_integral_value()
            else 0
        )
    )
    return minimum_integer is None or maximum_integer is None or minimum_integer <= maximum_integer


def _schemas_are_clearly_disjoint(
    source_schema: Any,
    source_root_schema: Any,
    parameter_schema: Any,
    parameter_root_schema: Any,
) -> bool:
    source_instances = _finite_schema_instances(source_schema, source_root_schema)
    if source_instances is not None:
        return not any(
            _schema_accepts_known_instance(parameter_schema, parameter_root_schema, source_instance)
            for source_instance in source_instances.values()
        )
    parameter_instances = _finite_schema_instances(parameter_schema, parameter_root_schema)
    if parameter_instances is not None:
        return not any(
            _schema_accepts_known_instance(source_schema, source_root_schema, parameter_instance)
            for parameter_instance in parameter_instances.values()
        )

    shared_type_atoms = _schema_type_atoms(source_schema, source_root_schema) & _schema_type_atoms(
        parameter_schema, parameter_root_schema
    )
    if not shared_type_atoms:
        return True
    numeric_atoms = frozenset({"integer", "non_integer_number"})
    if shared_type_atoms - numeric_atoms:
        return False
    return not _numeric_intervals_overlap(
        _schema_numeric_interval(source_schema, source_root_schema),
        _schema_numeric_interval(parameter_schema, parameter_root_schema),
        integer_only=shared_type_atoms == {"integer"},
    )


_SCHEMA_ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$comment",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)


def _schema_assertion_json(schema: Any) -> Any:
    if isinstance(schema, Mapping):
        return {
            keyword: _schema_assertion_json(keyword_value)
            for keyword, keyword_value in schema.items()
            if keyword not in _SCHEMA_ANNOTATION_KEYWORDS
        }
    if isinstance(schema, (list, tuple)):
        return [_schema_assertion_json(element) for element in schema]
    return schema


def _schema_without_keyword(schema: Mapping[str, Any], keyword: str) -> Any:
    remaining_schema = {
        schema_keyword: schema_value
        for schema_keyword, schema_value in schema.items()
        if schema_keyword != keyword
    }
    return remaining_schema or True


def _schema_length_bound(
    schema: Any,
    root_schema: Any,
    keyword: str,
    reference_stack: tuple[str, ...] = (),
) -> int | None:
    if not isinstance(schema, Mapping):
        return None
    candidate_bounds: list[int | None] = [
        cast(int, schema[keyword])
        if isinstance(schema.get(keyword), int) and not isinstance(schema[keyword], bool)
        else None
    ]
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            candidate_bounds.append(
                _schema_length_bound(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    keyword,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        candidate_bounds.extend(
            _schema_length_bound(branch, root_schema, keyword, reference_stack) for branch in all_of
        )
    concrete_bounds = [bound for bound in candidate_bounds if bound is not None]
    if not concrete_bounds:
        return None
    return max(concrete_bounds) if keyword == "minLength" else min(concrete_bounds)


def _schema_object_count_bound(
    schema: Any,
    root_schema: Any,
    keyword: str,
    reference_stack: tuple[str, ...] = (),
) -> int | None:
    if not isinstance(schema, Mapping):
        return None
    candidate_bounds: list[int | None] = [
        cast(int, schema[keyword])
        if isinstance(schema.get(keyword), int) and not isinstance(schema[keyword], bool)
        else None
    ]
    if keyword == "minProperties":
        candidate_bounds.append(
            len(
                {
                    property_name
                    for property_name in schema.get("required", ())
                    if isinstance(property_name, str)
                }
            )
        )
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            candidate_bounds.append(
                _schema_object_count_bound(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    keyword,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        candidate_bounds.extend(
            _schema_object_count_bound(branch, root_schema, keyword, reference_stack)
            for branch in all_of
        )
    concrete_bounds = [bound for bound in candidate_bounds if bound is not None]
    if not concrete_bounds:
        return None
    return max(concrete_bounds) if keyword == "minProperties" else min(concrete_bounds)


def _schema_string_assertions(
    schema: Any,
    root_schema: Any,
    keyword: str,
    reference_stack: tuple[str, ...] = (),
) -> frozenset[str]:
    if not isinstance(schema, Mapping):
        return frozenset()
    assertions = {assertion for assertion in (schema.get(keyword),) if isinstance(assertion, str)}
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            assertions.update(
                _schema_string_assertions(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    keyword,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            assertions.update(
                _schema_string_assertions(branch, root_schema, keyword, reference_stack)
            )
    return frozenset(assertions)


def _lower_boundary_contains(
    source_boundary: _NumericBoundary | None,
    target_boundary: _NumericBoundary | None,
) -> bool:
    if target_boundary is None:
        return True
    if source_boundary is None or source_boundary[0] < target_boundary[0]:
        return False
    if source_boundary[0] > target_boundary[0]:
        return True
    return target_boundary[1] or not source_boundary[1]


def _upper_boundary_contains(
    source_boundary: _NumericBoundary | None,
    target_boundary: _NumericBoundary | None,
) -> bool:
    if target_boundary is None:
        return True
    if source_boundary is None or source_boundary[0] > target_boundary[0]:
        return False
    if source_boundary[0] < target_boundary[0]:
        return True
    return target_boundary[1] or not source_boundary[1]


def _multiple_of_is_subset(source_multiple: Any, target_multiple: Any) -> bool:
    if not isinstance(target_multiple, (int, float)) or isinstance(target_multiple, bool):
        return True
    if not isinstance(source_multiple, (int, float)) or isinstance(source_multiple, bool):
        return False
    source_decimal = Decimal(str(source_multiple))
    target_decimal = Decimal(str(target_multiple))
    return target_decimal > 0 and source_decimal > 0 and source_decimal % target_decimal == 0


def _schema_is_proven_subset(
    source_schema: Any,
    source_root_schema: Any,
    target_schema: Any,
    target_root_schema: Any,
) -> bool:
    """Conservatively prove JSON-Schema language inclusion for flow value contracts."""

    if target_schema is True or source_schema is False:
        return True
    if target_schema is False:
        return _finite_schema_instances(source_schema, source_root_schema) == {}
    if _schema_assertion_json(source_schema) == _schema_assertion_json(target_schema):
        return True

    source_instances = _finite_schema_instances(source_schema, source_root_schema)
    if source_instances is not None:
        return all(
            _schema_accepts_known_instance(target_schema, target_root_schema, source_instance)
            for source_instance in source_instances.values()
        )
    if not isinstance(source_schema, Mapping) or not isinstance(target_schema, Mapping):
        return False

    if (source_reference := source_schema.get("$ref")) is not None:
        source_without_reference = _schema_without_keyword(source_schema, "$ref")
        if source_without_reference is True:
            return _schema_is_proven_subset(
                _schema_reference_target(source_root_schema, cast(str, source_reference)),
                source_root_schema,
                target_schema,
                target_root_schema,
            )

    for union_keyword in ("anyOf", "oneOf"):
        source_branches = source_schema.get(union_keyword)
        if not isinstance(source_branches, (list, tuple)):
            continue
        source_without_union = _schema_without_keyword(source_schema, union_keyword)
        return all(
            _schema_is_proven_subset(
                _combined_contract([source_without_union, source_branch]),
                source_root_schema,
                target_schema,
                target_root_schema,
            )
            for source_branch in source_branches
        )

    if (target_reference := target_schema.get("$ref")) is not None:
        return _schema_is_proven_subset(
            source_schema,
            source_root_schema,
            _schema_without_keyword(target_schema, "$ref"),
            target_root_schema,
        ) and _schema_is_proven_subset(
            source_schema,
            source_root_schema,
            _schema_reference_target(target_root_schema, cast(str, target_reference)),
            target_root_schema,
        )

    if isinstance((target_all_of := target_schema.get("allOf")), (list, tuple)):
        return _schema_is_proven_subset(
            source_schema,
            source_root_schema,
            _schema_without_keyword(target_schema, "allOf"),
            target_root_schema,
        ) and all(
            _schema_is_proven_subset(
                source_schema,
                source_root_schema,
                target_branch,
                target_root_schema,
            )
            for target_branch in target_all_of
        )

    for union_keyword in ("anyOf", "oneOf"):
        target_branches = target_schema.get(union_keyword)
        if not isinstance(target_branches, (list, tuple)):
            continue
        if not _schema_is_proven_subset(
            source_schema,
            source_root_schema,
            _schema_without_keyword(target_schema, union_keyword),
            target_root_schema,
        ):
            return False
        for branch_index, target_branch in enumerate(target_branches):
            if not _schema_is_proven_subset(
                source_schema,
                source_root_schema,
                target_branch,
                target_root_schema,
            ):
                continue
            if union_keyword == "anyOf" or all(
                _schemas_are_clearly_disjoint(
                    source_schema,
                    source_root_schema,
                    other_branch,
                    target_root_schema,
                )
                for other_index, other_branch in enumerate(target_branches)
                if other_index != branch_index
            ):
                return True
        return False

    if any(keyword in target_schema for keyword in ("if", "not")):
        return False
    if "const" in target_schema or "enum" in target_schema:
        return False

    source_atoms = _schema_type_atoms(source_schema, source_root_schema)
    target_atoms = _schema_type_atoms(target_schema, target_root_schema)
    if not source_atoms <= target_atoms:
        return False

    numeric_atoms = frozenset({"integer", "non_integer_number"})
    if source_atoms & numeric_atoms:
        source_interval = _schema_numeric_interval(source_schema, source_root_schema)
        target_interval = _schema_numeric_interval(target_schema, target_root_schema)
        if not (
            _lower_boundary_contains(source_interval[0], target_interval[0])
            and _upper_boundary_contains(source_interval[1], target_interval[1])
            and _multiple_of_is_subset(
                source_schema.get("multipleOf"), target_schema.get("multipleOf")
            )
        ):
            return False

    if "string" in source_atoms:
        source_minimum_length = _schema_length_bound(source_schema, source_root_schema, "minLength")
        target_minimum_length = _schema_length_bound(target_schema, target_root_schema, "minLength")
        if target_minimum_length is not None and (
            source_minimum_length is None or source_minimum_length < target_minimum_length
        ):
            return False
        source_maximum_length = _schema_length_bound(source_schema, source_root_schema, "maxLength")
        target_maximum_length = _schema_length_bound(target_schema, target_root_schema, "maxLength")
        if target_maximum_length is not None and (
            source_maximum_length is None or source_maximum_length > target_maximum_length
        ):
            return False
        for string_keyword in ("format", "pattern"):
            if not _schema_string_assertions(
                target_schema, target_root_schema, string_keyword
            ) <= _schema_string_assertions(source_schema, source_root_schema, string_keyword):
                return False

    structured_atoms = frozenset({"array", "object"})
    if source_atoms & structured_atoms:
        if "object" in source_atoms:
            source_minimum_properties = _schema_object_count_bound(
                source_schema, source_root_schema, "minProperties"
            )
            target_minimum_properties = _schema_object_count_bound(
                target_schema, target_root_schema, "minProperties"
            )
            if target_minimum_properties is not None and (
                source_minimum_properties is None
                or source_minimum_properties < target_minimum_properties
            ):
                return False
            source_maximum_properties = _schema_object_count_bound(
                source_schema, source_root_schema, "maxProperties"
            )
            target_maximum_properties = _schema_object_count_bound(
                target_schema, target_root_schema, "maxProperties"
            )
            if target_maximum_properties is not None and (
                source_maximum_properties is None
                or source_maximum_properties > target_maximum_properties
            ):
                return False
        structured_assertions = set(target_schema) - {
            *_SCHEMA_ANNOTATION_KEYWORDS,
            "$anchor",
            "$defs",
            "$id",
            "$schema",
            "maxProperties",
            "minProperties",
            "type",
        }
        if structured_assertions:
            return False
    return True


@dataclass(frozen=True)
class _FlowValueContract:
    schema: Any
    root_schema: Any
    origin: str
    total: bool


def _declared_slot_value_contract(
    document: dict[str, Any],
    slot_name: str,
    *,
    include_absence: bool,
) -> _FlowValueContract | None:
    slot_definition = document.get("slots", {}).get(slot_name)
    if not isinstance(slot_definition, Mapping):
        return None
    domain_name = cast(str, slot_definition["domain"])
    slot_model = make_slot_model(
        slot_name,
        domain_name,
        cast(dict[str, Any], document["domains"]),
        nullable=bool(slot_definition.get("nullable", False)),
    )
    source_root_schema = slot_model.model_json_schema()
    source_schema: Any = source_root_schema["properties"][slot_name]
    is_total = bool(slot_definition.get("required", False))
    if include_absence and not is_total:
        source_schema = {"anyOf": [source_schema, {"type": "null"}]}
    return _FlowValueContract(
        schema=source_schema,
        root_schema=source_root_schema,
        origin=f"slot {slot_name!r} from domain {domain_name!r}",
        total=is_total,
    )


def _schema_excluding_null(schema: Any, root_schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return schema
    for union_keyword in ("anyOf", "oneOf"):
        union_branches = schema.get(union_keyword)
        if not isinstance(union_branches, (list, tuple)):
            continue
        non_null_branches = [
            union_branch
            for union_branch in union_branches
            if _schema_type_atoms(union_branch, root_schema) != {"null"}
        ]
        schema_without_union = _schema_without_keyword(schema, union_keyword)
        return _combined_contract(
            [
                schema_without_union,
                (
                    _combined_contract(non_null_branches, union_keyword)
                    if non_null_branches
                    else False
                ),
            ]
        )
    schema_types = schema.get("type")
    if isinstance(schema_types, (list, tuple)) and "null" in schema_types:
        non_null_types = [schema_type for schema_type in schema_types if schema_type != "null"]
        return {
            **schema,
            "type": non_null_types[0] if len(non_null_types) == 1 else non_null_types,
        }
    return schema


def _exposed_slot_value_contracts(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    *,
    include_absence: bool,
) -> dict[str, _FlowValueContract]:
    contracts: dict[str, _FlowValueContract] = {}
    for use_declaration in document.get("uses", []):
        subflow_reference = cast(str, use_declaration["ref"])
        if not subflows.has(subflow_reference):
            continue
        definition = subflows.definition(subflow_reference)
        subflow_configuration = cast(dict[str, Any], use_declaration.get("with") or {})
        is_total = bool(subflow_configuration.get("required")) and (
            subflow_configuration.get("on_exhaust") != "skip"
        )
        for slot_name, slot_schema in definition.exposed_slot_schemas.items():
            standalone_slot_schema = _standalone_schema(slot_schema, slot_schema)
            effective_slot_schema = (
                _schema_excluding_null(standalone_slot_schema, standalone_slot_schema)
                if is_total
                else standalone_slot_schema
            )
            allows_null = _schema_accepts_known_instance(
                effective_slot_schema,
                effective_slot_schema,
                None,
            )
            contracts[slot_name] = _FlowValueContract(
                schema=(
                    {"anyOf": [effective_slot_schema, {"type": "null"}]}
                    if include_absence and not is_total and not allows_null
                    else effective_slot_schema
                ),
                root_schema=effective_slot_schema,
                origin=f"slot {slot_name!r} exposed by subflow {subflow_reference!r}",
                total=is_total,
            )
    return contracts


def _derive_key_component(source_value: Any) -> str:
    return "" if source_value is None else str(source_value)


def _derive_is_total(
    derive_definition: Mapping[str, Any],
    source_contracts: list[_FlowValueContract],
) -> bool:
    finite_source_values: list[tuple[Any, ...]] = []
    for source_contract in source_contracts:
        source_instances = _finite_schema_instances(
            source_contract.schema,
            source_contract.root_schema,
        )
        if source_instances is None:
            break
        finite_source_values.append(tuple(source_instances.values()))
    else:
        default_value = derive_definition.get("default")
        default_source_index = (
            int(default_match.group(1))
            if isinstance(default_value, str)
            and (default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value))
            else None
        )
        lookup = cast(Mapping[str, Any], derive_definition["lookup"])
        return all(
            "|".join(_derive_key_component(source_value) for source_value in source_values)
            in lookup
            or (
                default_value is not None
                and (
                    default_source_index is None or source_values[default_source_index] is not None
                )
            )
            for source_values in product(*finite_source_values)
        )

    default_value = derive_definition.get("default")
    if default_value is None:
        return False
    if not isinstance(default_value, str) or not (
        default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value)
    ):
        return True
    default_source_contract = source_contracts[int(default_match.group(1))]
    return default_source_contract.total and not _schema_accepts_known_instance(
        default_source_contract.schema,
        default_source_contract.root_schema,
        None,
    )


def _derived_value_contract(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    slot_name: str,
    *,
    location: str,
    derive_stack: tuple[str, ...] = (),
) -> _FlowValueContract | None:
    derive_definition = next(
        (
            cast(dict[str, Any], candidate_definition)
            for candidate_definition in document.get("derive", [])
            if candidate_definition["writes"] == slot_name
        ),
        None,
    )
    if derive_definition is None:
        return None
    if slot_name in derive_stack:
        raise ValueError(f"{location} resolves through a cyclic derive contract: {slot_name!r}")

    exposed_contracts = _exposed_slot_value_contracts(
        document,
        subflows,
        include_absence=True,
    )
    source_contracts: list[_FlowValueContract] = []
    for source_slot in cast(list[str], derive_definition["from"]):
        source_contract = (
            _declared_slot_value_contract(
                document,
                source_slot,
                include_absence=True,
            )
            or _derived_value_contract(
                document,
                subflows,
                source_slot,
                location=location,
                derive_stack=(*derive_stack, slot_name),
            )
            or exposed_contracts.get(source_slot)
        )
        if source_contract is None:
            return None
        source_contracts.append(source_contract)

    lookup_values = list(cast(dict[str, Any], derive_definition["lookup"]).values())
    unique_lookup_values = {
        _canonical_contract_instance(lookup_value): lookup_value for lookup_value in lookup_values
    }
    produced_alternatives: list[Any] = (
        [{"enum": list(unique_lookup_values.values())}] if unique_lookup_values else []
    )
    default_value = derive_definition.get("default")
    if isinstance(default_value, str) and (
        default_match := re.fullmatch(r"\$from\[(\d+)\]", default_value)
    ):
        default_source_contract = source_contracts[int(default_match.group(1))]
        produced_alternatives.append(
            _standalone_schema(
                default_source_contract.schema,
                default_source_contract.root_schema,
            )
        )
    elif (
        default_value is not None
        and (serialized_default := _canonical_contract_instance(default_value))
        not in unique_lookup_values
    ):
        produced_alternatives.append({"const": json.loads(serialized_default)})
    produced_schema = (
        _combined_contract(produced_alternatives, "anyOf") if (produced_alternatives) else False
    )

    if (
        declared_target_contract := _declared_slot_value_contract(
            document,
            slot_name,
            include_absence=False,
        )
    ) is not None and not _schema_is_proven_subset(
        produced_schema,
        produced_schema,
        declared_target_contract.schema,
        declared_target_contract.root_schema,
    ):
        raise ValueError(
            f"{location} uses derived value {slot_name!r}, but its produced values are not "
            "proven to satisfy the declared slot domain"
        )

    is_total = _derive_is_total(derive_definition, source_contracts)
    source_schema = (
        produced_schema
        if is_total
        else _combined_contract([produced_schema, {"type": "null"}], "anyOf")
    )
    return _FlowValueContract(
        schema=source_schema,
        root_schema=source_schema,
        origin=f"derived value {slot_name!r}",
        total=is_total,
    )


def _terminal_source_value_contract(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    slot_name: str,
    *,
    location: str,
) -> _FlowValueContract | None:
    return (
        _declared_slot_value_contract(document, slot_name, include_absence=True)
        or _derived_value_contract(
            document,
            subflows,
            slot_name,
            location=location,
        )
        or _exposed_slot_value_contracts(
            document,
            subflows,
            include_absence=True,
        ).get(slot_name)
    )


def _validate_terminal_binding_contracts(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    definition: ToolDefinition,
    input_bindings: list[dict[str, Any]],
) -> None:
    bound_keys = frozenset(cast(str, input_binding["param"]) for input_binding in input_bindings)
    for input_index, input_binding in enumerate(input_bindings):
        slot_name = cast(str, input_binding["slot"])
        input_location = f"$.terminal.input[{input_index}]"
        if (
            source_contract := _terminal_source_value_contract(
                document,
                subflows,
                slot_name,
                location=input_location,
            )
        ) is None:
            continue
        parameter_name = cast(str, input_binding["param"])
        parameter_schema = _schema_property_contract(
            definition.input_schema,
            parameter_name,
            definition.input_schema,
            bound_keys,
        )
        if _schema_is_proven_subset(
            source_contract.schema,
            source_contract.root_schema,
            parameter_schema,
            definition.input_schema,
        ):
            continue
        source_contract_text = json.dumps(
            _mutable_contract_json(source_contract.schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        parameter_contract_text = json.dumps(
            _mutable_contract_json(parameter_schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        raise ValueError(
            f"{input_location} binds {source_contract.origin} (source contract "
            f"{source_contract_text}) to tool parameter "
            f"{parameter_name!r} in {definition.identifier!r} (parameter contract "
            f"{parameter_contract_text}), but the source contract is not proven to be a "
            "subset of the parameter contract"
        )


def _projection_all(projections: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    return (
        all(projection[0] for projection in projections),
        all(projection[1] for projection in projections),
    )


def _projection_any(projections: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    return (
        any(projection[0] for projection in projections),
        any(projection[1] for projection in projections),
    )


def _projection_not(projection: tuple[bool, bool]) -> tuple[bool, bool]:
    can_match, must_match = projection
    return not must_match, not can_match


def _projection_one_of(projections: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    possible_projections = [projection for projection in projections if projection[0]]
    guaranteed_matches = sum(projection[1] for projection in possible_projections)
    return (
        bool(possible_projections) and guaranteed_matches < 2,
        guaranteed_matches == 1 and len(possible_projections) == 1,
    )


def _unknown_value_projection(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> tuple[bool, bool]:
    if schema is False:
        return False, False
    if schema is True or not isinstance(schema, Mapping):
        return True, True

    projections: list[tuple[bool, bool]] = []
    value_keywords = set(schema) - {
        "$anchor",
        "$comment",
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "allOf",
        "anyOf",
        "definitions",
        "description",
        "else",
        "examples",
        "if",
        "oneOf",
        "then",
        "title",
    }
    if value_keywords:
        projections.append((True, False))

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            projections.append((True, False))
        else:
            projections.append(
                _unknown_value_projection(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        projections.append(
            _projection_all(
                [
                    _unknown_value_projection(branch, root_schema, reference_stack)
                    for branch in all_of
                ]
            )
        )
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)):
        projections.append(
            _projection_any(
                [
                    _unknown_value_projection(branch, root_schema, reference_stack)
                    for branch in any_of
                ]
            )
        )
    if isinstance((one_of := schema.get("oneOf")), (list, tuple)):
        projections.append(
            _projection_one_of(
                [
                    _unknown_value_projection(branch, root_schema, reference_stack)
                    for branch in one_of
                ]
            )
        )
    if "not" in schema:
        projections.append(
            _projection_not(_unknown_value_projection(schema["not"], root_schema, reference_stack))
        )
    if "if" in schema:
        condition_projection = _unknown_value_projection(schema["if"], root_schema, reference_stack)
        conditional_projection = _projection_any(
            [
                _projection_all(
                    [
                        condition_projection,
                        _unknown_value_projection(
                            schema.get("then", True), root_schema, reference_stack
                        ),
                    ]
                ),
                _projection_all(
                    [
                        _projection_not(condition_projection),
                        _unknown_value_projection(
                            schema.get("else", True), root_schema, reference_stack
                        ),
                    ]
                ),
            ]
        )
        projections.append(conditional_projection)
    return _projection_all(projections) if projections else (True, True)


def _key_projection_match(
    schema: Any,
    root_schema: Any,
    bound_keys: frozenset[str],
    reference_stack: tuple[str, ...] = (),
) -> tuple[bool, bool]:
    if schema is False:
        return False, False
    if schema is True or not isinstance(schema, Mapping):
        return True, True

    projections: list[tuple[bool, bool]] = []
    schema_types = schema.get("type")
    if schema_types is not None:
        supports_object = schema_types == "object" or (
            isinstance(schema_types, (list, tuple)) and "object" in schema_types
        )
        projections.append((supports_object, supports_object))

    if "const" in schema:
        constant_object = schema["const"]
        constant_keys_match = (
            isinstance(constant_object, Mapping)
            and frozenset(str(property_name) for property_name in constant_object) == bound_keys
        )
        projections.append((constant_keys_match, False))
    if isinstance((enum_members := schema.get("enum")), (list, tuple)):
        matching_enum_member = any(
            isinstance(enum_member, Mapping)
            and frozenset(str(property_name) for property_name in enum_member) == bound_keys
            for enum_member in enum_members
        )
        projections.append((matching_enum_member, False))

    required_properties = {
        property_name
        for property_name in schema.get("required", ())
        if isinstance(property_name, str)
    }
    if required_properties:
        has_required_properties = required_properties <= bound_keys
        projections.append((has_required_properties, has_required_properties))
    if isinstance((minimum_properties := schema.get("minProperties")), int):
        meets_minimum = len(bound_keys) >= minimum_properties
        projections.append((meets_minimum, meets_minimum))
    if isinstance((maximum_properties := schema.get("maxProperties")), int):
        meets_maximum = len(bound_keys) <= maximum_properties
        projections.append((meets_maximum, meets_maximum))
    if (property_names := schema.get("propertyNames")) is not None:
        valid_property_names = all(
            _schema_accepts_known_instance(property_names, root_schema, bound_key)
            for bound_key in bound_keys
        )
        projections.append((valid_property_names, valid_property_names))

    properties = schema.get("properties")
    pattern_properties = schema.get("patternProperties")
    additional_properties = schema.get("additionalProperties", True)
    if isinstance(properties, Mapping) or isinstance(pattern_properties, Mapping):
        property_projections: list[tuple[bool, bool]] = []
        for bound_key in bound_keys:
            matching_property_schemas: list[Any] = []
            if isinstance(properties, Mapping) and bound_key in properties:
                matching_property_schemas.append(properties[bound_key])
            if isinstance(pattern_properties, Mapping):
                matching_property_schemas.extend(
                    property_schema
                    for property_pattern, property_schema in pattern_properties.items()
                    if re.search(property_pattern, bound_key) is not None
                )
            if not matching_property_schemas:
                if additional_properties is False:
                    property_projections.append((False, False))
                    continue
                matching_property_schemas.append(additional_properties)
            property_projections.append(
                _projection_all(
                    [
                        _unknown_value_projection(property_schema, root_schema, reference_stack)
                        for property_schema in matching_property_schemas
                    ]
                )
            )
        if property_projections:
            projections.append(_projection_all(property_projections))
    elif additional_properties is False and bound_keys:
        projections.append((False, False))

    dependent_required = schema.get("dependentRequired")
    if isinstance(dependent_required, Mapping):
        for trigger_property, dependent_properties in dependent_required.items():
            if trigger_property not in bound_keys:
                continue
            dependencies_present = all(
                dependent_property in bound_keys for dependent_property in dependent_properties
            )
            projections.append((dependencies_present, dependencies_present))
    dependent_schemas = schema.get("dependentSchemas")
    if isinstance(dependent_schemas, Mapping):
        projections.extend(
            _key_projection_match(
                dependent_schema,
                root_schema,
                bound_keys,
                reference_stack,
            )
            for trigger_property, dependent_schema in dependent_schemas.items()
            if trigger_property in bound_keys
        )

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            projections.append((True, False))
        else:
            projections.append(
                _key_projection_match(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    bound_keys,
                    (*reference_stack, reference_text),
                )
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        projections.append(
            _projection_all(
                [
                    _key_projection_match(branch, root_schema, bound_keys, reference_stack)
                    for branch in all_of
                ]
            )
        )
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)):
        projections.append(
            _projection_any(
                [
                    _key_projection_match(branch, root_schema, bound_keys, reference_stack)
                    for branch in any_of
                ]
            )
        )
    if isinstance((one_of := schema.get("oneOf")), (list, tuple)):
        projections.append(
            _projection_one_of(
                [
                    _key_projection_match(branch, root_schema, bound_keys, reference_stack)
                    for branch in one_of
                ]
            )
        )
    if "not" in schema:
        projections.append(
            _projection_not(
                _key_projection_match(schema["not"], root_schema, bound_keys, reference_stack)
            )
        )
    if "if" in schema:
        condition_projection = _key_projection_match(
            schema["if"], root_schema, bound_keys, reference_stack
        )
        projections.append(
            _projection_any(
                [
                    _projection_all(
                        [
                            condition_projection,
                            _key_projection_match(
                                schema.get("then", True),
                                root_schema,
                                bound_keys,
                                reference_stack,
                            ),
                        ]
                    ),
                    _projection_all(
                        [
                            _projection_not(condition_projection),
                            _key_projection_match(
                                schema.get("else", True),
                                root_schema,
                                bound_keys,
                                reference_stack,
                            ),
                        ]
                    ),
                ]
            )
        )

    return _projection_all(projections) if projections else (True, True)


def _schema_property_status(
    schema: Any,
    property_name: str,
    root_schema: Any | None = None,
    reference_stack: tuple[str, ...] = (),
) -> int:
    resolved_root_schema = schema if root_schema is None else root_schema
    if schema is False:
        return _CONTRACT_INVALID
    if schema is True or not isinstance(schema, Mapping):
        return _CONTRACT_UNKNOWN
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            return _CONTRACT_UNKNOWN
        return _schema_property_status(
            _schema_reference_target(resolved_root_schema, reference_text),
            property_name,
            resolved_root_schema,
            (*reference_stack, reference_text),
        )

    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        statuses = [
            _schema_property_status(branch, property_name, resolved_root_schema, reference_stack)
            for branch in all_of
        ]
        if _CONTRACT_INVALID in statuses:
            return _CONTRACT_INVALID
        if _CONTRACT_VALID in statuses:
            return _CONTRACT_VALID

    for union_keyword in ("oneOf", "anyOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        statuses = [
            _schema_property_status(branch, property_name, resolved_root_schema, reference_stack)
            for branch in branches
        ]
        if _CONTRACT_VALID in statuses:
            return _CONTRACT_VALID
        if statuses and all(status == _CONTRACT_INVALID for status in statuses):
            return _CONTRACT_INVALID
        return _CONTRACT_UNKNOWN

    schema_types = schema.get("type")
    if isinstance(schema_types, str) and schema_types != "object":
        return _CONTRACT_INVALID
    if isinstance(schema_types, (list, tuple)) and "object" not in schema_types:
        return _CONTRACT_INVALID

    properties = schema.get("properties")
    if isinstance(properties, Mapping) and property_name in properties:
        return _CONTRACT_VALID
    pattern_properties = schema.get("patternProperties")
    if isinstance(pattern_properties, Mapping) and any(
        re.search(pattern, property_name) is not None for pattern in pattern_properties
    ):
        return _CONTRACT_VALID
    additional_properties = schema.get("additionalProperties", True)
    if additional_properties is False:
        return _CONTRACT_INVALID
    return _CONTRACT_VALID if isinstance(additional_properties, Mapping) else _CONTRACT_UNKNOWN


def _required_schema_properties(
    schema: Any,
    root_schema: Any | None = None,
    reference_stack: tuple[str, ...] = (),
) -> set[str]:
    resolved_root_schema = schema if root_schema is None else root_schema
    if not isinstance(schema, Mapping):
        return set()
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            return set()
        return _required_schema_properties(
            _schema_reference_target(resolved_root_schema, reference_text),
            resolved_root_schema,
            (*reference_stack, reference_text),
        )
    required_properties = {
        property_name
        for property_name in schema.get("required", ())
        if isinstance(property_name, str)
    }
    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        for branch in all_of:
            required_properties.update(
                _required_schema_properties(branch, resolved_root_schema, reference_stack)
            )
    for union_keyword in ("oneOf", "anyOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)) or not branches:
            continue
        branch_requirements = [
            _required_schema_properties(branch, resolved_root_schema, reference_stack)
            for branch in branches
        ]
        required_properties.update(set.intersection(*branch_requirements))
    return required_properties


def _schema_path_status(
    schema: Any,
    path_segments: tuple[str, ...],
    root_schema: Any | None = None,
    reference_stack: tuple[str, ...] = (),
) -> int:
    resolved_root_schema = schema if root_schema is None else root_schema
    if schema is False:
        return _CONTRACT_INVALID
    if not path_segments:
        return _CONTRACT_VALID
    if schema is True or not isinstance(schema, Mapping):
        return _CONTRACT_UNKNOWN
    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text in reference_stack:
            return _CONTRACT_UNKNOWN
        return _schema_path_status(
            _schema_reference_target(resolved_root_schema, reference_text),
            path_segments,
            resolved_root_schema,
            (*reference_stack, reference_text),
        )

    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        statuses = [
            _schema_path_status(branch, path_segments, resolved_root_schema, reference_stack)
            for branch in all_of
        ]
        if _CONTRACT_INVALID in statuses:
            return _CONTRACT_INVALID
        if _CONTRACT_VALID in statuses:
            return _CONTRACT_VALID

    for union_keyword in ("oneOf", "anyOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        statuses = [
            _schema_path_status(branch, path_segments, resolved_root_schema, reference_stack)
            for branch in branches
        ]
        if _CONTRACT_VALID in statuses:
            return _CONTRACT_VALID
        if statuses and all(status == _CONTRACT_INVALID for status in statuses):
            return _CONTRACT_INVALID
        return _CONTRACT_UNKNOWN

    path_segment, remaining_segments = path_segments[0], path_segments[1:]
    schema_types = schema.get("type")
    supports_array = schema_types == "array" or (
        isinstance(schema_types, (list, tuple)) and "array" in schema_types
    )
    supports_object = (
        schema_types in (None, "object")
        or isinstance(schema.get("properties"), Mapping)
        or (isinstance(schema_types, (list, tuple)) and "object" in schema_types)
    )

    if supports_array and path_segment.isdigit():
        items = schema.get("items", True)
        return _schema_path_status(items, remaining_segments, resolved_root_schema, reference_stack)
    if supports_object:
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and path_segment in properties:
            return _schema_path_status(
                properties[path_segment],
                remaining_segments,
                resolved_root_schema,
                reference_stack,
            )
        pattern_properties = schema.get("patternProperties")
        if isinstance(pattern_properties, Mapping):
            matching_schemas = [
                nested_schema
                for pattern, nested_schema in pattern_properties.items()
                if re.search(pattern, path_segment) is not None
            ]
            if matching_schemas:
                statuses = [
                    _schema_path_status(
                        nested_schema,
                        remaining_segments,
                        resolved_root_schema,
                        reference_stack,
                    )
                    for nested_schema in matching_schemas
                ]
                if _CONTRACT_INVALID in statuses:
                    return _CONTRACT_INVALID
                if all(status == _CONTRACT_VALID for status in statuses):
                    return _CONTRACT_VALID
                return _CONTRACT_UNKNOWN
        additional_properties = schema.get("additionalProperties", True)
        if additional_properties is False:
            return _CONTRACT_INVALID
        if isinstance(additional_properties, Mapping):
            return _schema_path_status(
                additional_properties,
                remaining_segments,
                resolved_root_schema,
                reference_stack,
            )
        return _CONTRACT_UNKNOWN
    return _CONTRACT_INVALID


def _combine_schema_alternatives(
    left_alternatives: list[list[Any]],
    right_alternatives: list[list[Any]],
) -> list[list[Any]]:
    return [
        [*left_alternative, *right_alternative]
        for left_alternative in left_alternatives
        for right_alternative in right_alternatives
    ]


def _schema_conjunctive_alternatives(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> list[list[Any]]:
    if schema is False:
        return []
    if schema is True or not isinstance(schema, Mapping):
        return [[]]

    applicator_keywords = {
        "$ref",
        "allOf",
        "anyOf",
        "else",
        "if",
        "oneOf",
        "then",
    }
    direct_schema = {
        property_name: property_contract
        for property_name, property_contract in schema.items()
        if property_name not in applicator_keywords
    }
    alternatives: list[list[Any]] = [[direct_schema]] if direct_schema else [[]]

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack:
            alternatives = _combine_schema_alternatives(
                alternatives,
                _schema_conjunctive_alternatives(
                    _schema_reference_target(root_schema, reference_text),
                    root_schema,
                    (*reference_stack, reference_text),
                ),
            )
    if isinstance((all_of := schema.get("allOf")), (list, tuple)):
        for branch in all_of:
            alternatives = _combine_schema_alternatives(
                alternatives,
                _schema_conjunctive_alternatives(branch, root_schema, reference_stack),
            )
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if not isinstance(branches, (list, tuple)):
            continue
        union_alternatives = [
            branch_alternative
            for branch in branches
            for branch_alternative in _schema_conjunctive_alternatives(
                branch, root_schema, reference_stack
            )
        ]
        alternatives = _combine_schema_alternatives(alternatives, union_alternatives)
    if "if" in schema:
        condition_schema = schema["if"]
        conditional_alternatives = [
            *(
                _combine_schema_alternatives(
                    _schema_conjunctive_alternatives(
                        condition_schema, root_schema, reference_stack
                    ),
                    _schema_conjunctive_alternatives(
                        schema.get("then", True), root_schema, reference_stack
                    ),
                )
            ),
            *(
                _combine_schema_alternatives(
                    [[{"not": condition_schema}]],
                    _schema_conjunctive_alternatives(
                        schema.get("else", True), root_schema, reference_stack
                    ),
                )
            ),
        ]
        alternatives = _combine_schema_alternatives(alternatives, conditional_alternatives)
    return alternatives


def _alternative_required_properties(
    schema_fragments: list[Any],
    root_schema: Any,
) -> frozenset[str]:
    required_properties = {
        property_name
        for schema_fragment in schema_fragments
        if isinstance(schema_fragment, Mapping)
        for property_name in schema_fragment.get("required", ())
        if isinstance(property_name, str)
    }
    requirements_changed = True
    while requirements_changed:
        requirements_changed = False
        for schema_fragment in schema_fragments:
            if not isinstance(schema_fragment, Mapping):
                continue
            dependent_required = schema_fragment.get("dependentRequired")
            if isinstance(dependent_required, Mapping):
                for trigger_property, dependent_properties in dependent_required.items():
                    if trigger_property not in required_properties:
                        continue
                    previous_size = len(required_properties)
                    required_properties.update(
                        property_name
                        for property_name in dependent_properties
                        if isinstance(property_name, str)
                    )
                    requirements_changed = (
                        requirements_changed or len(required_properties) > previous_size
                    )
            dependent_schemas = schema_fragment.get("dependentSchemas")
            if not isinstance(dependent_schemas, Mapping):
                continue
            for trigger_property, dependent_schema in dependent_schemas.items():
                if trigger_property not in required_properties:
                    continue
                previous_size = len(required_properties)
                required_properties.update(
                    _required_schema_properties(dependent_schema, root_schema)
                )
                requirements_changed = (
                    requirements_changed or len(required_properties) > previous_size
                )
    return frozenset(required_properties)


def _alternative_property_contract(
    schema_fragments: list[Any],
    property_name: str,
    root_schema: Any,
    required_properties: frozenset[str],
) -> Any:
    return _combined_contract(
        [
            _schema_property_contract(
                schema_fragment,
                property_name,
                root_schema,
                required_properties,
            )
            for schema_fragment in schema_fragments
        ]
    )


def _schema_is_unconditional(
    schema: Any,
    root_schema: Any,
    reference_stack: tuple[str, ...] = (),
) -> bool:
    if schema is True:
        return True
    if schema is False or not isinstance(schema, Mapping):
        return False
    assertion_keywords = set(schema) - {
        "$anchor",
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "description",
        "examples",
        "title",
    }
    if not assertion_keywords:
        return True
    if assertion_keywords == {"$ref"}:
        reference_text = cast(str, schema["$ref"])
        return reference_text in reference_stack or _schema_is_unconditional(
            _schema_reference_target(root_schema, reference_text),
            root_schema,
            (*reference_stack, reference_text),
        )
    if assertion_keywords == {"allOf"} and isinstance(
        (all_of := schema.get("allOf")), (list, tuple)
    ):
        return all(
            _schema_is_unconditional(branch, root_schema, reference_stack) for branch in all_of
        )
    if assertion_keywords == {"anyOf"} and isinstance(
        (any_of := schema.get("anyOf")), (list, tuple)
    ):
        return any(
            _schema_is_unconditional(branch, root_schema, reference_stack) for branch in any_of
        )
    return False


def _schema_is_guaranteed_for_known_object(
    schema: Any,
    root_schema: Any,
    known_properties: Mapping[str, Any],
    required_properties: frozenset[str],
    reference_stack: tuple[str, ...] = (),
) -> bool:
    if schema is True:
        return True
    if schema is False or not isinstance(schema, Mapping):
        return False

    schema_types = schema.get("type")
    if schema_types is not None and not (
        schema_types == "object"
        or isinstance(schema_types, (list, tuple))
        and "object" in schema_types
    ):
        return False
    if "const" in schema or "enum" in schema:
        return False
    declared_required = {
        property_name
        for property_name in schema.get("required", ())
        if isinstance(property_name, str)
    }
    if not declared_required <= required_properties:
        return False
    if isinstance((minimum_properties := schema.get("minProperties")), int) and (
        len(required_properties) < minimum_properties
    ):
        return False
    if "maxProperties" in schema:
        return False
    if schema.get("additionalProperties") is False:
        return False
    if "propertyNames" in schema:
        return False
    if any(
        schema.get(dependency_keyword)
        for dependency_keyword in ("dependentRequired", "dependentSchemas")
    ):
        return False

    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for property_name, property_contract in properties.items():
            if property_name in known_properties:
                if not _schema_accepts_known_instance(
                    property_contract,
                    root_schema,
                    known_properties[property_name],
                ):
                    return False
            elif not _schema_is_unconditional(property_contract, root_schema):
                return False
    pattern_properties = schema.get("patternProperties")
    if isinstance(pattern_properties, Mapping) and any(
        not _schema_is_unconditional(property_contract, root_schema)
        for property_contract in pattern_properties.values()
    ):
        return False

    if (reference := schema.get("$ref")) is not None:
        reference_text = cast(str, reference)
        if reference_text not in reference_stack and not _schema_is_guaranteed_for_known_object(
            _schema_reference_target(root_schema, reference_text),
            root_schema,
            known_properties,
            required_properties,
            (*reference_stack, reference_text),
        ):
            return False
    if isinstance((all_of := schema.get("allOf")), (list, tuple)) and not all(
        _schema_is_guaranteed_for_known_object(
            branch,
            root_schema,
            known_properties,
            required_properties,
            reference_stack,
        )
        for branch in all_of
    ):
        return False
    if isinstance((any_of := schema.get("anyOf")), (list, tuple)) and not any(
        _schema_is_guaranteed_for_known_object(
            branch,
            root_schema,
            known_properties,
            required_properties,
            reference_stack,
        )
        for branch in any_of
    ):
        return False
    if any(composition_keyword in schema for composition_keyword in ("oneOf", "not", "if")):
        return False
    return True


def _alternative_is_success_compatible(
    schema_fragments: list[Any],
    root_schema: Any,
) -> bool:
    required_properties = _alternative_required_properties(schema_fragments, root_schema)
    if "status" not in required_properties:
        return True
    status_contract = _alternative_property_contract(
        schema_fragments,
        "status",
        root_schema,
        required_properties,
    )
    if not _schema_accepts_known_instance(status_contract, root_schema, "success"):
        return False
    known_properties = {"status": "success"}
    return not any(
        isinstance(schema_fragment, Mapping)
        and (negated_schema := schema_fragment.get("not")) is not None
        and _schema_is_guaranteed_for_known_object(
            negated_schema,
            root_schema,
            known_properties,
            required_properties,
        )
        for schema_fragment in schema_fragments
    )


def _alternative_requires_path(
    schema_fragments: list[Any],
    path_segments: tuple[str, ...],
    root_schema: Any,
) -> bool:
    required_properties = _alternative_required_properties(schema_fragments, root_schema)
    path_segment, remaining_segments = path_segments[0], path_segments[1:]
    if path_segment not in required_properties:
        return False
    if not remaining_segments:
        return True
    nested_contract = _alternative_property_contract(
        schema_fragments,
        path_segment,
        root_schema,
        required_properties,
    )
    return _schema_requires_path_in_every_alternative(
        nested_contract,
        remaining_segments,
        root_schema,
    )


def _schema_requires_path_in_every_alternative(
    schema: Any,
    path_segments: tuple[str, ...],
    root_schema: Any,
) -> bool:
    alternatives = _schema_conjunctive_alternatives(schema, root_schema)
    return bool(alternatives) and all(
        _alternative_requires_path(
            schema_fragments,
            path_segments,
            root_schema,
        )
        for schema_fragments in alternatives
    )


def _schema_requires_path_for_every_success(
    schema: Any,
    path_segments: tuple[str, ...],
) -> bool:
    alternatives = _schema_conjunctive_alternatives(schema, schema)
    return all(
        not _alternative_is_success_compatible(schema_fragments, schema)
        or _alternative_requires_path(schema_fragments, path_segments, schema)
        for schema_fragments in alternatives
    )


_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"success", "retryable", "fatal"})


def _validate_terminal_output_protocol(definition: ToolDefinition) -> None:
    output_schema = definition.output_schema
    if _schema_property_status(output_schema, "status") != _CONTRACT_INVALID:
        status_contract = _schema_property_contract(
            output_schema,
            "status",
            output_schema,
            frozenset(_required_schema_properties(output_schema)),
        )
        status_instances = _finite_schema_instances(status_contract, output_schema)
        if status_instances is None or any(
            status_instance not in _TERMINAL_STATUSES
            for status_instance in status_instances.values()
        ):
            allowed_statuses = ", ".join(sorted(_TERMINAL_STATUSES))
            raise ValueError(
                f"$.terminal.tool output contract {definition.identifier!r} must restrict "
                f"status to the terminal protocol: {allowed_statuses}"
            )

    output_alternatives = _schema_conjunctive_alternatives(output_schema, output_schema)
    if not any(
        _alternative_is_success_compatible(output_alternative, output_schema)
        for output_alternative in output_alternatives
    ):
        raise ValueError(
            f"$.terminal.tool output contract {definition.identifier!r} has no "
            "success-compatible branch"
        )


def _tool_supports_terminal_status(definition: ToolDefinition, status: str) -> bool:
    if _schema_property_status(definition.output_schema, "status") == _CONTRACT_INVALID:
        return False
    status_contract = _schema_property_contract(
        definition.output_schema,
        "status",
        definition.output_schema,
        frozenset(_required_schema_properties(definition.output_schema)),
    )
    return _schema_accepts_known_instance(
        status_contract,
        definition.output_schema,
        status,
    )


def _validate_read_only_tool_effects(
    definition: ToolDefinition,
    *,
    location: str,
) -> None:
    if definition.effects.read_only and not definition.effects.destructive:
        return
    raise ValueError(
        f"{location} tool contract {definition.identifier!r} must be read_only and non-destructive"
    )


def _registered_tool_definition(
    tools: ToolRegistry,
    tool_name: str,
    *,
    location: str,
) -> ToolDefinition:
    if not tools.has(tool_name):
        raise ValueError(f"{location} tool is not registered: {tool_name!r}")
    return tools.definition(tool_name)


def _validate_tool_inputs(
    definition: ToolDefinition,
    parameters: list[tuple[str, str]],
    *,
    call_location: str,
    require_all: bool = True,
) -> None:
    seen_parameters: dict[str, str] = {}
    for parameter_name, parameter_location in parameters:
        if parameter_name in seen_parameters:
            raise ValueError(
                f"{parameter_location} duplicates tool parameter {parameter_name!r}; "
                f"first bound at {seen_parameters[parameter_name]}"
            )
        seen_parameters[parameter_name] = parameter_location
        if _schema_property_status(definition.input_schema, parameter_name) == _CONTRACT_INVALID:
            raise ValueError(
                f"{parameter_location} does not exist in tool contract "
                f"{definition.identifier!r}: {parameter_name!r}"
            )

    if not require_all:
        return
    missing_parameters = _required_schema_properties(definition.input_schema) - set(seen_parameters)
    if missing_parameters:
        missing = ", ".join(repr(parameter) for parameter in sorted(missing_parameters))
        raise ValueError(
            f"{call_location} does not bind required parameters for tool contract "
            f"{definition.identifier!r}: {missing}"
        )
    bound_parameters = frozenset(seen_parameters)
    if not _key_projection_match(
        definition.input_schema,
        definition.input_schema,
        bound_parameters,
    )[0]:
        rendered_parameters = ", ".join(repr(parameter) for parameter in sorted(bound_parameters))
        raise ValueError(
            f"{call_location} bound-key projection [{rendered_parameters}] does not satisfy "
            f"the complete input contract {definition.identifier!r}"
        )


def _validate_tool_result_path(
    definition: ToolDefinition,
    result_path: str,
    *,
    location: str,
    allow_array_indices: bool,
) -> None:
    path_segments = tuple(result_path.split("."))
    required_namespace = "$result" if allow_array_indices else "result"
    if not path_segments or path_segments[0] != required_namespace:
        raise ValueError(f"{location} must use the {required_namespace}.* namespace")
    path_segments = path_segments[1:]
    if not path_segments or any(not segment for segment in path_segments):
        raise ValueError(f"{location} must contain a non-empty tool result path")
    if not allow_array_indices and any(segment.isdigit() for segment in path_segments):
        raise ValueError(f"{location} cannot traverse arrays in terminal tool results")
    if _schema_path_status(definition.output_schema, path_segments) == _CONTRACT_INVALID:
        raise ValueError(
            f"{location} does not resolve in tool contract {definition.identifier!r}: "
            f"{result_path!r}"
        )
    if not allow_array_indices and not _schema_requires_path_for_every_success(
        definition.output_schema,
        path_segments,
    ):
        raise ValueError(
            f"{location} must be required in every success-compatible output branch "
            f"of tool contract {definition.identifier!r}: {result_path!r}"
        )


def _validate_flow_tool_contracts(
    doc: dict[str, Any],
    tools: ToolRegistry,
    subflows: SubflowRegistry,
) -> None:
    entry_definition = doc.get("entry")
    if entry_definition:
        entry_tool_name = cast(str, entry_definition["tool"])
        tool_definition = _registered_tool_definition(
            tools,
            entry_tool_name,
            location="$.entry.tool",
        )
        _validate_read_only_tool_effects(tool_definition, location="$.entry.tool")
        _validate_tool_inputs(
            tool_definition,
            [],
            call_location="$.entry",
        )

    terminal_definition = doc.get("terminal")
    if not terminal_definition:
        return
    terminal_tool_name = cast(str, terminal_definition["tool"])
    tool_definition = _registered_tool_definition(
        tools,
        terminal_tool_name,
        location="$.terminal.tool",
    )
    _validate_terminal_output_protocol(tool_definition)
    terminal_declares_idempotency = bool(terminal_definition.get("idempotent", False))
    if terminal_declares_idempotency and not tool_definition.effects.idempotent:
        raise ValueError(
            f"$.terminal.idempotent is true, but tool contract "
            f"{tool_definition.identifier!r} does not declare idempotent effects"
        )
    retryable_outcome = cast(
        dict[str, Any],
        cast(dict[str, Any], terminal_definition.get("outcomes", {})).get("retryable", {}),
    )
    if (
        retryable_outcome.get("preserve_state", True) is True
        and _tool_supports_terminal_status(tool_definition, "retryable")
        and not (terminal_declares_idempotency or tool_definition.effects.idempotent)
    ):
        raise ValueError(
            "$.terminal.outcomes.retryable.preserve_state requires an idempotent terminal "
            f"or idempotent tool contract {tool_definition.identifier!r}"
        )
    terminal_input_bindings = cast(list[dict[str, Any]], terminal_definition.get("input", []))
    terminal_parameters = [
        (
            cast(str, input_binding["param"]),
            f"$.terminal.input[{input_index}].param",
        )
        for input_index, input_binding in enumerate(terminal_input_bindings)
    ]
    _validate_tool_inputs(
        tool_definition,
        terminal_parameters,
        call_location="$.terminal.input",
    )
    _validate_terminal_binding_contracts(
        doc,
        subflows,
        tool_definition,
        terminal_input_bindings,
    )
    for state_key, result_path in terminal_definition.get("outputs", {}).items():
        _validate_tool_result_path(
            tool_definition,
            cast(str, result_path),
            location=f"$.terminal.outputs[{state_key!r}]",
            allow_array_indices=False,
        )


@dataclass(frozen=True)
class _AwaitStateTargetContract:
    value_contract: _FlowValueContract
    partition: str


def _await_state_target_contract(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    state_key: str,
) -> _AwaitStateTargetContract | None:
    if declared_contract := _declared_slot_value_contract(
        document,
        state_key,
        include_absence=False,
    ):
        slot_definition = cast(dict[str, Any], document["slots"][state_key])
        return _AwaitStateTargetContract(
            value_contract=declared_contract,
            partition=cast(str, slot_definition.get("persist", "data")),
        )

    for use_definition in document.get("uses", []):
        subflow_reference = cast(str, use_definition["ref"])
        if not subflows.has(subflow_reference):
            continue
        subflow_definition = subflows.definition(subflow_reference)
        state_contract = subflow_definition.owned_state_keys.get(state_key)
        if state_contract is None:
            continue
        state_schema = _mutable_contract_json(state_contract.schema)
        return _AwaitStateTargetContract(
            value_contract=_FlowValueContract(
                schema=state_schema,
                root_schema=state_schema,
                origin=f"state key {state_key!r} owned by subflow {subflow_reference!r}",
                total=True,
            ),
            partition=state_contract.partition,
        )
    return None


def _validate_source_contract_for_await_state(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    state_key: str,
    source_contract: _FlowValueContract,
    *,
    location: str,
) -> None:
    target_contract = _await_state_target_contract(document, subflows, state_key)
    if target_contract is None:
        return
    if target_contract.partition == "internal" and state_key not in document.get("slots", {}):
        raise ValueError(f"{location} cannot write subflow-owned internal state key {state_key!r}")
    if _schema_is_proven_subset(
        source_contract.schema,
        source_contract.root_schema,
        target_contract.value_contract.schema,
        target_contract.value_contract.root_schema,
    ):
        return
    raise ValueError(
        f"{location} {source_contract.origin} is not proven to satisfy "
        f"{target_contract.value_contract.origin}"
    )


def _validate_literal_for_await_state(
    document: dict[str, Any],
    subflows: SubflowRegistry,
    state_key: str,
    literal_value: Any,
    *,
    location: str,
) -> None:
    target_contract = _await_state_target_contract(document, subflows, state_key)
    if target_contract is None:
        return
    if target_contract.partition == "internal" and state_key not in document.get("slots", {}):
        raise ValueError(f"{location} cannot write subflow-owned internal state key {state_key!r}")
    if not _schema_accepts_known_instance(
        target_contract.value_contract.schema,
        target_contract.value_contract.root_schema,
        literal_value,
    ):
        raise ValueError(
            f"{location} literal does not satisfy {target_contract.value_contract.origin}"
        )


def _validate_typed_resume_contract(
    document: dict[str, Any],
    capability: dict[str, Any],
    tools: ToolRegistry,
    subflows: SubflowRegistry,
) -> None:
    resume_contract = cast(dict[str, Any] | None, capability.get("resume"))
    if resume_contract is None:
        return
    token_schema = cast(dict[str, Any], resume_contract["schema"])
    _validate_resume_token_schema(token_schema)
    correlation_reference = cast(str, resume_contract["correlation"])
    correlation_contract = _resume_token_path_contract(
        token_schema,
        correlation_reference,
        location="$.capabilities.await_external.resume.correlation",
        require_presence=True,
    )
    scalar_correlation_types = frozenset({"boolean", "integer", "non_integer_number", "string"})
    correlation_types = _schema_type_atoms(
        correlation_contract.schema,
        correlation_contract.root_schema,
    )
    if not correlation_types or not correlation_types <= scalar_correlation_types:
        raise ValueError(
            "$.capabilities.await_external.resume.correlation must resolve to a "
            "non-null JSON scalar"
        )

    on_resume = cast(dict[str, Any], capability.get("on_resume") or {})
    for state_key, binding in cast(dict[str, Any], on_resume.get("set") or {}).items():
        binding_location = f"$.capabilities.await_external.on_resume.set[{state_key!r}]"
        if isinstance(binding, str) and binding.startswith("$"):
            source_contract = _resume_token_path_contract(
                token_schema,
                binding,
                location=binding_location,
                require_presence=False,
            )
            _validate_source_contract_for_await_state(
                document,
                subflows,
                state_key,
                source_contract,
                location=binding_location,
            )
        else:
            _validate_literal_for_await_state(
                document,
                subflows,
                state_key,
                binding,
                location=binding_location,
            )

    enrichment = on_resume.get("enrich")
    for transition_name, transition in {
        "timeout": capability.get("timeout"),
        **cast(dict[str, Any], capability.get("recovery") or {}),
    }.items():
        if not isinstance(transition, dict):
            continue
        for state_key, literal_value in cast(dict[str, Any], transition.get("set") or {}).items():
            _validate_literal_for_await_state(
                document,
                subflows,
                state_key,
                literal_value,
                location=(f"$.capabilities.await_external.{transition_name}.set[{state_key!r}]"),
            )
    if not isinstance(enrichment, dict):
        return
    tool_definition = tools.definition(cast(str, enrichment["tool"]))
    enrichment_input = cast(dict[str, Any], enrichment.get("input") or {})
    required_parameters = _required_schema_properties(tool_definition.input_schema)
    bound_parameters = frozenset(enrichment_input)
    for parameter_name, binding in enrichment_input.items():
        binding_location = (
            f"$.capabilities.await_external.on_resume.enrich.input[{parameter_name!r}]"
        )
        parameter_contract = _schema_property_contract(
            tool_definition.input_schema,
            parameter_name,
            tool_definition.input_schema,
            bound_parameters,
        )
        if isinstance(binding, str) and binding.startswith("$"):
            source_contract = _resume_token_path_contract(
                token_schema,
                binding,
                location=binding_location,
                require_presence=parameter_name in required_parameters,
            )
            if not _schema_is_proven_subset(
                source_contract.schema,
                source_contract.root_schema,
                parameter_contract,
                tool_definition.input_schema,
            ):
                raise ValueError(
                    f"{binding_location} {source_contract.origin} is not proven to satisfy "
                    f"tool parameter {parameter_name!r} in {tool_definition.identifier!r}"
                )
        elif not _schema_accepts_known_instance(
            parameter_contract,
            tool_definition.input_schema,
            binding,
        ):
            raise ValueError(
                f"{binding_location} literal does not satisfy tool parameter "
                f"{parameter_name!r} in {tool_definition.identifier!r}"
            )

    for state_key, binding in cast(dict[str, Any], enrichment.get("set") or {}).items():
        binding_location = f"$.capabilities.await_external.on_resume.enrich.set[{state_key!r}]"
        if isinstance(binding, str) and binding.startswith("$result."):
            result_path = tuple(binding.removeprefix("$result.").split("."))
            source_schema = _schema_path_value_contract(
                tool_definition.output_schema,
                result_path,
                tool_definition.output_schema,
            )
            _validate_source_contract_for_await_state(
                document,
                subflows,
                state_key,
                _FlowValueContract(
                    schema=source_schema,
                    root_schema=tool_definition.output_schema,
                    origin=f"tool result path {binding!r}",
                    total=False,
                ),
                location=binding_location,
            )
        else:
            _validate_literal_for_await_state(
                document,
                subflows,
                state_key,
                binding,
                location=binding_location,
            )


def _validate_await_external_definition(
    document: dict[str, Any],
    capability: Optional[dict[str, Any]],
    tools: ToolRegistry,
    subflows: SubflowRegistry,
    path_step: Optional[dict[str, Any]],
) -> None:
    if capability is None:
        return
    if not isinstance(capability.get("step"), str) or not capability["step"]:
        raise ValueError("capabilities.await_external.step must be a non-empty string")
    if not isinstance(capability.get("resume_on"), str) or not capability["resume_on"]:
        raise ValueError("capabilities.await_external.resume_on must be a non-empty string")
    effective_interactive = (path_step or {}).get("interactive") or capability.get("interactive")
    if effective_interactive:
        if effective_interactive.get("kind") != "cta_url":
            raise ValueError("await_external interactive.kind must be 'cta_url'")
        if effective_interactive.get("field") != capability["resume_on"]:
            raise ValueError("await_external interactive.field must match resume_on")
        if effective_interactive.get("out_of_band", True) is not True:
            raise ValueError("await_external interactive.out_of_band cannot be false")
        next_step = effective_interactive.get("next_step")
        if next_step is not None and next_step != capability["step"]:
            raise ValueError("await_external interactive.next_step must match step")
    on_resume = capability.get("on_resume") or {}
    token_bindings = on_resume.get("set") or {}
    _validate_binding_map(
        token_bindings,
        namespace="token",
        location="capabilities.await_external.on_resume.set",
    )

    enrichment = on_resume.get("enrich")
    if enrichment is None:
        _validate_typed_resume_contract(document, capability, tools, subflows)
        return
    enrichment_definition: dict[str, Any] | None = None
    if isinstance(enrichment, str):
        if path_step is not None:
            raise ValueError("path await_external requires object-form on_resume.enrich")
        tool_name = enrichment
    else:
        if not isinstance(enrichment, dict):
            raise ValueError("await_external enrichment must be a tool name or object")
        enrichment_definition = enrichment
        tool_name_value = enrichment.get("tool")
        if not isinstance(tool_name_value, str) or not tool_name_value:
            raise ValueError("await_external enrichment.tool must be a non-empty string")
        tool_name = tool_name_value
        _validate_binding_map(
            enrichment.get("input") or {},
            namespace="token",
            location="capabilities.await_external.on_resume.enrich.input",
        )
        enrichment_bindings = enrichment.get("set") or {}
        _validate_binding_map(
            enrichment_bindings,
            namespace="result",
            location="capabilities.await_external.on_resume.enrich.set",
        )
        duplicate_writes = set(token_bindings) & set(enrichment_bindings)
        if duplicate_writes:
            duplicates = ", ".join(sorted(duplicate_writes))
            raise ValueError(f"await_external mappings write the same keys twice: {duplicates}")
    tool_definition = _registered_tool_definition(
        tools,
        tool_name,
        location="$.capabilities.await_external.on_resume.enrich.tool",
    )
    _validate_read_only_tool_effects(
        tool_definition,
        location="$.capabilities.await_external.on_resume.enrich.tool",
    )
    if enrichment_definition is None:
        _validate_typed_resume_contract(document, capability, tools, subflows)
        return
    enrichment_input = cast(dict[str, Any], enrichment_definition.get("input") or {})
    _validate_tool_inputs(
        tool_definition,
        [
            (
                parameter_name,
                f"$.capabilities.await_external.on_resume.enrich.input[{parameter_name!r}]",
            )
            for parameter_name in enrichment_input
        ],
        call_location="$.capabilities.await_external.on_resume.enrich.input",
    )
    for state_key, result_binding in cast(
        dict[str, Any], enrichment_definition.get("set") or {}
    ).items():
        if isinstance(result_binding, str) and result_binding.startswith("$result."):
            _validate_tool_result_path(
                tool_definition,
                result_binding,
                location=(f"$.capabilities.await_external.on_resume.enrich.set[{state_key!r}]"),
                allow_array_indices=True,
            )
    _validate_typed_resume_contract(document, capability, tools, subflows)


def _validate_await_external_targets(
    capability: Optional[dict[str, Any]],
    node_ids: set[str],
    await_external_node_ids: set[str],
) -> None:
    if capability is None:
        return
    bound_node_id = capability["step"]
    if bound_node_id not in node_ids:
        raise ValueError(
            f"capabilities.await_external.step does not resolve to a node: {bound_node_id!r}"
        )
    if bound_node_id not in await_external_node_ids:
        raise ValueError(
            "capabilities.await_external.step resolves to a node that does not implement "
            f"await_external: {bound_node_id!r}"
        )
    transitions = {
        "timeout": capability.get("timeout"),
        **(capability.get("recovery") or {}),
    }
    for event, transition in transitions.items():
        if transition is None:
            continue
        target = transition["goto"]
        if event == "resend" and target != bound_node_id:
            raise ValueError(
                "capabilities.await_external resend target must match its bound step: "
                f"{bound_node_id!r}"
            )
        if target != "END" and target not in node_ids:
            raise ValueError(
                f"capabilities.await_external {event} target does not resolve to a node: {target!r}"
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
    _validate_flow_tool_contracts(doc, tools, subflows)
    await_external = _bind_await_external(doc)

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
    _validate_await_external_definition(
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
            if await_external is None:  # defended by _bind_await_external
                raise ValueError("path await_external requires capabilities.await_external")
            seq.append(make_await_external_node(ctx, await_external, step))
        elif "terminal" in step:
            if terminal:
                seq.append(make_terminal_node(ctx, terminal))

    _validate_compiled_slot_references(doc, ctx)
    _validate_entry_args_schema(doc, ctx, subflows)

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
    _validate_await_external_targets(
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
