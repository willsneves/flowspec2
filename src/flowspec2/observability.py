"""Snowflake correlation IDs and structured logging helpers."""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import time
from collections.abc import Callable, Mapping
from threading import Lock

SNOWFLAKE_EPOCH_MILLISECONDS = 1_704_067_200_000
SNOWFLAKE_TIMESTAMP_BITS = 41
SNOWFLAKE_WORKER_BITS = 10
SNOWFLAKE_SEQUENCE_BITS = 12
SNOWFLAKE_MAX_WORKER_ID = (1 << SNOWFLAKE_WORKER_BITS) - 1
SNOWFLAKE_MAX_SEQUENCE = (1 << SNOWFLAKE_SEQUENCE_BITS) - 1
SNOWFLAKE_MAX_TIMESTAMP_DELTA = (1 << SNOWFLAKE_TIMESTAMP_BITS) - 1
SNOWFLAKE_WORKER_SHIFT = SNOWFLAKE_SEQUENCE_BITS
SNOWFLAKE_TIMESTAMP_SHIFT = SNOWFLAKE_WORKER_BITS + SNOWFLAKE_SEQUENCE_BITS

ClockMilliseconds = Callable[[], int]
_LOG_RECORD_RESERVED_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "log_id",
    "message",
    "operation",
}


def _system_clock_milliseconds() -> int:
    return time.time_ns() // 1_000_000


class SnowflakeIdGenerator:
    """Generate ordered decimal Snowflake IDs with an injectable clock."""

    def __init__(
        self,
        *,
        worker_id: int,
        clock_milliseconds: ClockMilliseconds = _system_clock_milliseconds,
        epoch_milliseconds: int = SNOWFLAKE_EPOCH_MILLISECONDS,
    ) -> None:
        if isinstance(worker_id, bool) or not 0 <= worker_id <= SNOWFLAKE_MAX_WORKER_ID:
            raise ValueError(
                f"worker_id must be between 0 and {SNOWFLAKE_MAX_WORKER_ID}, got {worker_id!r}"
            )
        if isinstance(epoch_milliseconds, bool) or epoch_milliseconds < 0:
            raise ValueError("epoch_milliseconds must be a non-negative integer")
        self.worker_id = worker_id
        self._clock_milliseconds = clock_milliseconds
        self._epoch_milliseconds = epoch_milliseconds
        self._last_timestamp_milliseconds: int | None = None
        self._sequence = 0
        self._lock = Lock()

    def generate(self) -> str:
        """Return the next ID, advancing logical time across clock rollback or saturation."""

        with self._lock:
            clock_timestamp = self._clock_milliseconds()
            if isinstance(clock_timestamp, bool) or not isinstance(clock_timestamp, int):
                raise TypeError("clock_milliseconds must return an integer")
            if clock_timestamp < self._epoch_milliseconds:
                raise ValueError("clock timestamp precedes the configured Snowflake epoch")

            last_timestamp = self._last_timestamp_milliseconds
            logical_timestamp = (
                clock_timestamp if last_timestamp is None else max(clock_timestamp, last_timestamp)
            )
            if last_timestamp is not None and logical_timestamp == last_timestamp:
                self._sequence += 1
                if self._sequence > SNOWFLAKE_MAX_SEQUENCE:
                    logical_timestamp += 1
                    self._sequence = 0
            else:
                self._sequence = 0

            timestamp_delta = logical_timestamp - self._epoch_milliseconds
            if timestamp_delta > SNOWFLAKE_MAX_TIMESTAMP_DELTA:
                raise OverflowError("Snowflake timestamp exceeds the configured bit allocation")

            self._last_timestamp_milliseconds = logical_timestamp
            snowflake_id = (
                (timestamp_delta << SNOWFLAKE_TIMESTAMP_SHIFT)
                | (self.worker_id << SNOWFLAKE_WORKER_SHIFT)
                | self._sequence
            )
            return str(snowflake_id)


def _configured_worker_id() -> int:
    configured_worker_id = os.environ.get("FLOWSPEC2_SNOWFLAKE_WORKER_ID")
    if configured_worker_id is not None:
        try:
            worker_id = int(configured_worker_id)
        except ValueError as error:
            raise ValueError("FLOWSPEC2_SNOWFLAKE_WORKER_ID must be an integer") from error
        if not 0 <= worker_id <= SNOWFLAKE_MAX_WORKER_ID:
            raise ValueError("FLOWSPEC2_SNOWFLAKE_WORKER_ID is outside the supported worker range")
        return worker_id

    process_identity = f"{socket.gethostname()}:{os.getpid()}".encode("utf-8")
    identity_digest = hashlib.blake2s(process_identity, digest_size=2).digest()
    return int.from_bytes(identity_digest, byteorder="big") & SNOWFLAKE_MAX_WORKER_ID


_default_generator_lock = Lock()
_default_generator_process_id: int | None = None
_default_generator: SnowflakeIdGenerator | None = None


def default_log_id_generator() -> SnowflakeIdGenerator:
    """Return the process-local generator, recreating it after a process fork."""

    global _default_generator
    global _default_generator_process_id

    process_id = os.getpid()
    with _default_generator_lock:
        if _default_generator is None or _default_generator_process_id != process_id:
            _default_generator = SnowflakeIdGenerator(worker_id=_configured_worker_id())
            _default_generator_process_id = process_id
        return _default_generator


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    operation: str,
    log_id_generator: SnowflakeIdGenerator | None = None,
    context: Mapping[str, object] | None = None,
    exc_info: bool = False,
) -> str:
    """Emit one structured event and return its correlation ID."""

    if context:
        conflicting_fields = _LOG_RECORD_RESERVED_FIELDS.intersection(context)
        if conflicting_fields:
            conflicts = ", ".join(sorted(conflicting_fields))
            raise ValueError(f"logging context cannot override reserved fields: {conflicts}")
    log_id = (log_id_generator or default_log_id_generator()).generate()
    extra: dict[str, object] = {"log_id": log_id, "operation": operation}
    if context:
        extra.update(context)
    logger.log(level, message, extra=extra, exc_info=exc_info)
    return log_id
