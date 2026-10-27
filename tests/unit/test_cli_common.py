from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.entrypoints.cli.common import (
    CLIError,
    cli_json_dumps,
    parse_json_dict,
    redact_project_config_snapshot,
    render_cli_value,
    resolve_dsn,
    split_csv,
)


def test_resolve_dsn_prefers_explicit(monkeypatch):
    monkeypatch.setenv("FS_DB_DSN", "postgres://env")
    assert resolve_dsn("postgres://explicit") == "postgres://explicit"


def test_resolve_dsn_uses_env(monkeypatch):
    monkeypatch.delenv("FS_OBSERVABILITY_DSN", raising=False)
    monkeypatch.setenv("FS_DB_DSN", "postgres://db")
    assert resolve_dsn("") == "postgres://db"


def test_parse_json_dict_accepts_object():
    assert parse_json_dict('{"a": 1}', "meta") == {"a": 1}


def test_parse_json_dict_rejects_non_object():
    with pytest.raises(CLIError):
        parse_json_dict('["x"]', "meta")


def test_redact_project_config_snapshot_masks_sensitive_keys():
    payload = {
        "stack": {"language": "python"},
        "db_dsn": "postgres://secret",
        "nested": {"token_value": "abc", "safe": 1},
    }
    redacted = redact_project_config_snapshot(payload)
    assert redacted["db_dsn"] == "[REDACTED]"
    assert redacted["nested"]["token_value"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == 1


def test_split_csv_ignores_empty_items():
    assert split_csv("admin, operator, ,viewer") == ["admin", "operator", "viewer"]


def test_cli_json_dumps_serializes_datetime_values():
    payload = {
        "user": {
            "created_at": datetime(2026, 4, 10, 12, 30, tzinfo=UTC),
        }
    }

    rendered = cli_json_dumps(payload)

    assert "2026-04-10T12:30:00+00:00" in rendered


def test_render_cli_value_serializes_nested_datetime_values():
    payload = {
        "created_at": datetime(2026, 4, 10, 12, 30, tzinfo=UTC),
    }

    rendered = render_cli_value(payload)

    assert "2026-04-10T12:30:00+00:00" in rendered
