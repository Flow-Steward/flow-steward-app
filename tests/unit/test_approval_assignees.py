"""Assigned approvers of an Approval step and the approval decision journal (ADR 0082)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.approval_assignees import (
    APPROVAL_NOT_ASSIGNED_CODE,
    ApprovalAssigneeRepository,
    StepApproverRequiredError,
    may_decide_step,
    without_personal_identity,
)
from core.application.management.approval_journal import (
    DECIDED_BY_KINDS,
    ApprovalJournalEntry,
    ApprovalJournalRepository,
)
from core.application.management.command_models_api_inputs import (
    ApprovalDecisionInput,
    ExternalActorInput,
)
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.scope import (
    ScopeContext,
    scope_from_principal,
    scope_from_state,
)
from core.application.tooling.ops import retention_cleanup
from core.application.workflows.reaction_service_runtime import (
    resume_job_via_approval_command_service,
)
from core.infrastructure.db import system_settings_store

# These tests call the registry store outside a request or a job; they act for
# the installation account (ADR 0020, C5).
pytestmark = pytest.mark.usefixtures("installation_account")

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"
WORKFLOW = "acme.invoice_review"
STEP = "review"


# --- Fakes ----------------------------------------------------------------------------


class _Cursor:
    """Answers the approver and workflow-name reads; records every statement."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 1

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        text = " ".join(str(sql).split())
        self._conn.statements.append((text, params))
        self.description = None
        self._rows = []
        self.rowcount = 1
        if text.startswith("INSERT INTO public.approval_decision_journal"):
            if self._conn.journal_error is not None:
                raise self._conn.journal_error
            return
        if "FROM public.approval_step_approvers" in text and "ANY(" in text:
            workflows = set(params[0])
            self.description = [("workflow_id",), ("step_id",), ("user_id",)]
            self._rows = [
                (workflow, step, user_id)
                for (workflow, step), user_ids in self._conn.approvers.items()
                if workflow in workflows
                for user_id in user_ids
            ]
            return
        if "FROM public.approval_step_approvers" in text and text.startswith("SELECT"):
            workflow, step = params[0], params[1]
            self.description = [("user_id",)]
            self._rows = [(user_id,) for user_id in self._conn.approvers.get((workflow, step), ())]
            return
        if "FROM public.registry_workflows" in text:
            self.description = [("display_name",)]
            self._rows = [("Invoice review",)]
            return
        if "FROM public.registry_workflow_steps" in text:
            workflows, step = params[0], params[1]
            self.description = [("display_name",)]
            self._rows = [
                (name,)
                for (workflow, step_id), name in self._conn.step_names.items()
                if workflow in workflows and step_id == step
            ][:1]
            return
        if text.startswith("SELECT display_name FROM public.users"):
            key, account = params
            name = self._conn.user_names.get(key)
            self.description = [("display_name",)]
            self._rows = [(name,)] if name is not None and account == ACCOUNT else []
            return
        if text.startswith("DELETE FROM public.approval_decision_journal"):
            self.rowcount = self._conn.purge_rowcount

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(
        self,
        approvers: dict[tuple[str, str], tuple[str, ...]] | None = None,
        *,
        journal_error: Exception | None = None,
        purge_rowcount: int = 0,
    ) -> None:
        self.approvers = dict(approvers or {})
        self.journal_error = journal_error
        self.purge_rowcount = purge_rowcount
        # users.display_name by user id or username; step display names by (workflow, step).
        self.user_names: dict[str, str] = {}
        self.step_names: dict[tuple[str, str], str] = {}
        self.statements: list[tuple[str, tuple | None]] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self)

    def sql(self, prefix: str) -> list[tuple[str, tuple | None]]:
        return [item for item in self.statements if item[0].startswith(prefix)]


def _person(
    user_id: str = ALICE, *, auth_source: str = "local", name: str = "alice"
) -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        actor_id=name,
        user_id=user_id,
        principal_type="operator",
        auth_source=auth_source,
    )


