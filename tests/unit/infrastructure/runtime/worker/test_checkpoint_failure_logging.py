"""Checkpoint logging keeps actionable frames without driver credentials."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from core.infrastructure.runtime.worker.worker_execution_adapters import WorkerGraphExecutionAdapter


def _postgres_saver_module(monkeypatch: pytest.MonkeyPatch, saver_class: type) -> None:
    langgraph = ModuleType("langgraph")
    checkpoint = ModuleType("langgraph.checkpoint")
    postgres = ModuleType("langgraph.checkpoint.postgres")
    postgres.PostgresSaver = saver_class
    monkeypatch.setitem(sys.modules, "langgraph", langgraph)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", checkpoint)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres)


@pytest.mark.parametrize("failure_phase", ["import", "open"])
def test_checkpoint_failure_logs_redacted_traceback_without_driver_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure_phase: str
) -> None:
    import builtins
    import logging

    from core.application.orchestration.checkpoint_requirement import CheckpointUnavailableError

    password = "checkpoint-password-sentinel"
    uri = f"postgresql://checkpoint-user:{password}@checkpoint-db/private"
    driver_detail = f"driver cannot open {uri}"
    chained_detail = "underlying authentication response sentinel"

    def fail_driver() -> None:
        try:
            raise ValueError(chained_detail)
        except ValueError as cause:
            raise ConnectionError(driver_detail) from cause

    class _BrokenSaver:
        @classmethod
        def from_conn_string(cls, _uri: str):
            fail_driver()

    _postgres_saver_module(monkeypatch, _BrokenSaver)
    if failure_phase == "import":
        original_import = builtins.__import__

        def import_checkpoint(name, *args, **kwargs):
            if name == "langgraph.checkpoint.postgres":
                fail_driver()
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_checkpoint)
    adapter = WorkerGraphExecutionAdapter(
        config={"langgraph_postgres_uri": uri}, bl=SimpleNamespace()
    )
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(CheckpointUnavailableError),
        adapter._build_checkpointer(),
    ):
        pytest.fail("a failed checkpoint must not start execution")

    record = next(record for record in caplog.records if "PostgresSaver" in record.getMessage())
    assert record.exc_info is not None
    rendered = logging.Formatter().format(record)
    assert "Traceback (most recent call last)" in rendered
    assert "fail_driver" in rendered
    for sensitive in (password, uri, driver_detail, chained_detail):
        assert sensitive not in rendered
