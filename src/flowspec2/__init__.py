"""flowspec2 — a JSON conversational-flow format compiled to a LangGraph StateGraph.

Public API::

    from flowspec2 import FlowRuntime, check_flow, load_flow, validate_flow, schema
    from flowspec2 import ServiceState, AgentResponse, compile_flow

See ``docs/DESIGN.md`` for the format rationale and the construct→primitive map.
"""

from __future__ import annotations

import logging
from importlib.metadata import version as package_version

from .backends import BackendConfig, make_registry
from .checker import check_flow, check_json, structural_diagnostics
from .clock import UtcClock
from .codex_agent import CodexAgent, CodexAgentError
from .compiler import CompiledFlow, compile_flow
from .diagnostics import DiagnosticLocation, FlowCheckReport, FlowDiagnostic
from .ir import (
    FLOW_IR_FORMAT,
    FlowIR,
    FlowNode,
    FlowReference,
    FlowSlot,
    FlowTransition,
    build_flow_ir,
    normalize_flow,
)
from .models import AgentResponse, AwaitResumeProvenance, ServiceMetadata, ServiceState
from .observability import SnowflakeIdGenerator
from .profiles import FlowProfile, reference_profile
from .runtime import FlowRuntime
from .schema import load_flow, schema, validate_flow
from .semantics import FlowLinkError, semantic_diagnostics
from .state_migration import (
    FlowStateContract,
    StateCopy,
    StateDefault,
    StateMigrationError,
    StateMigrationLossReport,
    StateMigrationPlan,
    StateMigrationResult,
    StatePath,
    migrate_service_state,
)
from .subflows import SubflowDefinition, SubflowRegistry, default_subflows
from .tools import ToolDefinition, ToolEffects, ToolRegistry, default_tool_registry

__version__ = package_version("flowspec2")

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "FlowRuntime",
    "FlowLinkError",
    "FlowCheckReport",
    "FlowDiagnostic",
    "DiagnosticLocation",
    "CompiledFlow",
    "CodexAgent",
    "CodexAgentError",
    "check_flow",
    "check_json",
    "structural_diagnostics",
    "semantic_diagnostics",
    "migrate_service_state",
    "normalize_flow",
    "build_flow_ir",
    "FlowIR",
    "FLOW_IR_FORMAT",
    "FlowNode",
    "FlowSlot",
    "FlowReference",
    "FlowTransition",
    "FlowStateContract",
    "FlowProfile",
    "reference_profile",
    "compile_flow",
    "load_flow",
    "validate_flow",
    "schema",
    "ServiceState",
    "AgentResponse",
    "AwaitResumeProvenance",
    "ServiceMetadata",
    "StatePath",
    "StateCopy",
    "StateDefault",
    "StateMigrationPlan",
    "StateMigrationResult",
    "StateMigrationLossReport",
    "StateMigrationError",
    "SnowflakeIdGenerator",
    "ToolRegistry",
    "ToolDefinition",
    "ToolEffects",
    "UtcClock",
    "default_tool_registry",
    "SubflowRegistry",
    "SubflowDefinition",
    "default_subflows",
    "BackendConfig",
    "make_registry",
    "__version__",
]
