"""Canonical normalization and a deterministic intermediate representation.

Flow documents remain the only authoring surface.  This module turns a valid
source document into an explicit, immutable compiler input: schema defaults and
node identifiers are materialized, references and state access are indexed, and
stable digests make cache/provenance decisions independent of JSON key order.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal, cast

from jsonschema import Draft202012Validator

from .profiles import FlowProfile, capability_is_requested, reference_profile
from .schema import schema, validate_flow
from .subflows import SubflowDefinition

FLOW_IR_FORMAT: Final[str] = "flowspec2/ir@1"


@dataclass(frozen=True)
class FlowReference:
    """A statically discoverable reference from one source location."""

    source: str
    namespace: Literal[
        "domain",
        "slot",
        "step",
        "subflow",
        "tool",
        "config",
        "data",
        "internal",
        "payload",
        "address",
        "token",
    ]
    target: str


@dataclass(frozen=True)
class FlowSlot:
    """The normalized persistence and validation contract for one slot."""

    name: str
    domain: str
    partition: Literal["data", "internal"]
    required: bool
    nullable: bool
    requires: tuple[str, ...]


@dataclass(frozen=True)
class FlowNode:
    """A source-level node with explicit state access."""

    identifier: str
    kind: Literal[
        "init",
        "collect",
        "confirm",
        "derive",
        "terminal",
        "subflow",
        "await_external",
    ]
    source: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]


@dataclass(frozen=True)
class FlowTransition:
    """A deterministic source-level transition in the normalized rail."""

    source: str
    target: str
    condition: Literal[
        "next",
        "confirm",
        "reject",
        "correction",
        "resume",
        "abort",
        "resend",
        "switch",
        "timeout",
        "pause",
        "end",
    ]


@dataclass(frozen=True)
class FlowIR:
    """Immutable, serializable representation consumed after source checking."""

    ir_format: str
    format: str
    flow: str
    version: str
    profile: str
    profile_digest: str
    dependency_digest: str
    source_digest: str
    digest: str
    canonical_json: str
    state_schema_json: str
    slots: tuple[FlowSlot, ...]
    nodes: tuple[FlowNode, ...]
    transitions: tuple[FlowTransition, ...]
    references: tuple[FlowReference, ...]
    required_capabilities: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        """Return a fresh mutable document for a compiler invocation."""

        return cast(dict[str, Any], json.loads(self.canonical_json))

    def state_schema(self) -> dict[str, Any]:
        """Return the generated JSON Schema for the three state partitions."""

        return cast(dict[str, Any], json.loads(self.state_schema_json))

    def to_dict(self) -> dict[str, Any]:
        """Return the complete deterministic JSON-compatible IR projection."""

        projection = {
            "ir_format": self.ir_format,
            "format": self.format,
            "flow": self.flow,
            "version": self.version,
            "profile": self.profile,
            "profile_digest": self.profile_digest,
            "dependency_digest": self.dependency_digest,
            "source_digest": self.source_digest,
            "digest": self.digest,
            "document": self.to_document(),
            "state_schema": self.state_schema(),
            "slots": [asdict(slot) for slot in self.slots],
            "nodes": [asdict(node) for node in self.nodes],
            "transitions": [asdict(transition) for transition in self.transitions],
            "references": [asdict(reference) for reference in self.references],
            "required_capabilities": list(self.required_capabilities),
        }
        return cast(dict[str, Any], json.loads(_canonical_json(projection)))


def _canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(nested_value) for nested_value in value]
    if isinstance(value, list):
        return [_mutable_json(nested_value) for nested_value in value]
    return value


def _digest(document: Any) -> str:
    return hashlib.sha256(_canonical_json(document).encode()).hexdigest()


def _resolved_dependency_contract(
    document: dict[str, Any],
    profile: FlowProfile | str,
) -> dict[str, Any]:
    referenced_tools: set[str] = set()
    if (entry := document.get("entry")) is not None:
        referenced_tools.add(cast(str, entry["tool"]))
    if (terminal := document.get("terminal")) is not None:
        referenced_tools.add(cast(str, terminal["tool"]))
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    enrichment = cast(dict[str, Any], await_definition.get("on_resume", {})).get("enrich")
    if isinstance(enrichment, str):
        referenced_tools.add(enrichment)
    elif isinstance(enrichment, dict):
        referenced_tools.add(cast(str, enrichment["tool"]))

    referenced_subflows = {
        cast(str, use_definition["ref"]) for use_definition in document.get("uses", [])
    }
    requested_capabilities = {
        capability_name
        for capability_name, capability_value in document.get("capabilities", {}).items()
        if capability_is_requested(capability_value)
    }
    if document.get("auto_flow") is not None:
        requested_capabilities.add("auto_flow")
    used_domain_types = {
        cast(str, domain_definition.get("type", "categorical"))
        for domain_definition in cast(
            dict[str, dict[str, Any]], document.get("domains", {})
        ).values()
    }

    if isinstance(profile, str):
        return {
            "identifier": profile,
            "resolved": False,
            "tools": sorted(referenced_tools),
            "subflows": sorted(referenced_subflows),
            "capabilities": sorted(requested_capabilities),
            "domain_types": sorted(used_domain_types),
        }

    for subflow_reference in referenced_subflows:
        if profile.subflows.has(subflow_reference):
            subflow_definition = profile.subflows.definition(subflow_reference)
            requested_capabilities.update(subflow_definition.capabilities)
            referenced_tools.update(subflow_definition.required_tools)
    return {
        "identifier": profile.identifier,
        "allow_legacy_contracts": profile.allow_legacy_contracts,
        "tools": {
            tool_name: (
                profile.tools.definition(tool_name).as_dict()
                if profile.tools.has(tool_name)
                else None
            )
            for tool_name in sorted(referenced_tools)
        },
        "subflows": {
            subflow_reference: (
                profile.subflows.definition(subflow_reference).as_dict()
                if profile.subflows.has(subflow_reference)
                else None
            )
            for subflow_reference in sorted(referenced_subflows)
        },
        "capabilities": {
            capability_name: capability_name in profile.capabilities
            for capability_name in sorted(requested_capabilities)
        },
        "domain_types": {
            domain_type: domain_type in profile.domain_types
            for domain_type in sorted(used_domain_types)
        },
    }


def _resolve_local_reference(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"only local schema references are supported, got {reference!r}")
    resolved: Any = root_schema
    for encoded_segment in reference[2:].split("/"):
        segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        resolved = resolved[segment]
    return cast(dict[str, Any], resolved)


def _matching_branch(
    instance: Any,
    branches: list[dict[str, Any]],
    validator: Draft202012Validator,
) -> dict[str, Any] | None:
    return next(
        (branch for branch in branches if validator.evolve(schema=branch).is_valid(instance)),
        None,
    )


def _materialize_existing_defaults(
    instance: Any,
    schema_node: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    validator: Draft202012Validator,
) -> Any:
    """Copy an instance while applying defaults only inside existing containers."""

    normalized = copy.deepcopy(instance)
    reference = schema_node.get("$ref")
    if isinstance(reference, str):
        normalized = _materialize_existing_defaults(
            normalized,
            _resolve_local_reference(root_schema, reference),
            root_schema=root_schema,
            validator=validator,
        )
        schema_node = {key: value for key, value in schema_node.items() if key != "$ref"}

    all_of = schema_node.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            normalized = _materialize_existing_defaults(
                normalized,
                cast(dict[str, Any], branch),
                root_schema=root_schema,
                validator=validator,
            )

    for union_keyword in ("oneOf", "anyOf"):
        raw_branches = schema_node.get(union_keyword)
        if isinstance(raw_branches, list):
            branches = [cast(dict[str, Any], branch) for branch in raw_branches]
            if (branch := _matching_branch(normalized, branches, validator)) is not None:
                normalized = _materialize_existing_defaults(
                    normalized,
                    branch,
                    root_schema=root_schema,
                    validator=validator,
                )
                break

    conditional = schema_node.get("if")
    if isinstance(conditional, dict):
        branch_key = "then" if validator.evolve(schema=conditional).is_valid(normalized) else "else"
        if isinstance(branch := schema_node.get(branch_key), dict):
            normalized = _materialize_existing_defaults(
                normalized,
                branch,
                root_schema=root_schema,
                validator=validator,
            )

    if isinstance(normalized, dict):
        properties = cast(dict[str, dict[str, Any]], schema_node.get("properties", {}))
        for property_name, property_schema in properties.items():
            if property_name in normalized:
                normalized[property_name] = _materialize_existing_defaults(
                    normalized[property_name],
                    property_schema,
                    root_schema=root_schema,
                    validator=validator,
                )
            elif "default" in property_schema:
                normalized[property_name] = copy.deepcopy(property_schema["default"])

        additional_schema = schema_node.get("additionalProperties")
        if isinstance(additional_schema, dict):
            for property_name in normalized.keys() - properties.keys():
                normalized[property_name] = _materialize_existing_defaults(
                    normalized[property_name],
                    additional_schema,
                    root_schema=root_schema,
                    validator=validator,
                )
    elif isinstance(normalized, list) and isinstance(schema_node.get("items"), dict):
        item_schema = cast(dict[str, Any], schema_node["items"])
        normalized = [
            _materialize_existing_defaults(
                item,
                item_schema,
                root_schema=root_schema,
                validator=validator,
            )
            for item in normalized
        ]

    return normalized


def _materialize_container(
    container: dict[str, Any],
    key: str,
    schema_node: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    validator: Draft202012Validator,
) -> None:
    current = container.setdefault(key, {})
    container[key] = _materialize_existing_defaults(
        current,
        schema_node,
        root_schema=root_schema,
        validator=validator,
    )


def _materialize_effective_containers(
    document: dict[str, Any],
    root_schema: dict[str, Any],
    validator: Draft202012Validator,
) -> dict[str, Any]:
    top_properties = cast(dict[str, dict[str, Any]], root_schema["properties"])
    effective_containers = ["config"]
    if "service" in document:
        effective_containers.append("service")
    for key in effective_containers:
        _materialize_container(
            document,
            key,
            top_properties[key],
            root_schema=root_schema,
            validator=validator,
        )

    domain_schema = cast(dict[str, Any], top_properties["domains"]["additionalProperties"])
    domain_branches = cast(list[dict[str, Any]], domain_schema["oneOf"])
    for domain_definition in document.get("domains", {}).values():
        domain_branch = _matching_branch(domain_definition, domain_branches, validator)
        if domain_branch is None:
            raise ValueError("normalized domain does not match a closed domain-kind contract")
        domain_properties = cast(dict[str, dict[str, Any]], domain_branch["properties"])
        normalize_schema = domain_properties.get("normalize")
        if normalize_schema is None:
            continue
        _materialize_container(
            domain_definition,
            "normalize",
            normalize_schema,
            root_schema=root_schema,
            validator=validator,
        )

    if (terminal := document.get("terminal")) is not None:
        terminal_schema = _resolve_local_reference(root_schema, "#/$defs/terminalDef")
        terminal_properties = cast(dict[str, dict[str, Any]], terminal_schema["properties"])
        _materialize_container(
            terminal,
            "outcomes",
            terminal_properties["outcomes"],
            root_schema=root_schema,
            validator=validator,
        )
        outcomes = cast(dict[str, Any], terminal["outcomes"])
        outcome_properties = cast(
            dict[str, dict[str, Any]], terminal_properties["outcomes"]["properties"]
        )
        for outcome in ("success", "retryable", "fatal"):
            _materialize_container(
                outcomes,
                outcome,
                outcome_properties[outcome],
                root_schema=root_schema,
                validator=validator,
            )
        _materialize_container(
            terminal,
            "empty_payload",
            terminal_properties["empty_payload"],
            root_schema=root_schema,
            validator=validator,
        )

    return document


def _materialize_node_identifiers(document: dict[str, Any]) -> None:
    await_definition = cast(
        dict[str, Any] | None,
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external"),
    )
    for path_step in document["path"]:
        if "slot" in path_step:
            path_step.setdefault("step", path_step["slot"])
        elif "confirm" in path_step:
            path_step.setdefault("step", path_step["confirm"])
        elif "derive" in path_step:
            path_step.setdefault("step", f"derive_{path_step['derive']}")
        elif "await_external" in path_step:
            resolved_identifier = path_step.get("step")
            if resolved_identifier is None and await_definition is not None:
                resolved_identifier = await_definition.get("step")
            resolved_identifier = resolved_identifier or "await_external"
            path_step["step"] = resolved_identifier
            if await_definition is not None:
                await_definition.setdefault("step", resolved_identifier)


def normalize_flow(document: dict[str, Any], *, validate: bool = True) -> dict[str, Any]:
    """Return a schema-valid canonical copy with effective defaults and node ids."""

    if validate:
        validate_flow(document)
    root_schema = schema()
    validator = Draft202012Validator(root_schema)
    normalized = cast(
        dict[str, Any],
        _materialize_existing_defaults(
            document,
            root_schema,
            root_schema=root_schema,
            validator=validator,
        ),
    )
    normalized = _materialize_effective_containers(normalized, root_schema, validator)
    _materialize_node_identifiers(normalized)
    if validate:
        validate_flow(normalized)
    return cast(dict[str, Any], json.loads(_canonical_json(normalized)))


def _escape_pointer_segment(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _predicate_references(predicate: Any) -> tuple[str, ...]:
    if not isinstance(predicate, dict) or len(predicate) != 1:
        return ()
    operator, operand = next(iter(predicate.items()))
    candidates: list[Any]
    if operator in {"eq", "ne"} and isinstance(operand, list):
        candidates = operand
    elif operator == "in" and isinstance(operand, list):
        candidates = operand[:1]
    elif operator == "is_present":
        candidates = [operand]
    elif operator in {"and", "or"} and isinstance(operand, list):
        return tuple(
            dict.fromkeys(
                reference
                for nested_predicate in operand
                for reference in _predicate_references(nested_predicate)
            )
        )
    elif operator == "not":
        return _predicate_references(operand)
    else:
        candidates = []
    return tuple(
        candidate
        for candidate in candidates
        if isinstance(candidate, str)
        and candidate.split(".", 1)[0] in {"slots", "internal", "payload", "config", "address"}
        and "." in candidate
    )


def _state_reference(slot_name: str, slots: Mapping[str, Any]) -> str:
    slot_definition = cast(dict[str, Any], slots.get(slot_name, {}))
    partition = slot_definition.get("persist", "data")
    return f"{partition}.{slot_name}"


def _canonical_state_reference(reference: str, slots: dict[str, Any]) -> str:
    namespace, state_key = reference.split(".", 1)
    if namespace == "slots":
        return _state_reference(state_key, slots)
    if namespace == "address":
        return f"data.address.{state_key}"
    return reference


def _step_kind(path_step: dict[str, Any]) -> str:
    return next(
        kind
        for kind in ("slot", "confirm", "derive", "terminal", "use", "await_external")
        if kind in path_step
    )


def _path_predicate_reads(
    path_step: dict[str, Any],
    document: dict[str, Any],
) -> tuple[str, ...]:
    gates = cast(
        dict[str, Any], cast(dict[str, Any], document.get("overrides", {})).get("gates", {})
    )
    step_identifier = cast(str, path_step.get("step", ""))
    interactive = cast(dict[str, Any], path_step.get("interactive", {}))
    option_predicates = tuple(
        conditional_option.get("gate") for conditional_option in interactive.get("options_when", [])
    )
    confirmation = cast(dict[str, Any], document.get("confirm", {}))
    confirmation_interactive = (
        cast(dict[str, Any], confirmation.get("interactive", {}))
        if path_step.get("correctable") is True
        else {}
    )
    confirmation_option_predicates = tuple(
        conditional_option.get("gate")
        for conditional_option in confirmation_interactive.get("options_when", [])
    )
    predicates = (
        path_step.get("ask_when"),
        path_step.get("skip_when"),
        gates.get(step_identifier),
        *option_predicates,
        *confirmation_option_predicates,
    )
    slots = cast(dict[str, Any], document.get("slots", {}))
    return tuple(
        dict.fromkeys(
            _canonical_state_reference(reference, slots)
            for predicate in predicates
            for reference in _predicate_references(predicate)
        )
    )


def _state_writes_for_mapping(
    mapping: Mapping[str, Any],
    slots: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(_state_reference(state_key, slots) for state_key in mapping)


def _await_state_access(document: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    if not await_definition:
        return (), ()
    slots = cast(dict[str, Any], document.get("slots", {}))
    resume_on = cast(str, await_definition.get("resume_on", ""))
    reads = tuple(
        reference
        for reference in (f"payload.{resume_on}" if resume_on else "", "payload._external_event")
        if reference
    )
    on_resume = cast(dict[str, Any], await_definition.get("on_resume", {}))
    writes = list(_state_writes_for_mapping(cast(dict[str, Any], on_resume.get("set", {})), slots))
    enrichment = on_resume.get("enrich")
    if isinstance(enrichment, dict):
        writes.extend(
            _state_writes_for_mapping(
                cast(dict[str, Any], enrichment.get("set", {})),
                slots,
            )
        )
    for transition in (
        await_definition.get("timeout"),
        *cast(dict[str, Any], await_definition.get("recovery", {})).values(),
    ):
        if isinstance(transition, dict):
            writes.extend(
                _state_writes_for_mapping(
                    cast(dict[str, Any], transition.get("set", {})),
                    slots,
                )
            )
    return tuple(dict.fromkeys(reads)), tuple(dict.fromkeys(writes))


def _node_for_step(
    path_step: dict[str, Any],
    path_index: int,
    document: dict[str, Any],
    subflow_definitions: Mapping[str, SubflowDefinition],
) -> FlowNode:
    step_kind = _step_kind(path_step)
    source = f"/path/{path_index}"
    slots = cast(dict[str, Any], document.get("slots", {}))
    predicate_reads = _path_predicate_reads(path_step, document)
    if step_kind == "slot":
        slot_name = cast(str, path_step["slot"])
        slot_definition = cast(dict[str, Any], slots.get(slot_name, {}))
        dependency_reads = tuple(
            _state_reference(required_slot, slots)
            for required_slot in slot_definition.get("requires", [])
        )
        return FlowNode(
            cast(str, path_step["step"]),
            "collect",
            source,
            tuple(dict.fromkeys((*predicate_reads, *dependency_reads))),
            (_state_reference(slot_name, slots),),
        )
    if step_kind == "confirm":
        slot_name = cast(str, path_step["confirm"])
        return FlowNode(
            cast(str, path_step["step"]),
            "confirm",
            source,
            predicate_reads,
            (_state_reference(slot_name, slots),),
        )
    if step_kind == "derive":
        writes = cast(str, path_step["derive"])
        derive_definition = next(
            (
                cast(dict[str, Any], candidate)
                for candidate in document.get("derive", [])
                if candidate.get("writes") == writes
            ),
            {},
        )
        return FlowNode(
            cast(str, path_step["step"]),
            "derive",
            source,
            tuple(
                _state_reference(source_slot, slots)
                for source_slot in derive_definition.get("from", [])
            ),
            (_state_reference(writes, slots),),
        )
    if step_kind == "terminal":
        terminal = cast(dict[str, Any], document.get("terminal", {}))
        success_writes = cast(
            dict[str, Any],
            cast(dict[str, Any], terminal.get("outcomes", {})).get("success", {}).get("set", {}),
        )
        return FlowNode(
            cast(str, terminal.get("step", "terminal")),
            "terminal",
            source,
            tuple(
                _state_reference(binding["slot"], slots) for binding in terminal.get("input", [])
            ),
            tuple(
                dict.fromkeys(
                    _state_reference(state_key, slots)
                    for state_key in (*terminal.get("outputs", {}), *success_writes)
                )
            ),
        )
    if step_kind == "use":
        reference = cast(str, path_step["use"])
        definition = subflow_definitions.get(reference)
        owned_state_access = (
            tuple(
                f"{state_contract.partition}.{state_key}"
                for state_key, state_contract in sorted(definition.owned_state_keys.items())
            )
            if definition is not None
            else ()
        )
        await_reads: tuple[str, ...] = ()
        await_writes: tuple[str, ...] = ()
        await_definition = cast(
            dict[str, Any],
            cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
        )
        if definition is not None and await_definition.get("step") in definition.node_identifiers:
            await_reads, await_writes = _await_state_access(document)
        return FlowNode(
            f"use:{path_index}:{reference}",
            "subflow",
            source,
            tuple(dict.fromkeys((*owned_state_access, *await_reads))),
            tuple(dict.fromkeys((*owned_state_access, *await_writes))),
        )
    await_reads, await_writes = _await_state_access(document)
    return FlowNode(
        cast(str, path_step["step"]),
        "await_external",
        source,
        await_reads,
        await_writes,
    )


def _unplaced_derive_nodes(document: dict[str, Any]) -> tuple[FlowNode, ...]:
    placed = {path_step["derive"] for path_step in document["path"] if "derive" in path_step}
    slots = cast(dict[str, Any], document.get("slots", {}))
    return tuple(
        FlowNode(
            f"derive_{derive_definition['writes']}",
            "derive",
            f"/derive/{derive_index}",
            tuple(_state_reference(source, slots) for source in derive_definition["from"]),
            (_state_reference(derive_definition["writes"], slots),),
        )
        for derive_index, derive_definition in enumerate(document.get("derive", []))
        if derive_definition["writes"] not in placed
    )


def _ordered_nodes(
    document: dict[str, Any],
    subflow_definitions: Mapping[str, SubflowDefinition],
) -> tuple[FlowNode, ...]:
    path_nodes = [
        _node_for_step(path_step, path_index, document, subflow_definitions)
        for path_index, path_step in enumerate(document["path"])
    ]
    last_inserted_for_anchor: dict[str, str] = {}
    for derive_node in _unplaced_derive_nodes(document):
        derive_definition = document["derive"][int(derive_node.source.rsplit("/", 1)[1])]
        anchor = derive_definition.get("after")
        if anchor is None:
            raise ValueError(
                f"{derive_node.source} must be placed in path or declare an after anchor"
            )
        effective_anchor = last_inserted_for_anchor.get(anchor, anchor)
        anchor_index = next(
            (
                node_index
                for node_index, candidate in enumerate(path_nodes)
                if candidate.identifier == effective_anchor
            ),
            None,
        )
        if anchor_index is None:
            raise ValueError(
                f"{derive_node.source}/after does not resolve to a normalized node: {anchor!r}"
            )
        path_nodes.insert(anchor_index + 1, derive_node)
        last_inserted_for_anchor[anchor] = derive_node.identifier
    entry = cast(dict[str, Any], document.get("entry", {}))
    init_writes = ["data.service"] if document.get("service") else []
    if entry.get("writes"):
        init_writes.append(f"data.{entry['writes']}")
    init_writes.append("internal._entry_done")
    return (
        FlowNode("__init__", "init", "", (), tuple(dict.fromkeys(init_writes))),
        *path_nodes,
    )


def _transitions(
    nodes: tuple[FlowNode, ...], document: dict[str, Any]
) -> tuple[FlowTransition, ...]:
    transitions: list[FlowTransition] = []
    path_slot_nodes = {
        path_step.get("slot") or path_step.get("confirm"): path_step.get("step")
        for path_step in document["path"]
        if "slot" in path_step or "confirm" in path_step
    }
    confirmation = cast(dict[str, Any], document.get("confirm", {}))
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    for node_index, node in enumerate(nodes):
        next_identifier = (
            nodes[node_index + 1].identifier if node_index + 1 < len(nodes) else "$end"
        )
        if node.kind == "terminal":
            transitions.append(FlowTransition(node.identifier, "$end", "end"))
            continue
        if node.kind == "await_external":
            transitions.extend(
                (
                    FlowTransition(node.identifier, next_identifier, "resume"),
                    FlowTransition(node.identifier, "$end", "pause"),
                )
            )
            configured_transitions = {
                "timeout": await_definition.get("timeout"),
                **cast(dict[str, Any], await_definition.get("recovery", {})),
            }
            for event_name, transition in configured_transitions.items():
                if not isinstance(transition, dict):
                    continue
                transition_target = cast(str, transition["goto"])
                transitions.append(
                    FlowTransition(
                        node.identifier,
                        "$end" if transition_target == "END" else transition_target,
                        cast(Any, event_name),
                    )
                )
            continue
        if node.source and node.kind == "confirm":
            path_index = int(node.source.rsplit("/", 1)[1])
            path_step = cast(dict[str, Any], document["path"][path_index])
            if path_step.get("correctable") is True and confirmation:
                confirmation_target = cast(
                    str,
                    confirmation.get("on_confirm", next_identifier),
                )
                transitions.append(FlowTransition(node.identifier, confirmation_target, "confirm"))
                for correctable_slot in confirmation.get("correctable", []):
                    target = path_slot_nodes.get(correctable_slot)
                    transitions.append(
                        FlowTransition(
                            node.identifier,
                            cast(str, target or f"$slot:{correctable_slot}"),
                            "correction",
                        )
                    )
                transitions.append(FlowTransition(node.identifier, "$end", "pause"))
                continue
            transitions.append(FlowTransition(node.identifier, next_identifier, "confirm"))
            transitions.append(FlowTransition(node.identifier, "$end", "pause"))
            if "on_reject" in path_step:
                transitions.append(FlowTransition(node.identifier, "$end", "reject"))
            continue
        transitions.append(FlowTransition(node.identifier, next_identifier, "next"))
        if node.kind == "collect":
            transitions.append(FlowTransition(node.identifier, "$end", "pause"))
    return tuple(transitions)


def _validate_reachability(
    nodes: tuple[FlowNode, ...],
    transitions: tuple[FlowTransition, ...],
) -> None:
    if not nodes:
        raise ValueError("normalized flow must contain an init node")
    node_identifiers = {node.identifier for node in nodes}
    outgoing: dict[str, set[str]] = {}
    for transition in transitions:
        if transition.target in node_identifiers:
            outgoing.setdefault(transition.source, set()).add(transition.target)
    reachable = {nodes[0].identifier}
    pending = [nodes[0].identifier]
    while pending:
        source = pending.pop()
        for target in outgoing.get(source, set()):
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    if unreachable := sorted(node_identifiers - reachable):
        raise ValueError(f"normalized flow contains unreachable nodes: {', '.join(unreachable)}")


def _references(document: dict[str, Any]) -> tuple[FlowReference, ...]:
    references: list[FlowReference] = []

    def append_predicate_references(source: str, predicate: Any) -> None:
        for predicate_reference in _predicate_references(predicate):
            predicate_namespace, predicate_target = predicate_reference.split(".", 1)
            normalized_namespace = "data" if predicate_namespace == "slots" else predicate_namespace
            references.append(
                FlowReference(
                    source,
                    cast(Any, normalized_namespace),
                    predicate_target,
                )
            )

    for slot_name, slot_definition in document.get("slots", {}).items():
        escaped_slot = _escape_pointer_segment(slot_name)
        references.append(
            FlowReference(f"/slots/{escaped_slot}/domain", "domain", slot_definition["domain"])
        )
        references.extend(
            FlowReference(
                f"/slots/{escaped_slot}/requires/{requirement_index}",
                "slot",
                required_slot,
            )
            for requirement_index, required_slot in enumerate(slot_definition.get("requires", []))
        )
    for path_index, path_step in enumerate(document["path"]):
        step_kind = _step_kind(path_step)
        if step_kind in {"slot", "confirm"}:
            references.append(
                FlowReference(
                    f"/path/{path_index}/{step_kind}",
                    "slot",
                    cast(str, path_step[step_kind]),
                )
            )
        elif step_kind == "derive":
            references.append(
                FlowReference(
                    f"/path/{path_index}/derive",
                    "slot",
                    cast(str, path_step["derive"]),
                )
            )
        elif step_kind == "use":
            references.append(
                FlowReference(
                    f"/path/{path_index}/use",
                    "subflow",
                    cast(str, path_step["use"]),
                )
            )
        interactive = cast(dict[str, Any], path_step.get("interactive", {}))
        if (domain_name := interactive.get("from_domain")) is not None:
            references.append(
                FlowReference(
                    f"/path/{path_index}/interactive/from_domain",
                    "domain",
                    cast(str, domain_name),
                )
            )
        for option_index, conditional_option in enumerate(interactive.get("options_when", [])):
            append_predicate_references(
                f"/path/{path_index}/interactive/options_when/{option_index}/gate",
                conditional_option.get("gate"),
            )
        for predicate_key in ("ask_when", "skip_when"):
            append_predicate_references(
                f"/path/{path_index}/{predicate_key}",
                path_step.get(predicate_key),
            )
    references.extend(
        FlowReference(f"/uses/{use_index}/ref", "subflow", use_definition["ref"])
        for use_index, use_definition in enumerate(document.get("uses", []))
    )
    for derive_index, derive_definition in enumerate(document.get("derive", [])):
        references.extend(
            FlowReference(
                f"/derive/{derive_index}/from/{source_index}",
                "slot",
                source_slot,
            )
            for source_index, source_slot in enumerate(derive_definition["from"])
        )
        if (anchor := derive_definition.get("after")) is not None:
            references.append(FlowReference(f"/derive/{derive_index}/after", "step", anchor))
    if (entry := document.get("entry")) is not None:
        references.append(FlowReference("/entry/tool", "tool", entry["tool"]))
    if (terminal := document.get("terminal")) is not None:
        references.append(FlowReference("/terminal/tool", "tool", terminal["tool"]))
        references.extend(
            FlowReference(
                f"/terminal/input/{binding_index}/slot",
                "slot",
                binding["slot"],
            )
            for binding_index, binding in enumerate(terminal.get("input", []))
        )
    if (confirmation := document.get("confirm")) is not None:
        references.append(FlowReference("/confirm/slot", "slot", confirmation["slot"]))
        references.extend(
            FlowReference(
                f"/confirm/correctable/{correctable_index}",
                "slot",
                correctable_slot,
            )
            for correctable_index, correctable_slot in enumerate(
                confirmation.get("correctable", [])
            )
        )
        if (confirmation_target := confirmation.get("on_confirm")) is not None:
            references.append(FlowReference("/confirm/on_confirm", "step", confirmation_target))
        confirmation_interactive = cast(dict[str, Any], confirmation.get("interactive", {}))
        if (confirmation_domain := confirmation_interactive.get("from_domain")) is not None:
            references.append(
                FlowReference(
                    "/confirm/interactive/from_domain",
                    "domain",
                    cast(str, confirmation_domain),
                )
            )
        for option_index, conditional_option in enumerate(
            confirmation_interactive.get("options_when", [])
        ):
            append_predicate_references(
                f"/confirm/interactive/options_when/{option_index}/gate",
                conditional_option.get("gate"),
            )
    gates = cast(
        dict[str, Any], cast(dict[str, Any], document.get("overrides", {})).get("gates", {})
    )
    for gate_step, gate_predicate in gates.items():
        escaped_step = _escape_pointer_segment(gate_step)
        references.append(FlowReference(f"/overrides/gates/{escaped_step}", "step", gate_step))
        append_predicate_references(
            f"/overrides/gates/{escaped_step}",
            gate_predicate,
        )
    if (auto_flow := document.get("auto_flow")) is not None:
        if (resume_target := auto_flow.get("resume_at")) is not None:
            references.append(FlowReference("/auto_flow/resume_at", "step", resume_target))
        references.extend(
            FlowReference(
                f"/auto_flow/prefill_from/{prefill_index}",
                "slot",
                prefill_slot,
            )
            for prefill_index, prefill_slot in enumerate(auto_flow.get("prefill_from", []))
        )
        append_predicate_references("/auto_flow/send_when", auto_flow.get("send_when"))
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    if await_definition and (await_step := await_definition.get("step")) is not None:
        references.append(
            FlowReference(
                "/capabilities/await_external/step",
                "step",
                cast(str, await_step),
            )
        )
        resume_contract = cast(dict[str, Any], await_definition.get("resume", {}))
        if correlation_reference := resume_contract.get("correlation"):
            references.append(
                FlowReference(
                    "/capabilities/await_external/resume/correlation",
                    "token",
                    cast(str, correlation_reference),
                )
            )
        on_resume = cast(dict[str, Any], await_definition.get("on_resume", {}))
        for state_key, token_binding in cast(dict[str, Any], on_resume.get("set", {})).items():
            if isinstance(token_binding, str) and token_binding.startswith("$token."):
                references.append(
                    FlowReference(
                        "/capabilities/await_external/on_resume/set/"
                        + _escape_pointer_segment(state_key),
                        "token",
                        token_binding,
                    )
                )
        enrichment = on_resume.get("enrich")
        if isinstance(enrichment, str):
            references.append(
                FlowReference(
                    "/capabilities/await_external/on_resume/enrich",
                    "tool",
                    enrichment,
                )
            )
        elif isinstance(enrichment, dict):
            references.append(
                FlowReference(
                    "/capabilities/await_external/on_resume/enrich/tool",
                    "tool",
                    enrichment["tool"],
                )
            )
            for parameter_name, token_binding in cast(
                dict[str, Any], enrichment.get("input", {})
            ).items():
                if isinstance(token_binding, str) and token_binding.startswith("$token."):
                    references.append(
                        FlowReference(
                            "/capabilities/await_external/on_resume/enrich/input/"
                            + _escape_pointer_segment(parameter_name),
                            "token",
                            token_binding,
                        )
                    )
        configured_transitions = {
            "timeout": await_definition.get("timeout"),
            **cast(dict[str, Any], await_definition.get("recovery", {})),
        }
        for event_name, transition in configured_transitions.items():
            if isinstance(transition, dict) and transition.get("goto") != "END":
                references.append(
                    FlowReference(
                        f"/capabilities/await_external/{event_name}/goto",
                        "step",
                        transition["goto"],
                    )
                )
    return tuple(references)


def _domain_state_schema(domain_definition: dict[str, Any], nullable: bool) -> dict[str, Any]:
    domain_type = domain_definition["type"]
    if domain_type == "categorical":
        allowed_values: list[Any] = [
            value for value in domain_definition.get("values", []) if value is not None or nullable
        ]
        if nullable and None not in allowed_values:
            allowed_values.append(None)
        return {"enum": allowed_values}
    if domain_type == "bool":
        return {"type": ["boolean", "null"] if nullable else "boolean"}
    if domain_type in {"integer", "number"}:
        numeric_schema: dict[str, Any] = {
            "type": [domain_type, "null"] if nullable else domain_type
        }
        for constraint in ("minimum", "maximum"):
            if constraint in domain_definition:
                numeric_schema[constraint] = domain_definition[constraint]
        return numeric_schema
    state_type: str | list[str] = ["string", "null"] if nullable else "string"
    state_schema: dict[str, Any] = {"type": state_type}
    if domain_type == "cpf":
        state_schema["pattern"] = r"^[0-9]{11}$"
    elif domain_type == "email":
        state_schema["format"] = "email"
        state_schema["pattern"] = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    elif domain_type == "name":
        state_schema["minLength"] = 2
    elif domain_type == "free_text" and not domain_definition.get("optional"):
        state_schema["minLength"] = 1
    return state_schema


def _embedded_subflow_state_schema(
    definition: SubflowDefinition,
    state_key: str,
) -> dict[str, Any]:
    """Embed a subflow state contract as its own JSON Schema resource."""

    state_schema = cast(
        dict[str, Any],
        _mutable_json(definition.owned_state_keys[state_key].schema),
    )
    state_schema.setdefault(
        "$id",
        f"urn:flowspec2:subflow-state:{definition.ref}:{state_key}",
    )
    return state_schema


def _state_schema(
    document: dict[str, Any],
    subflow_definitions: Mapping[str, SubflowDefinition],
) -> dict[str, Any]:
    partitions: dict[str, dict[str, Any]] = {
        partition: {"type": "object", "properties": {}, "additionalProperties": True}
        for partition in ("data", "internal", "payload")
    }
    domains = cast(dict[str, dict[str, Any]], document.get("domains", {}))
    for slot_name, slot_definition in document.get("slots", {}).items():
        partition = cast(str, slot_definition["persist"])
        domain_definition = domains.get(slot_definition["domain"])
        if domain_definition is None:
            continue
        partitions[partition]["properties"][slot_name] = _domain_state_schema(
            domain_definition,
            cast(bool, slot_definition["nullable"]),
        )

    data_properties = cast(dict[str, Any], partitions["data"]["properties"])
    internal_properties = cast(dict[str, Any], partitions["internal"]["properties"])
    for use_definition in document.get("uses", []):
        definition = subflow_definitions.get(use_definition["ref"])
        if definition is None:
            continue
        for state_key, state_contract in definition.owned_state_keys.items():
            partition_properties = cast(
                dict[str, Any], partitions[state_contract.partition]["properties"]
            )
            partition_properties.setdefault(
                state_key,
                _embedded_subflow_state_schema(definition, state_key),
            )
    if service_definition := document.get("service"):
        data_properties.setdefault("service", {"const": copy.deepcopy(service_definition)})
    if (entry := document.get("entry")) is not None:
        if entry.get("writes"):
            data_properties.setdefault(entry["writes"], {})
        internal_properties.setdefault("_entry_done", {"type": "boolean"})
    for derive_definition in document.get("derive", []):
        writes = cast(str, derive_definition["writes"])
        if writes in document.get("slots", {}):
            continue
        possible_values = list(derive_definition.get("lookup", {}).values())
        literal_default = derive_definition.get("default")
        source_default_schema: dict[str, Any] | None = None
        if isinstance(literal_default, str) and (
            default_match := re.fullmatch(r"\$from\[(\d+)\]", literal_default)
        ):
            source_index = int(default_match.group(1))
            derive_sources = cast(list[str], derive_definition["from"])
            if source_index < len(derive_sources):
                source_name = derive_sources[source_index]
                source_slot = cast(
                    dict[str, Any] | None,
                    cast(dict[str, Any], document.get("slots", {})).get(source_name),
                )
                if source_slot is not None:
                    source_partition = cast(str, source_slot["persist"])
                    source_default_schema = copy.deepcopy(
                        cast(dict[str, Any], partitions[source_partition]["properties"]).get(
                            source_name,
                            {},
                        )
                    )
                else:
                    source_default_schema = copy.deepcopy(data_properties.get(source_name, {}))
        if literal_default is not None and not (
            isinstance(literal_default, str) and literal_default.startswith("$from[")
        ):
            possible_values.append(literal_default)
        unique_values = list(dict.fromkeys(_canonical_json(value) for value in possible_values))
        enumerated_schema: dict[str, Any] | None = (
            {"enum": [json.loads(serialized_value) for serialized_value in unique_values]}
            if unique_values
            else None
        )
        if source_default_schema == {}:
            derived_state_schema: dict[str, Any] = {}
        elif source_default_schema is not None and enumerated_schema is not None:
            if set(source_default_schema) == {"enum"}:
                combined_values = [
                    *cast(list[Any], enumerated_schema["enum"]),
                    *cast(list[Any], source_default_schema["enum"]),
                ]
                combined_serialized = dict.fromkeys(
                    _canonical_json(value) for value in combined_values
                )
                derived_state_schema = {
                    "enum": [json.loads(value) for value in combined_serialized]
                }
            else:
                derived_state_schema = {"anyOf": [enumerated_schema, source_default_schema]}
        elif source_default_schema is not None:
            derived_state_schema = source_default_schema
        else:
            derived_state_schema = enumerated_schema or {}
        data_properties.setdefault(
            writes,
            derived_state_schema,
        )
    if (terminal := document.get("terminal")) is not None:
        for output_key in terminal.get("outputs", {}):
            data_properties.setdefault(output_key, {})
        for state_key, state_value in (
            cast(dict[str, Any], terminal.get("outcomes", {})).get("success", {}).get("set", {})
        ).items():
            data_properties.setdefault(state_key, {"const": state_value})
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], document.get("capabilities", {})).get("await_external", {}),
    )
    if await_definition:
        slots = cast(dict[str, Any], document.get("slots", {}))

        def set_await_state_schema(state_key: str, state_value_schema: dict[str, Any]) -> None:
            slot_definition = cast(dict[str, Any] | None, slots.get(state_key))
            partition_name = (
                cast(str, slot_definition["persist"]) if slot_definition is not None else "data"
            )
            partition_properties = cast(dict[str, Any], partitions[partition_name]["properties"])
            partition_properties.setdefault(
                state_key,
                state_value_schema if slot_definition is not None else {},
            )

        on_resume = cast(dict[str, Any], await_definition.get("on_resume", {}))
        for state_key, binding in cast(dict[str, Any], on_resume.get("set", {})).items():
            set_await_state_schema(
                state_key,
                {} if isinstance(binding, str) and binding.startswith("$") else {"const": binding},
            )
        enrichment = on_resume.get("enrich")
        if isinstance(enrichment, dict):
            for state_key in cast(dict[str, Any], enrichment.get("set", {})):
                set_await_state_schema(state_key, {})
        for transition in (
            await_definition.get("timeout"),
            *cast(dict[str, Any], await_definition.get("recovery", {})).values(),
        ):
            if not isinstance(transition, dict):
                continue
            for state_key, state_value in cast(dict[str, Any], transition.get("set", {})).items():
                set_await_state_schema(state_key, {"const": state_value})
    data_properties.setdefault("_reset_on_next_call", {"type": "boolean"})
    internal_properties.setdefault("_flowspec2_flow_finished", {"type": "boolean"})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["data", "internal", "payload"],
        "properties": partitions,
    }


def build_flow_ir(
    document: dict[str, Any],
    *,
    profile: FlowProfile | str | None = None,
    validate: bool = True,
) -> FlowIR:
    """Normalize a source document and index its deterministic contracts."""

    effective_profile: FlowProfile | str = reference_profile() if profile is None else profile
    profile_identifier = (
        effective_profile.identifier
        if isinstance(effective_profile, FlowProfile)
        else effective_profile
    )
    if not profile_identifier.strip():
        raise ValueError("profile identifier must be non-empty")
    subflow_definitions: Mapping[str, SubflowDefinition] = (
        effective_profile.subflows.definitions if isinstance(effective_profile, FlowProfile) else {}
    )
    normalized = normalize_flow(document, validate=validate)
    profile_contract = (
        effective_profile.as_dict()
        if isinstance(effective_profile, FlowProfile)
        else {"identifier": profile_identifier, "resolved": False}
    )
    profile_digest = (
        effective_profile.digest
        if isinstance(effective_profile, FlowProfile)
        else _digest(profile_contract)
    )
    dependency_digest = _digest(_resolved_dependency_contract(normalized, effective_profile))
    slots = tuple(
        FlowSlot(
            name=slot_name,
            domain=slot_definition["domain"],
            partition=slot_definition["persist"],
            required=slot_definition["required"],
            nullable=slot_definition["nullable"],
            requires=tuple(slot_definition.get("requires", [])),
        )
        for slot_name, slot_definition in sorted(normalized.get("slots", {}).items())
    )
    capabilities = {
        *(
            f"capability:{name}"
            for name, capability_value in normalized.get("capabilities", {}).items()
            if capability_is_requested(capability_value)
        ),
        *(f"subflow:{use['ref']}" for use in normalized.get("uses", [])),
    }
    if normalized.get("auto_flow") is not None:
        capabilities.add("capability:auto_flow")
    for use_definition in normalized.get("uses", []):
        subflow_definition = subflow_definitions.get(use_definition["ref"])
        if subflow_definition is not None:
            capabilities.update(
                f"capability:{capability}" for capability in subflow_definition.capabilities
            )
            capabilities.update(
                f"tool:{tool_name}" for tool_name in subflow_definition.required_tools
            )
    if (entry := normalized.get("entry")) is not None:
        capabilities.add(f"tool:{entry['tool']}")
    if (terminal := normalized.get("terminal")) is not None:
        capabilities.add(f"tool:{terminal['tool']}")
    await_definition = cast(
        dict[str, Any],
        cast(dict[str, Any], normalized.get("capabilities", {})).get("await_external", {}),
    )
    enrichment = cast(dict[str, Any], await_definition.get("on_resume", {})).get("enrich")
    if isinstance(enrichment, str):
        capabilities.add(f"tool:{enrichment}")
    elif isinstance(enrichment, dict):
        capabilities.add(f"tool:{enrichment['tool']}")
    canonical_json = _canonical_json(normalized)
    nodes = _ordered_nodes(normalized, subflow_definitions)
    transitions = _transitions(nodes, normalized)
    _validate_reachability(nodes, transitions)
    return FlowIR(
        ir_format=FLOW_IR_FORMAT,
        format=normalized["schema"],
        flow=normalized["flow"],
        version=normalized["version"],
        profile=profile_identifier,
        profile_digest=profile_digest,
        dependency_digest=dependency_digest,
        source_digest=_digest(document),
        digest=_digest(
            {
                "ir_format": FLOW_IR_FORMAT,
                "dependency_digest": dependency_digest,
                "document": normalized,
            }
        ),
        canonical_json=canonical_json,
        state_schema_json=_canonical_json(_state_schema(normalized, subflow_definitions)),
        slots=slots,
        nodes=nodes,
        transitions=transitions,
        references=_references(normalized),
        required_capabilities=tuple(sorted(capabilities)),
    )