def _service_account() -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        actor_id="acme-robot",
        principal_type="service_account",
        auth_source="machine",
    )


def _service() -> ApprovalCommandService:
    service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: None,
        enqueue_workflow_resume=MagicMock(),
        workflow_resume_repository_factory=MagicMock(),
    )
    service._record_in_progress_marker = MagicMock()
    return service


def _row(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "44444444-4444-4444-8444-444444444444",
        "status": "awaiting_approval",
        "project_id": "55555555-5555-4555-8555-555555555555",
        "task_id": "TASK-1",
        "selected_workflow": WORKFLOW,
        "account_id": ACCOUNT,
        "started_at": datetime(2026, 11, 25, 9, 0, tzinfo=UTC),
        "meta": {"submitted_by": {"id": "carol", "kind": "person"}},
        **overrides,
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "approval_id": "gate-1",
        "step_id": STEP,
        "stage": "Review invoice",
        "message": "Check the total",
        "allowed_actions": ["approve", "reject"],
        **overrides,
    }


def _review_payload() -> dict[str, Any]:
    return _payload(
        review={
            "version": 1,
            "fields": [{"name": "total", "label": "Total", "type": "number", "editable": True}],
            "editable": ["total"],
            "values": {"total": 10},
            "invalid": {},
        }
    )


def _decide(
    conn: _Conn,
    scope: ScopeContext,
    *,
    decision: ApprovalDecisionInput | None = None,
    payload: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> Any:
    bl = SimpleNamespace(conn=conn)
    return _service()._persist_canonical_decision(
        bl=bl,
        transaction_conn=conn,
        row=row or _row(),
        thread_id="thread-1",
        approval_id="gate-1",
        payload=payload or _payload(),
        decision=decision or ApprovalDecisionInput(approved=True, approval_id="gate-1"),
        scope=scope,
    )


def _journal_rows(conn: _Conn) -> list[dict[str, Any]]:
    rows = []
    for sql, params in conn.sql("INSERT INTO public.approval_decision_journal"):
        columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        rows.append(dict(zip(columns, params, strict=True)))
    return rows


# --- may_decide_step -------------------------------------------------------------------


def test_a_step_without_approvers_is_decided_by_anyone_as_today() -> None:
    assert may_decide_step(_person(BOB), ()) is True
    assert may_decide_step(_service_account(), ()) is True
    assert may_decide_step(None, []) is True


def test_only_a_listed_person_may_decide_a_restricted_step() -> None:
    assert may_decide_step(_person(ALICE), {ALICE}) is True
    assert may_decide_step(_person(ALICE.upper()), {ALICE}) is True
    assert may_decide_step(_person(ALICE, auth_source="pat"), {ALICE}) is True
    assert may_decide_step(_person(ALICE, auth_source="oauth"), {ALICE}) is True
    assert may_decide_step(_person(BOB), {ALICE}) is False


def test_non_persons_never_pass_a_restricted_step() -> None:
    assert may_decide_step(_service_account(), {ALICE}) is False
    system = ScopeContext(
        account_id=ACCOUNT, actor_id="flow-steward-cli", principal_type="system_actor"
    )
    assert may_decide_step(system, {ALICE}) is False
    # Even a scope that claims a listed id is refused when it is not a person.
    forged = _service_account().model_copy(update={"user_id": ALICE})
    assert may_decide_step(forged, {ALICE}) is False
    assert may_decide_step(ScopeContext(account_id=ACCOUNT, actor_id="alice"), {ALICE}) is False
    assert may_decide_step(None, {ALICE}) is False


def test_a_reaction_scope_carries_no_person() -> None:
    stripped = without_personal_identity(_person(ALICE))
    assert stripped is not None
    assert stripped.user_id is None
    assert stripped.actor_id == "alice"
    assert may_decide_step(stripped, {ALICE}) is False


# --- ScopeContext.user_id -----------------------------------------------------------


def _principal(**overrides: Any) -> dict[str, Any]:
    return {
        "id": ALICE,
        "username": "alice",
        "account_id": ACCOUNT,
        "principal_type": "operator",
        "auth_source": "local",
        "roles": ["operator"],
        **overrides,
    }


@pytest.mark.parametrize("auth_source", ["local", "pat", "oauth", "external"])
def test_a_person_and_their_tokens_carry_the_user_id(auth_source: str) -> None:
    scope = scope_from_principal(_principal(auth_source=auth_source))
    assert scope.user_id == ALICE


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal_type": "service_account", "auth_source": "machine", "id": ""},
        {"principal_type": "service_account", "auth_source": "machine"},
        {"principal_type": "system_actor", "auth_source": "mcp_dev", "id": "mcp-dev-system"},
        {"auth_source": "project_connection"},
        {"auth_source": "oauth_callback"},
        {"id": "not-a-uuid"},
    ],
)
def test_non_persons_carry_no_user_id(overrides: dict[str, Any]) -> None:
    assert scope_from_principal(_principal(**overrides)).user_id is None


