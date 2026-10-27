"""Final definitions survive export → import → storage → run pin (spec §13-15)."""

from __future__ import annotations

import json
import os
from functools import partial
from types import SimpleNamespace

import pytest

from core.application.extensions.extension_descriptor import ExtensionDescriptorArtifacts
from core.application.workflows.workflow_definition_service import (
    apply_workflow_changes,
    definition_from_workflow,
)
from core.application.workflows.workflow_definition_snapshot import frozen_definition
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from tests.unit.test_workflow_definition_service import (
    _connect,
    _exit,
    build_group_for_each_collect,
    ref,
    step,
)
from tests.unit.test_workflow_hook_binding_postgres import hooked as hooked
from tests.unit.test_workflow_hook_contract import declaration

NOTE = {
    "id": "n1",
    "text": "Collect the emails of paid orders",
    "x": 40,
    "y": 60,
    "width": 280,
    "height": 200,
}
#: The variable the Collect adds to exists before the For each (spec §6).
PREPARE_EMAILS = {"set_variable": {"assignments": {"state.emails": []}}}


def full_operations() -> list[dict]:
    """Group → For each → If/Collect/Continue/Break, then a root hook before Stop."""
    operations = [
        _with_prepared_target(op)
        for op in build_group_for_each_collect()
        if op != _connect("g", ref("done"), kind="group")
        and op != _connect("keep", _exit("for_each_exit", "fe"))
    ]
    operations += [
        {"op": "create_step", "step": {**step("enough", "break"), "for_each_id": "fe"}},
        {
            "op": "create_step",
            "step": step(
                "before", "invoke_workflow", override={"invoke_workflow": {"hook": declaration()}}
            ),
        },
        _connect("keep", ref("enough")),
        _connect("g", ref("before"), kind="group"),
        _connect("before", ref("done")),
    ]
    return operations


def _with_prepared_target(op: dict) -> dict:
    """An import validates the whole definition: the prepare step sets its target."""
    if op.get("op") == "create_step" and op["step"]["step_id"] == "prepare":
        return {**op, "step": {**op["step"], "override": PREPARE_EMAILS}}
    return op


def _save_empty(store, workflow_id: str) -> None:
    store.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": workflow_id,
            "enabled": False,
            "settings": {"canvas_annotations": [NOTE]},
            "steps": [],
            "groups": [],
            "for_each": [],
        }
    )


def _shape(workflow: dict) -> dict:
    definition = definition_from_workflow(workflow)
    return {
        "steps": sorted(
            (s["step_id"], s["step_kind"], s.get("group_id"), s.get("for_each_id"))
            for s in definition["steps"]
        ),
        "groups": definition["groups"],
        "for_each": definition["for_each"],
        "edges": sorted(
            json.dumps(edge, sort_keys=True) for edge in definition["execution_graph"]["edges"]
        ),
        "hook": next(
            s["override"]["invoke_workflow"]["hook"]
            for s in definition["steps"]
            if s["step_id"] == "before"
        ),
    }


