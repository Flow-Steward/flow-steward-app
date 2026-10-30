"""Ordinary Builder admission through real PostgreSQL pin/outbox/progress owners."""

from __future__ import annotations

import base64
import logging
from types import SimpleNamespace

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.task_enqueue_runtime import enqueue_job
from core.application.management.command_services import ProjectCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext
from tests._workflow_definitions import linear_graph
from tests.support.platform_test_store import PlatformTestStore


@pytest.mark.parametrize("revision_race", [False, True])
def test_builder_normal_job_pins_and_prepares_actual_file_before_publication(
    audit_db, monkeypatch, tmp_path, revision_race, request
):
    registry, conn, workflow_id, project_id = audit_db
    registry.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": workflow_id,
            "enabled": True,
            "settings": {
                "execution_graph": linear_graph(["done"]),
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["ui"],
                    "input_mode": "fields",
                    "expected_inputs": [{"name": "source", "type": "file", "required": True}],
                    "output_mode": "none",
                },
            },
            "steps": [{"step_id": "done", "step_kind": "stop_success"}],
            "groups": [],
            "for_each": [],
        }
    )
    workflow = registry.get_workflow(workflow_id)
    revision = workflow["revision"]
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: SimpleNamespace(db_store=registry),
    )
    store = PlatformTestStore(registry.dsn)
    store.add_project_workflow_binding(str(project_id), workflow_id)
    publications = []
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.jobs.job_invocation_artifacts import load_replay_snapshot

    plane = ArtifactDatasetPlane(storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path)))
    actual = b"sku,quantity\nA1,7\n"
    request.addfinalizer(
        lambda: conn.execute(
            "DELETE FROM platform_events WHERE job_id IN (SELECT id FROM jobs WHERE project_id=%s)",
            (project_id,),
        )
    )
    request.addfinalizer(
        lambda: conn.execute(
            "DELETE FROM platform_action_log WHERE job_id IN (SELECT id FROM jobs WHERE project_id=%s)",
            (project_id,),
        )
    )

    def publish(**kwargs):
        job = store.get_job(str(kwargs["job_id"]))
        assert conn.execute(
            "SELECT workflow_definition_snapshot_id FROM jobs WHERE id=%s", (kwargs["job_id"],)
        ).fetchone()[0]
        assert job["task_source"] == "manual_ui"
        assert job["meta"]["expected_workflow_revision"] == revision
        assert not job["meta"].get("test_mode")
        descriptor = kwargs["extra_state"]["inputs"]["source"]
        assert descriptor["artifact_id"].startswith("art_")
        assert descriptor == kwargs["extra_state"]["files"][0]
        assert (
            plane.read_artifact_bytes(descriptor["artifact_id"], max_size_bytes=1_048_576) == actual
        )
        snapshot = load_replay_snapshot(
            artifact_plane=plane,
            pointer=job["meta"]["replay_snapshot"],
            project_id=str(project_id),
            job_id=str(kwargs["job_id"]),
        )
        assert snapshot["test_mode"] is False
        assert snapshot["inputs"]["source"]["artifact_id"] == descriptor["artifact_id"]
        assert job["meta"]["public_progress_v1"]["snapshot"]["job_id"] == str(kwargs["job_id"])
        assert (
            conn.execute(
                "SELECT count(*) FROM job_dispatch_outbox WHERE job_id=%s", (kwargs["job_id"],)
            ).fetchone()[0]
            == 1
        )
        publications.append(kwargs)

    def enqueue(**kwargs):
        if revision_race:
            conn.execute(
                "UPDATE registry_workflows SET revision=revision+1 WHERE workflow_id=%s",
                (workflow_id,),
            )
        return enqueue_job(
            bl=store,
            artifact_plane=plane,
            enqueue_task_fn=publish,
            build_project_config_snapshot=lambda config: dict(config),
            logger=logging.getLogger(__name__),
            log_prefix="builder-test",
            notification_dispatch_fn=None,
            **kwargs,
        )

    service = ProjectCommandService(
        get_logger=lambda: store,
        resolve_workflow_selection=lambda requested, project_workflows=None: (
            requested,
            "explicit_request",
        ),
        validate_explicit_workflow_override=lambda **_kwargs: None,
        enqueue_task=enqueue,
        artifact_plane=plane,
    )
    command = {
        "project_id": str(project_id),
        "expected_revision": revision,
        "acknowledged": True,
        "summary": "Load stock",
        "files": [
            {
                "input_name": "source",
                "filename": "actual.csv",
                "mime_type": "text/csv",
                "content_base64": base64.b64encode(actual).decode(),
            }
        ],
    }
    if revision_race:
        scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceConflictError) as conflict:
            service.start_builder_workflow_run(workflow_id, command, scope=scope)
        assert conflict.value.payload["code"] == "workflow_revision_conflict"
        assert publications == []
        assert not list(tmp_path.rglob("actual.csv"))
        assert (
            conn.execute("SELECT count(*) FROM jobs WHERE project_id=%s", (project_id,)).fetchone()[
                0
            ]
            == 0
        )
    else:
        result = service.start_builder_workflow_run(
            workflow_id,
            command,
            idempotency_key="normal-retry",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
        )
        replay = service.start_builder_workflow_run(
            workflow_id,
            command,
            idempotency_key="normal-retry",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
        )
        assert replay.job_id == result.job_id
        assert replay.deduplicated is True
        assert len(publications) == 1
        assert str(publications[0]["job_id"]) == result.job_id
        assert "content_base64" not in repr(store.get_job(result.job_id))
