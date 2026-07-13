"""Canonical lookup-key encoding shared by derive linking and execution."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def encode_derive_component(component: Any) -> str:
    """Encode one state value without Python-specific boolean/null spellings."""

    if component is None or isinstance(component, bool):
        return json.dumps(component, ensure_ascii=False, separators=(",", ":"))
    return str(component)


def encode_derive_key(components: Iterable[Any]) -> str:
    """Encode one complete lookup key using the format's component delimiter."""

    encoded_components = tuple(encode_derive_component(component) for component in components)
    if not encoded_components:
        raise ValueError("derive lookup keys require at least one component")
    return encoded_components[0] if len(encoded_components) == 1 else "|".join(encoded_components)


def decode_derive_key(lookup_key: str, source_count: int) -> tuple[str, ...]:
    """Decode an authored key while preserving delimiters in a single source token."""

    if source_count < 1:
        raise ValueError("derive lookup keys require at least one source")
    return (lookup_key,) if source_count == 1 else tuple(lookup_key.split("|"))
