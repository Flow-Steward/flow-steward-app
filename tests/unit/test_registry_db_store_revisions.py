from __future__ import annotations

import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from core.application.registry.db_store import RegistryDBStore, RestoreValidationError
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def test_save_workflow_metadata_only_update_still_logs_changelog():
    db = RegistryDBStore("postgres://unused")
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(return_value=False)
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._log_change_cur = MagicMock()

    class _Cursor:
        def execute(self, _sql, _params=None):
            return None

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.save_workflow(
        {
            "workflow_id": "wf_meta_only",
            "display_name": "WF Meta Only",
            "description": "metadata update",
        },
        author="unit-test",
    )

    assert db._log_change_cur.call_count == 1
    args = db._log_change_cur.call_args.args
    assert args[1] == "workflow"
    assert args[2] == "wf_meta_only"
    assert args[3] == "create"
    assert args[4]["history_schema_version"] == 1


def test_save_workflow_revision_compare_and_swap_is_atomic():
    db = RegistryDBStore("postgres://unused")
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=True)
    db._column_exists = MagicMock(
        side_effect=lambda table, column: table == "registry_workflows" and column == "revision"
    )
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchone(self):
            if "FOR UPDATE" in executed[-1][0]:
                return {"revision": 3}
            return None

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    with pytest.raises(WorkflowRevisionConflictError):
        db.save_workflow(
            {
                "workflow_id": "wf_compare_swap",
                "display_name": "Stale update",
            },
            expected_revision=3,
        )

    upsert_sql, params = next(
        (sql, params) for sql, params in executed if "INSERT INTO registry_workflows" in sql
    )
    assert "revision = registry_workflows.revision + 1" in upsert_sql
    assert "WHERE registry_workflows.revision = %(expected_revision)s" in upsert_sql
    assert "RETURNING revision" in upsert_sql
    assert params["expected_revision"] == 3
    db._log_change_cur.assert_not_called()


def test_save_workflow_bulk_steps_preserves_connector_install_identity():
    db = RegistryDBStore("postgres://unused")
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(return_value=False)
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchone(self):
            return {"id": "group-row"}

        def fetchall(self):
            # This fixture starts with no workflow reaction rules.
            return []

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.save_workflow(
        {
            "workflow_id": "wf_connector_install",
            "display_name": "Connector install workflow",
            "steps": [
                {
                    "step_id": "send_email",
                    "step_kind": "connector",
                    "connector_id": "extension.mail.send_message",
                    "connector_install_id": "install-mail-current",
                }
            ],
            "groups": [],
            "for_each": [],
        },
        author="unit-test",
    )

    step_inserts = [
        (sql, params) for sql, params in executed if "INSERT INTO registry_workflow_steps" in sql
    ]
    connector_step_inserts = [
        (sql, params)
        for sql, params in step_inserts
        if "extension.mail.send_message" in (params or ())
    ]
    assert len(connector_step_inserts) == 1
    step_sql, step_params = connector_step_inserts[0]
    assert "connector_install_id" in step_sql
    assert "install-mail-current" in step_params


def test_clone_workflow_preserves_connector_install_identity():
    db = RegistryDBStore("postgres://unused")
    db.get_workflow = MagicMock(
        side_effect=[
            {
                "workflow_id": "wf_source",
                "display_name": "Source",
                "steps": [
                    {
                        "step_id": "send_email",
                        "step_kind": "connector",
                        "connector_id": "extension.mail.send_message",
                        "connector_install_id": "install-mail-current",
                    }
                ],
                "groups": [],
            },
            None,
        ]
    )
    db.save_workflow = MagicMock()

    db.clone_workflow(
        "wf_source",
        "wf_clone",
        account_id="account-1",
        scope_kind="account",
        author="unit-test",
    )

    saved = db.save_workflow.call_args.args[0]
    assert saved["steps"][0]["connector_install_id"] == ("install-mail-current")


def _step_insert_columns(sql: str) -> list[str]:
    """The column list of the step upsert, in the order of its parameters."""
    start = sql.index("registry_workflow_steps") + len("registry_workflow_steps")
    inner = sql[sql.index("(", start) + 1 : sql.index(")", start)]
    return [column.strip() for column in inner.split(",")]


