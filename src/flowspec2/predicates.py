"""The frozen object-form predicate grammar.

A predicate is a single-key object over five read-only namespaces. There is no
string sugar and no arbitrary computation — every gate, ``ask_when``,
``send_when`` and correction route compiles to a call to :func:`evaluate`, which
is a pure boolean function over ``ServiceState`` + the flow ``config`` block.

Grammar (one operator per object)::

    {"in":  ["slots.x", ["a", "b"]]}      # resolved(x) in the literal list
    {"eq":  ["slots.x", "literal"]}       # resolved(x) == literal (either side may be a ref)
    {"eq":  ["slots.x", {"literal": "config.flag"}]}  # namespaced literal
    {"ne":  ["slots.x", "literal"]}
    {"is_present": "slots.x"}              # filled and non-null/non-empty
    {"and": [<pred>, ...]}
    {"or":  [<pred>, ...]}
    {"not": <pred>}

Namespaces: ``slots.`` (state.data), ``internal.`` (state.internal),
``payload.`` (state.payload), ``config.`` (the flow config block),
``address.`` (state.data["address"]).
"""

from __future__ import annotations

from typing import Any, TypeGuard

from .models import ServiceState

NAMESPACES = ("slots", "internal", "payload", "config", "address")


def _is_ref(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and "." in value and value.split(".", 1)[0] in NAMESPACES


def _resolve(ref: str, state: ServiceState, config: dict[str, Any]) -> Any:
    ns, key = ref.split(".", 1)
    if ns == "slots":
        return state.data.get(key)
    if ns == "internal":
        return state.internal.get(key)
    if ns == "payload":
        return state.payload.get(key)
    if ns == "config":
        return config.get(key)
    if ns == "address":
        resolved: Any = state.data.get("address") or {}
        for path_segment in key.split("."):
            if not isinstance(resolved, dict) or path_segment not in resolved:
                return None
            resolved = resolved[path_segment]
        return resolved
    raise ValueError(f"unknown predicate namespace: {ns!r}")


def _operand(value: Any, state: ServiceState, config: dict[str, Any]) -> Any:
    """Resolve namespaced strings and unwrap explicit ambiguous literals."""
    if isinstance(value, dict) and set(value) == {"literal"}:
        return value["literal"]
    return _resolve(value, state, config) if _is_ref(value) else value


def evaluate(pred: dict[str, Any], state: ServiceState, config: dict[str, Any]) -> bool:
    """Evaluate a predicate against state + config. Pure, side-effect free."""
    if not isinstance(pred, dict) or len(pred) != 1:
        raise ValueError(f"predicate must be a single-key object, got {pred!r}")
    ((op, val),) = pred.items()

    if op == "in":
        ref, options = val
        return _operand(ref, state, config) in options
    if op == "eq":
        left, right = val
        return bool(_operand(left, state, config) == _operand(right, state, config))
    if op == "ne":
        left, right = val
        return bool(_operand(left, state, config) != _operand(right, state, config))
    if op == "is_present":
        resolved = _resolve(val, state, config)
        return resolved is not None and resolved != ""
    if op == "and":
        return all(evaluate(p, state, config) for p in val)
    if op == "or":
        return any(evaluate(p, state, config) for p in val)
    if op == "not":
        return not evaluate(val, state, config)

    raise ValueError(f"unknown predicate operator: {op!r}")