def test_runtime_state_never_carries_a_user_id() -> None:
    state = {"scope_context": {"account_id": ACCOUNT, "user_id": ALICE}, "user_id": ALICE}
    assert scope_from_state(state).user_id is None
    assert scope_from_state({"account_id": ACCOUNT, "user_id": ALICE}).user_id is None


# --- The check in the decision path ---------------------------------------------------


def test_unrestricted_step_is_decided_and_journaled_as_today() -> None:
    conn = _Conn()

    resume_row = _decide(conn, _person(BOB, name="bob"))

    assert resume_row is not None
    assert len(conn.sql("UPDATE jobs")) == 1
    assert len(_journal_rows(conn)) == 1


def test_listed_user_decides_a_restricted_step() -> None:
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})

    resume_row = _decide(conn, _person(ALICE))

    assert resume_row is not None
    assert len(conn.sql("UPDATE jobs")) == 1


@pytest.mark.parametrize(
    "scope",
    [
        _person(BOB, name="bob"),
        _service_account(),
        ScopeContext(
            account_id=ACCOUNT, actor_id="flow-steward-cli", principal_type="system_actor"
        ),
    ],
    ids=["unlisted-user", "service-account", "system-actor"],
)
def test_others_are_refused_before_anything_is_written(scope: ScopeContext) -> None:
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})

    with pytest.raises(StepApproverRequiredError) as refused:
        _decide(conn, scope)

    assert refused.value.status_code == 403
    assert refused.value.payload["code"] == APPROVAL_NOT_ASSIGNED_CODE
    assert conn.sql("UPDATE jobs") == []
    assert _journal_rows(conn) == []


def test_an_external_actor_is_refused_on_a_restricted_step() -> None:
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})
    decision = ApprovalDecisionInput(
        approved=True,
        approval_id="gate-1",
        external_actor=ExternalActorInput(id="chat:alice"),
    )

    with pytest.raises(StepApproverRequiredError):
        _decide(conn, _service_account(), decision=decision)
    # Not even a listed person's scope lets an external actor through.
    with pytest.raises(StepApproverRequiredError):
        _decide(conn, _person(ALICE), decision=decision)


def test_restriction_holds_under_any_name_of_the_job_workflow() -> None:
    conn = _Conn({("acme.invoice_review_key", STEP): (ALICE,)})
    row = _row(selected_workflow="", selected_workflow_key="acme.invoice_review_key")

    with pytest.raises(StepApproverRequiredError):
        _decide(conn, _person(BOB, name="bob"), row=row)


def test_restriction_applies_without_any_entitlement() -> None:
    """ADR 0082 section 4: the check never asks the entitlement service."""
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})

    with pytest.raises(StepApproverRequiredError):
        _decide(conn, _person(BOB, name="bob"))
    assert _decide(conn, _person(ALICE)) is not None


