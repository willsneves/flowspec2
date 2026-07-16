"""Regression tests for correction round-trips across node/subflow boundaries.

These lock the three bugs an adversarial review found: corrections that cross a
subflow boundary used to drop the re-answer or submit stale downstream data.
"""

from __future__ import annotations

from conftest import require_agent_response, step


async def _to_confirm_square(streetlight):
    """Drive to the confirm-ticket hub with a Public square address (sports_court asked)."""
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Damaged", "location": "Public square"},
    )
    st = await step(streetlight, st, {"address": "Central Square, Downtown"})  # kind=square
    st = await step(streetlight, st, {"confirmation": "yes"})  # confirm address
    st = await step(streetlight, st, {"near_sports_court": "yes"})  # sports_court (gated open)
    st = await step(streetlight, st, {"reference_point": "near the tennis court"})
    st = await step(streetlight, st, {"identification_method": "anonymous"})
    return st  # now at confirm_ticket_data


async def test_bug_a_address_correction_clears_dependents(streetlight):
    st = await _to_confirm_square(streetlight)
    assert st.data.get("near_sports_court") is True
    assert st.data.get("reference_point")

    # correct the address -> address + its requires-dependents must be cleared,
    # so no stale sports_court flag / reference point reaches the ticket.
    st = await step(streetlight, st, {"correction": "address"})
    assert "address" not in st.data
    assert "near_sports_court" not in st.data
    assert "reference_point" not in st.data
    assert "ticket_data_confirmed" not in st.data


async def test_bug_b_text_reanswer_after_correction_is_not_swallowed(streetlight):
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "single",
            "location": "Street",
        },
    )
    st = await step(streetlight, st, {"address": "Street A, 1"})
    st = await step(streetlight, st, {"confirmation": "yes"})
    st = await step(streetlight, st, {"reference_point": "esquina"})
    st = await step(streetlight, st, {"identification_method": "anonymous"})
    st = await step(
        streetlight, st, {"correction": "streetlight_issue"}
    )  # clears streetlight_issue
    assert "streetlight_issue" not in st.data

    # the citizen TYPES the new defect — auto_flow must NOT re-fire and swallow it
    st = await step(streetlight, st, {"streetlight_issue": "Flickering"})
    assert (require_agent_response(st).interactive or {}).get("status") != "flow_sent"
    assert st.data["streetlight_issue"] == "Flickering"


async def test_bug_c_correct_brazilian_tax_id_after_anonymous_reenters_subflow(streetlight):
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Damaged", "location": "Street"},
    )
    st = await step(streetlight, st, {"address": "Street A, 1"})
    st = await step(streetlight, st, {"confirmation": "yes"})
    st = await step(streetlight, st, {"reference_point": "esquina"})
    st = await step(streetlight, st, {"identification_method": "anonymous"})
    # at confirm: now the citizen wants to identify after all
    st = await step(streetlight, st, {"correction": "brazilian_tax_id"})
    st = await step(streetlight, st, {"brazilian_tax_id": "52998224725"})
    assert st.data.get("brazilian_tax_id") == "52998224725"
