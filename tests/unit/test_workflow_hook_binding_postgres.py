"""Hook bindings and their pins in the canonical PostgreSQL schema (spec §9.2, §11)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowDefinitionSnapshotRepository,
)
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.application.workflows.workflow_hook_binding_repository import (
    HookBindingConflictError,
    WorkflowHookBindingRepository,
)
from core.application.workflows.workflow_hook_binding_service import (
    HookBindingError,
    HookBindingRequest,
    WorkflowHookBindingService,
)
from core.application.workflows.workflow_invocation_target import (
    HOOK_BINDING_PINS_META_KEY,
    hook_owner_for,
)
from tests.unit.test_workflow_definition_snapshot_repository import _job, _RealDictConnection
from tests.unit.test_workflow_hook_contract import declaration


@pytest.fixture
def hooked(audit_db):
    store, conn, owner_id, project_a = audit_db
    project_b = conn.execute(
        "INSERT INTO projects (name) VALUES (%s) RETURNING id", (f"b {owner_id}",)
    ).fetchone()[0]
    account_id = str(DEFAULT_ACCOUNT_ID)
    created: list[str] = []

    def save(workflow_id: str, *, steps, settings=None, enabled=False, entry=None, edges=()):
        created.append(workflow_id)
        store.save_workflow(
            {
                "workflow_id": workflow_id,
                "display_name": workflow_id,
                "enabled": enabled,
                "account_id": account_id,
                "settings": {
                    **(settings or {}),
                    "execution_graph": {
                        "schema_version": 1,
                        "entry": {"kind": "step", "id": entry or steps[0]["step_id"]},
                        "edges": list(edges),
                    },
                },
                "steps": steps,
                "groups": [],
                "for_each": [],
            }
        )

    def hook_step(step_id: str, **changes) -> dict:
        return {
            "step_id": step_id,
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"hook": declaration(**changes)}},
        }

    done = {"step_id": "done", "step_kind": "stop_success"}
    edge = {
        "source": {"kind": "step", "id": "before"},
        "outcome": {"name": "success"},
        "target": {"kind": "step", "id": "done"},
    }
    save(owner_id, steps=[hook_step("before"), done], edges=[edge])
    child_id = f"{owner_id}_child"
    callable_child = {
        "callable": {
            "enabled": True,
            "trigger_channels": ["parent_workflow"],
            "input_mode": "fields",
            "expected_inputs": [
                {"name": "invoice_id", "type": "number", "required": True},
                {"name": "company", "type": "string"},
            ],
            "output_mode": "variables",
            "expected_outputs": [{"name": "company_name", "type": "string"}],
        }
    }
    save(child_id, steps=[done], settings=callable_child, enabled=True)
    service = WorkflowHookBindingService(store, account_id=account_id, actor="audit")
    repository = WorkflowHookBindingRepository(_RealDictConnection(conn))
    try:
        yield {
            "store": store,
            "conn": conn,
            "owner_id": owner_id,
            "child_id": child_id,
            "project_a": str(project_a),
            "project_b": str(project_b),
            "account_id": account_id,
            "service": service,
            "repository": repository,
            "save": save,
            "hook_step": hook_step,
            "callable_child": callable_child,
            "created": created,
        }
    finally:
        conn.execute("DELETE FROM jobs WHERE project_id = %s", (project_b,))
        for workflow_id in created:
            # Jobs pinned to these snapshots go first (project A's jobs included).
            conn.execute(
                "DELETE FROM jobs WHERE workflow_definition_snapshot_id IN "
                "(SELECT id FROM workflow_definition_snapshots WHERE workflow_id = %s)",
                (workflow_id,),
            )
            conn.execute(
                "DELETE FROM workflow_definition_snapshots WHERE workflow_id = %s", (workflow_id,)
            )
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
        conn.execute("DELETE FROM projects WHERE id = %s", (project_b,))


def owner(ctx, project: str):
    workflow = ctx["store"].get_workflow(ctx["owner_id"])
    return hook_owner_for(workflow, account_id=ctx["account_id"], project_id=project)


def request(ctx, **changes) -> HookBindingRequest:
    fields = {
        "owner_workflow_id": ctx["owner_id"],
        "project_id": ctx["project_a"],
        "hook_id": "before_invoice",
        "expected_revision": 0,
        "target_workflow_id": ctx["child_id"],
        "input_mapping": {"invoice_id": "invoice_id", "company": "company"},
        "output_mapping": {"company_name": "company"},
    }
    fields.update(changes)
    return HookBindingRequest(**fields)


def test_parent_hook_reads_current_project_binding_without_granting_target_scope(hooked) -> None:
    ctx = hooked
    project_a, project_b = ctx["project_a"], ctx["project_b"]
    parent, child = ctx["owner_id"], ctx["child_id"]
    query = SimpleNamespace(
        get_workflow_bindings=lambda project_id, scope=None: SimpleNamespace(
            project_workflows=[{"workflow_id": parent, "enabled": True}]
            if project_id in {project_a, project_b}
            else []
        )
    )
    operator = WorkflowOperatorService(
        get_logger=lambda: ctx["store"],
        project_query_service=lambda: query,
        registry_db_provider=lambda: ctx["store"],
    )
    scope = ScopeContext(account_id=ctx["account_id"])

    empty = operator.hook_get(
        project_id=project_a, workflow_id=parent, hook_id="before_invoice", scope=scope
    )
    assert empty["binding_revision"] == 0
    assert empty["binding"] is None
    saved = ctx["service"].save(request(ctx))

    listed = operator.hooks_list(project_id=project_a, workflow_id=parent, scope=scope)
    got = operator.hook_get(
        project_id=project_a, workflow_id=parent, hook_id="before_invoice", scope=scope
    )
    assert listed == {
        "project_id": project_a,
        "workflow_id": parent,
        "hooks": [
            {key: value for key, value in got.items() if key not in {"project_id", "workflow_id"}}
        ],
    }
    assert (got["project_id"], got["workflow_id"], got["hook_id"], got["step_id"]) == (
        project_a,
        parent,
        "before_invoice",
        "before",
    )
    assert got["contract"] == declaration()
    assert got["contract_version"] == 1
    assert got["hook_semantic_digest"] == saved.hook_semantic_digest
    assert got["binding_revision"] == 1
    assert got["binding"] == {
        "revision_id": saved.id,
        "target_workflow_id": child,
        "input_mapping": {"invoice_id": "invoice_id", "company": "company"},
        "output_mapping": {"company_name": "company"},
        "hook_contract_version": 1,
        "hook_semantic_digest": saved.hook_semantic_digest,
    }
    assert "steps" not in got
    assert "execution_graph" not in got

    # The second project's parent can be read, but its Hook has no binding.
    other = operator.hook_get(
        project_id=project_b, workflow_id=parent, hook_id="before_invoice", scope=scope
    )
    assert other["binding_revision"] == 0
    assert other["binding"] is None
    with pytest.raises(ServiceNotFoundError, match="declares no hook"):
        operator.hook_get(project_id=project_a, workflow_id=parent, hook_id="missing", scope=scope)
    with pytest.raises(ServiceNotFoundError, match="not linked"):
        operator.hook_get(
            project_id="foreign-project", workflow_id=parent, hook_id="before_invoice", scope=scope
        )
    restricted = ScopeContext(account_id=ctx["account_id"], allowed_workflow_ids=frozenset({child}))
    with pytest.raises(ServiceNotFoundError, match="not linked"):
        operator.hook_get(
            project_id=project_a, workflow_id=parent, hook_id="before_invoice", scope=restricted
        )
    with pytest.raises(ServiceNotFoundError, match="not linked"):
        operator.project_workflow_contract(project_a, child, scope=scope)
    with pytest.raises(ServiceNotFoundError, match="not linked"):
        operator.where_used(project_id=project_a, workflow_id=child, scope=scope)


def test_empty_binding_differs_from_a_configured_missing_target(hooked) -> None:
    ctx = hooked
    assert ctx["repository"].binding(owner(ctx, ctx["project_a"]), "before_invoice") is None
    missing_target = request(ctx, target_workflow_id="no_such_workflow")
    with pytest.raises(HookBindingError) as raised:
        ctx["service"].save(missing_target)
    assert [issue["code"] for issue in raised.value.issues] == ["workflow.hook.target_missing"]
    # Nothing was written: the hook is still empty, not "missing".
    assert ctx["repository"].binding_revision(owner(ctx, ctx["project_a"]), "before_invoice") == 0


def test_projects_a_and_b_are_isolated(hooked) -> None:
    ctx = hooked
    saved = ctx["service"].save(request(ctx))
    assert saved.target_workflow_id == ctx["child_id"]
    assert ctx["repository"].binding(owner(ctx, ctx["project_b"]), "before_invoice") is None
    assert ctx["repository"].binding(owner(ctx, ctx["project_a"]), "before_invoice").id == saved.id


def test_a_stale_expected_revision_does_not_change_the_target(hooked) -> None:
    ctx = hooked
    first = ctx["service"].save(request(ctx))
    stale = request(ctx, expected_revision=0, target_workflow_id=None)
    with pytest.raises(HookBindingConflictError):
        ctx["service"].save(stale)
    current = ctx["repository"].binding(owner(ctx, ctx["project_a"]), "before_invoice")
    assert (current.id, current.target_workflow_id) == (first.id, ctx["child_id"])


def test_create_and_bind_twice_creates_one_child(hooked) -> None:
    ctx = hooked
    child_id = f"{ctx['owner_id']}_actions"
    ctx["created"].append(child_id)
    first = ctx["service"].create_child_and_bind(
        owner_workflow_id=ctx["owner_id"],
        project_id=ctx["project_a"],
        hook_id="before_invoice",
        expected_revision=0,
        child_workflow_id=child_id,
    )
    again = ctx["service"].create_child_and_bind(
        owner_workflow_id=ctx["owner_id"],
        project_id=ctx["project_a"],
        hook_id="before_invoice",
        expected_revision=0,
        child_workflow_id=child_id,
    )
    assert again.id == first.id
    count = (
        ctx["conn"]
        .execute("SELECT count(*) FROM registry_workflows WHERE workflow_id = %s", (child_id,))
        .fetchone()[0]
    )
    assert count == 1
    child = ctx["store"].get_workflow(child_id)
    assert child["enabled"] is True
    assert child["settings"]["callable"]["trigger_channels"] == ["parent_workflow"]
    # R2-11: a new hook is empty; only its hidden end, no steps nobody added.
    assert [step["step_id"] for step in child["steps"]] == ["hook_end"]
    assert child["settings"]["hook_storage"] == {
        "author_workflow_id": ctx["owner_id"],
        "hook_id": "before_invoice",
        "project_id": ctx["project_a"],
    }
    assert child.get("visible_in_catalog") is False
    # The child belongs to the binding's project, so the project can open and run it.
    linked = (
        ctx["conn"]
        .execute(
            "SELECT project_id::text FROM project_workflows "
            "WHERE workflow_id = %s AND enabled AND deleted_at IS NULL",
            (child_id,),
        )
        .fetchall()
    )
    assert [row[0] for row in linked] == [ctx["project_a"]]
    # Someone else's existing workflow is never taken over by the first step of a hook.
    with pytest.raises(HookBindingError) as raised:
        ctx["service"].create_child_and_bind(
            owner_workflow_id=ctx["owner_id"],
            project_id=ctx["project_b"],
            hook_id="before_invoice",
            expected_revision=0,
            child_workflow_id=ctx["child_id"],
        )
    assert raised.value.issues[0]["code"] == "workflow.hook.child_id_taken"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"output_mapping": {"company_name": "invoice_id"}}, "workflow.hook.target_read_only"),
        ({"output_mapping": {"company_name": "nowhere"}}, "workflow.hook.target_unknown"),
        ({"output_mapping": {"unknown_out": "company"}}, "workflow.hook.binding_output_unknown"),
        ({"input_mapping": {"company": "company"}}, "workflow.hook.binding_input_required"),
        ({"hook_contract_version": 2}, "workflow.hook.contract_changed"),
    ],
)
def test_bindings_that_do_not_fit_the_contracts_are_refused(hooked, changes, code) -> None:
    misfit = request(hooked, **changes)
    with pytest.raises(HookBindingError) as raised:
        hooked["service"].save(misfit)
    assert code in [issue["code"] for issue in raised.value.issues]


def test_a_hook_invoke_hook_cycle_is_refused_in_its_project(hooked) -> None:
    ctx = hooked
    middle_id = f"{ctx['owner_id']}_middle"
    ctx["save"](
        middle_id,
        steps=[
            {
                "step_id": "back",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": ctx["owner_id"]}},
            },
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        settings=ctx["callable_child"],
        enabled=True,
        edges=[
            {
                "source": {"kind": "step", "id": "back"},
                "outcome": {"name": "success"},
                "target": {"kind": "step", "id": "done"},
            }
        ],
    )
    cyclic = request(ctx, target_workflow_id=middle_id)
    with pytest.raises(HookBindingError) as raised:
        ctx["service"].save(cyclic)
    assert "workflow.child.recursion" in [issue["code"] for issue in raised.value.issues]


def _pin(conn, job_id: str, workflow_id: str, *, validate: bool = True) -> dict:
    with conn.transaction():
        WorkflowDefinitionSnapshotRepository(_RealDictConnection(conn)).pin_for_job(
            job_id=job_id, workflow_id=workflow_id, validate=validate
        )
    meta = conn.execute("SELECT meta FROM jobs WHERE id = %s", (job_id,)).fetchone()[0]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    return meta[HOOK_BINDING_PINS_META_KEY]


def test_binding_snapshot_is_pinned_for_test_run_and_invoked_child(hooked) -> None:
    ctx = hooked
    conn = ctx["conn"]
    # No binding yet: the pin records the explicit absence.
    assert _pin(conn, _job(conn, ctx["project_a"]), ctx["owner_id"]) == {"before_invoice": None}

    first = ctx["service"].save(request(ctx))
    started = _job(conn, ctx["project_a"])
    assert _pin(conn, started, ctx["owner_id"]) == {"before_invoice": first.id}
    # A Test Run job (a step test run pins without validating) pins the same way.
    test_run = _job(conn, ctx["project_a"])
    assert _pin(conn, test_run, ctx["owner_id"], validate=False) == {"before_invoice": first.id}

    cleared = ctx["service"].save(request(ctx, expected_revision=1, target_workflow_id=None))
    later = _job(conn, ctx["project_a"])
    assert _pin(conn, later, ctx["owner_id"]) == {"before_invoice": cleared.id}
    # The earlier job keeps the revision it was submitted with, unchanged.
    meta = conn.execute("SELECT meta FROM jobs WHERE id = %s", (started,)).fetchone()[0]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta[HOOK_BINDING_PINS_META_KEY] == {"before_invoice": first.id}
    kept = ctx["repository"].revision(first.id, account_id=ctx["account_id"])
    assert kept.target_workflow_id == ctx["child_id"]
    # Another project's job sees only its own (absent) binding.
    assert _pin(conn, _job(conn, ctx["project_b"]), ctx["owner_id"]) == {"before_invoice": None}

    # An invoked child with a hook of its own pins its own project binding on
    # its own submission.
    hooked_child = f"{ctx['owner_id']}_hooked_child"
    ctx["save"](
        hooked_child,
        steps=[ctx["hook_step"]("inner", hook_id="inner_hook")],
        settings=ctx["callable_child"],
        enabled=False,
    )
    child_job = _job(conn, ctx["project_a"])
    assert _pin(conn, child_job, hooked_child, validate=False) == {"inner_hook": None}


def test_a_binding_revision_is_never_rewritten(hooked) -> None:
    ctx = hooked
    first = ctx["service"].save(request(ctx))
    ctx["service"].save(request(ctx, expected_revision=1, output_mapping={}))
    rows = (
        ctx["conn"]
        .execute(
            "SELECT revision, output_mapping_jsonb FROM registry_workflow_hook_binding_revisions "
            "WHERE binding_id = %s ORDER BY revision",
            (first.binding_id,),
        )
        .fetchall()
    )
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][1] == {"company_name": "company"}
    assert rows[1][1] == {}
    assert uuid.UUID(first.id)


@pytest.mark.parametrize("mode", ["production", "development"])
def test_existing_hook_binding_and_child_creation_work_in_each_mode(hooked, monkeypatch, mode):
    from copy import deepcopy

    ctx = hooked
    # Provisioning is trusted fixture setup; the public writes below use the live server mode.
    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    before = deepcopy(ctx["store"].get_workflow(ctx["owner_id"]))
    first = ctx["service"].save(request(ctx))
    assert first.target_workflow_id == ctx["child_id"]
    cleared = ctx["service"].save(
        request(ctx, expected_revision=first.revision, target_workflow_id=None)
    )
    assert cleared.target_workflow_id is None
    child_id = f"{ctx['owner_id']}_mode_actions"
    ctx["created"].append(child_id)
    bound = ctx["service"].create_child_and_bind(
        owner_workflow_id=ctx["owner_id"],
        project_id=ctx["project_a"],
        hook_id="before_invoice",
        expected_revision=cleared.revision,
        child_workflow_id=child_id,
    )
    assert bound.revision == cleared.revision + 1
    assert ctx["repository"].binding(owner(ctx, ctx["project_a"]), "before_invoice").id == bound.id
    assert ctx["repository"].binding(owner(ctx, ctx["project_b"]), "before_invoice") is None
    child = ctx["store"].get_workflow(child_id)
    assert child["enabled"] is True
    assert child["settings"]["callable"]["trigger_channels"] == ["parent_workflow"]
    assert not any(
        "hook" in ((item.get("override") or {}).get("invoke_workflow") or {})
        for item in child["steps"]
    )
    linked = (
        ctx["conn"]
        .execute(
            "SELECT count(*) FROM project_workflows WHERE project_id = %s AND workflow_id = %s",
            (ctx["project_a"], child_id),
        )
        .fetchone()[0]
    )
    assert linked == 1
    after = ctx["store"].get_workflow(ctx["owner_id"])
    assert after["steps"] == before["steps"]
    assert after["settings"]["execution_graph"] == before["settings"]["execution_graph"]
    assert after["revision"] == before["revision"]
