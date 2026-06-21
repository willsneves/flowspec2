"""The frozen predicate grammar — pure boolean over five namespaces."""

from __future__ import annotations

from flowspec2.models import ServiceState
from flowspec2.predicates import evaluate


def _state(**data) -> ServiceState:
    st = ServiceState(user_id="u", service_name="s")
    st.data.update(data)
    return st


def test_in():
    st = _state(luminaria_defeito="Apagada")
    assert evaluate({"in": ["slots.luminaria_defeito", ["Apagada", "Piscando"]]}, st, {})
    assert not evaluate({"in": ["slots.luminaria_defeito", ["Pendurada"]]}, st, {})


def test_eq_ne_with_literal_and_ref():
    st = _state(q="grupo")
    assert evaluate({"eq": ["slots.q", "grupo"]}, st, {})
    assert evaluate({"ne": ["slots.q", "uma"]}, st, {})


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
    st = _state(address={"kind": "praca"})
    assert evaluate({"eq": ["address.kind", "praca"]}, st, {})
    assert evaluate({"eq": ["config.flag", True]}, st, {"flag": True})


def test_payload_namespace():
    st = ServiceState(user_id="u", service_name="s")
    st.payload = {"_source": "whatsapp_flow"}
    assert evaluate({"eq": ["payload._source", "whatsapp_flow"]}, st, {})
