"""Strict RFC 8259 JSON decoding for every external document boundary."""

from __future__ import annotations

import json
import math
from typing import Any


class StrictJsonError(ValueError):
    """Raised for JSON extensions or ambiguous duplicate object members."""


class StrictJsonValueError(StrictJsonError):
    """Raised with a JSON Pointer for an in-memory non-JSON value."""

    def __init__(self, message: str, *, path: str) -> None:
        self.path = path
        super().__init__(message)


def _value_error(boundary: str, path_segments: tuple[str | int, ...], detail: str) -> None:
    path = _json_pointer(path_segments)
    raise StrictJsonValueError(f"{boundary} at {path}: {detail}", path=path)


def _json_pointer(path_segments: tuple[str | int, ...]) -> str:
    return (
        "".join(
            "/" + str(path_segment).replace("~", "~0").replace("/", "~1")
            for path_segment in path_segments
        )
        or "/"
    )


def validate_json_value(
    value: Any,
    *,
    boundary: str,
    path_segments: tuple[str | int, ...] = (),
    active_containers: set[int] | None = None,
) -> None:
    """Reject Python values that cannot be represented as strict JSON."""

    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _value_error(boundary, path_segments, "non-finite number")
        return

    traversed_containers = set() if active_containers is None else active_containers
    if isinstance(value, dict):
        container_identifier = id(value)
        if container_identifier in traversed_containers:
            _value_error(boundary, path_segments, "cyclic JSON object")
        traversed_containers.add(container_identifier)
        try:
            for member_name, member_value in value.items():
                if not isinstance(member_name, str):
                    _value_error(
                        boundary,
                        path_segments,
                        "object member name has unsupported Python type "
                        f"{type(member_name).__name__}",
                    )
                validate_json_value(
                    member_value,
                    boundary=boundary,
                    path_segments=(*path_segments, member_name),
                    active_containers=traversed_containers,
                )
        finally:
            traversed_containers.remove(container_identifier)
        return
    if isinstance(value, list):
        container_identifier = id(value)
        if container_identifier in traversed_containers:
            _value_error(boundary, path_segments, "cyclic JSON array")
        traversed_containers.add(container_identifier)
        try:
            for element_index, element in enumerate(value):
                validate_json_value(
                    element,
                    boundary=boundary,
                    path_segments=(*path_segments, element_index),
                    active_containers=traversed_containers,
                )
        finally:
            traversed_containers.remove(container_identifier)
        return
    _value_error(
        boundary,
        path_segments,
        f"unsupported Python type {type(value).__name__}",
    )


def _reject_nonstandard_constant(constant: str) -> None:
    raise StrictJsonError(f"non-standard JSON numeric constant {constant!r} is not allowed")


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded_object: dict[str, Any] = {}
    for member_name, member_value in pairs:
        if member_name in decoded_object:
            raise StrictJsonError(f"duplicate JSON object member {member_name!r} is not allowed")
        decoded_object[member_name] = member_value
    return decoded_object


def strict_json_loads(serialized_document: str | bytes | bytearray) -> Any:
    """Decode standards-compliant JSON while rejecting NaN and duplicate keys."""

    return json.loads(
        serialized_document,
        parse_constant=_reject_nonstandard_constant,
        object_pairs_hook=_reject_duplicate_members,
    )
