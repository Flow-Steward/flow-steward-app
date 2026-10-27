"""Definition/reaction integrity survives the notes and extraction integration."""

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.workflows import workflow_group_extraction_service as extraction_module
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
)

# Explicitly imported because pytest fixtures from imported modules have dependencies.
from tests.unit.test_workflow_group_extraction_postgres import extraction as extraction
from tests.unit.test_workflow_reaction_scope_integrity_postgres import (
    _rule,
)
from tests.unit.test_workflow_reaction_scope_integrity_postgres import (
    scoped_workflow as scoped_workflow,
)


@pytest.mark.parametrize("dry_run", [True, False])
def test_disabled_changeset_scope_guard_keeps_parent_and_latest_annotations(
    scoped_workflow, dry_run
):
    store, reactions, _, workflow_id = scoped_workflow
    reactions.save_workflow_reactions(
        workflow_id, [_rule("group", "batch")], account_id=DEFAULT_ACCOUNT_ID
    )
    with store._transaction() as cur:
        cur.execute(
            "UPDATE registry_workflows SET settings = settings || %s::jsonb WHERE workflow_id = %s",
            ('{"canvas_annotations":[],"canvas_annotations_revision":7}', workflow_id),
        )
    before = store.get_workflow(workflow_id)
    kwargs = {
        "expected_revision": before["revision"],
        "operations": [{"op": "delete_group", "group_id": "batch", "with_contents": True}],
        "author": "integration-regression",
        "dry_run": dry_run,
    }
    if dry_run:
        result = apply_workflow_changes(store, workflow_id, **kwargs)
        assert any(
            issue["code"] == "workflow.reaction.scope_missing" for issue in result.blocking_issues
        )
        assert result.applied is False
    else:
        with pytest.raises(WorkflowDefinitionChangeError, match="scope"):
            apply_workflow_changes(store, workflow_id, **kwargs)
    after = store.get_workflow(workflow_id)
    assert after["revision"] == before["revision"]
    assert after["groups"] == before["groups"]
    assert after["settings"]["canvas_annotations_revision"] == 7
    assert after["settings"]["canvas_annotations"] == []


def test_reaction_scope_failure_after_child_creation_rolls_back_conversion(extraction, monkeypatch):
    service, store, conn, project_id, args = extraction
    preview = service.preview(**args)
    assert preview["can_convert"], preview["issues"]
    before = store.get_workflow(args["workflow_id"])
    original_create = extraction_module.create_workflow_with_definition

    def create_then_add_scope(*positional, **keywords):
        result = original_create(*positional, **keywords)
        store.add_project_workflow_binding(str(project_id), args["child_workflow_id"])
        # Deterministic same-transaction late write, not an impossible second
        # connection bypass of the owning workflow row lock. The real scope
        # checker must observe this retained rule and abort all prior writes.
        with store._transaction() as cur:
            cur.execute(
                "SELECT workflow_id FROM project_workflows WHERE project_id=%s AND workflow_id=%s",
                (project_id, args["child_workflow_id"]),
            )
            assert cur.fetchone()["workflow_id"] == args["child_workflow_id"]
            cur.execute(
                "INSERT INTO workflow_reactions (workflow_id,reaction_key,event_type,account_id) "
                "VALUES (%s,%s,%s,%s)",
                (
                    args["workflow_id"],
                    "scope::group::process::notify",
                    "job.approval_waiting",
                    service.account_id,
                ),
            )
        return result

    monkeypatch.setattr(extraction_module, "create_workflow_with_definition", create_then_add_scope)
    with pytest.raises(WorkflowDefinitionChangeError, match="scope"):
        service.apply(
            **args, preview_digest=preview["preview_digest"], author="integration-regression"
        )
    after = store.get_workflow(args["workflow_id"])
    assert after["revision"] == before["revision"]
    assert after["groups"] == before["groups"]
    assert store.get_workflow(args["child_workflow_id"]) is None
    assert (
        conn.execute(
            "SELECT count(*) FROM project_workflows WHERE project_id=%s AND workflow_id=%s",
            (project_id, args["child_workflow_id"]),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM workflow_reactions WHERE workflow_id=%s", (args["workflow_id"],)
        ).fetchone()[0]
        == 0
    )