def test_a_reaction_is_refused_on_a_restricted_step_even_with_a_listed_persons_scope() -> None:
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})
    seen: dict[str, Any] = {}

    class _Approvals:
        def resume_task(self, *, thread_id, scope, decision, idempotency_key):
            del thread_id, idempotency_key
            seen["scope"] = scope
            return _decide(conn, scope, decision=decision)

    job_store = MagicMock()
    job_store.get_job.return_value = {"id": "job-1", "thread_id": "thread-1"}

    result = resume_job_via_approval_command_service(
        "job-1",
        "approve from chat",
        approved=True,
        expected_approval_id="gate-1",
        scope=_person(ALICE),
        idempotency_key="reaction-1",
        get_job_store=lambda: job_store,
        approval_command_service_factory=_Approvals,
    )

    assert seen["scope"].user_id is None
    assert result == {"status": "ignored", "reason": APPROVAL_NOT_ASSIGNED_CODE}
    assert conn.sql("UPDATE jobs") == []


def test_a_gate_without_a_step_id_uses_the_jobs_current_step() -> None:
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})
    payload = _payload(step_id="")

    with pytest.raises(StepApproverRequiredError):
        _decide(conn, _person(BOB, name="bob"), payload=payload, row=_row(current_step_id=STEP))
    _decide(conn, _person(ALICE), payload=payload, row=_row(current_step_id=STEP))

    assert len(_journal_rows(conn)) == 1


def test_a_gate_whose_step_is_unknown_is_refused_when_the_workflow_is_restricted() -> None:
    # E.g. a Test Run's state-based gate whose payload could not be read.
    restricted = _Conn({(WORKFLOW, STEP): (ALICE,)})
    with pytest.raises(StepApproverRequiredError):
        _decide(restricted, _person(ALICE), payload=_payload(step_id=""))
    assert _journal_rows(restricted) == []

    unrestricted = _Conn()
    _decide(unrestricted, _person(BOB, name="bob"), payload=_payload(step_id=""))
    assert len(_journal_rows(unrestricted)) == 1


def test_a_decision_override_of_a_restricted_step_is_refused() -> None:
    from core.application.management.command_models import JobDecisionOverrideInput
    from core.application.management.command_services_job import JobCommandService

    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})
    bl = MagicMock()
    bl.conn = conn
    bl.get_job.return_value = {
        "id": "job-9",
        "project_id": "1",
        "thread_id": "thread-9",
        "selected_workflow": WORKFLOW,
        "meta": {},
    }
    bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
    service = JobCommandService(
        get_logger=lambda: bl,
        get_thread_state_values=lambda thread_id: {},
        runtime_step_scope_id=lambda runtime_state: "",
        latest_runtime_message_id=lambda runtime_state, step_execution_id="": None,
        override_workflow_id_from_value=lambda value: "",
        is_empty_original_value=lambda value: value in (None, "", {}),
        derive_override_original_value=lambda **kwargs: {"outcome": "approved"},
        validate_explicit_workflow_override=lambda **kwargs: None,
        to_json_obj=lambda value: value,
        serialize_decision_override_row=lambda row: row,
    )
    command = JobDecisionOverrideInput(
        domain="gate_outcome",
        override_value={"outcome": "rejected"},
        reason="Override",
        created_by="bob",
        # A caller naming another workflow does not escape the restriction.
        workflow_id="acme.other_workflow",
        step_id=STEP,
    )
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="bob", user_id=BOB)

    with pytest.raises(StepApproverRequiredError):
        service.create_decision_override("job-9", command, scope=scope)

    bl.create_job_decision_override.assert_not_called()


# --- The journal ------------------------------------------------------------------------


