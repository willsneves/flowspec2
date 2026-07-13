"""Compile a flowspec/2 document into an executable LangGraph ``StateGraph``.

The compiler:

1. builds a :class:`~flowspec2.nodes.FlowContext` (domains, slots, config, tools)
   and the dependency indexes (transitive ``requires`` → ``dependents``,
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
from dataclasses import dataclass
from typing import Any, Callable, Final, Optional, cast

from langgraph.graph import END as LANGGRAPH_END
from langgraph.graph import StateGraph

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
from .subflows import SubflowRegistry, default_subflows
from .tools import ToolRegistry, default_tool_registry

END: Final[str] = LANGGRAPH_END


@dataclass
class CompiledFlow:
    graph: Any  # compiled langgraph
    ctx: FlowContext
    doc: dict[str, Any]
    terminal_id: Optional[str]
    entry_node_id: str


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
    readers: dict[str, list[str]] = {}
    for d in derives:
        for src in d.get("from", []):
            readers.setdefault(src, []).append(d["writes"])
    return readers


def _gate_for(step: dict[str, Any], gates: dict[str, Any]) -> Optional[dict[str, Any]]:
    if "ask_when" in step:
        return cast(dict[str, Any], step["ask_when"])
    return cast(Optional[dict[str, Any]], gates.get(step["id"]))


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


def _validate_await_external_definition(
    capability: Optional[dict[str, Any]],
    tools: ToolRegistry,
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
        return
    if isinstance(enrichment, str):
        if path_step is not None:
            raise ValueError("path await_external requires object-form on_resume.enrich")
        tool_name = enrichment
    else:
        if not isinstance(enrichment, dict):
            raise ValueError("await_external enrichment must be a tool name or object")
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
    if not tool_name or not tools.has(tool_name):
        raise ValueError(f"await_external enrichment tool is not registered: {tool_name!r}")


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


def compile_flow(
    doc: dict[str, Any],
    *,
    tools: Optional[ToolRegistry] = None,
    subflows: Optional[SubflowRegistry] = None,
    log_id_generator: Optional[SnowflakeIdGenerator] = None,
) -> CompiledFlow:
    tools = tools or default_tool_registry()
    subflows = subflows or default_subflows()
    log_id_generator = log_id_generator or default_log_id_generator()

    # Work on a private copy: the compiler annotates path steps with synthesized
    # node ids, and must never mutate the caller's document (which is re-validated
    # against an additionalProperties:false schema).
    doc = copy.deepcopy(doc)

    await_external = _bind_await_external(doc)

    slots = dict(doc.get("slots", {}))
    ctx = FlowContext(
        domains=dict(doc.get("domains", {})),
        slots=slots,
        config=dict(doc.get("config", {})),
        tools=tools,
        log_id_generator=log_id_generator,
        await_external=await_external,
    )
    await_external_path_step = next(
        (step for step in doc["path"] if step.get("await_external") is True),
        None,
    )
    _validate_await_external_definition(
        await_external,
        tools,
        await_external_path_step,
    )

    terminal = doc.get("terminal")
    terminal_id = terminal["step"] if terminal else None
    gates = (doc.get("overrides", {}) or {}).get("gates", {}) or {}
    confirm_block = doc.get("confirm")

    seq: list[NodeDesc] = []

    # init node (service seed + best-effort entry tool) is always the entry point
    seq.append(make_init_node(ctx, doc.get("entry"), dict(doc.get("service", {}))))

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

    # Main pass: build nodes in path order.
    for step in doc["path"]:
        if "use" in step:
            ref = step["use"]
            with_cfg: dict[str, Any] = next(
                (
                    cast(dict[str, Any], use.get("with", {}))
                    for use in doc.get("uses", [])
                    if use.get("ref") == ref
                ),
                {},
            )
            build = subflows.get(ref).build(ctx, with_cfg)
            seq.extend(build.descriptors)
            continue
        if "slot" in step:
            seq.append(make_collect_node(ctx, step, _gate_for(step, gates)))
        elif "confirm" in step:
            if step.get("correctable") and confirm_block:
                seq.append(make_hub_confirm_node(ctx, confirm_block, terminal_id or "terminal"))
            elif step.get("on_reject"):
                seq.append(make_summary_confirm_node(ctx, step))
            else:
                seq.append(make_bool_confirm_node(ctx, step, _gate_for(step, gates)))
        elif "derive" in step:
            derive_def = next(d for d in doc.get("derive", []) if d["writes"] == step["derive"])
            seq.append(make_derive_node(ctx, derive_def))
        elif "await_external" in step:
            if await_external is None:  # defended by _bind_await_external
                raise ValueError("path await_external requires capabilities.await_external")
            seq.append(make_await_external_node(ctx, await_external, step))
        elif "terminal" in step:
            if terminal:
                seq.append(make_terminal_node(ctx, terminal))

    # Compute dependency indexes now that subflow slots are registered.
    ctx.dependents = _transitive_dependents(ctx.slots)
    ctx.derive_readers = _derive_readers(doc.get("derive", []))

    # Insert derive[] nodes at their `after` anchor (those not already placed as path steps).
    placed = {s["derive"] for s in doc["path"] if "derive" in s}
    for derive_def in doc.get("derive", []):
        if derive_def["writes"] in placed:
            continue
        after = derive_def.get("after")
        node = make_derive_node(ctx, derive_def)
        if after is None:
            seq.append(node)
            continue
        idx = next((i for i, d in enumerate(seq) if d.id == after), None)
        if idx is None:
            seq.append(node)
        else:
            seq.insert(idx + 1, node)

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
        path_map = list({*desc.targets, next_id, END})

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
        graph=compiled, ctx=ctx, doc=doc, terminal_id=terminal_id, entry_node_id=seq[0].id
    )
