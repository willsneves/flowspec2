"""Runtime profiles bind portable flow contracts to executable capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .subflows import SubflowRegistry, default_subflows
from .tools import ToolRegistry, default_tool_registry

REFERENCE_PROFILE_ID: Final[str] = "flowspec2/reference@2"
REFERENCE_DOMAIN_TYPES: Final[frozenset[str]] = frozenset(
    {"categorical", "bool", "free_text", "brazilian_tax_id", "email", "name", "integer", "number"}
)
REFERENCE_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "auto_flow",
        "await_external",
        "external_authentication",
        "geocoding",
        "handoff",
        "identity_lookup",
        "location_in",
        "media_in",
        "media_out",
        "session_reset",
        "tts",
    }
)


def capability_is_requested(capability_value: Any) -> bool:
    """Return whether a normalized capability value requests host support."""

    return (
        capability_value is True
        or isinstance(capability_value, Mapping)
        and bool(capability_value)
        or isinstance(capability_value, (list, tuple))
        and bool(capability_value)
    )


@dataclass(frozen=True)
class FlowProfile:
    """Named registries and host features against which a flow is checked."""

    identifier: str
    tools: ToolRegistry
    subflows: SubflowRegistry
    capabilities: frozenset[str]
    domain_types: frozenset[str]
    allow_legacy_contracts: bool = False

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("profile identifier must be non-empty")
        if any(not capability.strip() for capability in self.capabilities):
            raise ValueError("profile capabilities must be non-empty strings")
        if any(not domain_type.strip() for domain_type in self.domain_types):
            raise ValueError("profile domain types must be non-empty strings")

    def as_dict(self) -> dict[str, Any]:
        """Return the complete owned runtime-profile contract."""

        return {
            "identifier": self.identifier,
            "allow_legacy_contracts": self.allow_legacy_contracts,
            "capabilities": sorted(self.capabilities),
            "domain_types": sorted(self.domain_types),
            "tools": {
                tool_name: tool_definition.as_dict()
                for tool_name, tool_definition in sorted(self.tools.definitions.items())
            },
            "subflows": {
                subflow_reference: subflow_definition.as_dict()
                for subflow_reference, subflow_definition in sorted(
                    self.subflows.definitions.items()
                )
            },
        }

    def canonical_json(self) -> str:
        """Serialize the complete profile contract deterministically."""

        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def digest(self) -> str:
        """Return the content identity used by IR and authoring provenance."""

        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def reference_profile(
    *,
    tools: ToolRegistry | None = None,
    subflows: SubflowRegistry | None = None,
) -> FlowProfile:
    """Build the executable profile shipped with the reference runtime."""

    return FlowProfile(
        identifier=REFERENCE_PROFILE_ID,
        tools=tools or default_tool_registry(),
        subflows=subflows or default_subflows(),
        capabilities=REFERENCE_CAPABILITIES,
        domain_types=REFERENCE_DOMAIN_TYPES,
        allow_legacy_contracts=False,
    )
