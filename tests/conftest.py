from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from flowspec2 import AgentResponse, FlowRuntime, ServiceState, load_flow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def streetlight_document() -> dict[str, Any]:
    return load_flow(str(EXAMPLES / "streetlight_repair.flow.json"))


@pytest.fixture
def pothole_document() -> dict[str, Any]:
    return load_flow(str(EXAMPLES / "pothole_repair.flow.json"))


@pytest.fixture
def streetlight(streetlight_document) -> FlowRuntime:
    return FlowRuntime(streetlight_document)


@pytest.fixture
def pothole(pothole_document) -> FlowRuntime:
    return FlowRuntime(pothole_document)


async def step(
    rt: FlowRuntime, state: Optional[ServiceState], payload: dict[str, Any], *, user: str = "u1"
) -> ServiceState:
    """Run a single turn. Creates a fresh state when none is given."""
    if state is None:
        state = rt.new_state(user)
    return await rt.execute(state, payload)


def require_agent_response(state: ServiceState) -> AgentResponse:
    """Return the response after asserting the test's pause/result precondition."""
    assert state.agent_response is not None
    return state.agent_response