@pytest.mark.parametrize(("stored", "expected"), [(True, True), (False, False), ("yes", False)])
def test_save_workflow_writes_the_step_disabled_flag(stored: object, expected: bool) -> None:
    """A disabled step is stored as disabled; only a literal true disables it."""
    db = RegistryDBStore("postgres://unused")
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(return_value=False)
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchone(self):
            return {"id": "group-row"}

        def fetchall(self):
            return []

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.save_workflow(
        {
            "workflow_id": "wf_disabled_step",
            "display_name": "Disabled step",
            "steps": [{"step_id": "draft", "step_kind": "transform", "disabled": stored}],
            "groups": [],
            "for_each": [],
        },
        author="unit-test",
    )

    step_sql, params = next(
        (sql, params) for sql, params in executed if "INSERT INTO registry_workflow_steps" in sql
    )
    columns = _step_insert_columns(step_sql)
    assert len(columns) == len(params)
    assert params[columns.index("disabled")] is expected
    assert "disabled = EXCLUDED.disabled" in step_sql


def test_clone_workflow_keeps_a_disabled_step_disabled():
    db = RegistryDBStore("postgres://unused")
    db.get_workflow = MagicMock(
        side_effect=[
            {
                "workflow_id": "wf_source",
                "steps": [{"step_id": "draft", "step_kind": "transform", "disabled": True}],
                "groups": [],
            },
            None,
        ]
    )
    db.save_workflow = MagicMock()

    db.clone_workflow("wf_source", "wf_clone", account_id="a", scope_kind="account", author="t")

    assert db.save_workflow.call_args.args[0]["steps"][0]["disabled"] is True


@pytest.mark.parametrize("source_enabled", [True, False])
def test_a_clone_is_always_created_disabled(source_enabled: bool) -> None:
    """The owner's decision (2026-09-25): a clone into another account or project can
    miss what the source had (a tool, a connection), and it was created enabled with no
    readiness check. Like an import, it starts disabled; its author enables it."""
    db = RegistryDBStore("postgres://unused")
    source = {"workflow_id": "wf_source", "enabled": source_enabled, "steps": [], "groups": []}
    db.get_workflow = MagicMock(side_effect=[source, None])
    db.save_workflow = MagicMock()

    db.clone_workflow(
        "wf_source", "wf_clone", account_id="account-1", scope_kind="account", author="t"
    )

    (call,) = db.save_workflow.call_args_list  # the source is not written
    assert call.args[0]["workflow_id"] == "wf_clone"
    assert call.args[0]["enabled"] is False
    assert source["enabled"] is source_enabled


def test_workflow_revision_restore_preserves_connector_install_identity():
    db = RegistryDBStore("postgres://unused")
    db.save_workflow = MagicMock()
    db.get_workflow = MagicMock(return_value=None)
    snapshot = {
        "workflow_id": "wf_snapshot",
        "steps": [
            {
                "step_id": "send_email",
                "connector_install_id": "install-mail-current",
            }
        ],
        "groups": [],
    }

    db.apply_entity_snapshot(
        "workflow",
        "wf_snapshot",
        snapshot,
        author="unit-test",
    )

    restored = db.save_workflow.call_args.args[0]
    assert restored["steps"][0]["connector_install_id"] == ("install-mail-current")