def test_the_final_definition_round_trips_through_export_import_and_pin(audit_db) -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from core.infrastructure.db import psycopg_runtime
    from core.infrastructure.runtime.entrypoint_db_adapters import workflow_import_transaction

    store, conn, workflow_id, _project_id = audit_db
    copy_id = f"{workflow_id}_copy"
    _save_empty(store, workflow_id)
    # The import writes in one transaction, built the way the admin routes build it.
    import_conn = psycopg_runtime.connect(os.environ["FS_DB_DSN"])
    import_conn.autocommit = True  # the helper then owns, and commits, the import transaction
    importer = WorkflowPortabilityService(
        store,
        import_transaction_factory=partial(
            workflow_import_transaction, store, SimpleNamespace(conn=import_conn)
        ),
    )
    try:
        result = apply_workflow_changes(
            store, workflow_id, expected_revision=1, operations=full_operations(), author="audit"
        )
        assert result.issues == []
        original = store.get_workflow(workflow_id)
        assert validate_workflow_definition(definition_from_workflow(original)) == []

        bundle = WorkflowPortabilityService(store).export_yaml(workflow_id)
        importer.apply_import(bundle, target_workflow_id=copy_id, author="audit")
        imported = store.get_workflow(copy_id)
        assert _shape(imported) == _shape(original)
        assert validate_workflow_definition(definition_from_workflow(imported)) == []
        assert imported["settings"]["canvas_annotations"][0]["text"] == NOTE["text"]

        # The run pin carries the same containers the runtime walks.
        pinned = frozen_definition(imported)
        assert [item["for_each_id"] for item in pinned["for_each"]] == ["fe"]
        assert {s["step_id"] for s in pinned["steps"]} >= {"keep", "skip", "enough", "before"}
    finally:
        import_conn.close()
        conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (copy_id,))


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (
            {"groups": [{"group_id": "g", "kind": "loop", "body": {"kind": "sequence"}}]},
            "workflow.group.loop_removed",
        ),
        (
            {"steps": [{"step_id": "x", "step_kind": "for_each"}]},
            "workflow.step.for_each_inline_removed",
        ),
        (
            {
                "steps": [
                    {
                        "step_id": "x",
                        "step_kind": "invoke_workflow",
                        "override": {
                            "invoke_workflow": {"workflow_id": "child", "hook": declaration()}
                        },
                    }
                ]
            },
            "workflow.hook.direct_target_conflict",
        ),
    ],
)
def test_an_extension_package_carries_only_the_final_definition(snapshot, code) -> None:
    with pytest.raises(ValueError, match=code):
        ExtensionDescriptorArtifacts.model_validate(
            {"workflows": [{"workflow_id": "packaged", "snapshot": snapshot}]}
        )


# -- hook bindings travel with a project's export (spec §9.2, §13, §14) ---------------

PROJECT = "project-a"
OTHER_PROJECT = "project-b"
ACCOUNT = "acc-1"
INPUT_MAPPING = {"invoice_id": "invoice_id", "company": "company"}
OUTPUT_MAPPING = {"company_name": "company"}


def _hook_digest(**changes) -> str:
    from core.application.workflows.workflow_hook_contract import (
        hook_semantic_digest,
        parse_hook_declaration,
    )

    return hook_semantic_digest(parse_hook_declaration(declaration(**changes)))


def _owner_row(workflow_id: str = "invoice_flow") -> dict:
    from tests._workflow_definitions import linear_workflow

    return linear_workflow(
        step("before", "invoke_workflow", override={"invoke_workflow": {"hook": declaration()}}),
        workflow_id=workflow_id,
        account_id=ACCOUNT,
        enabled=False,
    )


def _child_row(workflow_id: str = "buyer_fix", *, outputs=("company_name",)) -> dict:
    from tests._workflow_definitions import linear_workflow

    return linear_workflow(
        workflow_id=workflow_id,
        account_id=ACCOUNT,
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "input_mode": "fields",
                "expected_inputs": [
                    {"name": "invoice_id", "type": "number", "required": True},
                    {"name": "company", "type": "string"},
                ],
                "output_mode": "variables",
                "expected_outputs": [{"name": name, "type": "string"} for name in outputs],
            }
        },
    )


class _Bindings:
    """The binding service as portability uses it: ``describe`` and ``save``.

    Bindings are keyed by ``(project, owner workflow, hook)``; a save is recorded
    with the drafts it was allowed to target, or refused with *refuse*.
    """

    def __init__(self, db, bindings=None, refuse=None) -> None:
        self.db = db
        self.bindings = dict(bindings or {})
        self.refuse = refuse
        self.saved: list = []

    def describe(self, owner_workflow_id: str, project_id: str) -> list[dict]:
        from core.application.workflows.workflow_invocation_target import hook_ids

        rows = []
        for hook_id in hook_ids(self.db.get_workflow(owner_workflow_id) or {}):
            binding = self.bindings.get((project_id, owner_workflow_id, hook_id))
            rows.append(
                {"hook_id": hook_id, "binding_revision": 1 if binding else 0, "binding": binding}
            )
        return rows

    def save(self, request, *, draft_targets=()):
        from core.application.workflows.workflow_hook_binding_service import HookBindingError

        if self.refuse is not None:
            raise HookBindingError([self.refuse])
        self.saved.append((request, frozenset(draft_targets)))
        return SimpleNamespace(revision=request.expected_revision + 1)


