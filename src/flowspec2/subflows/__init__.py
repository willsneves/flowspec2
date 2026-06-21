"""Versioned reusable subflows (mixins) referenced by ``uses[]`` as ``name@major``.

A subflow splices its own nodes/slots/routers into the compiled graph at its
anchor, exposes its slot names upward (so corrections/terminal mappings can
reference them), and inherits idempotency/reset/attempt defaults. Each ends with
a ``<name>_done`` no-op exit node whose router returns ``NEXT`` — that single
exit lets internal branches (``anonimo``, confirmed-address) skip the remaining
internal nodes cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..nodes import FlowContext, NodeDesc


@dataclass
class SubflowBuild:
    descriptors: list[NodeDesc]
    entry_id: str
    node_for_slot: dict[str, str] = field(default_factory=dict)


class Subflow(Protocol):
    name: str
    major: int

    def build(self, ctx: FlowContext, with_cfg: dict[str, Any]) -> SubflowBuild: ...


class SubflowRegistry:
    def __init__(self) -> None:
        self._subflows: dict[str, Subflow] = {}

    def register(self, subflow: Subflow) -> None:
        self._subflows[f"{subflow.name}@{subflow.major}"] = subflow

    def get(self, ref: str) -> Subflow:
        if ref not in self._subflows:
            raise KeyError(f"subflow not registered: {ref!r}")
        return self._subflows[ref]


def default_subflows() -> SubflowRegistry:
    from .address import AddressSubflow
    from .identification import IdentificationSubflow

    reg = SubflowRegistry()
    reg.register(AddressSubflow())
    reg.register(IdentificationSubflow())
    return reg
