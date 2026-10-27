"""A long connector step keeps telling the job it is alive."""

from __future__ import annotations

import threading

from core.application.orchestration.step_activity import step_activity_heartbeat


class _Logger:
    def __init__(self, *, fail: bool = False) -> None:
        self.beats: list[tuple[str, str]] = []
        self.fail = fail
        self.second_beat = threading.Event()

    def update_job_runtime(self, job_id, *, heartbeat_now, last_response_excerpt):
        assert heartbeat_now is True
        self.beats.append((job_id, last_response_excerpt))
        if len(self.beats) >= 2:
            self.second_beat.set()
        if self.fail:
            raise RuntimeError("database is down")


def test_the_step_beats_when_it_starts_and_while_it_runs() -> None:
    logger = _Logger()
    ticks = iter([100.0, 175.0, 260.0, 999.0])

    with step_activity_heartbeat(
        logger,
        job_id="job-1",
        label="export_products",
        interval_seconds=0.01,
        clock=lambda: next(ticks),
    ):
        assert logger.second_beat.wait(2)

    assert logger.beats[0] == ("job-1", "export_products: started")
    assert logger.beats[1] == ("job-1", "export_products: running for 1 min 15 s")


def test_the_heartbeat_stops_with_the_step() -> None:
    logger = _Logger()

    with step_activity_heartbeat(logger, job_id="job-1", label="step", interval_seconds=0.01):
        assert logger.second_beat.wait(2)
    beats_at_exit = len(logger.beats)
    threading.Event().wait(0.05)

    assert len(logger.beats) == beats_at_exit


def test_a_failing_heartbeat_never_fails_the_step() -> None:
    logger = _Logger(fail=True)

    with step_activity_heartbeat(logger, job_id="job-1", label="step", interval_seconds=0.01):
        assert logger.second_beat.wait(2)
        result = "step finished"

    assert result == "step finished"


def test_no_job_or_no_logger_means_no_heartbeat() -> None:
    logger = _Logger()

    with step_activity_heartbeat(logger, job_id="", label="step", interval_seconds=0.01):
        pass
    with step_activity_heartbeat(None, job_id="job-1", label="step", interval_seconds=0.01):
        pass

    assert logger.beats == []
