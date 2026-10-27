"""Real PostgreSQL acceptance coverage for workflow-secret hard deletion."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.scope import ScopeContext
from core.application.registry.workflow_secret_defaults import secret_default_ref_key
from core.application.workflows.workflow_secret_default_service import (
    WorkflowSecretDefaultService,
)
from core.infrastructure.db.platform_stores import PlatformProjectStore
from core.infrastructure.db.workflow_secret_default_repository import (
    PostgresWorkflowSecretDefaultRepository,
)
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration

FOREIGN_ACCOUNT_ID = "00000000-0000-0000-0000-000000000099"


def _require_audit_database(connection) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT current_database()")
        database_name = str(cur.fetchone()[0])
    if database_name != "flow_steward_audit":
        pytest.skip("destructive lifecycle regression requires the isolated pg-audit database")


def _scope() -> ScopeContext:
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="workflow-secret-trash-postgres",
        principal_type="admin",
    )


def _seed_workflow_secrets(connection, *, trashed: bool) -> dict[str, object]:
    workflow_id = f"secret-trash-{uuid4().hex}"
    other_workflow_id = f"secret-trash-sentinel-{uuid4().hex}"
    owned_refs = [
        secret_default_ref_key(workflow_id, field_name, version_token=str(version))
        for field_name in ("login", "token")
        for version in (1, 2, 3)
    ]
    other_workflow_ref = secret_default_ref_key(
        other_workflow_id,
        "login",
        version_token="1",
    )
    settings = {
        "callable": {
            "expected_inputs": [
                {
                    "name": "login",
                    "type": "secret",
                    "default_secret_ref": owned_refs[2],
                },
                {
                    "name": "token",
                    "type": "secret",
                    "default_secret_ref": owned_refs[5],
                },
            ]
        }
    }
    with connection.cursor() as cur:
        cur.execute(
            """INSERT INTO registry_workflows
                   (workflow_key, display_name, account_id, scope_kind, settings, deleted_at)
               VALUES (%s, %s, %s, 'account', %s::jsonb,
                       CASE WHEN %s THEN now() ELSE NULL END)""",
            (
                workflow_id,
                "Workflow secret trash acceptance",
                DEFAULT_ACCOUNT_ID,
                json.dumps(settings),
                trashed,
            ),
        )
        cur.executemany(
            """INSERT INTO secret_refs (account_id, ref_key, encrypted_value)
               VALUES (%s, %s, 'ciphertext-fixture')""",
            [(DEFAULT_ACCOUNT_ID, ref_key) for ref_key in owned_refs],
        )
        cur.execute(
            """INSERT INTO secret_refs (account_id, ref_key, encrypted_value)
               VALUES (%s, %s, 'ciphertext-fixture'),
                      (%s, %s, 'ciphertext-fixture')""",
            (
                DEFAULT_ACCOUNT_ID,
                other_workflow_ref,
                FOREIGN_ACCOUNT_ID,
                owned_refs[0],
            ),
        )
    connection.commit()
    return {
        "workflow_id": workflow_id,
        "owned_refs": owned_refs,
        "other_workflow_ref": other_workflow_ref,
    }


def _cleanup_workflow_secrets(connection, fixture: dict[str, object]) -> None:
    connection.rollback()
    owned_refs = list(fixture["owned_refs"])
    all_refs = [*owned_refs, str(fixture["other_workflow_ref"])]
    with connection.cursor() as cur:
        cur.execute(
            "DELETE FROM secret_refs WHERE ref_key = ANY(%s)",
            (all_refs,),
        )
        cur.execute(
            "DELETE FROM registry_workflows WHERE workflow_id = %s",
            (str(fixture["workflow_id"]),),
        )
    connection.commit()


def _counts(connection, fixture: dict[str, object]) -> dict[str, int]:
    workflow_id = str(fixture["workflow_id"])
    owned_refs = list(fixture["owned_refs"])
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM registry_workflows WHERE workflow_id = %s",
            (workflow_id,),
        )
        workflow_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM secret_refs WHERE account_id = %s AND ref_key = ANY(%s)",
            (DEFAULT_ACCOUNT_ID, owned_refs),
        )
        owned_secret_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM secret_refs WHERE account_id = %s AND ref_key = %s",
            (DEFAULT_ACCOUNT_ID, str(fixture["other_workflow_ref"])),
        )
        other_workflow_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM secret_refs WHERE account_id = %s AND ref_key = %s",
            (FOREIGN_ACCOUNT_ID, owned_refs[0]),
        )
        foreign_account_count = int(cur.fetchone()[0])
    # Release observer table locks before the service reloads registry schema.
    connection.commit()
    return {
        "workflow": workflow_count,
        "owned_secrets": owned_secret_count,
        "other_workflow": other_workflow_count,
        "foreign_account": foreign_account_count,
    }


def _service(
    dsn: str,
    *,
    service_type: type[TrashLifecycleService] = TrashLifecycleService,
) -> tuple[TrashLifecycleService, MagicMock]:
    project_store = PlatformProjectStore(dsn)
    audit = MagicMock()
    service = service_type(
        get_logger=lambda: project_store,
        get_audit_store=lambda: SimpleNamespace(log_action=audit),
        workflow_secret_default_service=WorkflowSecretDefaultService(
            PostgresWorkflowSecretDefaultRepository(project_store)
        ),
    )
    return service, audit


def test_soft_trash_restore_and_direct_hard_delete_have_exact_secret_retention(pg_conn) -> None:
    _require_audit_database(pg_conn)
    fixture = _seed_workflow_secrets(pg_conn, trashed=False)
    service, audit = _service(str(resolve_test_db_dsn()))
    initial = _counts(pg_conn, fixture)
    try:
        assert initial == {
            "workflow": 1,
            "owned_secrets": 6,
            "other_workflow": 1,
            "foreign_account": 1,
        }

        service.trash(
            "registry_workflow",
            str(fixture["workflow_id"]),
            scope=_scope(),
        )
        assert _counts(pg_conn, fixture) == initial

        restored = service.restore(
            "registry_workflow",
            str(fixture["workflow_id"]),
            scope=_scope(),
        )
        assert restored.status == "restored"
        assert _counts(pg_conn, fixture) == initial

        service.trash(
            "registry_workflow",
            str(fixture["workflow_id"]),
            scope=_scope(),
        )
        deleted = service.hard_delete(
            "registry_workflow",
            str(fixture["workflow_id"]),
            scope=_scope(),
        )

        assert deleted.status == "hard_deleted"
        assert _counts(pg_conn, fixture) == {
            "workflow": 0,
            "owned_secrets": 0,
            "other_workflow": 1,
            "foreign_account": 1,
        }
        audit.assert_called_once()
    finally:
        _cleanup_workflow_secrets(pg_conn, fixture)


def test_registry_workflow_plan_execute_hard_delete_purges_owned_secrets(pg_conn) -> None:
    _require_audit_database(pg_conn)
    fixture = _seed_workflow_secrets(pg_conn, trashed=True)
    service, audit = _service(str(resolve_test_db_dsn()))
    try:
        plan = service.plan(
            "registry_workflow",
            str(fixture["workflow_id"]),
            action_type="hard_delete",
            scope=_scope(),
        )
        result = service.execute_plan(
            "registry_workflow",
            str(fixture["workflow_id"]),
            action_type="hard_delete",
            selection={
                "plan_version": plan.plan_version,
                "selected_node_ids": [plan.tree["node_id"]],
            },
            scope=_scope(),
        )

        assert result.status == "hard_deleted"
        assert _counts(pg_conn, fixture) == {
            "workflow": 0,
            "owned_secrets": 0,
            "other_workflow": 1,
            "foreign_account": 1,
        }
        audit.assert_called_once()
    finally:
        _cleanup_workflow_secrets(pg_conn, fixture)


class _FailAfterWorkflowSecretPurge(TrashLifecycleService):
    def _purge_workflow_secrets(self, **kwargs) -> None:
        super()._purge_workflow_secrets(**kwargs)
        raise RuntimeError("forced failure after workflow secret purge")


def test_direct_hard_delete_rolls_back_secret_purge_on_intermediate_failure(pg_conn) -> None:
    _require_audit_database(pg_conn)
    fixture = _seed_workflow_secrets(pg_conn, trashed=True)
    service, audit = _service(
        str(resolve_test_db_dsn()),
        service_type=_FailAfterWorkflowSecretPurge,
    )
    initial = _counts(pg_conn, fixture)
    try:
        _raises_input_278_1 = str(fixture["workflow_id"])
        _raises_input_278_2 = _scope()
        with pytest.raises(RuntimeError, match="forced failure after workflow secret purge"):
            service.hard_delete("registry_workflow", _raises_input_278_1, scope=_raises_input_278_2)

        assert _counts(pg_conn, fixture) == initial
        audit.assert_not_called()
    finally:
        _cleanup_workflow_secrets(pg_conn, fixture)
