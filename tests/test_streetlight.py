"""Drive the streetlight flow turn by turn through the compiled LangGraph graph."""

from __future__ import annotations

from conftest import require_agent_response, step


async def test_auto_flow_then_submission_fills_via_alias_map(streetlight):
    st = await step(streetlight, None, {})
    assert (require_agent_response(st).interactive or {}).get("status") == "flow_sent"

    st = await step(
        streetlight,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "block",
            "location": "Street",
        },
    )
    # alias_map fan-out: qty_pattern=block -> count=group + outage pattern=block
    assert st.data["streetlight_issue"] == "Not working"
    assert st.data["streetlight_count"] == "group"
    assert st.data["streetlight_outage_pattern"] == "block"
    # derived lookup table
    assert st.data["classified_streetlight_issue"] == (
        "A block or group of streetlights that are not working"
    )
    # summary was satisfied by its explicit skip_when without inventing consent
    assert "service_confirmed" not in st.data
    assert st.internal["_slot_skipped:service_confirmed"] is True
    # now collecting the address
    assert "complete address" in require_agent_response(st).description.lower()


async def test_full_happy_path_opens_ticket_and_resets(streetlight):
    st = await step(streetlight, None, {})  # flow_sent
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
    st = await step(streetlight, st, {"address": "Flower Street, 100, Downtown"})
    st = await step(streetlight, st, {"confirmation": "yes"})  # confirm address
    # sports_court is gated OFF for a Street address -> we should now be at reference point
    st = await step(streetlight, st, {"reference_point": "across from the bakery"})
    st = await step(streetlight, st, {"identification_method": "anonymous"})
    st = await step(streetlight, st, {"confirmation": "yes"})  # confirm ticket -> open

    assert st.status == "completed"
    assert st.data.get("protocol_id", "").startswith("REQ-")
    assert st.data.get("ticket_created") is True
    assert st.data.get("_reset_on_next_call") is True

    # next turn wipes and starts fresh (auto_flow fires again)
    st = await step(streetlight, st, {})
    assert (require_agent_response(st).interactive or {}).get("status") == "flow_sent"
    assert "streetlight_issue" not in st.data


async def test_square_address_opens_the_sports_court_gate(streetlight):
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Damaged", "location": "Public square"},
    )
    st = await step(streetlight, st, {"address": "Central Square, Downtown"})
    st = await step(streetlight, st, {"confirmation": "yes"})  # confirm address
    # The address.kind==square gate is true, so the sports-court question is asked.
    assert "sports court" in require_agent_response(st).description.lower()


async def test_non_visual_defect_skips_quantity_gate(streetlight):
    st = await step(streetlight, None, {})
    # Hanging is non-visual, so count and outage-pattern gates are closed.
    st = await step(
        streetlight,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Hanging", "location": "Street"},
    )
    assert st.data["streetlight_issue"] == "Hanging"
    assert "streetlight_count" not in st.data
    assert st.data["classified_streetlight_issue"] == "Hanging"
    assert "complete address" in require_agent_response(st).description.lower()


async def test_correction_clears_slot_and_dependents(streetlight):
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Not working",
            "qty_pattern": "block",
            "location": "Street",
        },
    )
    st = await step(streetlight, st, {"address": "Flower Street, 100, Downtown"})
    st = await step(streetlight, st, {"confirmation": "yes"})
    st = await step(streetlight, st, {"reference_point": "bakery"})
    st = await step(streetlight, st, {"identification_method": "anonymous"})
    # at confirm_ticket_data -> request a correction of the defect
    st = await step(streetlight, st, {"correction": "streetlight_issue"})
    # defect + its requires-dependents + the derived classification are cleared
    assert "streetlight_issue" not in st.data
    assert "streetlight_count" not in st.data
    assert "streetlight_outage_pattern" not in st.data
    assert "classified_streetlight_issue" not in st.data
    assert "ticket_data_confirmed" not in st.data
    # ...and the address it did NOT touch is preserved
    assert st.data.get("address_confirmed") is True


async def test_govbr_await_external_resume(streetlight):
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {"_source": "whatsapp_flow", "defect_type": "Damaged", "location": "Street"},
    )
    st = await step(streetlight, st, {"address": "Street A, 1, Downtown"})
    st = await step(streetlight, st, {"confirmation": "yes"})
    st = await step(streetlight, st, {"reference_point": "corner"})
    st = await step(streetlight, st, {"identification_method": "govbr"})
    # out-of-band CTA sent; turn ends waiting for the external signal
    assert (require_agent_response(st).interactive or {}).get("out_of_band_sent") is True
    # the external signal arrives -> slots populated from the token
    st = await step(
        streetlight,
        st,
        {
            "govbr_token": {
                "brazilian_tax_id": "52998224725",
                "name": "Mary Smith",
                "email": "mary@example.com",
            }
        },
    )
    assert st.data.get("govbr_authenticated") is True
    assert st.data.get("brazilian_tax_id") == "52998224725"
    assert st.data.get("name") == "Mary Smith"
    assert st.data.get("email") == "mary@example.com"
    assert st.data.get("phone")  # enriched via get_user_info


async def test_govbr_external_switch_routes_to_tax_id(streetlight):
    st = await step(streetlight, None, {})
    st = await step(
        streetlight,
        st,
        {
            "_source": "whatsapp_flow",
            "defect_type": "Damaged",
            "location": "Street",
        },
    )
    st = await step(streetlight, st, {"address": "Street A, 1, Downtown"})
    st = await step(streetlight, st, {"confirmation": "yes"})
    st = await step(streetlight, st, {"reference_point": "corner"})
    st = await step(streetlight, st, {"identification_method": "govbr"})

    st = await step(streetlight, st, {"_external_event": "switch"})

    assert st.data["identification_method"] == "brazilian_tax_id"
    assert "brazilian tax id" in require_agent_response(st).description.lower()