def test_one_journal_row_per_decision_with_the_actor() -> None:
    conn = _Conn({(WORKFLOW, STEP): (ALICE,)})
    decision = ApprovalDecisionInput(
        approved=True, approval_id="gate-1", comment="corrected", values={"total": 12}
    )

    _decide(conn, _person(ALICE, auth_source="pat"), decision=decision, payload=_review_payload())

    rows = _journal_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["account_id"] == ACCOUNT
    assert row["job_id"] == "44444444-4444-4444-8444-444444444444"
    assert row["workflow_id"] == WORKFLOW
    assert row["workflow_name"] == "Invoice review"
    assert row["step_id"] == STEP
    assert row["step_label"] == "Review invoice"
    assert row["started_by"] == "carol"
    assert row["decided_by"] == "alice"
    assert row["decided_by_user_id"] == ALICE
    assert row["decided_by_kind"] == "user"
    assert row["external_actor"] is None
    assert row["action"] == "approve"
    assert row["comment"] == "corrected"
    assert json.loads(row["reviewed_values"]) == {"total": 12}
    assert json.loads(row["changed_values"]) == {"total": {"before": 10, "after": 12}}
    # Written in the decision's transaction, inside its own savepoint.
    statements = [sql for sql, _ in conn.statements]
    assert "SAVEPOINT approval_decision_journal_entry" in statements
    assert "RELEASE SAVEPOINT approval_decision_journal_entry" in statements


def test_the_journal_records_display_names_as_they_are_at_decision_time() -> None:
    conn = _Conn()
    conn.user_names = {ALICE: "Alice Example", "carol": "Carol Example"}

    _decide(conn, _person(ALICE))

    row = _journal_rows(conn)[0]
    assert row["decided_by"] == "alice"
    assert row["decided_by_display_name"] == "Alice Example"
    assert row["started_by"] == "carol"
    assert row["started_by_display_name"] == "Carol Example"


def _users_lookups(conn: _Conn) -> list[tuple | None]:
    return [
        params
        for sql, params in conn.statements
        if sql.startswith("SELECT display_name FROM public.users")
    ]


@pytest.mark.parametrize(
    ("submitted_by", "expected"),
    [
        (
            {"id": "schedule:sched-7", "kind": "schedule", "display_name": "Nightly invoices"},
            "Nightly invoices",
        ),
        (
            {"id": "webhook:ep-1", "kind": "webhook", "display_name": "Supplier portal"},
            "Supplier portal",
        ),
        (
            {"id": "automation:workflow_test_run", "kind": "automation"},
            "automation:workflow_test_run",
        ),
        ({"id": "acme-robot", "kind": "service_account"}, "acme-robot"),
        (
            {"id": "acme-robot", "kind": "service_account", "display_name": "Acme robot"},
            "Acme robot",
        ),
    ],
    ids=["schedule", "webhook", "automation", "service-account", "service-account-named"],
)
def test_the_journal_names_a_non_person_starter_by_itself_not_from_users(
    submitted_by: dict[str, str], expected: str
) -> None:
    conn = _Conn()
    # A user that happens to share the starter id must not lend it a name.
    conn.user_names = {submitted_by["id"]: "Somebody Else"}

    _decide(conn, _person(ALICE), row={**_row(), "meta": {"submitted_by": submitted_by}})

    row = _journal_rows(conn)[0]
    assert row["started_by"] == submitted_by["id"]
    assert row["started_by_display_name"] == expected
    assert all(submitted_by["id"] not in (params or ()) for params in _users_lookups(conn))


@pytest.mark.parametrize(
    "submitted_by", ["carol", {"id": "carol"}, {"id": "carol", "kind": "person"}]
)
def test_the_journal_names_a_person_starter_from_users(submitted_by: Any) -> None:
    conn = _Conn()
    conn.user_names = {"carol": "Carol Example"}

    _decide(conn, _person(ALICE), row={**_row(), "meta": {"submitted_by": submitted_by}})

    assert _journal_rows(conn)[0]["started_by_display_name"] == "Carol Example"


def test_the_journal_leaves_display_names_empty_for_people_without_one_and_non_people() -> None:
    conn = _Conn()

    _decide(conn, _service_account())

    row = _journal_rows(conn)[0]
    assert row["decided_by_display_name"] is None
    assert row["started_by_display_name"] is None
    # A service account is never looked up as a person.
    assert not [
        params
        for sql, params in conn.statements
        if sql.startswith("SELECT display_name FROM public.users") and "acme-robot" in params
    ]


