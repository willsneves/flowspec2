"""Snowflake generation and structured-log correlation contracts."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from flowspec2.observability import (
    SNOWFLAKE_EPOCH_MILLISECONDS,
    SNOWFLAKE_MAX_SEQUENCE,
    SNOWFLAKE_MAX_WORKER_ID,
    SNOWFLAKE_TIMESTAMP_SHIFT,
    SNOWFLAKE_WORKER_SHIFT,
    ClockMilliseconds,
    SnowflakeIdGenerator,
    log_event,
)


def test_snowflake_ids_remain_ordered_across_clock_rollback() -> None:
    clock_timestamps = iter(
        [
            SNOWFLAKE_EPOCH_MILLISECONDS + 100,
            SNOWFLAKE_EPOCH_MILLISECONDS + 100,
            SNOWFLAKE_EPOCH_MILLISECONDS + 99,
            SNOWFLAKE_EPOCH_MILLISECONDS + 101,
        ]
    )
    worker_id = 17
    generator = SnowflakeIdGenerator(
        worker_id=worker_id,
        clock_milliseconds=clock_timestamps.__next__,
    )

    snowflake_ids = [int(generator.generate()) for generation_index in range(4)]

    assert snowflake_ids == sorted(snowflake_ids)
    assert len(snowflake_ids) == len(set(snowflake_ids))
    assert all(
        (snowflake_id >> SNOWFLAKE_WORKER_SHIFT) & SNOWFLAKE_MAX_WORKER_ID == worker_id
        for snowflake_id in snowflake_ids
    )


def test_sequence_saturation_advances_logical_time() -> None:
    clock_timestamp = SNOWFLAKE_EPOCH_MILLISECONDS + 200
    generator = SnowflakeIdGenerator(
        worker_id=3,
        clock_milliseconds=lambda: clock_timestamp,
    )

    snowflake_ids = [
        int(generator.generate()) for generation_index in range(SNOWFLAKE_MAX_SEQUENCE + 2)
    ]

    first_timestamp_delta = snowflake_ids[0] >> SNOWFLAKE_TIMESTAMP_SHIFT
    last_timestamp_delta = snowflake_ids[-1] >> SNOWFLAKE_TIMESTAMP_SHIFT
    assert last_timestamp_delta == first_timestamp_delta + 1
    assert snowflake_ids == sorted(set(snowflake_ids))


def test_snowflake_generator_is_thread_safe() -> None:
    generator = SnowflakeIdGenerator(
        worker_id=9,
        clock_milliseconds=lambda: SNOWFLAKE_EPOCH_MILLISECONDS + 300,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        generation_futures = [
            executor.submit(generator.generate)
            for generation_index in range(SNOWFLAKE_MAX_SEQUENCE + 2)
        ]
    snowflake_ids = [generation_future.result() for generation_future in generation_futures]

    assert len(snowflake_ids) == len(set(snowflake_ids))


def test_snowflake_generator_rejects_invalid_configuration_and_clock() -> None:
    with pytest.raises(ValueError, match="worker_id"):
        SnowflakeIdGenerator(worker_id=SNOWFLAKE_MAX_WORKER_ID + 1)

    invalid_clock = cast(ClockMilliseconds, lambda: "not-an-integer")
    generator = SnowflakeIdGenerator(worker_id=0, clock_milliseconds=invalid_clock)
    with pytest.raises(TypeError, match="integer"):
        generator.generate()


def test_log_event_returns_the_id_attached_to_the_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    generator = SnowflakeIdGenerator(
        worker_id=11,
        clock_milliseconds=lambda: SNOWFLAKE_EPOCH_MILLISECONDS + 400,
    )
    event_logger = logging.getLogger("flowspec2.tests.observability")

    with caplog.at_level(logging.WARNING, logger=event_logger.name):
        log_id = log_event(
            event_logger,
            logging.WARNING,
            "Correlated warning",
            operation="test_operation",
            log_id_generator=generator,
            context={"flow": "test_flow"},
        )

    matching_records = [
        record for record in caplog.records if getattr(record, "log_id", None) == log_id
    ]
    assert log_id.isdigit()
    assert len(matching_records) == 1
    assert getattr(matching_records[0], "operation", None) == "test_operation"
    assert getattr(matching_records[0], "flow", None) == "test_flow"


def test_log_event_rejects_standard_log_record_field_overrides() -> None:
    generator = SnowflakeIdGenerator(
        worker_id=12,
        clock_milliseconds=lambda: SNOWFLAKE_EPOCH_MILLISECONDS + 500,
    )

    with pytest.raises(ValueError, match="reserved fields: message"):
        log_event(
            logging.getLogger("flowspec2.tests.observability"),
            logging.ERROR,
            "Invalid context",
            operation="test_operation",
            log_id_generator=generator,
            context={"message": "must not override LogRecord.message"},
        )
