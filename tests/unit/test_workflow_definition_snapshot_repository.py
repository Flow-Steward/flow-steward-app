from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowDefinitionPinError,
    WorkflowDefinitionSnapshotRepository,
    WorkflowRevisionChangedError,
)
from tests._workflow_definitions import linear_graph

OTHER_ACCOUNT = "00000000-0000-0000-0000-0000000000a7"


class _RealDictConnection:
    """The repository asks for RealDictCursor; psycopg 3 gives dict rows directly."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def cursor(self, cursor_factory=None, **kwargs):
        # Like the store's ConnectionAdapter: psycopg arguments pass through.
        del cursor_factory
        kwargs.setdefault("row_factory", dict_row)
        return self.conn.cursor(**kwargs)


def _save(store, workflow_id: str, *, step_kind: str = "transform", graph: dict | None = None):
    store.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": workflow_id,
            "enabled": False,
            "settings": {"execution_graph": graph or linear_graph(["work", "done"])},
            "steps": [
                {"step_id": "work", "step_kind": step_kind},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            "groups": [],
            "for_each": [],
        }
    )


def _job(conn, project_id) -> str:
    return str(
        conn.execute(
            "INSERT INTO jobs (project_id, task_id, status) VALUES (%s, %s, 'pending') RETURNING id",
            (project_id, uuid.uuid4().hex),
        ).fetchone()[0]
    )


def _pin(conn, job_id: str, workflow_id: str) -> dict:
    with conn.transaction():
        return WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).pin_for_job(
            job_id=job_id, workflow_id=workflow_id
        )


def test_archive_pins_extension_protection_before_source_is_deleted(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    conn.execute(
        "UPDATE registry_workflows SET projection_mode='derived' WHERE workflow_id=%s",
        (workflow_id,),
    )
    job_id = _job(conn, project_id)
    _pin(conn, job_id, workflow_id)
    conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (workflow_id,))
    archived = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).load_for_job(job_id)
    assert archived["definition_jsonb"]["protected"] is True


def test_archive_pins_explicit_extension_export_permission_from_stored_source(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    account_id = conn.execute(
        "SELECT account_id FROM registry_workflows WHERE workflow_id=%s", (workflow_id,)
    ).fetchone()[0]
    install_id = f"snapshot_install_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO extension_installs (extension_id, install_id, account_id) VALUES (%s,%s,%s)",
        ("snapshot_fixture", install_id, account_id),
    )
    try:
        conn.execute(
            "UPDATE registry_workflows SET source_install_id=%s, "
            "settings=settings || %s::jsonb WHERE workflow_id=%s",
            (install_id, json.dumps({"export_policy": {"allow_export": True}}), workflow_id),
        )
        job_id = _job(conn, project_id)
        _pin(conn, job_id, workflow_id)
        conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (workflow_id,))
        archived = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).load_for_job(
            job_id
        )
        assert archived["definition_jsonb"]["protected"] is False
    finally:
        conn.execute("DELETE FROM extension_installs WHERE install_id=%s", (install_id,))


def test_jobs_of_one_revision_share_one_snapshot_and_carry_its_ref(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    first_job, second_job = _job(conn, project_id), _job(conn, project_id)

    first = _pin(conn, first_job, workflow_id)
    second = _pin(conn, second_job, workflow_id)

    assert first == second
    rows = conn.execute(
        "SELECT count(*) FROM workflow_definition_snapshots WHERE workflow_id = %s", (workflow_id,)
    ).fetchone()[0]
    assert rows == 1
    job = conn.execute(
        "SELECT workflow_definition_snapshot_id::text, meta FROM jobs WHERE id = %s", (first_job,)
    ).fetchone()
    assert job[0] == first["snapshot_id"]
    meta = job[1] if isinstance(job[1], dict) else json.loads(job[1])
    assert meta["workflow_definition_ref"] == {
        "revision": first["revision"],
        "digest": first["digest"],
    }


def test_a_new_revision_gets_its_own_snapshot_and_old_runs_keep_theirs(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    old_job = _job(conn, project_id)
    old = _pin(conn, old_job, workflow_id)

    _save(store, workflow_id, step_kind="template")
    new = _pin(conn, _job(conn, project_id), workflow_id)

    assert new["revision"] > old["revision"]
    assert new["snapshot_id"] != old["snapshot_id"]
    archived = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).load_for_job(old_job)
    assert archived["definition_jsonb"]["steps"][0]["step_kind"] == "transform"


def test_a_definition_that_is_not_runnable_is_never_pinned(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id, graph={"schema_version": 1, "entry": None, "edges": []})
    job_id = _job(conn, project_id)

    with pytest.raises(WorkflowDefinitionPinError) as raised:
        _pin(conn, job_id, workflow_id)

    assert raised.value.issues
    assert (
        conn.execute(
            "SELECT workflow_definition_snapshot_id FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()[0]
        is None
    )


def test_a_test_run_of_a_revision_that_moved_on_is_never_pinned(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    loaded = int(store.get_workflow(workflow_id)["revision"])
    _save(store, workflow_id, step_kind="template")
    stale_job, current_job = _job(conn, project_id), _job(conn, project_id)
    for job_id, revision in ((stale_job, loaded), (current_job, loaded + 1)):
        conn.execute(
            "UPDATE jobs SET meta = jsonb_build_object('expected_workflow_revision', %s::int)"
            " WHERE id = %s",
            (revision, job_id),
        )

    with pytest.raises(WorkflowRevisionChangedError) as raised:
        _pin(conn, stale_job, workflow_id)

    assert (raised.value.expected_revision, raised.value.revision) == (loaded, loaded + 1)
    assert _pin(conn, current_job, workflow_id)["revision"] == loaded + 1


def test_a_referenced_snapshot_cannot_be_deleted(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    ref = _pin(conn, _job(conn, project_id), workflow_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "DELETE FROM workflow_definition_snapshots WHERE id = %s", (ref["snapshot_id"],)
        )


def test_another_account_cannot_read_the_snapshot(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    ref = _pin(conn, _job(conn, project_id), workflow_id)
    repository = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn))
    own_account = conn.execute(
        "SELECT account_id::text FROM projects WHERE id = %s", (project_id,)
    ).fetchone()[0]

    assert repository.load(ref["snapshot_id"], account_id=own_account) is not None
    assert repository.load(ref["snapshot_id"], account_id=OTHER_ACCOUNT) is None


def test_a_step_test_run_pins_an_incomplete_draft(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id, graph={"schema_version": 1, "entry": None, "edges": []})
    job_id = _job(conn, project_id)

    with conn.transaction():
        ref = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).pin_for_job(
            job_id=job_id, workflow_id=workflow_id, validate=False
        )

    assert ref["snapshot_id"]


def test_retention_removes_a_snapshot_only_once_no_job_references_it(audit_db):
    from core.application.tooling.ops.retention_repository import (
        _delete_or_count_unreferenced_definition_snapshots,
    )

    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    job_id = _job(conn, project_id)
    ref = _pin(conn, job_id, workflow_id)

    def sweep() -> None:
        with conn.transaction(), conn.cursor() as cur:
            _delete_or_count_unreferenced_definition_snapshots(cur, cache={}, dry_run=False)

    def archived() -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM workflow_definition_snapshots WHERE id = %s", (ref["snapshot_id"],)
            ).fetchone()
            is not None
        )

    sweep()
    assert archived()
    conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
    sweep()
    assert not archived()


@pytest.mark.parametrize("method", ["mark_retryable", "mark_finalizing"])
def test_cancel_fences_a_late_resume_writer(audit_db, method):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from core.application.jobs.workflow_resume_repository import (
        WorkflowResumeRepositoryAdapter,
    )

    _store, conn, _workflow_id, project_id = audit_db
    job_id = _job(conn, project_id)
    resume = {
        "revision": 1,
        "state": "running",
        "lease_token": "old-worker",
        "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    conn.execute(
        "UPDATE jobs SET status='cancelled', finished_at=NOW(), meta=%s::jsonb WHERE id=%s",
        (json.dumps({"workflow_resume": resume}), job_id),
    )
    repository = WorkflowResumeRepositoryAdapter(SimpleNamespace(conn=conn))
    args = {
        "job_id": job_id,
        "resume_revision": 1,
        "lease_token": "old-worker",
        "error": "late graph error",
    }
    if method == "mark_finalizing":
        args.update(final_status="completed", result={"final_report": "too late"})
    assert getattr(repository, method)(**args) is False
    assert (
        conn.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()[0] == "cancelled"
    )


def test_lane_job_cannot_pin_a_revision_with_wait_added_after_prescan(audit_db):
    from core.application.orchestration.invoke_workflow_step_node_common import (
        INVOKE_LANE_PLACEMENT_KEY,
    )

    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    # The prescan saw a finite child. A concurrent edit adds a durable pause.
    _save(store, workflow_id, step_kind="wait")
    row = store.get_workflow(workflow_id)
    row["steps"][0]["override"] = {"wait": {"duration_ms": 1}}
    store.save_workflow(row)
    job_id = _job(conn, project_id)
    conn.execute(
        "UPDATE jobs SET meta=%s::jsonb WHERE id=%s",
        (json.dumps({INVOKE_LANE_PLACEMENT_KEY: True}), job_id),
    )
    with pytest.raises(WorkflowDefinitionPinError, match="lane"):
        _pin(conn, job_id, workflow_id)
    assert (
        conn.execute(
            "SELECT workflow_definition_snapshot_id FROM jobs WHERE id=%s", (job_id,)
        ).fetchone()[0]
        is None
    )


@pytest.mark.parametrize("pause_kind", ["wait", "approval"])
@pytest.mark.parametrize("inherited_lane", [False, True])
def test_launch_pin_refuses_nested_child_pause_inside_parallel_lane(
    audit_db, pause_kind, inherited_lane
):
    from tests._workflow_definitions import edge, linear_workflow, node

    store, conn, workflow_id, project_id = audit_db
    child_id, grandchild_id = f"{workflow_id}_child", f"{workflow_id}_grandchild"
    try:
        store.save_workflow(
            linear_workflow(
                {"step_id": "pause", "step_kind": pause_kind},
                workflow_id=grandchild_id,
                enabled=False,
            )
        )
        store.save_workflow(
            linear_workflow(
                {
                    "step_id": "nested",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"workflow_id": grandchild_id}},
                },
                workflow_id=child_id,
                enabled=False,
            )
        )
        store.save_workflow(
            {
                "workflow_id": workflow_id,
                "display_name": workflow_id,
                "enabled": False,
                "settings": {
                    "execution_graph": {
                        "schema_version": 1,
                        "entry": node("fan", "group"),
                        "edges": [
                            edge(
                                "call", {"kind": "group_exit", "group_id": "fan", "lane_id": "left"}
                            ),
                            edge(
                                "other",
                                {"kind": "group_exit", "group_id": "fan", "lane_id": "right"},
                            ),
                            edge("fan", node("done"), source_kind="group"),
                        ],
                    }
                },
                "steps": [
                    {
                        "step_id": "call",
                        "step_kind": "invoke_workflow",
                        "group_id": "fan",
                        "lane_id": "left",
                        "override": {"invoke_workflow": {"workflow_id": child_id}},
                    },
                    {
                        "step_id": "other",
                        "step_kind": "transform",
                        "group_id": "fan",
                        "lane_id": "right",
                    },
                    {"step_id": "done", "step_kind": "stop_success"},
                ],
                "groups": [
                    {
                        "group_id": "fan",
                        "body": {
                            "kind": "lanes",
                            "concurrency_limit": 2,
                            "lanes": [
                                {"lane_id": "left", "entry": node("call")},
                                {"lane_id": "right", "entry": node("other")},
                            ],
                        },
                    }
                ],
                "for_each": [],
            }
        )
        job_id = _job(conn, project_id)
        if inherited_lane:
            conn.execute(
                "UPDATE jobs SET meta = jsonb_build_object('invoke_lane_placement', true) WHERE id=%s",
                (job_id,),
            )
        with pytest.raises(WorkflowDefinitionPinError) as refused:
            _pin(conn, job_id, child_id if inherited_lane else workflow_id)
        assert [issue["code"] for issue in refused.value.issues] == [
            "workflow.placement.lane_child_waits"
        ]
        assert grandchild_id in refused.value.issues[0]["message"]
        assert (
            conn.execute(
                "SELECT workflow_definition_snapshot_id FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()[0]
            is None
        )
    finally:
        conn.execute(
            "DELETE FROM registry_workflows WHERE workflow_id IN (%s,%s)", (child_id, grandchild_id)
        )


def test_worker_cannot_create_missing_pin_from_live_definition_after_dispatch(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    job_id = _job(conn, project_id)
    _save(store, workflow_id, step_kind="template")
    repository = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn))
    with conn.transaction(), pytest.raises(WorkflowDefinitionPinError, match="pinned"):
        repository.ensure_pinned(
            job_id=job_id,
            workflow_id=workflow_id,
        )
    assert (
        conn.execute(
            "SELECT workflow_definition_snapshot_id FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()[0]
        is None
    )


def test_worker_uses_submission_pin_even_when_live_workflow_revision_changes(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    job_id = _job(conn, project_id)
    submitted = _pin(conn, job_id, workflow_id)
    _save(store, workflow_id, step_kind="template")
    with conn.transaction():
        actual = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).ensure_pinned(
            job_id=job_id,
            workflow_id=workflow_id,
        )
    assert actual == submitted


def _extension_anchor(store, conn, install_id: str, *, install_state: str = "active") -> str:
    """An installed extension workflow: a step-less anchor plus the install's projection."""
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id

    workflow_id = projection_bridge_id(install_id, "prepare_order")
    _save(store, workflow_id)
    account_id = conn.execute(
        "SELECT account_id FROM registry_workflows WHERE workflow_id=%s", (workflow_id,)
    ).fetchone()[0]
    conn.execute("DELETE FROM registry_workflow_steps WHERE owner_workflow_id=%s", (workflow_id,))
    release_id = f"{install_id}-release"
    conn.execute(
        "INSERT INTO extension_releases (release_id, extension_id, release_version, account_id) "
        "VALUES (%s,%s,'1.0.0',%s)",
        (release_id, "acme.prepare", account_id),
    )
    conn.execute(
        "INSERT INTO extension_installs (extension_id, install_id, account_id, state, release_id) "
        "VALUES (%s,%s,%s,%s,%s)",
        ("acme.prepare", install_id, account_id, install_state, release_id),
    )
    conn.execute(
        "UPDATE registry_workflows SET projection_mode='derived', source_install_id=%s "
        "WHERE workflow_id=%s",
        (install_id, workflow_id),
    )
    conn.execute(
        "INSERT INTO extension_registry_projections (account_id, install_id, release_id, "
        "extension_id, artifact_kind, artifact_id, projection_jsonb, is_active) "
        "VALUES (%s,%s,%s,%s,'workflow','prepare_order',%s::jsonb,true)",
        (
            account_id,
            install_id,
            release_id,
            "acme.prepare",
            json.dumps(
                {
                    "workflow_id": "prepare_order",
                    "display_name": "Prepare order",
                    "enabled": True,
                    "settings": {"execution_graph": linear_graph(["shape", "done"])},
                    "steps": [
                        {"step_id": "shape", "step_kind": "transform"},
                        {"step_id": "done", "step_kind": "stop_success"},
                    ],
                    "groups": [],
                    "for_each": [],
                }
            ),
        ),
    )
    return workflow_id