def test_definition_write_rolls_back_when_dependency_sync_fails():
    """Steps and their runtime dependencies are written in one transaction."""
    db = RegistryDBStore("postgres://unused")
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(return_value=False)
    db._log_change_cur = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    transaction_state = {"active": False, "committed": False, "rolled_back": False}

    class _Cursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return {"id": "group-row"}

        def fetchall(self):
            # This fixture starts with no workflow reaction rules.
            return []

    @contextmanager
    def _tx():
        transaction_state["active"] = True
        try:
            yield _Cursor()
        except Exception:
            transaction_state["rolled_back"] = True
            raise
        else:
            transaction_state["committed"] = True
        finally:
            transaction_state["active"] = False

    def _fail_dependency_sync(_workflow_id, **_kwargs):
        assert transaction_state["active"] is True
        raise RuntimeError("dependency install foreign key is invalid")

    db._transaction = _tx
    db._sync_workflow_runtime_dependencies = _fail_dependency_sync

    with pytest.raises(RuntimeError, match="dependency install foreign key is invalid"):
        db.save_workflow(
            {
                "workflow_id": "wf_atomic_step",
                "display_name": "Atomic",
                "steps": [
                    {
                        "step_id": "send",
                        "step_kind": "connector",
                        "connector_id": "extension.mail.send_message",
                        "connector_install_id": "install-stale",
                    }
                ],
                "groups": [],
                "for_each": [],
            }
        )

    assert transaction_state == {"active": False, "committed": False, "rolled_back": True}


def test_agent_step_with_runtime_derived_empty_skill_writes_null():
    db = RegistryDBStore("postgres://unused")
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(return_value=False)
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._log_change_cur = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchone(self):
            return None

        def fetchall(self):
            # This fixture starts with no workflow reaction rules.
            return []

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.save_workflow(
        {
            "workflow_id": "wf_agent_catalog",
            "display_name": "Agent",
            "steps": [
                {
                    "step_id": "summarize",
                    "step_kind": "agent",
                    "agent_id": "summarizer",
                    "skill_id": "",
                }
            ],
            "groups": [],
            "for_each": [],
        }
    )

    _sql, params = next(
        (sql, params) for sql, params in executed if "INSERT INTO registry_workflow_steps" in sql
    )
    assert "summarizer" in params
    assert params[params.index("summarizer") + 1] is None


def test_skill_contract_and_enabled_mutations_write_complete_changelog_snapshots():
    """AUDIT-029: every persisted Skill contract/status change is reversible/auditable."""
    db = RegistryDBStore("postgres://unused")
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    db.get_skill = MagicMock(return_value={"skill_id": "review_summary", "enabled": True})

    class _Cursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return {"enabled": False}

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx
    db._cursor = _tx

    db.set_skill_inputs("review_summary", [{"name": "story_id", "type": "text", "required": True}])
    db.set_skill_outputs("review_summary", [{"name": "verdict", "type": "text", "required": True}])
    assert db.toggle_skill("review_summary") is False

    assert db._log_change_cur.call_count == 3
    snapshots = [call.args[4] for call in db._log_change_cur.call_args_list]
    assert snapshots[0]["expected_inputs"] == [
        {"name": "story_id", "type": "text", "required": True}
    ]
    assert snapshots[1]["expected_outputs"] == [
        {"name": "verdict", "type": "text", "required": True}
    ]
    assert snapshots[2]["enabled"] is False


def test_skill_contract_mutations_normalize_empty_contract_id_to_null():
    """Unlinked UI rows send an empty contract id; SQL foreign keys require NULL."""
    db = RegistryDBStore("postgres://unused")
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    db.get_skill = MagicMock(return_value={"skill_id": "review_summary", "enabled": False})
    executed: list[tuple[str, object]] = []

    class _Cursor:
        rowcount = 1

        def execute(self, sql, params=None):
            executed.append((sql, params))

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.set_skill_inputs(
        "review_summary",
        [{"name": "story_id", "type": "text", "contract_id": ""}],
    )
    db.set_skill_outputs(
        "review_summary",
        [{"name": "verdict", "type": "text", "contract_id": ""}],
    )

    inserts = [params for sql, params in executed if "INSERT INTO registry_skill_" in sql]
    assert len(inserts) == 2
    assert [params[-1] for params in inserts] == [None, None]


def test_save_tool_preserves_transport_account_ownership():
    db = RegistryDBStore("postgres://unused")
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(
        side_effect=lambda table, column: table == "registry_tools" and column == "account_id"
    )
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchone(self):
            return None

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.save_tool(
        {
            "tool_id": "account_tool",
            "display_name": "Account Tool",
            "account_id": "acc_beta",
            "enabled": False,
            "interface": {},
            "implementation": {},
            "config": {},
        },
        author="admin-beta",
    )

    inserts = [params for sql, params in executed if "INSERT INTO registry_tools" in sql]
    assert len(inserts) == 1
    assert inserts[0]["account_id"] == "acc_beta"