def test_the_step_label_falls_back_to_the_definition_then_to_the_step_id() -> None:
    named = _Conn()
    named.step_names = {(WORKFLOW, STEP): "Check the invoice"}
    _decide(named, _person(ALICE), payload=_payload(stage=""))
    assert _journal_rows(named)[0]["step_label"] == "Check the invoice"

    unnamed = _Conn()
    _decide(unnamed, _person(ALICE), payload=_payload(stage=""))
    assert _journal_rows(unnamed)[0]["step_label"] == STEP


def test_a_relayed_external_decision_is_journaled_as_external() -> None:
    conn = _Conn()
    decision = ApprovalDecisionInput(
        approved=False,
        action="reject",
        comment="no",
        approval_id="gate-1",
        external_actor=ExternalActorInput(id="chat:dave", display_name="Dave"),
    )

    _decide(conn, _service_account(), decision=decision)

    row = _journal_rows(conn)[0]
    assert row["decided_by"] == "acme-robot"
    assert row["decided_by_user_id"] is None
    assert row["decided_by_kind"] == "external"
    assert json.loads(row["external_actor"]) == {"id": "chat:dave", "display_name": "Dave"}
    assert row["action"] == "reject"


def test_a_service_account_decision_is_journaled_without_a_user_id() -> None:
    conn = _Conn()

    _decide(conn, _service_account())

    row = _journal_rows(conn)[0]
    assert row["decided_by_kind"] == "service_account"
    assert row["decided_by_user_id"] is None


@pytest.mark.parametrize("principal_type", [None, "operator", "admin"])
def test_a_decision_without_a_person_is_journaled_as_automation(principal_type) -> None:
    conn = _Conn()
    # What a reaction or a dispatched command passes: the triggering user's
    # identity with the user id removed.
    scope = without_personal_identity(_person(ALICE))
    if principal_type is None:
        scope = ScopeContext(account_id=ACCOUNT, actor_id="reaction")
    else:
        scope = scope.model_copy(update={"principal_type": principal_type})

    _decide(conn, scope)

    row = _journal_rows(conn)[0]
    assert row["decided_by_kind"] == "automation"
    assert row["decided_by_user_id"] is None


def test_a_journal_failure_does_not_lose_the_decision(caplog: pytest.LogCaptureFixture) -> None:
    conn = _Conn(journal_error=RuntimeError("journal unavailable"))

    with caplog.at_level(logging.ERROR):
        resume_row = _decide(conn, _person(ALICE))

    assert resume_row is not None
    assert len(conn.sql("UPDATE jobs")) == 1
    statements = [sql for sql, _ in conn.statements]
    assert "ROLLBACK TO SAVEPOINT approval_decision_journal_entry" in statements
    assert "journal write failed" in caplog.text


def test_journal_entry_validates_its_kind() -> None:
    with pytest.raises(ValueError, match="decided_by_kind"):
        ApprovalJournalEntry(
            account_id=ACCOUNT, job_id="job", decided_by="x", decided_by_kind="robot", action="a"
        )


# --- Repositories ---------------------------------------------------------------------------


def test_assignee_repository_reads_and_replaces() -> None:
    conn = _Conn({(WORKFLOW, STEP): (BOB, ALICE)})
    repository = ApprovalAssigneeRepository(conn)

    assert repository.list_approver_ids(WORKFLOW, STEP) == (ALICE, BOB)
    assert repository.list_approver_ids(WORKFLOW, "") == ()

    repository.replace_approvers(WORKFLOW, STEP, [ALICE, ALICE.upper()], created_by="admin")
    deletes = conn.sql("DELETE FROM public.approval_step_approvers")
    inserts = conn.sql("INSERT INTO public.approval_step_approvers")
    # Every statement names the workflow's account (ADR 0020, C4b): the default one here.
    assert deletes[0][1] == (WORKFLOW, STEP, [ALICE], DEFAULT_ACCOUNT_ID)
    assert [params for _, params in inserts] == [
        (WORKFLOW, STEP, ALICE, "admin", DEFAULT_ACCOUNT_ID)
    ]

    with pytest.raises(ValueError, match="not a user id"):
        repository.replace_approvers(WORKFLOW, STEP, ["alice"], created_by="admin")


