"""Clock contracts and the real UTC adapter used at runtime boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

UtcClock = Callable[[], datetime]


def system_utc_now() -> datetime:
    """Read the real UTC clock for callers that do not inject one."""

    return datetime.now(timezone.utc)


def utc_timestamp(clock: UtcClock) -> datetime:
    """Read and normalize an aware clock sample, rejecting ambiguous local time."""

    timestamp = clock()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return timestamp.astimezone(timezone.utc)
