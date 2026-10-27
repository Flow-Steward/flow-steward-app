from threading import Event

from core.infrastructure.runtime.monitoring.heartbeat_runtime import (
    start_monitoring_publisher,
    start_monitoring_threads,
)


def test_owned_threads_stop_once_and_cleanup_runs_without_business_admission():
    connected = Event()
    cleaned = Event()
    stopped = []

    def consume(stop):
        connected.set()
        stop.wait()
        stopped.append("consumer")

    handle = start_monitoring_threads(
        consumer=consume, cleanup=cleaned.set, interval_seconds=60, close_timeout_seconds=2
    )
    assert connected.wait(2)
    assert cleaned.wait(2)
    handle.close()
    handle.close()
    assert stopped == ["consumer"]
    assert all(not thread.is_alive() for thread in handle.threads)


def test_publisher_collects_latest_after_failure_without_replay_buffer():
    sent = []
    polled = []
    closed = []
    recovered = Event()
    snapshots = iter([(b"old",), (b"latest",)])

    def publish(body):
        sent.append(body)
        if body == b"old":
            raise ConnectionError("broker unavailable")
        recovered.set()

    handle = start_monitoring_publisher(
        collect=lambda: next(snapshots, ()),
        publish=publish,
        idle=lambda: polled.append(True),
        close=lambda: closed.append(True),
        interval_seconds=0.1,
        close_timeout_seconds=2,
    )
    assert recovered.wait(2)
    handle.close()
    handle.close()
    assert sent == [b"old", b"latest"]
    assert polled
    assert closed == [True]
    assert not handle.threads[0].is_alive()