def test_list_skills_applies_nullable_account_boundary():
    db = RegistryDBStore("postgres://unused")
    db._column_exists = MagicMock(
        side_effect=lambda table, column: table == "registry_skills" and column == "account_id"
    )
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchall(self):
            return []

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor

    assert db.list_skills(account_id="acc-owner") == []

    sql, params = executed[0]
    assert "account_id = %(account_id)s" in sql
    assert params["account_id"] == "acc-owner"


def test_save_workflow_resurrects_soft_deleted_workflow_id():
    """Regression: delete_workflow is a SOFT delete (sets deleted_at). Re-saving a
    previously-deleted workflow_id hits ON CONFLICT DO UPDATE; without clearing
    deleted_at there, the re-created workflow stays trashed and invisible to
    get_workflow / list_workflows. The upsert must reset deleted_at/deleted_by."""
    db = RegistryDBStore("postgres://unused")
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "stored", "steps": []})
    db._exists = MagicMock(return_value=True)  # the soft-deleted row already exists
    db._column_exists = MagicMock(
        side_effect=lambda _table, col: col in {"deleted_at", "deleted_by"}
    )
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql, _params=None):
            executed.append(sql)
            return

        def fetchone(self):
            # No workflow row for the consumer guard: nobody calls it yet.
            return None

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx

    db.save_workflow(
        {"workflow_id": "wf_resurrect", "display_name": "Resurrected"},
        author="unit-test",
    )

    upserts = [s for s in executed if "INSERT INTO registry_workflows" in s]
    assert upserts, "expected an upsert into registry_workflows"
    assert "deleted_at = NULL" in upserts[0]
    assert "deleted_by = NULL" in upserts[0]


def _serve_history(db, history: list[dict]) -> None:
    """History reads, listed and by entry id, served from *history* (newest first)."""
    db.get_entity_history = MagicMock(
        side_effect=lambda **kwargs: list(history)[: kwargs.get("limit") or len(history)]
    )
    by_id = {str(entry.get("id")): entry for entry in history}
    db.get_changelog_entry = MagicMock(
        side_effect=lambda entry_id, **_kwargs: by_id.get(str(entry_id))
    )


def test_diff_entity_history_defaults_to_latest_vs_previous():
    db = RegistryDBStore("postgres://unused")
    _serve_history(
        db,
        [
            {
                "id": 20,
                "created_at": "2026-03-14T10:00:00Z",
                "snapshot": {"display_name": "New", "enabled": True},
            },
            {
                "id": 10,
                "created_at": "2026-03-14T09:00:00Z",
                "snapshot": {"display_name": "Old", "enabled": False},
            },
        ],
    )

    diff = db.diff_entity_history(entity_type="agent", entity_id="proofreader")
    assert diff["comparison_kind"] == "content"
    assert diff["to_entry_id"] == "20"
    assert diff["from_entry_id"] == "10"
    assert diff["summary"]["total"] >= 1
    assert any(change["path"] == "display_name" for change in diff["changes"])


def test_history_read_projection_labels_lifecycle_and_content_without_mutating_rows():
    db = RegistryDBStore("postgres://unused")
    stored_rows = [
        {
            "id": index,
            "action": action,
            "author": f"author-{action}",
            "snapshot": {"value": action},
        }
        for index, action in enumerate(
            ("trash", " Restore ", "hard_delete", "create", "update", "delete"), start=1
        )
    ]
    for row in stored_rows:
        row["revision_kind"] = (
            "content"
            if str(row["action"]).strip().lower() in {"trash", "restore", "hard_delete", "delete"}
            else "lifecycle"
        )
    stored_before = deepcopy(stored_rows)
    db.get_changelog = MagicMock(return_value=stored_rows)

    history = db.get_entity_history("agent", "history-kind-agent")

    assert [entry["revision_kind"] for entry in history] == [
        "lifecycle",
        "lifecycle",
        "lifecycle",
        "content",
        "content",
        "lifecycle",
    ]
    assert [entry["action"] for entry in history] == [row["action"] for row in stored_before]
    assert [entry["author"] for entry in history] == [row["author"] for row in stored_before]
    assert [entry["snapshot"] for entry in history] == [row["snapshot"] for row in stored_before]
    assert stored_rows == stored_before