def _bound(target: str = "buyer_fix", **changes) -> dict:
    return {
        "target_workflow_id": target,
        "input_mapping": dict(INPUT_MAPPING),
        "output_mapping": dict(OUTPUT_MAPPING),
        "hook_contract_version": 1,
        "hook_semantic_digest": _hook_digest(),
        **changes,
    }


def _portability(db, bindings):
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    service = WorkflowPortabilityService(db, account_id=ACCOUNT)
    service._hook_binding_service = lambda **_kwargs: bindings
    return service


def _source(*, include_children: bool = True, project_id: str = PROJECT) -> dict:
    import yaml

    from tests.unit.test_workflow_portability import _PortabilityDB

    db = _PortabilityDB(workflows={"invoice_flow": _owner_row(), "buyer_fix": _child_row()})
    bindings = _Bindings(db, {(PROJECT, "invoice_flow", "before_invoice"): _bound()})
    text = _portability(db, bindings).export_yaml(
        "invoice_flow", include_children=include_children, project_id=project_id
    )
    return yaml.safe_load(text)


def _yaml(bundle: dict) -> str:
    import yaml

    return yaml.safe_dump(bundle, sort_keys=False)


def _target(*rows: dict):
    from tests.unit.test_workflow_portability import _PortabilityDB

    db = _PortabilityDB(workflows={row["workflow_id"]: row for row in rows})
    return db, _Bindings(db)


def test_a_project_export_carries_the_binding_and_its_child() -> None:
    bundle = _source()

    assert bundle["hook_bindings"] == [
        {
            "hook_id": "before_invoice",
            "owner_workflow_id": "invoice_flow",
            "hook_contract_version": 1,
            "hook_semantic_digest": _hook_digest(),
            "target_workflow_id": "buyer_fix",
            "input_mapping": INPUT_MAPPING,
            "output_mapping": OUTPUT_MAPPING,
        }
    ]
    # The bound child is an ordinary dependency, carried like an invoked child.
    assert [row["workflow_id"] for row in bundle["workflows"]] == ["invoice_flow", "buyer_fix"]
    assert [row["workflow_id"] for row in bundle["dependencies"]["child_workflows"]] == [
        "buyer_fix"
    ]
    assert bundle["metadata"]["child_workflow_ids"] == ["buyer_fix"]
    # No account or project of the exporting installation travels.
    text = _yaml(bundle)
    assert PROJECT not in text
    assert ACCOUNT not in text


def test_without_children_the_bound_child_stays_a_dependency() -> None:
    bundle = _source(include_children=False)

    assert [row["workflow_id"] for row in bundle["workflows"]] == ["invoice_flow"]
    assert [row["workflow_id"] for row in bundle["dependencies"]["child_workflows"]] == [
        "buyer_fix"
    ]
    assert bundle["hook_bindings"][0]["target_workflow_id"] == "buyer_fix"


def test_an_export_without_a_project_has_no_binding_section() -> None:
    from core.application.workflows.workflow_portability import TOP_LEVEL_KEYS

    bundle = _source(project_id="")

    assert list(bundle) == list(TOP_LEVEL_KEYS)
    assert [row["workflow_id"] for row in bundle["workflows"]] == ["invoice_flow"]