def _drop_extension(conn, install_id: str, workflow_id: str) -> None:
    conn.execute(
        "DELETE FROM jobs WHERE workflow_definition_snapshot_id IN "
        "(SELECT id FROM workflow_definition_snapshots WHERE workflow_id=%s)",
        (workflow_id,),
    )
    conn.execute("DELETE FROM workflow_definition_snapshots WHERE workflow_id=%s", (workflow_id,))
    conn.execute("DELETE FROM registry_workflows WHERE workflow_id=%s", (workflow_id,))
    conn.execute("DELETE FROM extension_registry_projections WHERE install_id=%s", (install_id,))
    conn.execute("DELETE FROM extension_installs WHERE install_id=%s", (install_id,))
    conn.execute("DELETE FROM extension_releases WHERE release_id=%s", (f"{install_id}-release",))


def _installed_source_state(conn, workflow_id: str, install_id: str) -> dict:
    release_id = f"{install_id}-release"
    anchor = conn.execute(
        "SELECT revision, display_name, description, settings, projection_mode, enabled, "
        "source_install_id, source_release_id FROM registry_workflows WHERE workflow_id=%s",
        (workflow_id,),
    ).fetchone()
    steps = conn.execute(
        "SELECT row_to_json(s) FROM registry_workflow_steps s "
        "WHERE owner_workflow_id=%s ORDER BY step_id",
        (workflow_id,),
    ).fetchall()
    projection = conn.execute(
        "SELECT projection_jsonb FROM extension_registry_projections "
        "WHERE install_id=%s AND artifact_kind='workflow'",
        (install_id,),
    ).fetchone()
    release = conn.execute(
        "SELECT descriptor_jsonb, descriptor_sha256 FROM extension_releases WHERE release_id=%s",
        (release_id,),
    ).fetchone()
    artifact = conn.execute(
        "SELECT snapshot_jsonb FROM extension_artifact_snapshots WHERE release_id=%s "
        "AND artifact_kind='workflow'",
        (release_id,),
    ).fetchone()
    return {
        "anchor": anchor,
        "steps": steps,
        "projection": projection,
        "release": release,
        "artifact": artifact,
    }