@pytest.mark.parametrize("lifecycle_action", [" Restore ", "delete"])
def test_changelog_list_projects_derived_revision_kind_without_mutating_rows(lifecycle_action):
    db = RegistryDBStore("postgres://unused")
    stored_rows = [
        {
            "id": "list-lifecycle",
            "action": lifecycle_action,
            "revision_kind": "content",
            "author": "lifecycle-author",
            "snapshot": {"agent_id": "history-kind-agent", "display_name": "Before deletion"},
        },
        {
            "id": "list-content",
            "action": "update",
            "revision_kind": "lifecycle",
            "author": "content-author",
            "snapshot": {"display_name": "Updated"},
        },
    ]
    stored_before = deepcopy(stored_rows)
    executed = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchall(self):
            return stored_rows

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor

    rows = db.get_changelog(
        entity_type="agent",
        entity_id="history-kind-agent",
        limit=7,
        account_id="account-owner",
    )

    assert [row["revision_kind"] for row in rows] == ["lifecycle", "content"]
    assert [row["action"] for row in rows] == [lifecycle_action, "update"]
    assert [row["author"] for row in rows] == ["lifecycle-author", "content-author"]
    assert [row["snapshot"] for row in rows] == [
        {"agent_id": "history-kind-agent", "display_name": "Before deletion"},
        {"display_name": "Updated"},
    ]
    assert stored_rows == stored_before
    assert len(executed) == 1
    assert "entity_type = %s" in executed[0][0]
    assert "entity_id = %s" in executed[0][0]
    assert "account_id::text = %s" in executed[0][0]
    assert executed[0][1] == ["agent", "history-kind-agent", "account-owner", 7]


@pytest.mark.parametrize(
    ("action", "expected_kind"),
    [
        ("trash", "lifecycle"),
        (" Restore ", "lifecycle"),
        ("hard_delete", "lifecycle"),
        ("create", "content"),
        ("update", "content"),
        ("delete", "lifecycle"),
        (" Delete ", "lifecycle"),
    ],
)
def test_changelog_entry_read_projection_labels_revision_kind_without_mutating_row(
    action, expected_kind
):
    db = RegistryDBStore("postgres://unused")
    stored_row = {
        "id": "entry-kind",
        "entity_type": "agent",
        "entity_id": "history-kind-agent",
        "action": action,
        "author": f"author-{action}",
        "snapshot": {"value": action},
        "revision_kind": "content" if expected_kind == "lifecycle" else "lifecycle",
    }
    stored_before = deepcopy(stored_row)

    class _Cursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return stored_row

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor

    entry = db.get_changelog_entry(
        "entry-kind",
        entity_type="agent",
        entity_id="history-kind-agent",
    )

    assert entry["revision_kind"] == expected_kind
    assert entry["action"] == stored_before["action"]
    assert entry["author"] == stored_before["author"]
    assert entry["snapshot"] == stored_before["snapshot"]
    assert stored_row == stored_before