def test_import_into_another_project_restores_the_binding_with_the_new_ids() -> None:
    bundle = _yaml(_source())
    db, bindings = _target()

    result = _portability(db, bindings).apply_import(
        bundle,
        target_workflow_id="invoice_copy",
        resolutions=[
            {
                "dependency_key": "workflow:buyer_fix",
                "action": "rename_workflow",
                "target_workflow_id": "buyer_fix_copy",
            }
        ],
        owner_account_id=ACCOUNT,
        owner_project_id=OTHER_PROJECT,
    )

    [(request, drafts)] = bindings.saved
    assert (request.owner_workflow_id, request.project_id, request.hook_id) == (
        "invoice_copy",
        OTHER_PROJECT,
        "before_invoice",
    )
    assert request.target_workflow_id == "buyer_fix_copy"
    assert (request.input_mapping, request.output_mapping) == (INPUT_MAPPING, OUTPUT_MAPPING)
    assert (request.hook_contract_version, request.hook_semantic_digest) == (1, _hook_digest())
    assert request.expected_revision == 0
    # Both were created by this import as disabled drafts.
    assert drafts == {"invoice_copy", "buyer_fix_copy"}
    assert result["hook_bindings"] == [
        {
            "hook_id": "before_invoice",
            "owner_workflow_id": "invoice_copy",
            "target_workflow_id": "buyer_fix_copy",
            "binding_revision": 1,
            "status": "restored",
        }
    ]


def test_a_binding_to_an_existing_workflow_follows_its_mapping() -> None:
    bundle = _yaml(_source(include_children=False))
    db, bindings = _target(_child_row("their_fix"))

    _portability(db, bindings).apply_import(
        bundle,
        resolutions=[
            {
                "dependency_key": "child_workflow:buyer_fix",
                "action": "map_existing",
                "target_ref": "their_fix",
            }
        ],
        owner_account_id=ACCOUNT,
        owner_project_id=OTHER_PROJECT,
    )

    [(request, drafts)] = bindings.saved
    assert request.target_workflow_id == "their_fix"
    # An existing target is never let through disabled.
    assert "their_fix" not in drafts


def _refusal(bundle: str, db, bindings, **kwargs):
    from core.application.workflows.workflow_portability import WorkflowPortabilityError

    before = set(db.workflows)
    service = _portability(db, bindings)
    with pytest.raises(WorkflowPortabilityError) as raised:
        service.apply_import(
            bundle, owner_account_id=ACCOUNT, owner_project_id=OTHER_PROJECT, **kwargs
        )
    # Nothing is written and no hook is left empty behind a success.
    assert set(db.workflows) == before
    assert db.project_workflow_bindings == []
    assert bindings.saved == []
    return raised.value


def _binding_codes(error) -> list[str]:
    return [
        issue["code"]
        for row in error.errors
        if row.get("dependency_type") == "hook_binding"
        for issue in row["metadata"]["issues"]
    ]


def test_import_without_the_bound_child_is_refused() -> None:
    db, bindings = _target()

    error = _refusal(_yaml(_source(include_children=False)), db, bindings)

    assert error.code == "workflow_import_preflight_blocked"
    assert _binding_codes(error) == ["workflow.hook.binding_target_missing"]


def test_import_onto_an_incompatible_child_is_refused() -> None:
    db, bindings = _target(_child_row("their_fix", outputs=("crm_id",)))

    error = _refusal(
        _yaml(_source(include_children=False)),
        db,
        bindings,
        resolutions=[
            {
                "dependency_key": "child_workflow:buyer_fix",
                "action": "map_existing",
                "target_ref": "their_fix",
            }
        ],
    )

    assert _binding_codes(error) == ["workflow.hook.binding_mapping_incompatible"]


def test_import_of_a_binding_saved_against_another_contract_is_refused() -> None:
    bundle = _source()
    bundle["hook_bindings"][0]["hook_contract_version"] = 2
    db, bindings = _target()

    error = _refusal(_yaml(bundle), db, bindings)

    assert _binding_codes(error) == ["workflow.hook.contract_changed"]


