"""The frozen predicate grammar — pure boolean over five namespaces."""

from __future__ import annotations

from flowspec2.models import ServiceState
from flowspec2.predicates import evaluate


def _state(**data) -> ServiceState:
    st = ServiceState(user_id="u", service_name="s")
    st.data.update(data)
    return st


def test_in():
    st = _state(streetlight_issue="Not working")
    assert evaluate({"in": ["slots.streetlight_issue", ["Not working", "Flickering"]]}, st, {})
    assert not evaluate({"in": ["slots.streetlight_issue", ["Hanging"]]}, st, {})


def test_eq_ne_with_literal_and_ref():
    st = _state(q="group")
    assert evaluate({"eq": ["slots.q", "group"]}, st, {})
    assert evaluate({"ne": ["slots.q", "single"]}, st, {})


def test_is_present():
    st = _state(x="v")
    assert evaluate({"is_present": "slots.x"}, st, {})
    assert not evaluate({"is_present": "slots.missing"}, st, {})
    st.data["empty"] = ""
    assert not evaluate({"is_present": "slots.empty"}, st, {})


def test_and_or_not():
    st = _state(a="1", b="2")
    assert evaluate({"and": [{"eq": ["slots.a", "1"]}, {"eq": ["slots.b", "2"]}]}, st, {})
    assert evaluate({"or": [{"eq": ["slots.a", "x"]}, {"eq": ["slots.b", "2"]}]}, st, {})
    assert evaluate({"not": {"eq": ["slots.a", "x"]}}, st, {})


def test_config_and_address_namespaces():
    st = _state(address={"kind": "square"})
    assert evaluate({"eq": ["address.kind", "square"]}, st, {})
    assert evaluate({"eq": ["config.flag", True]}, st, {"flag": True})


def test_namespaced_string_can_be_an_explicit_literal() -> None:
    state = _state(answer="config.flag")

    assert evaluate(
        {"eq": ["slots.answer", {"literal": "config.flag"}]},
        state,
        {"flag": "different"},
    )


def test_payload_namespace():
    st = ServiceState(user_id="u", service_name="s")
    st.payload = {"_source": "whatsapp_flow"}
    assert evaluate({"eq": ["payload._source", "whatsapp_flow"]}, st, {})