def test_licensed_installed_projection_refuses_browser_source_edit_without_mutating_release(
    audit_db,
):
    from core.application.registry.workflow_lookup import get_workflow_row
    from core.entrypoints.admin import workflows as workflow_routes
    from core.entrypoints.api.app_factory import create_app

    store, conn, _workflow_id, _project_id = audit_db
    install_id = f"task70-install-{uuid.uuid4().hex[:12]}"
    workflow_id = _extension_anchor(store, conn, install_id)
    release_id = f"{install_id}-release"
    try:
        conn.execute(
            "UPDATE registry_workflows SET enabled=true WHERE workflow_id=%s", (workflow_id,)
        )
        conn.execute(
            "UPDATE extension_registry_projections SET "
            "projection_jsonb=jsonb_set(projection_jsonb, '{settings,portability}', "
            "'{\"allow_full_export\":true}'::jsonb, true) "
            "WHERE install_id=%s AND artifact_kind='workflow'",
            (install_id,),
        )
        conn.execute(
            "INSERT INTO extension_artifact_snapshots "
            "(release_id, artifact_kind, artifact_id, snapshot_jsonb) "
            "SELECT release_id, artifact_kind, artifact_id, projection_jsonb "
            "FROM extension_registry_projections WHERE install_id=%s AND artifact_kind='workflow'",
            (install_id,),
        )
        projected = get_workflow_row(store, workflow_id)
        assert projected["settings"]["portability"]["allow_full_export"] is True
        before = _installed_source_state(conn, workflow_id, install_id)
        app = create_app()
        app.state.local_auth_enabled_override = lambda: False
        app.state.external_auth_enabled_override = lambda: False
        with (
            patch.object(workflow_routes, "_get_db", return_value=store),
            patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                f"/api/workflows/{workflow_id}/changes",
                json={
                    "expected_revision": projected["revision"],
                    "operations": [
                        {
                            "op": "create_step",
                            "step": {"step_id": "injected", "step_kind": "set_variable"},
                        }
                    ],
                },
            )
            assert _installed_source_state(conn, workflow_id, install_id) == before
            assert response.status_code == 403, response.text
            assert response.json()["detail"]["error"] == "workflow_internal_structure_protected"

            source_writes = (
                (
                    "display name PATCH",
                    client.patch(
                        f"/api/workflows/{workflow_id}",
                        json={"display_name": "Forged", "expected_revision": projected["revision"]},
                    ),
                    "workflow_protected_metadata_immutable",
                ),
                (
                    "builder metadata PATCH",
                    client.patch(
                        f"/api/workflows/{workflow_id}",
                        json={
                            "builder_metadata_jsonb": {"purpose": "Forged"},
                            "expected_revision": projected["revision"],
                        },
                    ),
                    "workflow_protected_metadata_immutable",
                ),
                (
                    "full metadata PUT",
                    client.put(
                        f"/api/workflows/{workflow_id}",
                        json={
                            "workflow_id": "forged_metadata_identity",
                            "display_name": "Forged",
                            "expected_revision": projected["revision"],
                        },
                    ),
                    "workflow_update_protected",
                ),
                (
                    "history restore",
                    client.post(
                        f"/api/workflows/{workflow_id}/restore",
                        json={"entry_id": 1, "expected_revision": projected["revision"]},
                    ),
                    "workflow_internal_structure_protected",
                ),
                (
                    "delete",
                    client.delete(f"/api/workflows/{workflow_id}"),
                    "workflow_internal_structure_protected",
                ),
            )
            for action, refused, code in source_writes:
                assert refused.status_code == 403, f"{action}: {refused.text}"
                assert refused.json()["detail"]["error"] == code
                assert _installed_source_state(conn, workflow_id, install_id) == before
            toggled = client.patch(f"/api/workflows/{workflow_id}/toggle")
            assert toggled.status_code == 200, toggled.text
            assert toggled.json()["enabled"] is False
        after = _installed_source_state(conn, workflow_id, install_id)
        assert after["anchor"][0] == before["anchor"][0] + 1
        assert after["anchor"][1:5] == before["anchor"][1:5]
        assert before["anchor"][5] is True
        assert after["anchor"][5] is False
        assert after["anchor"][6:] == before["anchor"][6:]
        assert {key: value for key, value in after.items() if key != "anchor"} == {
            key: value for key, value in before.items() if key != "anchor"
        }
    finally:
        conn.execute("DELETE FROM extension_artifact_snapshots WHERE release_id=%s", (release_id,))
        _drop_extension(conn, install_id, workflow_id)