def test_an_unknown_binding_field_is_refused_not_ignored() -> None:
    bundle = _source()
    bundle["hook_bindings"][0]["outcomes"] = {"skip": "stop"}
    db, bindings = _target()

    error = _refusal(_yaml(bundle), db, bindings)

    assert _binding_codes(error) == ["workflow.hook.binding_entry_invalid"]


def test_a_binding_the_save_refuses_rolls_the_whole_import_back() -> None:
    db, bindings = _target()
    bindings.refuse = {
        "code": "workflow.child.recursion",
        "message": "Workflow invocation chain repeats",
        "path": "",
    }

    error = _refusal(_yaml(_source()), db, bindings)

    assert error.code == "workflow_import_hook_binding_invalid"
    [row] = error.errors
    assert (row["hook_id"], row["owner_workflow_id"], row["target_workflow_id"]) == (
        "before_invoice",
        "invoice_flow",
        "buyer_fix",
    )
    assert [issue["code"] for issue in row["issues"]] == ["workflow.child.recursion"]


def test_an_import_with_bindings_needs_a_project() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityError

    db, bindings = _target()
    service = _portability(db, bindings)
    bundle = _yaml(_source())
    with pytest.raises(WorkflowPortabilityError) as raised:
        service.apply_import(bundle, owner_account_id=ACCOUNT)

    assert raised.value.code == "workflow_import_hook_binding_project_required"
    assert db.workflows == {}


def test_an_existing_different_binding_in_the_project_is_not_overwritten() -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityError

    db, bindings = _target()
    service = _portability(db, bindings)
    # The owner arrives under an id whose hook is already connected in the project.
    original_describe = bindings.describe

    def describe(owner_workflow_id, project_id):
        rows = original_describe(owner_workflow_id, project_id)
        return [{**row, "binding": _bound("someone_elses")} for row in rows]

    bindings.describe = describe
    bundle = _yaml(_source())
    with pytest.raises(WorkflowPortabilityError) as raised:
        service.apply_import(bundle, owner_account_id=ACCOUNT, owner_project_id=OTHER_PROJECT)

    assert raised.value.code == "workflow_import_hook_binding_invalid"
    assert [issue["code"] for issue in raised.value.errors[0]["issues"]] == [
        "workflow.hook.binding_exists"
    ]
    assert db.workflows == {}
    assert bindings.saved == []


# -- the same in the canonical schema (audit database) --------------------------------


def _audit_importer(store):
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from core.infrastructure.db import psycopg_runtime
    from core.infrastructure.runtime.entrypoint_db_adapters import workflow_import_transaction

    import_conn = psycopg_runtime.connect(os.environ["FS_DB_DSN"])
    import_conn.autocommit = True  # the helper then owns, and commits, the import transaction
    importer = WorkflowPortabilityService(
        store,
        account_id=str(DEFAULT_ACCOUNT_ID),
        import_transaction_factory=partial(
            workflow_import_transaction, store, SimpleNamespace(conn=import_conn)
        ),
    )
    return importer, import_conn


def _bind_in_project_a(ctx) -> None:
    from tests.unit.test_workflow_hook_binding_postgres import request

    ctx["service"].save(request(ctx))


def _export_from_project_a(ctx, *, include_children: bool) -> str:
    from core.application.workflows.workflow_portability import WorkflowPortabilityService

    return WorkflowPortabilityService(ctx["store"], account_id=ctx["account_id"]).export_yaml(
        ctx["owner_id"], include_children=include_children, project_id=ctx["project_a"]
    )


def _binding_in(ctx, workflow_id: str, project_id: str):
    from core.application.workflows.workflow_invocation_target import hook_owner_for

    workflow = ctx["store"].get_workflow(workflow_id)
    owner = hook_owner_for(workflow, account_id=ctx["account_id"], project_id=project_id)
    return ctx["repository"], owner


