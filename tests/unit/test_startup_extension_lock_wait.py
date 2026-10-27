"""AC17-R150: an application process waits for extension reconciliation instead of dying.

Found by the auditor in the persistent log of a Scalable installation, eight times in
one day, at every update and every `config apply`:

    RuntimeError: Startup extension lifecycle reconciliation is already in progress
    uvicorn.error: Application startup failed. Exiting.

Startup reconciles extensions under one cluster-wide advisory lock. Compact waited for
it; every other profile tried once and raised when anything else held it -- another
process starting at the same moment, or an extension operation in flight. The restart
policy brought the application back a few seconds later, so updates looked fine, but a
readiness probe landing in that window got HTTP 500 and `config apply` rolled back.

The lock below is held by "another process": a lock the application's own code never
took, standing in for the Postgres advisory lock on a different session.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.infrastructure.runtime import app_lifecycle_runtime

_ADVISORY = threading.Lock()  # the server-side lock another session holds


class _Cursor:
    def __init__(self, connection: _AdvisorySession) -> None:
        self._connection = connection
        self._row: object = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        del params
        self._connection.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            self._row = (_ADVISORY.acquire(blocking=False),)
        elif "pg_advisory_lock" in sql:
            self._row = (_ADVISORY.acquire(timeout=10),)
        elif "pg_advisory_unlock" in sql:
            _ADVISORY.release()
            self._row = (True,)

    def fetchone(self) -> object:
        return self._row


class _AdvisorySession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        pass


def _start(monkeypatch: pytest.MonkeyPatch, session: _AdvisorySession) -> MagicMock:
    sync = MagicMock()
    fake_bl = MagicMock()
    fake_bl.conn = session
    monkeypatch.setattr(app_lifecycle_runtime, "_has_live_db_runtime_context", lambda _bl: False)
    monkeypatch.setattr(app_lifecycle_runtime, "_recover_extension_publications", MagicMock())
    monkeypatch.setattr(
        app_lifecycle_runtime, "_reconcile_extension_cleanup", MagicMock(return_value=True)
    )
    monkeypatch.setattr(
        "core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn",
        lambda *_args, **_kwargs: None,
    )
    asyncio.run(
        app_lifecycle_runtime.startup_app(
            config={"fs_db_dsn": "postgresql://example"},
            env_flag=lambda _name, default=False: False,
            get_logger_only=lambda: fake_bl,
            init_sentry=MagicMock(),
            prewarm_state_reader_graph=MagicMock(),
            start_control_threads=MagicMock(),
            control_config=object(),
            watchdog_scan_fn=MagicMock(),
            scheduled_jobs_scan_fn=MagicMock(),
            dashboard_rollup_scan_fn=MagicMock(),
            maintenance_active_fn=lambda: False,
            sync_extension_scan=sync,
            ensure_canonical_guardrails_seeded_fn=MagicMock(return_value={}),
            extension_install_is_installed_fn=MagicMock(return_value=False),
            logger=MagicMock(),
        )
    )
    return sync


@pytest.mark.parametrize("profile", ["scalable", "compact", ""])
def test_startup_waits_for_a_reconciliation_another_process_holds(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", profile)
    assert _ADVISORY.acquire(blocking=False)
    releaser = threading.Timer(0.3, _ADVISORY.release)
    releaser.start()
    session = _AdvisorySession()
    try:
        sync = _start(monkeypatch, session)
    finally:
        releaser.join()

    # It reconciled, after the other holder let go, and released the lock itself.
    sync.assert_called_once()
    assert any("pg_advisory_lock" in sql for sql in session.statements)
    assert any("pg_advisory_unlock" in sql for sql in session.statements)
    assert _ADVISORY.acquire(blocking=False)
    _ADVISORY.release()


def test_an_uncontended_startup_still_reconciles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    session = _AdvisorySession()

    sync = _start(monkeypatch, session)

    sync.assert_called_once()
    assert _ADVISORY.acquire(blocking=False)
    _ADVISORY.release()
