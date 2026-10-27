"""Revision retention keeps the newest N revisions per item, measured on PostgreSQL.

Everything runs in one transaction that is rolled back, so the test leaves no rows
behind on whatever database ``pg_conn`` names.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.tooling.ops import retention_repository

_OTHER_ACCOUNT_ID = "00000000-0000-0000-0000-0000000000aa"


@pytest.fixture
def cur(pg_conn, default_account):
    del default_account
    pg_conn.rollback()
    with pg_conn.cursor() as cursor:
        try:
            yield cursor
        finally:
            pg_conn.rollback()


def _changelog(cur, *, entity_id: str, action: str, age_days: int, account_id: str | None):
    cur.execute(
        """
        INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot, author,
                                        created_at, account_id)
        VALUES ('agent', %s, %s, %s::jsonb, 'test', %s, %s)
        RETURNING id
        """,
        (
            entity_id,
            action,
            json.dumps({"age": age_days}),
            datetime.now(UTC) - timedelta(days=age_days),
            account_id,
        ),
    )
    return str(cur.fetchone()[0])


def _remaining(cur, entity_id: str) -> list[tuple[str, int]]:
    cur.execute(
        "SELECT action, (snapshot->>'age')::int FROM registry_changelog "
        "WHERE entity_id = %s ORDER BY created_at DESC",
        (entity_id,),
    )
    return [(str(action), int(age)) for action, age in cur.fetchall()]


def test_each_item_keeps_its_newest_revisions_however_old(cur) -> None:
    agent = f"agent-{uuid.uuid4()}"
    other = f"agent-{uuid.uuid4()}"
    for age in (400, 300, 200, 100, 50, 10):
        _changelog(cur, entity_id=agent, action="update", age_days=age, account_id=None)
    for age in (500, 450):
        _changelog(cur, entity_id=other, action="update", age_days=age, account_id=None)

    removed = retention_repository._delete_or_count_registry_changelog(
        cur, cache={}, account_id=DEFAULT_ACCOUNT_ID, keep=3, dry_run=False
    )

    assert removed >= 3
    assert _remaining(cur, agent) == [("update", 10), ("update", 50), ("update", 100)]
    # Two revisions, both more than a year old, are within the count and stay.
    assert _remaining(cur, other) == [("update", 450), ("update", 500)]


def test_lifecycle_and_dataset_assignment_rows_are_not_counted_or_removed(cur) -> None:
    agent = f"agent-{uuid.uuid4()}"
    _changelog(cur, entity_id=agent, action="trash", age_days=900, account_id=None)
    _changelog(
        cur, entity_id=agent, action="project_assignment_delete", age_days=800, account_id=None
    )
    for age in (30, 20, 10):
        _changelog(cur, entity_id=agent, action="update", age_days=age, account_id=None)
    _changelog(cur, entity_id=agent, action="restore", age_days=1, account_id=None)

    retention_repository._delete_or_count_registry_changelog(
        cur, cache={}, account_id=DEFAULT_ACCOUNT_ID, keep=1, dry_run=False
    )

    assert _remaining(cur, agent) == [
        ("restore", 1),
        ("update", 10),
        ("project_assignment_delete", 800),
        ("trash", 900),
    ]


def test_another_accounts_revisions_follow_their_own_setting(cur) -> None:
    agent = f"agent-{uuid.uuid4()}"
    for age in (30, 20, 10):
        _changelog(
            cur, entity_id=agent, action="update", age_days=age, account_id=_OTHER_ACCOUNT_ID
        )

    retention_repository._delete_or_count_registry_changelog(
        cur, cache={}, account_id=DEFAULT_ACCOUNT_ID, keep=1, dry_run=False
    )
    assert len(_remaining(cur, agent)) == 3

    retention_repository._delete_or_count_registry_changelog(
        cur, cache={}, account_id=_OTHER_ACCOUNT_ID, keep=2, dry_run=False
    )
    assert _remaining(cur, agent) == [("update", 10), ("update", 20)]


def test_dry_run_counts_without_deleting(cur) -> None:
    agent = f"agent-{uuid.uuid4()}"
    for age in (30, 20, 10):
        _changelog(cur, entity_id=agent, action="update", age_days=age, account_id=None)
    cur.execute(
        "SELECT count(*) FROM registry_changelog WHERE COALESCE(account_id::text, %s) = %s",
        (DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_ID),
    )
    total_before = int(cur.fetchone()[0])

    counted = retention_repository._delete_or_count_registry_changelog(
        cur, cache={}, account_id=DEFAULT_ACCOUNT_ID, keep=1, dry_run=True
    )

    assert counted >= 2
    assert len(_remaining(cur, agent)) == 3
    cur.execute(
        "SELECT count(*) FROM registry_changelog WHERE COALESCE(account_id::text, %s) = %s",
        (DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_ID),
    )
    assert int(cur.fetchone()[0]) == total_before


def test_project_settings_keep_their_newest_live_revisions(cur) -> None:
    project_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
        (project_id, f"Revision retention {project_id}", DEFAULT_ACCOUNT_ID),
    )
    for age in (40, 30, 20, 10):
        cur.execute(
            "INSERT INTO project_settings_revisions (project_id, snapshot_jsonb, created_at) "
            "VALUES (%s, %s::jsonb, %s)",
            (project_id, json.dumps({"age": age}), datetime.now(UTC) - timedelta(days=age)),
        )
    cur.execute(
        "INSERT INTO project_settings_revisions (project_id, snapshot_jsonb, created_at, "
        "deleted_at) VALUES (%s, %s::jsonb, %s, now())",
        (project_id, json.dumps({"age": 99}), datetime.now(UTC) - timedelta(days=99)),
    )

    removed = retention_repository._delete_or_count_project_settings_revisions(
        cur, cache={}, account_id=DEFAULT_ACCOUNT_ID, keep=2, dry_run=False
    )

    assert removed >= 2
    cur.execute(
        "SELECT (snapshot_jsonb->>'age')::int, deleted_at IS NOT NULL "
        "FROM project_settings_revisions WHERE project_id = %s ORDER BY created_at DESC",
        (project_id,),
    )
    # The trashed revision is the trash policy's to purge, not this one's.
    assert cur.fetchall() == [(10, False), (20, False), (99, True)]