def test_an_exported_binding_is_connected_again_in_another_project(hooked) -> None:
    import yaml

    ctx = hooked
    owner_copy, child_copy = f"{ctx['owner_id']}_copy", f"{ctx['child_id']}_copy"
    ctx["created"].extend([owner_copy, child_copy])
    _bind_in_project_a(ctx)
    bundle = _export_from_project_a(ctx, include_children=True)
    parsed = yaml.safe_load(bundle)
    assert [row["workflow_id"] for row in parsed["workflows"]] == [
        ctx["owner_id"],
        ctx["child_id"],
    ]
    assert parsed["hook_bindings"][0]["target_workflow_id"] == ctx["child_id"]

    importer, import_conn = _audit_importer(ctx["store"])
    try:
        result = importer.apply_import(
            bundle,
            target_workflow_id=owner_copy,
            resolutions=[
                {
                    "dependency_key": f"workflow:{ctx['child_id']}",
                    "action": "rename_workflow",
                    "target_workflow_id": child_copy,
                }
            ],
            author="audit",
            owner_account_id=ctx["account_id"],
            owner_project_id=ctx["project_b"],
        )
    finally:
        import_conn.close()

    assert [row["status"] for row in result["hook_bindings"]] == ["restored"]
    repository, owner = _binding_in(ctx, owner_copy, ctx["project_b"])
    restored = repository.binding(owner, "before_invoice")
    assert restored.target_workflow_id == child_copy
    assert restored.input_mapping == {"invoice_id": "invoice_id", "company": "company"}
    assert restored.output_mapping == {"company_name": "company"}
    # The child arrived as a disabled draft, like every imported workflow; the
    # hook names it and a run refuses it until it is enabled — it is not empty.
    assert ctx["store"].get_workflow(child_copy)["enabled"] is False
    # The exporting project keeps its own binding.
    repository, source_owner = _binding_in(ctx, ctx["owner_id"], ctx["project_a"])
    assert repository.binding(source_owner, "before_invoice").target_workflow_id == ctx["child_id"]


def test_a_binding_the_save_refuses_leaves_nothing_imported(hooked) -> None:
    from core.application.workflows.workflow_portability import WorkflowPortabilityError

    ctx = hooked
    owner_copy, disabled = f"{ctx['owner_id']}_copy", f"{ctx['owner_id']}_disabled"
    ctx["created"].extend([owner_copy])
    # An existing callable workflow with the same Interface, but disabled.
    ctx["save"](
        disabled,
        steps=[{"step_id": "done", "step_kind": "stop_success"}],
        settings=ctx["callable_child"],
        enabled=False,
    )
    _bind_in_project_a(ctx)
    bundle = _export_from_project_a(ctx, include_children=False)

    importer, import_conn = _audit_importer(ctx["store"])
    try:
        with pytest.raises(WorkflowPortabilityError) as raised:
            importer.apply_import(
                bundle,
                target_workflow_id=owner_copy,
                resolutions=[
                    {
                        "dependency_key": f"child_workflow:{ctx['child_id']}",
                        "action": "map_existing",
                        "target_ref": disabled,
                    }
                ],
                author="audit",
                owner_account_id=ctx["account_id"],
                owner_project_id=ctx["project_b"],
            )
    finally:
        import_conn.close()

    assert raised.value.code == "workflow_import_hook_binding_invalid"
    assert [issue["code"] for issue in raised.value.errors[0]["issues"]] == [
        "workflow.hook.target_disabled"
    ]
    # One transaction: the owner it wrote is gone too, and no binding exists.
    assert ctx["store"].get_workflow(owner_copy) is None
    count = (
        ctx["conn"]
        .execute(
            "SELECT count(*) FROM registry_workflow_hook_bindings "
            "WHERE project_id = %s AND local_workflow_id = %s",
            (ctx["project_b"], owner_copy),
        )
        .fetchone()[0]
    )
    assert count == 0
