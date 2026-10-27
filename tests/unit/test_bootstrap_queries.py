"""Private trusted count/status transports and their fail-closed contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.auth.bootstrap_queries import bootstrap_runtime_status, count_bootstrap_users


class Cursor:
    def __init__(self, count):
        self.count = count
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query):
        self.query = query

    def fetchone(self):
        return (self.count,)


def test_auth_count_uses_all_rows_without_bootstrap_or_active_filters():
    cursor = Cursor(3)
    assert count_bootstrap_users(SimpleNamespace(cursor=lambda: cursor)) == 3
    assert cursor.query == "SELECT COUNT(*) FROM users"


@pytest.mark.parametrize("count", [False, -1, "0", None])
def test_auth_count_fails_closed_on_invalid_database_response(count):
    with pytest.raises(ValueError):
        count_bootstrap_users(SimpleNamespace(cursor=lambda: Cursor(count)))


def test_status_reads_only_supplied_running_environment(tmp_path):
    fixture_env = {
        "FS_AUTH_BOOTSTRAP_ENABLED": "1",
        "FS_AUTH_BOOTSTRAP_TOKEN": "wrong-new-token",
    }
    (tmp_path / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in fixture_env.items())
    )
    status = bootstrap_runtime_status({"FS_AUTH_BOOTSTRAP_ENABLED": "0"})
    assert status["bootstrap_enabled"] is False
    assert status["token_sha256"] == ""
    assert "wrong-new-token" not in json.dumps(status)


def test_private_transport_refuses_unknown_operation_without_output(capsys):
    from core.entrypoints.cli.bootstrap_query import main

    assert main(["dump-env"]) == 2
    assert capsys.readouterr().out == ""


def test_status_transport_preserves_container_environment_before_config_imports(tmp_path):
    root = Path(__file__).resolve().parents[2]
    code = """
from core.infrastructure.runtime_env import runtime_env
runtime_env['FS_AUTH_BOOTSTRAP_ENABLED']='0'
from core.entrypoints.cli.bootstrap_query import main
runtime_env['FS_AUTH_BOOTSTRAP_ENABLED']='1'
raise SystemExit(main(['status']))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["bootstrap_enabled"] is False


@pytest.mark.parametrize(
    "metadata",
    [
        {"FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT": "not-a-timestamp"},
        {"FS_AUTH_BOOTSTRAP_STATE_FILE": "/unexpected/path"},
    ],
)
def test_status_refuses_unstructured_or_foreign_metadata(metadata):
    with pytest.raises(ValueError):
        bootstrap_runtime_status(metadata)


def test_count_query_preserves_supplied_environment_and_bounded_connection(monkeypatch):
    from core.application.auth.bootstrap_queries import bootstrap_user_count
    from core.infrastructure.db import psycopg_runtime

    observed = []
    cursor = Cursor(3)

    def connect(dsn, **kwargs):
        observed.append((dsn, kwargs))
        return nullcontext(SimpleNamespace(cursor=lambda: cursor))

    monkeypatch.setattr(psycopg_runtime, "connect", connect)
    assert bootstrap_user_count({"FS_DB_DSN": "supplied-dsn"}) == {
        "schema_version": 1,
        "count": 3,
    }
    assert observed == [
        ("supplied-dsn", {"connect_timeout": 5, "options": "-c statement_timeout=5000"})
    ]
    assert cursor.query == "SELECT COUNT(*) FROM users"


def test_count_transport_missing_dsn_retains_safe_exit(monkeypatch, capsys):
    from core.entrypoints.cli import bootstrap_query

    monkeypatch.setattr(bootstrap_query, "_PROCESS_ENVIRONMENT", {})
    assert bootstrap_query.main(["count"]) == 4
    assert capsys.readouterr().out == ""
