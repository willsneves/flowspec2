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
from typing import Any, Optional

from langgraph.graph import END, StateGraph

from .models import ServiceState
from .nodes import (
    NEXT,
    FlowContext,
    NodeDesc,
    make_bool_confirm_node,
    make_collect_node,
    make_derive_node,
    make_hub_confirm_node,
    make_init_node,
    make_summary_confirm_node,
    make_terminal_node,
)
from .subflows import SubflowRegistry, default_subflows
from .tools import ToolRegistry, default_tool_registry


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
        return step["ask_when"]
    return gates.get(step["id"])


def compile_flow(
    doc: dict[str, Any],
    *,
    tools: Optional[ToolRegistry] = None,
    subflows: Optional[SubflowRegistry] = None,
) -> CompiledFlow:
    tools = tools or default_tool_registry()
    subflows = subflows or default_subflows()

    # Work on a private copy: the compiler annotates path steps with synthesized
    # node ids, and must never mutate the caller's document (which is re-validated
    # against an additionalProperties:false schema).
    doc = copy.deepcopy(doc)

    slots = dict(doc.get("slots", {}))
    ctx = FlowContext(
        domains=dict(doc.get("domains", {})),
        slots=slots,
        config=dict(doc.get("config", {})),
        tools=tools,
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
        # `use` ids come from the subflow

    # Main pass: build nodes in path order.
    for step in doc["path"]:
        if "use" in step:
            ref = step["use"]
            with_cfg = next((u.get("with", {}) for u in doc.get("uses", []) if u.get("ref") == ref), {})
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

    # ── wire the StateGraph ──────────────────────────────────────────────
    graph = StateGraph(ServiceState)
    for desc in seq:
        graph.add_node(desc.id, desc.fn)
    graph.set_entry_point(seq[0].id)

    for i, desc in enumerate(seq):
        next_id = seq[i + 1].id if i + 1 < len(seq) else END
        path_map = list({*desc.targets, next_id, END})

        def make_router(router, resolved_next):  # noqa: ANN001
            def routed(state: ServiceState) -> str:
                target = router(state)
                return resolved_next if target == NEXT else target
            return routed

        graph.add_conditional_edges(desc.id, make_router(desc.router, next_id), path_map)

    compiled = graph.compile()
    return CompiledFlow(graph=compiled, ctx=ctx, doc=doc, terminal_id=terminal_id, entry_node_id=seq[0].id)
