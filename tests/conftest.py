from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from flowspec2 import AgentResponse, FlowRuntime, ServiceState, load_flow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def luminaria_doc() -> dict[str, Any]:
    return load_flow(str(EXAMPLES / "reparo_luminaria.flow.json"))


@pytest.fixture
def buraco_doc() -> dict[str, Any]:
    return load_flow(str(EXAMPLES / "reparo_buraco.flow.json"))


@pytest.fixture
def luminaria(luminaria_doc) -> FlowRuntime:
    return FlowRuntime(luminaria_doc)


@pytest.fixture
def buraco(buraco_doc) -> FlowRuntime:
    return FlowRuntime(buraco_doc)


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
