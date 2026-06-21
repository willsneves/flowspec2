"""flowspec2 — a JSON conversational-flow format compiled to a LangGraph StateGraph.

Public API::

    from flowspec2 import FlowRuntime, load_flow, validate_flow, schema
    from flowspec2 import ServiceState, AgentResponse, compile_flow

See ``docs/DESIGN.md`` for the format rationale and the construct→primitive map.
"""

from __future__ import annotations

from .backends import BackendConfig, make_registry
from .compiler import CompiledFlow, compile_flow
from .models import AgentResponse, ServiceMetadata, ServiceState
from .runtime import FlowRuntime
from .schema import load_flow, schema, validate_flow
from .subflows import SubflowRegistry, default_subflows
from .tools import ToolRegistry, default_tool_registry

__version__ = "0.1.0"

__all__ = [
    "FlowRuntime",
    "CompiledFlow",
    "compile_flow",
    "load_flow",
    "validate_flow",
    "schema",
    "ServiceState",
    "AgentResponse",
    "ServiceMetadata",
    "ToolRegistry",
    "default_tool_registry",
    "SubflowRegistry",
    "default_subflows",
    "BackendConfig",
    "make_registry",
    "__version__",
]