@pytest.mark.parametrize(
    ("history", "from_entry_id", "to_entry_id", "expected_from", "expected_to"),
    [
        (
            [
                {
                    "id": 20,
                    "action": "restore",
                    "revision_kind": "content",
                    "snapshot": {"lifecycle_action": "restore"},
                },
                {
                    "id": 10,
                    "action": "update",
                    "revision_kind": "lifecycle",
                    "snapshot": {"display_name": "Before"},
                },
            ],
            None,
            None,
            "10",
            "20",
        ),
        (
            [
                {
                    "id": 20,
                    "action": "restore",
                    "revision_kind": "content",
                    "snapshot": {"lifecycle_action": "restore"},
                },
                {
                    "id": 10,
                    "action": "update",
                    "revision_kind": "lifecycle",
                    "snapshot": {"display_name": "Before"},
                },
            ],
            "20",
            "10",
            "20",
            "10",
        ),
        (
            [
                {
                    "id": 30,
                    "action": "restore",
                    "revision_kind": "content",
                    "snapshot": {"lifecycle_action": "restore"},
                },
                {
                    "id": 20,
                    "action": "trash",
                    "revision_kind": "content",
                    "snapshot": {"lifecycle_action": "trash"},
                },
            ],
            "20",
            "30",
            "20",
            "30",
        ),
        (
            [
                {
                    "id": 20,
                    "action": "hard_delete",
                    "revision_kind": "content",
                    "snapshot": {"lifecycle_action": "hard_delete"},
                }
            ],
            None,
            None,
            None,
            "20",
        ),
    ],
)
def test_diff_entity_history_classifies_lifecycle_comparisons_without_content_counts(
    history,
    from_entry_id,
    to_entry_id,
    expected_from,
    expected_to,
):
    db = RegistryDBStore("postgres://unused")
    stored_before = deepcopy(history)
    _serve_history(db, history)

    diff = db.diff_entity_history(
        entity_type="agent",
        entity_id="history-kind-agent",
        from_entry_id=from_entry_id,
        to_entry_id=to_entry_id,
    )

    assert diff["changes"] == []
    assert diff["summary"] == {}
    assert diff["comparison_kind"] == "lifecycle"
    assert diff["from_entry_id"] == expected_from
    assert diff["to_entry_id"] == expected_to
    assert history == stored_before


@pytest.mark.parametrize(
    ("from_entry_id", "to_entry_id", "only_delete", "expected_from", "expected_to"),
    [
        ("content-10", "delete-20", False, "content-10", "delete-20"),
        ("delete-20", "content-10", False, "delete-20", "content-10"),
        (None, None, False, "content-10", "delete-20"),
        (None, None, True, None, "delete-20"),
    ],
)
def test_full_delete_snapshot_diff_is_not_a_content_comparison(
    from_entry_id, to_entry_id, only_delete, expected_from, expected_to
):
    db = RegistryDBStore("postgres://unused")
    history = [
        {
            "id": "delete-20",
            "action": "delete",
            "revision_kind": "content",
            "snapshot": {
                "workflow_id": "owned-workflow",
                "display_name": "Before deletion",
                "enabled": False,
                "settings": {"interfaces": []},
                "steps": [{"step_id": "start", "step_kind": "set_variable"}],
                "updated_at": "2026-10-04T12:00:00Z",
            },
        },
    ]
    if not only_delete:
        history.append(
            {
                "id": "content-10",
                "action": "update",
                "revision_kind": "lifecycle",
                "snapshot": {"workflow_id": "owned-workflow", "display_name": "Older content"},
            }
        )
    before = deepcopy(history)
    _serve_history(db, history)

    result = db.diff_entity_history(
        "workflow", "owned-workflow", from_entry_id=from_entry_id, to_entry_id=to_entry_id
    )

    assert result["comparison_kind"] == "lifecycle"
    assert result["changes"] == []
    assert result["summary"] == {}
    assert result["from_entry_id"] == expected_from
    assert result["to_entry_id"] == expected_to
    assert history == before


def test_full_delete_snapshot_remains_replayable_with_lifecycle_presentation():
    """Presentation must not replace the compact-marker replay guard.

    A Delete event shows as lifecycle but keeps its full snapshot, so a
    workflow restore may still take it back; a compact Trash marker never.
    """
    db = RegistryDBStore("postgres://unused")
    full = {
        "workflow_id": "owned-workflow",
        "display_name": "Before deletion",
        "settings": {},
        "steps": [{"step_id": "start", "step_kind": "set_variable"}],
        "groups": [],
        "for_each": [],
    }
    deleted = db._project_history_entry(
        {"id": "d", "entity_type": "workflow", "action": "delete", "snapshot": full}
    )
    trashed = db._project_history_entry(
        {"id": "t", "entity_type": "workflow", "action": "trash", "snapshot": full}
    )

    assert deleted["revision_kind"] == trashed["revision_kind"] == "lifecycle"
    assert deleted["restorable"] is True
    assert trashed["restorable"] is False
    assert deleted["revision"] is None