def test_journal_repository_has_no_update_and_purges_by_rule() -> None:
    assert not hasattr(ApprovalJournalRepository, "update")
    conn = _Conn(purge_rowcount=3)
    repository = ApprovalJournalRepository(conn)
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    assert repository.purge(ACCOUNT, older_than=None, only_orphaned=True) == 3
    assert repository.purge(ACCOUNT, older_than=cutoff, only_orphaned=False) == 3

    orphaned, aged = conn.sql("DELETE FROM public.approval_decision_journal")
    assert "NOT EXISTS (SELECT 1 FROM public.jobs job WHERE job.id = j.job_id)" in orphaned[0]
    assert "decided_at" not in orphaned[0]
    assert orphaned[1] == (ACCOUNT,)
    assert "NOT EXISTS" not in aged[0]
    assert aged[1] == (ACCOUNT, cutoff)


# --- Retention ------------------------------------------------------------------------------


def _policy(account_id: str, journal_days: int) -> retention_cleanup.AccountRetentionPolicy:
    return retention_cleanup.AccountRetentionPolicy(
        account_id=account_id,
        job_logs_days=30,
        action_logs_days=30,
        trash_days=30,
        terminal_job_checkpoints_days=30,
        approval_journal_retention_days=journal_days,
    )


def _cfg(journal_entitled=None) -> retention_cleanup.RetentionConfig:
    return retention_cleanup.RetentionConfig(
        jobs_days=30,
        job_logs_days=30,
        action_logs_days=30,
        events_days=30,
        retry_queue_days=14,
        trash_days=30,
        terminal_job_checkpoints_days=30,
        dry_run=False,
        retention_defaults={},
        journal_entitled=journal_entitled,
    )


def _purges(cfg: retention_cleanup.RetentionConfig, policies) -> list[tuple[str, tuple]]:
    conn = _Conn()
    cache = {"table:approval_decision_journal": True}
    now = datetime(2026, 11, 25, tzinfo=UTC)
    retention_cleanup._purge_approval_journal(
        conn, conn.cursor(), cache=cache, policies=policies, cfg=cfg, now=now
    )
    return conn.sql("DELETE FROM public.approval_decision_journal")


def test_journal_rows_go_with_their_job_by_default() -> None:
    purges = _purges(
        _cfg(journal_entitled=lambda _account_id: False),
        [_policy(ACCOUNT, 0), _policy("acme-two", 90)],
    )

    # A definite "not entitled" ignores a configured retention: rows follow jobs.
    assert all("NOT EXISTS" in sql for sql, _ in purges)
    assert [params for _, params in purges] == [(ACCOUNT,), ("acme-two",)]


def test_an_entitled_account_keeps_rows_for_its_journal_retention() -> None:
    purges = _purges(
        _cfg(journal_entitled=lambda account_id: account_id == ACCOUNT),
        [_policy(ACCOUNT, 90), _policy("acme-two", 90), _policy("acme-three", 0)],
    )

    entitled, not_entitled, no_setting = purges
    assert "NOT EXISTS" not in entitled[0]
    assert entitled[1] == (ACCOUNT, datetime(2026, 8, 27, tzinfo=UTC))
    assert "NOT EXISTS" in not_entitled[0]
    assert "NOT EXISTS" in no_setting[0]


def _broken(_account_id: str) -> bool:
    raise RuntimeError("source down")


@pytest.mark.parametrize(
    "journal_entitled",
    [None, _broken, lambda _account_id: None],
    ids=["no-entitlement-service", "check-raised", "source-failed"],
)
def test_an_unknown_entitlement_purges_nothing_for_an_account_with_a_journal_retention(
    journal_entitled, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=retention_cleanup.__name__)

    purges = _purges(
        _cfg(journal_entitled=journal_entitled),
        [_policy(ACCOUNT, 90), _policy("acme-two", 0)],
    )

    # The account with a retention keeps every row; the one without follows its jobs.
    assert [params for _, params in purges] == [("acme-two",)]
    assert "NOT EXISTS" in purges[0][0]
    assert any("is unknown" in record.getMessage() for record in caplog.records)


