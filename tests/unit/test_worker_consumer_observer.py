from __future__ import annotations

import json
import os

import pytest

from core.infrastructure.runtime.worker.consumer_observer import (
    ConsumerObservationSink,
    create_observation_sink,
    run_observation_heartbeat,
)


def test_observation_sink_emits_bounded_state_records() -> None:
    read_fd, write_fd = os.pipe()
    try:
        sink = ConsumerObservationSink(
            descriptor=write_fd,
            role="worker-0",
            worker_incarnation="incarnation-1",
        )

        sink.record("registered")
        sink.record("active")
        sink.record("registered")
        sink.record("draining")
        sink.close()

        records = [
            json.loads(line) for line in os.read(read_fd, 16_384).decode("utf-8").splitlines()
        ]
    finally:
        os.close(read_fd)

    assert [record["state"] for record in records] == [
        "registered",
        "active",
        "registered",
        "draining",
        "disconnected",
    ]
    assert [record["sequence"] for record in records] == [1, 2, 3, 4, 5]
    assert all(record["role"] == "worker-0" for record in records)
    assert all(record["worker_incarnation"] == "incarnation-1" for record in records)
    assert all(
        set(record) == {"role", "worker_incarnation", "state", "sequence"} for record in records
    )


def test_observation_sink_is_created_only_from_supervisor_descriptor() -> None:
    assert (
        ConsumerObservationSink.from_environment(
            {}, role="worker-0", worker_incarnation="incarnation-1"
        )
        is None
    )

    with pytest.raises(ValueError, match="FS_COMPACT_OBSERVATION_FD"):
        ConsumerObservationSink.from_environment(
            {"FS_COMPACT_OBSERVATION_FD": "not-a-descriptor"},
            role="worker-0",
            worker_incarnation="incarnation-1",
        )


def test_observation_sink_does_not_announce_readiness_before_queue_registration() -> None:
    read_fd, write_fd = os.pipe()
    try:
        sink = create_observation_sink(
            {"FS_COMPACT_OBSERVATION_FD": str(write_fd)},
            role="worker-0",
            worker_incarnation="incarnation-1",
        )
        assert sink is not None
        sink.close()
        records = [
            json.loads(line) for line in os.read(read_fd, 16_384).decode("utf-8").splitlines()
        ]
    finally:
        os.close(read_fd)

    assert [record["state"] for record in records] == ["disconnected"]


def test_observation_heartbeat_refreshes_current_state() -> None:
    class StopAfterOneRefresh:
        calls = 0

        def wait(self, _timeout: float | None = None) -> bool:
            self.calls += 1
            return self.calls > 1

    read_fd, write_fd = os.pipe()
    try:
        sink = ConsumerObservationSink(
            descriptor=write_fd,
            role="worker-0",
            worker_incarnation="incarnation-1",
        )
        sink.record("registered")
        run_observation_heartbeat(sink, stop=StopAfterOneRefresh(), interval_seconds=2)
        sink.close()
        records = [
            json.loads(line) for line in os.read(read_fd, 16_384).decode("utf-8").splitlines()
        ]
    finally:
        os.close(read_fd)

    assert [record["state"] for record in records] == [
        "registered",
        "registered",
        "disconnected",
    ]
    assert [record["sequence"] for record in records] == [1, 2, 3]