def test_restore_entity_revision_returns_validation_payload():
    db = RegistryDBStore("postgres://unused")
    db.get_changelog_entry = MagicMock(
        return_value={
            "id": 101,
            "entity_type": "tool",
            "entity_id": "search_tool",
            "snapshot": {"tool_id": "search_tool", "display_name": "Search Tool"},
        }
    )
    db.apply_entity_snapshot = MagicMock(
        return_value={"tool_id": "search_tool", "display_name": "Search Tool"}
    )

    @contextmanager
    def _tx():
        yield object()

    db._transaction = _tx

    outcome = db.restore_entity_revision(
        entity_type="tool",
        entity_id="search_tool",
        entry_id=101,
        author="unit-test",
        validate_restored=lambda *_args: {"valid": True, "errors": [], "warnings": []},
    )

    assert outcome["entry"]["id"] == 101
    assert outcome["restored"]["tool_id"] == "search_tool"
    assert outcome["validation"]["valid"] is True
    db.apply_entity_snapshot.assert_called_once()


def test_restore_entity_revision_raises_restore_validation_error():
    db = RegistryDBStore("postgres://unused")
    db.get_changelog_entry = MagicMock(
        return_value={
            "id": 102,
            "entity_type": "guardrail",
            "entity_id": "11",
            "snapshot": {
                "id": 11,
                "display_name": "Rule",
                "mode": "deterministic",
                "rule_type": "custom",
            },
        }
    )
    db.apply_entity_snapshot = MagicMock(return_value={"id": 11})
    db.save_guardrail = MagicMock(return_value={"id": 11})
    db.get_guardrail = MagicMock(return_value={"id": 11})

    @contextmanager
    def _tx():
        yield object()

    db._transaction = _tx

    with pytest.raises(RestoreValidationError):
        db.restore_entity_revision(
            entity_type="guardrail",
            entity_id="11",
            entry_id=102,
            author="unit-test",
            account_id="account-a",
            actor_id="user-a",
            actor_type="user",
            validate_restored=lambda *_args: {
                "valid": False,
                "errors": [{"message": "Guardrail mode is invalid"}],
                "warnings": [],
            },
        )


def test_cursor_normalizes_stale_shared_connection_outside_transaction():
    db = RegistryDBStore("postgres://unused")

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def __init__(self):
            self.closed = 0
            self.autocommit = False
            self.rollback_calls = 0

        def get_transaction_status(self):
            return psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS

        def rollback(self):
            self.rollback_calls += 1

        def cursor(self, cursor_factory=None):
            return _Cursor()

    conn = _Conn()
    db._conn = conn

    with db._cursor() as cur:
        assert isinstance(cur, _Cursor)

    assert conn.rollback_calls == 1
    assert conn.autocommit is True


def _block_store_with_row(row: dict | None) -> RegistryDBStore:
    """Build a RegistryDBStore whose cursor yields *row* for a block-contract fetch."""
    db = RegistryDBStore("postgres://unused")

    class _Cursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return dict(row) if isinstance(row, dict) else None

        def fetchall(self):
            return []

    @contextmanager
    def _cur():
        yield _Cursor()

    db._cursor = _cur
    return db


def test_get_published_block_contract_denies_cross_account(monkeypatch):
    """FS-DEEP-CAND-019: an account-owned published block must not resolve for a
    caller in a different account when account scope is supplied."""
    db = _block_store_with_row(
        {"block_key": "blk_b", "account_id": "account-B", "scope_kind": "account"}
    )
    assert db.get_published_block_contract("blk_b", account_id="account-A") is None


def test_get_published_block_contract_allows_same_account():
    db = _block_store_with_row(
        {"block_key": "blk_b", "account_id": "account-B", "scope_kind": "account"}
    )
    contract = db.get_published_block_contract("blk_b", account_id="account-B")
    assert contract is not None
    assert contract["block_key"] == "blk_b"


