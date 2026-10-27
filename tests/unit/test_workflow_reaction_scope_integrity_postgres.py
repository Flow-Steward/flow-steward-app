"""Rules and graph definitions cannot race into dangling step/group scopes."""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import WorkflowReactionsUpdateInput
from core.application.management.command_services import EventCommandService
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.application.workflows.workflow_definition_service import apply_workflow_changes
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.platform_stores import PlatformReactionStore
from tests._workflow_definitions import edge, node


def _rule(scope, identity):
    return {
        "reaction_key": f"scope::{scope}::{identity}::notify",
        "event_type": "job.approval_waiting",
        "enabled": True,
        "action": {"type": "notify"},
    }


@pytest.fixture
def scoped_workflow(audit_db):
    registry, conn, workflow_id, _ = audit_db
    registry.save_workflow(
        {
            "workflow_id": workflow_id,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "enabled": False,
            "steps": [
                {"step_id": "work", "step_kind": "transform", "group_id": "batch"},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            "groups": [
                {
                    "group_id": "batch",
                    "body": {"kind": "sequence", "entry": node("work")},
                }
            ],
            "for_each": [],
            "settings": {
                "execution_graph": {
                    "schema_version": 1,
                    "entry": node("batch", "group"),
                    "edges": [
                        edge("work", {"kind": "group_exit", "group_id": "batch"}),
                        edge("batch", node("done"), source_kind="group"),
                    ],
                }
            },
        }
    )
    reactions = PlatformReactionStore(os.environ["FS_DB_DSN"])
    return registry, reactions, conn, workflow_id


@pytest.mark.parametrize("scope", ["step", "group"])
def test_reaction_writer_rejects_missing_definition_scope(scoped_workflow, scope):
    _, reactions, conn, workflow_id = scoped_workflow
    rules = [_rule(scope, "missing")]
    with pytest.raises(ValueError, match="scope"):
        reactions.save_workflow_reactions(workflow_id, rules, account_id=DEFAULT_ACCOUNT_ID)
    assert (
        conn.execute(
            "SELECT count(*) FROM workflow_reactions WHERE workflow_id = %s", (workflow_id,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(("scope", "identity"), [("step", "work"), ("group", "batch")])
def test_full_definition_save_cannot_orphan_a_retained_rule(scoped_workflow, scope, identity):
    registry, reactions, conn, workflow_id = scoped_workflow
    reactions.save_workflow_reactions(
        workflow_id, [_rule(scope, identity)], account_id=DEFAULT_ACCOUNT_ID
    )
    candidate = registry.get_workflow(workflow_id)
    candidate["steps"] = [step for step in candidate["steps"] if step["step_id"] == "done"]
    candidate["groups"] = []
    candidate["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("done"),
        "edges": [],
    }
    with pytest.raises(ValueError, match="scope"):
        registry.save_workflow(candidate, expected_revision=1)
    current = registry.get_workflow(workflow_id)
    assert current["revision"] == 1
    assert current["groups"][0]["group_id"] == "batch"
    assert (
        conn.execute(
            "SELECT count(*) FROM workflow_reactions WHERE workflow_id = %s", (workflow_id,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_change_set_reports_or_refuses_orphaned_reaction_scope(scoped_workflow, dry_run):
    registry, reactions, _, workflow_id = scoped_workflow
    reactions.save_workflow_reactions(
        workflow_id, [_rule("group", "batch")], account_id=DEFAULT_ACCOUNT_ID
    )
    kwargs = {
        "expected_revision": 1,
        "operations": [{"op": "delete_group", "group_id": "batch", "with_contents": True}],
        "author": "audit",
        "dry_run": dry_run,
    }
    if dry_run:
        result = apply_workflow_changes(registry, workflow_id, **kwargs)
        assert any(
            "reaction" in issue["code"] and "scope" in issue["code"]
            for issue in result.blocking_issues
        )
    else:
        with pytest.raises(ValueError, match="scope"):
            apply_workflow_changes(registry, workflow_id, **kwargs)
    assert registry.get_workflow(workflow_id)["revision"] == 1


def test_removing_rules_before_deleting_scope_is_supported(scoped_workflow):
    registry, reactions, _, workflow_id = scoped_workflow
    reactions.save_workflow_reactions(
        workflow_id, [_rule("group", "batch")], account_id=DEFAULT_ACCOUNT_ID
    )
    reactions.save_workflow_reactions(workflow_id, [], account_id=DEFAULT_ACCOUNT_ID)
    result = apply_workflow_changes(
        registry,
        workflow_id,
        expected_revision=1,
        operations=[{"op": "delete_group", "group_id": "batch", "with_contents": True}],
        author="audit",
    )
    assert result.revision == 2
    assert registry.get_workflow(workflow_id)["groups"] == []


def _command_service(store):
    return EventCommandService(
        get_logger=lambda: store,
        resolve_domain_event_by_ref=lambda *_: None,
        to_json_obj=lambda value: value,
        serialize_domain_event_datetime=lambda _: None,
        workflow_exists=lambda _: True,
    )


def _replace_scope(store, workflow_id, kind, identity, rules):
    return _command_service(store).update_workflow_reactions(
        workflow_id,
        WorkflowReactionsUpdateInput(reactions=rules),
        scope_kind=kind,
        scope_id=identity,
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
    )


def _submitted_rule(name):
    return {
        "reaction_key": "notify",
        "display_name": name,
        "event_type": "job.approval_waiting",
        "action": {"type": "noop"},
    }


@pytest.mark.parametrize(
    ("first_scope", "second_scope", "initial", "first_name", "second_name"),
    [
        (("workflow", ""), ("group", "batch"), False, "first", "second"),
        (("group", "batch"), ("step", "work"), True, "first", "second"),
        (("step", "work"), ("workflow", ""), True, None, "second"),
        (("workflow", ""), ("group", "batch"), True, "first", None),
        (("group", "batch"), ("step", "work"), True, None, None),
    ],
    ids=["empty-additions", "distinct-edits", "delete-and-edit", "edit-and-delete", "deletions"],
)
def test_concurrent_scope_replacements_preserve_both_writes(
    scoped_workflow, monkeypatch, first_scope, second_scope, initial, first_name, second_name
):
    _, reactions, observer, workflow_id = scoped_workflow
    scopes = [("workflow", ""), ("group", "batch"), ("step", "work")]
    if initial:
        for kind, identity in scopes:
            _replace_scope(reactions, workflow_id, kind, identity, [_submitted_rule("original")])
    first_read = threading.Event()
    second_read = threading.Event()
    release_first = threading.Event()
    dsn = os.environ["FS_DB_DSN"]
    # Pin two actual, independent driver connections; instrumentation pauses
    # after a real repository read, without replacing transaction/lock behavior.
    with (
        psycopg_runtime.connect(dsn, autocommit=True) as first_conn,
        psycopg_runtime.connect(dsn, autocommit=True) as second_conn,
    ):
        first = PlatformReactionStore(dsn)
        second = PlatformReactionStore(dsn)
        monkeypatch.setattr(first, "conn", first_conn)
        monkeypatch.setattr(second, "conn", second_conn)
        first_pid = first_conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        second_pid = second_conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        assert first_pid != second_pid
        read_first = first.list_workflow_reactions
        read_second = second.list_workflow_reactions

        def paused_read(*args, **kwargs):
            rows = read_first(*args, **kwargs)
            if not first_read.is_set():
                first_read.set()
                assert release_first.wait(15), "first writer was not released"
            return rows

        def observed_read(*args, **kwargs):
            rows = read_second(*args, **kwargs)
            second_read.set()
            return rows

        monkeypatch.setattr(first, "list_workflow_reactions", paused_read)
        monkeypatch.setattr(second, "list_workflow_reactions", observed_read)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_result = executor.submit(
                _replace_scope,
                first,
                workflow_id,
                *first_scope,
                [] if first_name is None else [_submitted_rule(first_name)],
            )
            try:
                assert first_read.wait(15), "first writer did not read persisted rules"
                second_result = executor.submit(
                    _replace_scope,
                    second,
                    workflow_id,
                    *second_scope,
                    [] if second_name is None else [_submitted_rule(second_name)],
                )
                deadline = time.monotonic() + 15
                while not second_read.is_set():
                    blockers = observer.execute(
                        "SELECT pg_blocking_pids(%s)", (second_pid,)
                    ).fetchone()[0]
                    if first_pid in blockers:
                        break
                    assert time.monotonic() < deadline, (
                        "second writer neither read nor waited on first"
                    )
                    second_read.wait(0.01)
                if second_read.is_set():
                    # Baseline can finish B while A holds stale W0: force that
                    # schedule deterministically before letting A overwrite it.
                    second_result.result(timeout=15)
            finally:
                release_first.set()
            first_result.result(timeout=15)
            second_result.result(timeout=15)
    expected = dict.fromkeys(scopes, "original") if initial else {}
    for scope, name in [(first_scope, first_name), (second_scope, second_name)]:
        if name is None:
            expected.pop(scope, None)
        else:
            expected[scope] = name
    from core.application.workflows.reaction_scopes import split_scoped_reaction_key

    rows = reactions.list_workflow_reactions(workflow_id, account_id=DEFAULT_ACCOUNT_ID)
    assert {
        split_scoped_reaction_key(row["reaction_key"])[:2]: row["display_name"] for row in rows
    } == expected


@pytest.mark.parametrize(
    ("kind", "identity"), [("workflow", ""), ("group", "batch"), ("step", "work")]
)
def test_scope_command_returns_persisted_canonical_rules(scoped_workflow, kind, identity):
    _, reactions, _, workflow_id = scoped_workflow
    rows = [
        {**_submitted_rule(" first "), "idempotency_key_template": "  {{job_id}}  "},
        {**_submitted_rule(" second "), "idempotency_key_template": "  ", "enabled": False},
    ]
    result = _replace_scope(reactions, workflow_id, kind, identity, rows)
    assert (result.scope_kind, result.scope_id) == (kind, identity)
    assert [
        (r.reaction_key, r.display_name, r.idempotency_key_template, r.enabled)
        for r in result.reactions
    ] == [
        ("notify", "first", "{{job_id}}", True),
        ("notify_2", "second", None, False),
    ]
    # A subsequent query obtains precisely the same canonical rule view.
    from core.application.workflows.reaction_scopes import serialize_scoped_reaction_rows

    _, _, stored = serialize_scoped_reaction_rows(
        reactions.list_workflow_reactions(workflow_id, account_id=DEFAULT_ACCOUNT_ID),
        scope_kind=kind,
        scope_id=identity,
    )
    assert [r.model_dump() for r in result.reactions] == [
        type(result.reactions[0]).model_validate(row).model_dump() for row in stored
    ]


@pytest.mark.parametrize(
    ("kind", "identity"),
    [
        ("for_each", "batch"),
        ("phase", "batch"),
        ("unknown", "batch"),
        ("group", ""),
        ("step", ""),
        ("group", "missing"),
        ("step", "missing"),
    ],
)
def test_scope_command_rejects_unsupported_or_missing_scope(scoped_workflow, kind, identity):
    _, reactions, _, workflow_id = scoped_workflow
    rules = [_submitted_rule("invalid")]
    with pytest.raises(ServiceValidationError):
        _replace_scope(reactions, workflow_id, kind, identity, rules)
    assert reactions.list_workflow_reactions(workflow_id, account_id=DEFAULT_ACCOUNT_ID) == []


def test_scope_replacement_preserves_other_account_rules(scoped_workflow):
    _, reactions, observer, workflow_id = scoped_workflow
    other_account = str(uuid.uuid4())
    observer.execute(
        "INSERT INTO accounts (id, account_key) VALUES (%s, %s)",
        (other_account, "scope_atomicity_" + other_account),
    )
    try:
        reactions.save_workflow_reactions(
            workflow_id, [_submitted_rule("other account")], account_id=other_account
        )
        other_before = reactions.list_workflow_reactions(workflow_id, account_id=other_account)
        _replace_scope(reactions, workflow_id, "workflow", "", [_submitted_rule("ours")])
        _replace_scope(reactions, workflow_id, "group", "batch", [_submitted_rule("group")])
        _replace_scope(reactions, workflow_id, "workflow", "", [])
        assert (
            reactions.list_workflow_reactions(workflow_id, account_id=other_account) == other_before
        )
        assert [
            r["display_name"]
            for r in reactions.list_workflow_reactions(workflow_id, account_id=DEFAULT_ACCOUNT_ID)
        ] == ["group"]
    finally:
        observer.execute("DELETE FROM workflow_reactions WHERE account_id = %s", (other_account,))
        observer.execute("DELETE FROM accounts WHERE id = %s", (other_account,))


def test_scope_replacement_revalidates_after_concurrent_definition_delete(
    scoped_workflow, monkeypatch
):
    registry, reactions, observer, workflow_id = scoped_workflow
    dsn = os.environ["FS_DB_DSN"]
    with (
        psycopg_runtime.connect(dsn, autocommit=True) as definition_conn,
        psycopg_runtime.connect(dsn, autocommit=True) as rule_conn,
    ):
        editor = RegistryDBStore(dsn)
        monkeypatch.setattr(editor, "_conn", definition_conn)
        writer = PlatformReactionStore(dsn)
        monkeypatch.setattr(writer, "conn", rule_conn)
        definition_pid = definition_conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        writer_pid = rule_conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        assert definition_pid != writer_pid
        with ThreadPoolExecutor(max_workers=1) as executor:
            with (
                transaction_connection(definition_conn),
                editor.participate_in_external_transaction(definition_conn),
            ):
                apply_workflow_changes(
                    editor,
                    workflow_id,
                    expected_revision=1,
                    operations=[{"op": "delete_group", "group_id": "batch", "with_contents": True}],
                    author="audit",
                )
                result = executor.submit(
                    _replace_scope, writer, workflow_id, "group", "batch", [_submitted_rule("late")]
                )
                deadline = time.monotonic() + 15
                while (
                    definition_pid
                    not in observer.execute(
                        "SELECT pg_blocking_pids(%s)", (writer_pid,)
                    ).fetchone()[0]
                ):
                    assert not result.done(), "rule writer did not wait for definition transaction"
                    assert time.monotonic() < deadline, "rule writer did not reach workflow lock"
                    threading.Event().wait(0.01)
            with pytest.raises(ServiceValidationError, match="missing group scope"):
                result.result(timeout=15)
    assert registry.get_workflow(workflow_id)["revision"] == 2
    assert reactions.list_workflow_reactions(workflow_id, account_id=DEFAULT_ACCOUNT_ID) == []
