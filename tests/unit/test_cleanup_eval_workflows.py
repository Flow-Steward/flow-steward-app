from __future__ import annotations

from core.application.tooling.ops.cleanup_eval_workflows import (
    _cleanup_eval_workflows,
    _eval_workflow_ids,
    _prune_soft_deleted_eval_workflows,
    _select_eval_workflow_ids_sql,
)


class _FakeDB:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.pruned: list[str] = []
        self.select_calls: list[tuple[str, tuple[object, ...]]] = []

    def select_eval_workflow_ids(self, sql: str, params: tuple[object, ...]):
        self.select_calls.append((sql, params))
        return [
            {"workflow_id": "eval_m0_ticket_triage"},
            {"workflow_id": "eval_batch_invoice_emails"},
        ]

    def delete_workflow(self, workflow_id: str, author: str = "admin") -> None:
        self.deleted.append((workflow_id, author))

    def prune_eval_workflows(self, workflow_ids: list[str]) -> int:
        self.pruned.extend(workflow_ids)
        return len(workflow_ids)


def test_eval_workflow_ids_only_match_eval_prefix():
    db = _FakeDB()
    assert _eval_workflow_ids(db) == ["eval_batch_invoice_emails", "eval_m0_ticket_triage"]
    sql, params = db.select_calls[0]
    assert "LIKE %s ESCAPE '\\'" in sql
    assert "deleted_at IS NULL" in sql
    assert params == (r"eval\_%",)


def test_eval_workflow_sql_scopes_account_and_project():
    sql, params = _select_eval_workflow_ids_sql(
        account_id="00000000-0000-0000-0000-000000000002",
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000003",
        workflow_id_prefix="eval_run42_",
    )

    assert "w.account_id::text = %s" in sql
    assert "FROM project_workflows pw" in sql
    assert "JOIN projects p ON p.id = pw.project_id" in sql
    assert "p.tenant_id::text = %s" in sql
    assert params == (
        r"eval\_run42\_%",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
        "00000000-0000-0000-0000-000000000003",
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000001",
    )


def test_eval_workflow_sql_scopes_tenant_without_active_project_link():
    sql, params = _select_eval_workflow_ids_sql(
        account_id="acc",
        tenant_id="tenant",
        project_id="",
        workflow_id_prefix="eval_run42_",
    )

    assert "FROM project_workflows pw" in sql
    assert "pw.deleted_at IS NULL" not in sql
    assert "w.account_id::text = %s" in sql
    assert "p.tenant_id::text = %s" in sql
    assert params == (r"eval\_run42\_%", "acc", "", "", "tenant", "tenant")


def test_eval_workflow_sql_can_select_only_soft_deleted_for_prune():
    sql, params = _select_eval_workflow_ids_sql(
        account_id="acc",
        tenant_id="tenant",
        project_id="project",
        workflow_id_prefix="eval_run42_",
        only_deleted=True,
    )

    assert "w.deleted_at IS NOT NULL" in sql
    assert "w.account_id::text = %s" in sql
    assert "p.tenant_id::text = %s" in sql
    assert params == (r"eval\_run42\_%", "acc", "project", "project", "tenant", "tenant")


def test_cleanup_eval_workflows_dry_run_does_not_delete():
    db = _FakeDB()

    payload = _cleanup_eval_workflows(db, apply=False, author="tester")

    assert payload["dry_run"] is True
    assert payload["matched_workflow_ids"] == [
        "eval_batch_invoice_emails",
        "eval_m0_ticket_triage",
    ]
    assert payload["deleted_workflow_ids"] == []
    assert db.deleted == []
    assert db.select_calls


def test_cleanup_eval_workflows_apply_deletes_only_eval_prefix():
    db = _FakeDB()

    payload = _cleanup_eval_workflows(db, apply=True, author="tester")

    assert payload["dry_run"] is False
    assert payload["deleted_workflow_ids"] == [
        "eval_batch_invoice_emails",
        "eval_m0_ticket_triage",
    ]
    assert db.deleted == [
        ("eval_batch_invoice_emails", "tester"),
        ("eval_m0_ticket_triage", "tester"),
    ]


def test_cleanup_eval_workflows_scopes_to_run_prefix():
    db = _FakeDB()

    payload = _cleanup_eval_workflows(
        db,
        apply=True,
        author="tester",
        account_id="acc",
        workflow_id_prefix="eval_run42_",
    )

    sql, params = db.select_calls[0]
    assert "w.account_id::text = %s" in sql
    assert params == (r"eval\_run42\_%", "acc")
    assert payload["workflow_id_prefix"] == "eval_run42_"


def test_prune_soft_deleted_eval_workflows_is_dry_run_by_default():
    db = _FakeDB()

    payload = _prune_soft_deleted_eval_workflows(db, apply=False, account_id="acc")

    sql, params = db.select_calls[0]
    assert "w.deleted_at IS NOT NULL" in sql
    assert params == (r"eval\_%", "acc")
    assert payload["dry_run"] is True
    assert payload["matched_count"] == 2
    assert payload["pruned_count"] == 0
    assert db.pruned == []


def test_prune_soft_deleted_eval_workflows_apply_uses_scoped_ids():
    db = _FakeDB()

    payload = _prune_soft_deleted_eval_workflows(
        db,
        apply=True,
        account_id="acc",
        tenant_id="tenant",
        project_id="project",
        workflow_id_prefix="eval_run42_",
    )

    sql, params = db.select_calls[0]
    assert "w.deleted_at IS NOT NULL" in sql
    assert params == (r"eval\_run42\_%", "acc", "project", "project", "tenant", "tenant")
    assert payload["pruned_count"] == 2
    assert db.pruned == ["eval_batch_invoice_emails", "eval_m0_ticket_triage"]