def test_scheduled_retention_asks_the_entitlement_service(monkeypatch) -> None:
    seen = {}

    def fake_run(dsn, cfg):
        seen["cfg"] = cfg
        return {}

    monkeypatch.setattr(retention_cleanup, "resolve_observability_dsn", lambda value: "dsn://x")
    monkeypatch.setattr(
        retention_cleanup,
        "_load_db_retention_settings",
        lambda *_a, **_k: {
            "action_logs_days": 30,
            "job_logs_days": 30,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 30,
            "revisions_keep": 5,
        },
    )
    monkeypatch.setattr(retention_cleanup, "run_cleanup", fake_run)
    entitlements = MagicMock()
    entitlements.entitlement_status.side_effect = lambda account, feature: (
        None if account == "acme-down" else account == ACCOUNT and feature == "approval_journal"
    )

    retention_cleanup.run_scheduled_retention(entitlement_service=entitlements)

    assert seen["cfg"].journal_entitled(ACCOUNT) is True
    assert seen["cfg"].journal_entitled("acme-two") is False
    assert seen["cfg"].journal_entitled("acme-down") is None


def test_journal_retention_setting_allows_zero_and_survives_a_form_without_it() -> None:
    normalized = system_settings_store.normalize_retention_settings(
        {"approval_journal_retention_days": "0"}
    )
    assert normalized["approval_journal_retention_days"] == 0
    assert (
        system_settings_store.normalize_retention_settings(
            {"approval_journal_retention_days": 99999}
        )["approval_journal_retention_days"]
        == 3650
    )

    class _SaveCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((str(sql), params))

        def fetchone(self):
            return None

    cursor = _SaveCursor()
    conn = SimpleNamespace(cursor=lambda *_a, **_k: cursor)

    system_settings_store.save_retention_settings(
        conn=conn, settings={"trash_days": 7}, actor="admin", account_id=ACCOUNT
    )
    system_settings_store.save_retention_settings(
        conn=conn,
        settings={"trash_days": 7, "approval_journal_retention_days": 30},
        actor="admin",
        account_id=ACCOUNT,
    )

    without_field, with_field = (json.loads(params[2]) for _, params in cursor.calls)
    assert "approval_journal_retention_days" not in without_field
    assert with_field["approval_journal_retention_days"] == 30
    upsert = " ".join(cursor.calls[0][0].split())
    assert "system_settings.setting_value_jsonb -> 'approval_journal_retention_days'" in upsert


# --- Schema ------------------------------------------------------------------------------


def _journal_ddl(relative: str) -> str:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / relative).read_text(encoding="utf-8")
    start = text.index("public.approval_decision_journal (")
    return " ".join(text[start : text.index(");", start)].split())


@pytest.mark.parametrize(
    "relative",
    [
        "db/schema/001_schema_part_12.sql",
        "db/migrations/20261125120000_approval_assignees_journal.sql",
    ],
)
def test_the_journal_table_outlives_its_account_and_accepts_every_actor_kind(
    relative: str,
) -> None:
    ddl = _journal_ddl(relative)

    # No foreign key to the account (or the job): rows outlive both.
    assert "account_id uuid NOT NULL," in ddl
    assert "REFERENCES" not in ddl
    for kind in DECIDED_BY_KINDS:
        assert f"'{kind}'::text" in ddl


def test_a_decision_without_a_database_is_refused_not_waved_through() -> None:
    from core.application.management.approval_assignees import require_step_approver
    from core.application.management.errors import ServiceUnavailableError

    with pytest.raises(ServiceUnavailableError):
        require_step_approver(None, None, workflow_ids=["wf-1"], step_id="approve")


def test_a_job_without_a_workflow_needs_no_lookup() -> None:
    from core.application.management.approval_assignees import require_step_approver

    assert require_step_approver(None, None, workflow_ids=[], step_id="approve") == frozenset()
