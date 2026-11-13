"""The database accepts every webhook auth mode the code supports.

'webhook_signature' (Signed webhook, HMAC) reached the creation service without
the canonical CHECK constraint learning about it, so every save of such a trigger
answered 500 with a constraint violation while every unit test passed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.application.workflows import workflow_webhook_creation_service as creation
from core.application.workflows.workflow_webhook_creation_service import _AUTH_MODES
from core.entrypoints.api.workflow_entrypoints_registry import _normalize_auth_mode
from core.infrastructure.db.schema_state import read_canonical_schema_sql

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "20261113073401_webhook_signature_auth_mode.sql"
_CONSTRAINT = re.compile(
    r"workflow_entrypoints_auth_mode_check CHECK \(\(auth_mode = ANY \(ARRAY\[(.*?)\]\)\)\)"
)


def _allowed(sql: str, source: str) -> set[str]:
    match = _CONSTRAINT.search(sql)
    assert match, f"workflow_entrypoints_auth_mode_check is missing from {source}"
    return set(re.findall(r"'([a-z0-9_]+)'::text", match.group(1)))


def test_the_schema_check_lists_exactly_the_auth_modes_the_creation_service_accepts() -> None:
    allowed = _allowed(read_canonical_schema_sql(), "db/schema")

    assert allowed == set(_AUTH_MODES), {
        "accepted_by_code_only": sorted(set(_AUTH_MODES) - allowed),
        "allowed_by_schema_only": sorted(allowed - set(_AUTH_MODES)),
    }


def test_the_registry_accepts_every_auth_mode_the_schema_allows() -> None:
    allowed = _allowed(read_canonical_schema_sql(), "db/schema")

    assert {_normalize_auth_mode(mode) for mode in allowed} == allowed


def test_the_migration_installs_the_same_constraint_as_the_baseline() -> None:
    """Installed databases never re-read db/schema/; the migration must match it."""
    migration_sql = MIGRATION.read_text(encoding="utf-8")

    assert _allowed(migration_sql, MIGRATION.name) == _allowed(
        read_canonical_schema_sql(), "db/schema"
    )
    assert "DROP CONSTRAINT IF EXISTS workflow_entrypoints_auth_mode_check" in migration_sql


_ANY_CHECK = re.compile(
    r"CONSTRAINT (workflow_entrypoints_[a-z_]+_check) CHECK \(\(([a-z_]+) = ANY \(ARRAY\[(.*?)\]\)\)\)"
)
_RANGE_CHECK = re.compile(
    r"CONSTRAINT workflow_entrypoints_([a-z_]+)_check CHECK \(+\s*\(\1 (>=?) (\d+)\) AND \(\1 <= (\d+)\)"
)


def _entrypoints_table_sql() -> str:
    sql = read_canonical_schema_sql()
    start = sql.index("CREATE TABLE public.workflow_entrypoints (")
    return sql[start : sql.index(");", start)]


def _any_checks() -> dict[str, set[str]]:
    return {
        column: set(re.findall(r"'([a-z0-9_]+)'::text", values))
        for _name, column, values in _ANY_CHECK.findall(_entrypoints_table_sql())
    }


def test_every_value_list_check_of_workflow_entrypoints_has_a_code_side_list() -> None:
    """A new CHECK list on the table must be added to the comparisons below."""
    assert set(_any_checks()) == {"auth_mode", "entry_kind", "idempotency_mode", "response_mode"}


def test_the_schema_check_lists_exactly_the_idempotency_modes_the_service_accepts() -> None:
    assert _any_checks()["idempotency_mode"] == set(creation._IDEMPOTENCY_MODES)


def test_the_schema_check_lists_exactly_the_response_modes_the_service_produces() -> None:
    assert _any_checks()["response_mode"] == set(creation._RESPONSE_MODES)
    aliases = ("", "accepted", "async", "queued", "202", "ok", "sync", "immediate", "200")
    produced = {
        creation._response_mode(alias) for alias in (*aliases, "no_content", "empty", "204")
    }
    assert produced == set(creation._RESPONSE_MODES)


def test_the_schema_check_allows_the_entry_kind_the_service_writes() -> None:
    assert creation._ENTRY_KIND in _any_checks()["entry_kind"]


@pytest.mark.parametrize(
    ("column", "maximum"),
    [
        ("max_body_bytes", creation._MAX_BODY_OR_FILE_BYTES),
        ("max_file_bytes", creation._MAX_BODY_OR_FILE_BYTES),
        ("max_files", creation._MAX_FILES),
    ],
)
def test_the_service_clamps_limits_to_the_schema_range(column: str, maximum: int) -> None:
    ranges = {
        name: int(upper)
        for name, _op, _lower, upper in _RANGE_CHECK.findall(_entrypoints_table_sql())
    }
    assert ranges[column] == maximum
