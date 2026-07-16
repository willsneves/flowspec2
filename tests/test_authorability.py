"""Authorability: the pothole example runs, and a brand-new flow authored inline
from a one-line description compiles and runs without touching the library."""

from __future__ import annotations

from conftest import require_agent_response, step

from flowspec2 import FlowRuntime, validate_flow


async def test_pothole_runs_end_to_end(pothole):
    st = await step(pothole, None, {"pothole_type": "pothole", "pothole_size": "large"})
    assert st.data["pothole_type"] == "Asphalt pothole"
    assert st.data["pothole_size"] == "Large"
    st = await step(pothole, st, {"address": "Main Avenue, 1000"})
    st = await step(pothole, st, {"confirmation": "yes"})
    st = await step(pothole, st, {"identification_method": "anonymous"})
    assert (require_agent_response(st).interactive or {})["field"] == "confirmation"
    st = await step(pothole, st, {"confirmation": "yes"})
    assert st.status == "completed"
    assert st.data["protocol_id"].startswith("REQ-")


# "Citizen reports an abandoned or overgrown vacant lot, needs the address,
#  optional identification, and opens a ticket." — authored as JSON,
#  no Python, no subflow changes: just a domain, two slots, a flat path, two `use`s.
VACANT_LOT_FLOW = {
    "schema": "flowspec/2",
    "flow": "vacant_lot_report",
    "version": "1.0.0",
    "service": {"id": "27001"},
    "route": {
        "description": "Report an overgrown vacant lot, accumulated waste, or an insect hazard."
    },
    "config": {"address_required": True, "identification_required": False, "max_attempts": 3},
    "domains": {
        "VacantLotIssue": {
            "type": "categorical",
            "values": ["Overgrown vegetation", "Accumulated waste", "Both"],
            "normalize": {
                "accent_fold": True,
                "synonyms": {
                    "overgrown": "Overgrown vegetation",
                    "waste": "Accumulated waste",
                    "both issues": "Both",
                },
            },
        },
        "YesNo": {"type": "bool", "normalize": {"affirmation": True}},
    },
    "slots": {
        "lot_issue": {"domain": "VacantLotIssue", "required": True},
        "ticket_data_confirmed": {"domain": "YesNo"},
    },
    "path": [
        {
            "step": "collect_problem",
            "slot": "lot_issue",
            "prompt": {"text": "What is the problem with the vacant lot?"},
            "interactive": {
                "kind": "buttons",
                "field": "lot_issue",
                "from_domain": "VacantLotIssue",
            },
        },
        {"use": "address@1"},
        {"use": "identification@2"},
        {
            "step": "confirm_ticket_data",
            "confirm": "ticket_data_confirmed",
            "correctable": True,
            "prompt": {"text": "Do you confirm the report?"},
            "interactive": {"kind": "buttons", "field": "confirmation", "from_domain": "YesNo"},
        },
        {"terminal": True},
    ],
    "uses": [
        {"ref": "address@1", "with": {"required": True, "needs_confirmation": True}},
        {"ref": "identification@2", "with": {"required": False}},
    ],
    "confirm": {
        "step": "confirm_ticket_data",
        "slot": "ticket_data_confirmed",
        "on_confirm": "open_ticket",
        "correctable": ["lot_issue", "address", "brazilian_tax_id", "email", "name"],
    },
    "terminal": {
        "step": "open_ticket",
        "tool": "open_service_request",
        "idempotent": True,
        "input": [
            {"param": "problem", "slot": "lot_issue"},
            {"param": "address", "slot": "address"},
            {"param": "requester", "slot": "brazilian_tax_id"},
        ],
        "outputs": {"protocol_id": "result.protocol_id"},
        "outcomes": {
            "success": {"reset_next": True},
            "retryable": {"preserve_state": True},
            "fatal": {"reset_next": True},
        },
    },
    "capabilities": {
        "media_in": {"analyze": ["image"], "route_by": "suggested_workflow"},
        "session_reset": True,
    },
}


def test_new_flow_validates_against_schema():
    validate_flow(VACANT_LOT_FLOW)


async def test_new_flow_compiles_and_runs():
    rt = FlowRuntime(VACANT_LOT_FLOW)
    st = await step(rt, None, {"lot_issue": "overgrown"})
    assert st.data["lot_issue"] == "Overgrown vegetation"
    st = await step(rt, st, {"address": "Vacant Lot Street, 7"})
    st = await step(rt, st, {"confirmation": "yes"})  # confirm address
    st = await step(rt, st, {"identification_method": "anonymous"})
    assert (require_agent_response(st).interactive or {})["field"] == "confirmation"
    st = await step(rt, st, {"confirmation": "yes"})  # confirm ticket -> open
    assert st.status == "completed"
    assert st.data["protocol_id"].startswith("REQ-")
