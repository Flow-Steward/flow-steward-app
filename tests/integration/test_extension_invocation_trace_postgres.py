from __future__ import annotations

from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.infrastructure.db.platform_stores import PlatformObservabilityStore


@pytest.mark.integration
def test_canonical_extension_invocation_trace_is_structured_and_job_scoped(
    disposable_db,
) -> None:
    suffix = uuid4().hex
    project_a = str(uuid4())
    project_b = str(uuid4())
    job_a = str(uuid4())
    job_b = str(uuid4())
    workflow_id = f"fixture.trace.{suffix}"
    extension_key = f"fixture.trace-extension-{suffix}"

    store = PlatformObservabilityStore(disposable_db.dsn)
    try:
        with store.conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'extension_invocation_logs'
                  AND column_name = ANY(%s)
                """,
                (["job_id", "workflow_id", "group_id", "step_id"],),
            )
            assert dict(cur.fetchall()) == {
                "job_id": "uuid",
                "workflow_id": "text",
                "group_id": "text",
                "step_id": "text",
            }
            cur.execute(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'public.extension_invocation_logs'::regclass
                """
            )
            constraints = dict(cur.fetchall())
            assert constraints["extension_invocation_logs_job_id_fkey"] == (
                "FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE"
            )
            assert constraints["extension_invocation_logs_workflow_id_fkey"] == (
                "FOREIGN KEY (workflow_id) REFERENCES registry_workflows(workflow_id) "
                "ON DELETE SET NULL"
            )
            for column in ("workflow_id", "group_id", "step_id"):
                assert f"extension_invocation_logs_{column}_nonblank_check" in constraints

            cur.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'extension_invocation_logs'
                """
            )
            index_names = {row[0] for row in cur.fetchall()}
            assert {
                "idx_extension_invocation_logs_job_created",
                "idx_extension_invocation_logs_workflow_created",
            } <= index_names

            cur.execute(
                "INSERT INTO registry_workflows (workflow_key, display_name) VALUES (%s, %s)",
                (workflow_id, "Trace fixture"),
            )
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_a, "Trace project A", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_b, "Trace project B", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, task_summary, selected_workflow
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (job_a, project_a, f"trace-a-{suffix}", "Trace A", workflow_id),
            )
            cur.execute(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, task_summary, selected_workflow
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (job_b, project_b, f"trace-b-{suffix}", "Trace B", workflow_id),
            )
            cur.execute(
                """
                INSERT INTO extension_bundles (
                    extension_key, kind, display_name, bundle_root, manifest_path,
                    latest_version, status, account_id
                ) VALUES (%s, 'tool_provider', %s, %s, %s, '1.0.0', 'valid', %s)
                RETURNING id
                """,
                (
                    extension_key,
                    extension_key,
                    f"/tmp/{extension_key}",
                    f"/tmp/{extension_key}/extension.yaml",
                    DEFAULT_ACCOUNT_ID,
                ),
            )
            bundle_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO extension_versions (
                    bundle_id, version, manifest_jsonb, compiled_manifest_jsonb
                ) VALUES (%s, '1.0.0', '{}'::jsonb, '{}'::jsonb)
                RETURNING id
                """,
                (bundle_id,),
            )
            version_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO enabled_extensions (
                    bundle_id, version_id, account_id
                ) VALUES (%s, %s, %s)
                """,
                (bundle_id, version_id, DEFAULT_ACCOUNT_ID),
            )

        for label, job_id, group_id, step_id in (
            ("current", job_a, "group-a", "step-a"),
            ("foreign", job_b, "group-b", "step-b"),
        ):
            store.log_extension_invocation(
                extension_key=extension_key,
                invocation_type=f"action:{label}",
                status="ok",
                account_id=DEFAULT_ACCOUNT_ID,
                request_context={"correlation_id": f"corr-{label}"},
                job_id=job_id,
                workflow_id=workflow_id,
                group_id=group_id,
                step_id=step_id,
            )

        with store.conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text, workflow_id, group_id, step_id
                FROM extension_invocation_logs
                WHERE bundle_id = %s
                ORDER BY invocation_type
                """,
                (bundle_id,),
            )
            assert cur.fetchall() == [
                (job_a, workflow_id, "group-a", "step-a"),
                (job_b, workflow_id, "group-b", "step-b"),
            ]

        rows = store.list_extension_invocations(
            job_id=job_a,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert [row["invocation_type"] for row in rows] == ["action:current"]

        trace = store.build_job_trace(
            job_a,
            account_id=DEFAULT_ACCOUNT_ID,
            job={
                "id": job_a,
                "project_id": project_a,
                "account_id": DEFAULT_ACCOUNT_ID,
                "task_id": f"trace-a-{suffix}",
                "status": "completed",
            },
        )
        assert trace is not None
        extension_spans = [span for span in trace["spans"] if span["layer"] == "extension"]
        assert [span["name"] for span in extension_spans] == ["action:current"]

        with store.conn.cursor() as cur:
            cur.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
            cur.execute(
                """
                SELECT workflow_id, group_id, step_id
                FROM extension_invocation_logs
                WHERE bundle_id = %s
                ORDER BY invocation_type
                """,
                (bundle_id,),
            )
            assert cur.fetchall() == [
                (None, "group-a", "step-a"),
                (None, "group-b", "step-b"),
            ]

            cur.execute("DELETE FROM jobs WHERE id = %s", (job_a,))
            cur.execute(
                """
                SELECT job_id::text, invocation_type
                FROM extension_invocation_logs
                WHERE bundle_id = %s
                """,
                (bundle_id,),
            )
            assert cur.fetchall() == [(job_b, "action:foreign")]
    finally:
        store.conn.close()