def test_get_published_block_contract_allows_global_block():
    """A platform-global block (NULL account_id) stays resolvable for any account."""
    db = _block_store_with_row({"block_key": "blk_sys", "account_id": None, "scope_kind": "system"})
    contract = db.get_published_block_contract("blk_sys", account_id="account-A")
    assert contract is not None
    assert contract["block_key"] == "blk_sys"


def test_get_published_block_contract_unscoped_call_is_unchanged():
    """No account_id argument => legacy global behavior (no extra filtering)."""
    db = _block_store_with_row(
        {"block_key": "blk_b", "account_id": "account-B", "scope_kind": "account"}
    )
    assert db.get_published_block_contract("blk_b") is not None


@pytest.mark.parametrize("has_revision", [True, False])
@pytest.mark.parametrize("conflict_constraint", ["workflow_key", "workflow_id"])
def test_registry_atomic_create_refuses_conflict_before_changing_graph_or_history(
    has_revision, conflict_constraint
):
    from core.application.registry.db_store_workflows_mixin import RegistryDBStoreWorkflowsMixin

    class Cursor:
        def __init__(self):
            self.rowcount = 0
            self.executions = []

        def execute(self, sql, params=None):
            self.executions.append(sql)
            if conflict_constraint == "workflow_id" and "ON CONFLICT (workflow_key)" in sql:
                from psycopg.errors import UniqueViolation

                raise UniqueViolation("registry_workflows_workflow_id_key")
            if "DO UPDATE" in sql:
                self.rowcount = 1

        def fetchone(self):
            return {"revision": 2} if self.rowcount else None

    class Store(RegistryDBStoreWorkflowsMixin):
        def __init__(self):
            self.cursor = Cursor()
            self.history = []

        def _exists(self, *args):
            return False  # A racing writer inserts after this optimistic read.

        def _column_exists(self, table, column):
            return has_revision and table == "registry_workflows" and column == "revision"

        @contextmanager
        def _transaction(self):
            yield self.cursor

        def _log_change_cur(self, *args):
            self.history.append(args)

        def _sync_workflow_runtime_dependencies(self, *args, **kwargs):
            pass

        def _trigger_public_contract_projection_sync(self):
            pass

    store = Store()
    with pytest.raises(ValueError, match="already exists"):
        store.save_workflow(
            {"workflow_id": "taken", "steps": [], "groups": [], "for_each": []}, create_only=True
        )
    assert len(store.cursor.executions) == 1
    assert store.history == []


def _changelog_recorder(db: RegistryDBStore) -> list[tuple[str, object]]:
    executed: list[tuple[str, object]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), params))

        def fetchall(self):
            return []

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor
    return executed


@pytest.mark.parametrize("entity_type", [None, "", "workflow"])
def test_cross_entity_changelog_leaves_out_hook_storage_workflow_revisions(entity_type):
    db = RegistryDBStore("postgres://unused")
    executed = _changelog_recorder(db)

    db.get_changelog(entity_type=entity_type, limit=9)

    sql, params = executed[0]
    assert "settings ? 'hook_storage'" in sql
    assert "COALESCE(snapshot->'settings', '{}'::jsonb) ? 'hook_storage'" in sql
    assert "entity_type = 'workflow'" in sql
    assert params == ([entity_type, 9] if entity_type else [9])


def test_changelog_of_one_entity_and_other_entity_types_keep_their_filters_unchanged():
    db = RegistryDBStore("postgres://unused")
    executed = _changelog_recorder(db)

    db.get_changelog(entity_type="agent", limit=5)
    db.get_changelog(entity_type="workflow", entity_id="wf-1", limit=5)

    assert all("hook_storage" not in sql for sql, _params in executed)


def test_changelog_name_search_leaves_out_hook_storage_workflows():
    db = RegistryDBStore("postgres://unused")
    executed = _changelog_recorder(db)
    db._column_exists = MagicMock(return_value=False)

    db.search_entities("order", entity_type="workflow")
    db.search_entities("order", entity_type="agent")

    workflow_sql, agent_sql = executed[0][0], executed[1][0]
    assert "NOT (settings ? 'hook_storage')" in workflow_sql
    assert "hook_storage" not in agent_sql