def test_an_installed_extension_workflow_pins_the_definition_of_its_projection(audit_db):
    store, conn, _workflow_id, project_id = audit_db
    install_id = f"pin-install-{uuid.uuid4().hex[:12]}"
    workflow_id = _extension_anchor(store, conn, install_id)
    try:
        job_id = _job(conn, project_id)
        _pin(conn, job_id, workflow_id)
        archived = WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).load_for_job(
            job_id
        )
        definition = archived["definition_jsonb"]
        assert [step["step_id"] for step in definition["steps"]] == ["shape", "done"]
        assert definition["protected"] is True
    finally:
        _drop_extension(conn, install_id, workflow_id)


def test_an_extension_workflow_of_an_inactive_install_is_refused_with_its_reason(audit_db):
    store, conn, _workflow_id, project_id = audit_db
    install_id = f"pin-install-{uuid.uuid4().hex[:12]}"
    workflow_id = _extension_anchor(store, conn, install_id, install_state="disabled")
    try:
        job_id = _job(conn, project_id)
        with pytest.raises(WorkflowDefinitionPinError) as excinfo:
            _pin(conn, job_id, workflow_id)
        assert [issue["code"] for issue in excinfo.value.issues] == [
            "workflow.extension_unavailable"
        ]
    finally:
        _drop_extension(conn, install_id, workflow_id)
