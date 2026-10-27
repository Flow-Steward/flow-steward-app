"""Direct-action recovery binds reloads and retries to one exact host ledger intent."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from core.application.extensions import external_effects
from core.application.extensions.external_effect_resolution import resolve_external_effect
from core.application.extensions.operation_context import _ActionExecutionContext
from core.application.extensions.operation_effects import _claim_action_external_effect
from core.application.management.errors import (
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
)
from core.application.management.requests import ExtensionActionRequest
from core.application.management.scope import ScopeContext
from core.infrastructure.db import external_effect_store

pytestmark = pytest.mark.integration


@pytest.fixture
def direct_case(disposable_db, two_tenants, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "unit-direct-effect-recovery-root")
    conn = psycopg.connect(disposable_db.dsn, autocommit=True, row_factory=dict_row)
    # Shared fixture project INSERTs predate explicit account ownership. Repair
    # only these four owned rows so isolation is tested against the real owners.
    with conn.cursor() as cur:
        for tenant in (two_tenants.a, two_tenants.b):
            for owned_project in tenant.projects.values():
                cur.execute(
                    "UPDATE projects SET account_id = %s WHERE id = %s",
                    (tenant.account_id, owned_project.id),
                )
                cur.execute("SELECT account_id FROM projects WHERE id = %s", (owned_project.id,))
                assert str(cur.fetchone()["account_id"]) == tenant.account_id
    account = two_tenants.a.account_id
    project = two_tenants.a.shared.id
    extension_id = "fixture.direct-recovery"
    install_id = str(uuid4())
    foreign_install_id = str(uuid4())
    with conn.cursor() as cur:
        for owner, installation in (
            (account, install_id),
            (two_tenants.b.account_id, foreign_install_id),
        ):
            cur.execute(
                "INSERT INTO extension_installs (extension_id, install_id, account_id) VALUES (%s, %s, %s)",
                (extension_id, installation, owner),
            )

    def context(
        *,
        key=None,
        value="original",
        actor="first-operator",
        project_id=project,
        account_id=account,
        install=None,
        connection="connection-original",
        ref=None,
    ):
        install = install or (
            foreign_install_id if account_id == two_tenants.b.account_id else install_id
        )
        body = ExtensionActionRequest.model_validate(
            {
                "page_id": "home",
                "component_id": "create",
                "context": {"project_id": project_id},
                "target": {"connection_id": connection},
                "input": {"value": value},
                **({"resolution_ref": ref} if ref else {}),
            }
        )
        scope = ScopeContext(
            account_id=account_id, project_id=project_id, actor_id=actor, principal_type="operator"
        )
        return _ActionExecutionContext(
            extension_id=extension_id,
            action_id="create",
            body=body,
            scope=scope,
            resolved_scope=scope,
            extension={
                "extension_key": extension_id,
                "extension_install_id": install,
                "operation_manifest": [
                    {
                        "operation_id": "create",
                        "outputs": [{"name": "record_id", "value_type": "string"}],
                    }
                ],
                "external_effects": [
                    {"operation_id": "create", "effect_kind": "record_create", "channel": "api"}
                ],
            },
            bl=store,
            target=dict(body.target),
            input_payload=dict(body.input),
            audit={
                "account_id": account_id,
                "project_id": project_id,
                "actor_id": actor,
                "actor_type": "operator",
            },
            runtime_ctx={},
            direct_action_idempotency_key=key or str(uuid4()),
            direct_action_intent={
                "page_id": body.page_id,
                "component_id": body.component_id,
                "context": dict(body.context),
                "target": dict(body.target),
                "input": dict(body.input),
                "artifact_inputs": [],
                "artifact_outputs": [],
                "artifact_output_specs": [],
                "dataset_inputs": [],
                "dataset_outputs": [],
                "dataset_output_specs": [],
            },
        )

    def seed_binding(ctx):
        if ctx.target.get("connection_id"):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO project_connections "
                    "(connection_id, account_id, project_id, extension_id, extension_install_id, connection_kind) "
                    "VALUES (%s, %s, %s, %s, %s, 'extension_runtime') ON CONFLICT DO NOTHING",
                    (
                        ctx.target["connection_id"],
                        ctx.audit["account_id"],
                        ctx.audit["project_id"],
                        extension_id,
                        ctx.extension["extension_install_id"],
                    ),
                )
                cur.execute(
                    "SELECT p.account_id AS project_account, c.account_id AS connection_account, "
                    "c.project_id, c.extension_id, c.extension_install_id, i.account_id AS install_account "
                    "FROM project_connections c JOIN projects p ON p.id = c.project_id "
                    "JOIN extension_installs i ON i.install_id = c.extension_install_id "
                    "WHERE c.connection_id = %s AND c.project_id = %s",
                    (ctx.target["connection_id"], ctx.audit["project_id"]),
                )
                binding = cur.fetchone()
            assert str(binding["project_account"]) == ctx.audit["account_id"]
            assert str(binding["connection_account"]) == ctx.audit["account_id"]
            assert str(binding["install_account"]) == ctx.audit["account_id"]
            assert str(binding["project_id"]) == ctx.audit["project_id"]
            assert binding["extension_id"] == extension_id
            assert binding["extension_install_id"] == ctx.extension["extension_install_id"]

    def unknown(ctx):
        seed_binding(ctx)
        assert _claim_action_external_effect(ctx) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM external_effects WHERE account_id = %s AND idempotency_key = %s",
                (
                    ctx.audit["account_id"],
                    hashlib.sha256(ctx.direct_action_idempotency_key.encode()).hexdigest(),
                ),
            )
            row = cur.fetchone()
        return external_effect_store.update_external_effect_status(
            conn,
            effect_id=str(row["effect_id"]),
            claim_token=row["claim_token"],
            status="timeout_unknown",
            status_reason="provider_timeout",
            error_code="provider_timeout",
        )

    class Store:
        def __init__(self):
            self.conn = conn

        def cursor(self, *args, **kwargs):
            return self.conn.cursor(*args, **kwargs)

        def get_project(self, project_id):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, account_id, workspace_id FROM projects WHERE id = %s", (project_id,)
                )
                return cur.fetchone()

        def get_project_connection(self, **kwargs):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM project_connections WHERE connection_id = %s AND project_id = %s",
                    (kwargs.get("connection_id"), kwargs.get("project_id")),
                )
                return cur.fetchone()

        def get_extension_install_for_operator(self, requested, *, account_id):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT *, account_id AS install_account_id FROM extension_installs "
                    "WHERE install_id = %s AND account_id = %s AND visibility = 'account'",
                    (requested, account_id),
                )
                row = cur.fetchone()
                if row:
                    row["enabled"] = row.pop("state") == "active"
                return row

    store = Store()

    def service():
        from core.application.extensions.direct_effect_recovery import DirectEffectRecoveryService

        return DirectEffectRecoveryService(
            get_operation_store=lambda: store,
            get_install_store=lambda: store,
            get_project_by_id=store.get_project,
        )

    case = SimpleNamespace(
        conn=conn,
        store=store,
        context=context,
        unknown=unknown,
        seed_binding=seed_binding,
        service=service,
        account=account,
        project=project,
        install=install_id,
        extension_id=extension_id,
        tenants=two_tenants,
    )
    yield case
    with conn.cursor() as cur:
        for tenant in (two_tenants.a, two_tenants.b):
            cur.execute("DELETE FROM command_audit_log WHERE account_id = %s", (tenant.account_id,))
            cur.execute("DELETE FROM external_effects WHERE account_id = %s", (tenant.account_id,))
            cur.execute(
                "DELETE FROM project_connections WHERE account_id = %s", (tenant.account_id,)
            )
            cur.execute(
                "DELETE FROM extension_installs WHERE account_id = %s", (tenant.account_id,)
            )
    conn.close()


def _resolve(case, effect, action="reject", actor="decision-operator"):
    return resolve_external_effect(
        case.conn,
        account_id=case.account,
        project_id=case.project,
        effect_id=str(effect["effect_id"]),
        observed_version=effect["claim_version"],
        action=action,
        comment="Checked provider records",
        actor_id=actor,
    )


def _count(case):
    with case.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM external_effects WHERE account_id = %s", (case.account,)
        )
        return cur.fetchone()["n"]


def test_fresh_browser_uuid_cannot_bypass_the_same_unknown_direct_intent(direct_case):
    case = direct_case
    effect = case.unknown(case.context())
    fresh = case.context()
    with pytest.raises(ServiceError, match="external_effect_timeout_unknown"):
        _claim_action_external_effect(fresh)
    assert _count(case) == 1
    assert (
        external_effect_store.get_external_effect(case.conn, effect_id=str(effect["effect_id"]))[
            "status"
        ]
        == "timeout_unknown"
    )


def test_distinct_full_request_is_not_blocked_by_an_unrelated_unknown_action(direct_case):
    case = direct_case
    case.unknown(case.context())
    assert _claim_action_external_effect(case.context(value="a distinct intended record")) is None
    assert _count(case) == 2


@pytest.mark.parametrize("status", ["pending", "claimed", "running"])
def test_reload_during_an_authorized_retry_does_not_create_another_intent(direct_case, status):
    case = direct_case
    effect = case.unknown(case.context())
    receipt = _resolve(case, effect)
    if status != "pending":
        retried = case.context(ref=receipt["resolution_ref"])
        assert _claim_action_external_effect(retried) is None
        assert retried.external_effect_id == str(effect["effect_id"])
        if status == "running":
            external_effects.mark_external_effect_running(
                case.conn,
                effect_id=retried.external_effect_id,
                claim_token=retried.external_effect_claim_token,
            )
    restored = (
        case.service()
        .list_external_effects(
            case.install,
            project_id=case.project,
            scope=case.context().scope,
        )
        .dump()["effects"][0]
    )
    assert restored["decision_allowed"] is False
    assert restored["retry_supported"] is (status == "pending")
    assert restored["resolution_ref"] == receipt["resolution_ref"]
    assert restored["retry_unavailable_reason"] == (
        None if status == "pending" else "retry_in_progress"
    )
    reloaded = case.context(actor="another-operator")
    with pytest.raises(ServiceError, match=r"external_effect_(?:timeout_unknown|claimed|running)"):
        _claim_action_external_effect(reloaded)
    assert _count(case) == 1


def test_exact_retry_recovers_original_actor_and_key_from_the_host_receipt(direct_case):
    case = direct_case
    original = case.context()
    effect = case.unknown(original)
    receipt = _resolve(case, effect)
    retried = case.context(actor="retry-operator", ref=receipt["resolution_ref"])
    assert _claim_action_external_effect(retried) is None
    row = external_effect_store.get_external_effect(case.conn, effect_id=str(effect["effect_id"]))
    assert row["claim_version"] == receipt["fenced_version"] + 1
    assert row["source_ref_jsonb"]["actor_id"] == "first-operator"
    assert (
        row["idempotency_key"]
        == hashlib.sha256(original.direct_action_idempotency_key.encode()).hexdigest()
    )
    assert _count(case) == 1


@pytest.mark.parametrize("change", ["payload", "project", "account", "install", "connection"])
def test_retry_ref_cannot_authorize_a_changed_request_or_scope(direct_case, change):
    case = direct_case
    effect = case.unknown(case.context())
    receipt = _resolve(case, effect)
    changes = {
        "payload": {"value": "changed"},
        "project": {"project_id": case.tenants.a.private.id},
        "account": {
            "account_id": case.tenants.b.account_id,
            "project_id": case.tenants.b.shared.id,
        },
        "install": {"install": str(uuid4())},
        "connection": {"connection": "other-connection"},
    }[change]
    changed_ctx = case.context(ref=receipt["resolution_ref"], **changes)
    with pytest.raises(ServiceError):
        _claim_action_external_effect(changed_ctx)
    row = external_effect_store.get_external_effect(case.conn, effect_id=str(effect["effect_id"]))
    assert row["claim_version"] == receipt["fenced_version"]
    assert row["status_reason"] == "operator_confirmed_no_effect"


def test_summary_is_scoped_safe_and_restores_a_pending_decision(direct_case):
    case = direct_case
    effect = case.unknown(case.context())
    case.unknown(
        case.context(
            project_id=case.tenants.b.shared.id,
            account_id=case.tenants.b.account_id,
            connection="connection-foreign",
        )
    )
    operator = case.context().scope
    service = case.service()
    summary = service.list_external_effects(
        case.install, project_id=case.project, scope=operator
    ).dump()
    assert summary == {
        "effects": [
            {
                "effect_id": str(effect["effect_id"]),
                "observed_version": 1,
                "operation_id": "create",
                "status": "timeout_unknown",
                "decision_allowed": True,
                "retry_supported": False,
                "retry_requires_original_request": True,
                "resolution_ref": None,
                "retry_unavailable_reason": None,
            }
        ],
        "truncated": False,
    }
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    receipt = service.resolve_external_effect(
        case.install,
        str(effect["effect_id"]),
        DirectEffectDecisionRequest(
            observed_version=1, action="reject", comment="  Confirmed no effect  "
        ),
        project_id=case.project,
        scope=operator,
    ).dump()
    pending = service.list_external_effects(
        case.install, project_id=case.project, scope=operator
    ).dump()["effects"][0]
    assert pending["decision_allowed"] is False
    assert pending["retry_supported"] is True
    assert pending["resolution_ref"] == receipt["resolution_ref"]
    with case.conn.cursor() as cur:
        cur.execute(
            "SELECT payload_jsonb, actor_id FROM command_audit_log WHERE command_id = %s",
            (receipt["resolution_ref"]["command_id"],),
        )
        audit = cur.fetchone()
    assert audit["actor_id"] == operator.actor_id
    assert audit["payload_jsonb"]["comment"] == "Confirmed no effect"


def test_viewer_and_wrong_project_cannot_resolve_a_known_effect(direct_case):
    case = direct_case
    effect = case.unknown(case.context())
    service = case.service()
    viewer = case.context().scope.model_copy(update={"principal_type": "viewer"})
    assert (
        service.list_external_effects(case.install, project_id=case.project, scope=viewer).dump()[
            "effects"
        ][0]["decision_allowed"]
        is False
    )
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    decision = DirectEffectDecisionRequest(observed_version=1, action="approve", comment="Checked")
    with pytest.raises(ServiceForbiddenError):
        service.resolve_external_effect(
            case.install, str(effect["effect_id"]), decision, project_id=case.project, scope=viewer
        )
    foreign_scope = case.context().scope
    with pytest.raises(ServiceForbiddenError):
        service.list_external_effects(
            case.install, project_id=case.tenants.b.shared.id, scope=foreign_scope
        )
    unknown_install = str(uuid4())
    effect_id = str(effect["effect_id"])
    operator_scope = case.context().scope
    with pytest.raises(ServiceNotFoundError):
        service.resolve_external_effect(
            unknown_install,
            effect_id,
            decision,
            project_id=case.project,
            scope=operator_scope,
        )


@pytest.mark.parametrize("source_type", ["workflow_step", "notification_event"])
def test_host_summary_and_resolver_never_treat_workflow_or_event_rows_as_direct_actions(
    direct_case,
    source_type,
):
    case = direct_case
    effect = case.unknown(case.context())
    with case.conn.cursor() as cur:
        cur.execute(
            "UPDATE external_effects SET source_type = %s WHERE effect_id = %s",
            (source_type, effect["effect_id"]),
        )
    service = case.service()
    assert service.list_external_effects(
        case.install, project_id=case.project, scope=case.context().scope
    ).dump() == {"effects": [], "truncated": False}
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    effect_id = str(effect["effect_id"])
    decision = DirectEffectDecisionRequest(observed_version=1, action="approve", comment="Checked")
    operator_scope = case.context().scope
    with pytest.raises(ServiceNotFoundError):
        service.resolve_external_effect(
            case.install,
            effect_id,
            decision,
            project_id=case.project,
            scope=operator_scope,
        )


def test_bypass_guard_does_not_reuse_the_twenty_row_ui_limit(direct_case):
    case = direct_case
    effect = case.unknown(case.context())
    for index in range(21):
        case.unknown(case.context(value=f"unrelated-{index}"))
    summary = (
        case.service()
        .list_external_effects(case.install, project_id=case.project, scope=case.context().scope)
        .dump()
    )
    assert summary["truncated"] is True
    assert len(summary["effects"]) == 20
    assert str(effect["effect_id"]) not in {item["effect_id"] for item in summary["effects"]}
    same_intent = case.context()
    with pytest.raises(ServiceError, match="external_effect_timeout_unknown"):
        _claim_action_external_effect(same_intent)
    assert _count(case) == 22


def test_disconnected_connection_does_not_grant_recovery_visibility_or_mutation(direct_case):
    case = direct_case
    effect = case.unknown(case.context())
    with case.conn.cursor() as cur:
        cur.execute(
            "UPDATE project_connections SET enabled = false WHERE connection_id = %s AND project_id = %s",
            ("connection-original", case.project),
        )
    service = case.service()
    assert service.list_external_effects(
        case.install, project_id=case.project, scope=case.context().scope
    ).dump() == {"effects": [], "truncated": False}
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    effect_id = str(effect["effect_id"])
    decision = DirectEffectDecisionRequest(observed_version=1, action="approve", comment="Checked")
    operator_scope = case.context().scope
    with pytest.raises(ServiceNotFoundError):
        service.resolve_external_effect(
            case.install,
            effect_id,
            decision,
            project_id=case.project,
            scope=operator_scope,
        )


def test_direct_resolution_requires_a_trusted_actor_before_any_decision_write(direct_case):
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    case = direct_case
    effect = case.unknown(case.context())
    missing_actor = case.context().scope.model_copy(update={"actor_id": None})
    service = case.service()
    effect_id = str(effect["effect_id"])
    decision = DirectEffectDecisionRequest(observed_version=1, action="reject", comment="Checked")
    with pytest.raises(ServiceForbiddenError, match="actor"):
        service.resolve_external_effect(
            case.install,
            effect_id,
            decision,
            project_id=case.project,
            scope=missing_actor,
        )
    row = external_effect_store.get_external_effect(case.conn, effect_id=str(effect["effect_id"]))
    assert row["claim_version"] == 1
    with case.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM command_audit_log WHERE account_id = %s", (case.account,)
        )
        assert cur.fetchone()["n"] == 0


def test_malformed_direct_effect_id_fails_safely_before_any_decision_write(direct_case):
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    case = direct_case
    effect = case.unknown(case.context())
    service = case.service()
    decision = DirectEffectDecisionRequest(observed_version=1, action="reject", comment="Checked")
    operator_scope = case.context().scope
    with pytest.raises(ServiceError) as error:
        service.resolve_external_effect(
            case.install,
            "not-a-ledger-uuid",
            decision,
            project_id=case.project,
            scope=operator_scope,
        )
    assert error.value.status_code in {400, 404, 422}
    row = external_effect_store.get_external_effect(case.conn, effect_id=str(effect["effect_id"]))
    assert row["claim_version"] == 1
    with case.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM command_audit_log WHERE account_id = %s", (case.account,)
        )
        assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "page_id",
        "component_id",
        "context",
        "target",
        "input",
        "test_mode",
        "artifact_inputs",
        "artifact_outputs",
        "artifact_output_specs",
        "dataset_inputs",
        "dataset_outputs",
        "dataset_output_specs",
    ],
)
def test_complete_current_request_must_match_before_a_retry_ref_is_consumed(direct_case, field):
    case = direct_case
    effect = case.unknown(case.context())
    receipt = _resolve(case, effect)
    values = {
        "page_id": "another-page",
        "component_id": "another-component",
        "context": {"project_id": case.project, "record_scope": "another-scope"},
        "target": {"connection_id": "connection-original", "record_type": "another-type"},
        "input": {"value": "another-record"},
        "test_mode": {"project_id": case.project, "test_mode": True},
        "artifact_inputs": [{"artifact_id": "another-artifact"}],
        "artifact_outputs": [{"name": "another-output"}],
        "artifact_output_specs": [{"name": "receipt", "content_type": "text/plain"}],
        "dataset_inputs": [{"dataset_id": "another-dataset"}],
        "dataset_outputs": [{"name": "another-dataset-output"}],
        "dataset_output_specs": [{"name": "rows", "schema_ref": "another-schema"}],
    }
    body_field = "context" if field == "test_mode" else field

    def changed(*, ref=None):
        ctx = case.context(ref=ref)
        ctx.body = ExtensionActionRequest.model_validate(
            {**ctx.body.model_dump(), body_field: values[field]}
        )
        ctx.direct_action_intent[body_field] = values[field]
        ctx.input_payload = dict(ctx.body.input)
        ctx.target = dict(ctx.body.target)
        return ctx

    # A distinct complete request remains independent from the pending decision.
    independent = changed()
    _claim_action_external_effect(independent)
    assert independent.external_effect_id != str(effect["effect_id"])
    assert _count(case) == 2
    changed_retry = changed(ref=receipt["resolution_ref"])
    with pytest.raises(ServiceError):
        _claim_action_external_effect(changed_retry)
    original = external_effect_store.get_external_effect(
        case.conn, effect_id=str(effect["effect_id"])
    )
    assert original["claim_version"] == receipt["fenced_version"]
    assert original["status_reason"] == "operator_confirmed_no_effect"


def test_null_connection_ambiguity_cannot_be_bypassed_or_invent_a_project_binding(direct_case):
    from core.application.extensions.direct_effect_recovery_models import (
        DirectEffectDecisionRequest,
    )

    case = direct_case
    effect = case.unknown(case.context(connection=None))
    assert effect["connection_id"] is None
    other_operator = case.context(connection=None, actor="another-operator")
    with pytest.raises(ServiceError, match="external_effect_timeout_unknown"):
        _claim_action_external_effect(other_operator)
    assert _count(case) == 1
    assert case.service().list_external_effects(
        case.install,
        project_id=case.project,
        scope=case.context().scope,
    ).dump() == {"effects": [], "truncated": False}
    service = case.service()
    effect_id = str(effect["effect_id"])
    decision = DirectEffectDecisionRequest(observed_version=1, action="reject", comment="Checked")
    operator_scope = case.context().scope
    with pytest.raises(ServiceNotFoundError):
        service.resolve_external_effect(
            case.install,
            effect_id,
            decision,
            project_id=case.project,
            scope=operator_scope,
        )
    assert (
        external_effect_store.get_external_effect(case.conn, effect_id=str(effect["effect_id"]))[
            "claim_version"
        ]
        == 1
    )


@pytest.mark.parametrize("provider_forges_effect", [False, True])
def test_initial_provider_timeout_public_error_associates_only_the_real_host_effect(
    direct_case, monkeypatch, provider_forges_effect
):
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.infrastructure.runtime import serve_request_runtime

    case = direct_case
    context = case.context()
    case.seed_binding(context)
    forged_id = str(uuid4())
    service = ExtensionOperationService(
        get_logger=lambda: context.bl,
        get_operation_store=lambda: context.bl,
        get_project_by_id=context.bl.get_project,
        resolve_extension_runtime_record=lambda _extension_id: (context.extension, context.bl),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )
    # These seams stop at validated host preparation and the actual provider.
    # Claim, running fence, confirmation SQL and public projection stay real.
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args: context)
    monkeypatch.setattr(
        service,
        "_invoke_action_with_grants",
        lambda _context: {
            "status": "failed",
            "detail": "provider_timeout",
            **(
                {
                    "external_effect": {
                        "effect_id": forged_id,
                        "claim_token": "provider-forged-lease",
                    }
                }
                if provider_forges_effect
                else {}
            ),
            "response": {
                "ok": False,
                "error_code": "provider_timeout",
                "external_effect_status": "timeout_unknown",
                **(
                    {
                        "external_effect": {
                            "effect_id": forged_id,
                            "claim_version": 900,
                            "status": "succeeded",
                        }
                    }
                    if provider_forges_effect
                    else {}
                ),
            },
        },
    )
    with pytest.raises(ServiceError) as failure:
        service.execute_action(
            case.extension_id,
            "create",
            context.body,
            scope=context.scope,
            idempotency_key=context.direct_action_idempotency_key,
        )
    assert failure.value.status_code == 502
    row = external_effect_store.get_external_effect(case.conn, effect_id=context.external_effect_id)
    assert row["status"] == "timeout_unknown"
    assert row["claim_version"] == 1
    assert str(row["effect_id"]) != forged_id
    expected = {
        "effect_id": str(row["effect_id"]),
        "claim_version": 1,
        "status": "timeout_unknown",
        "operation_id": "create",
    }
    assert failure.value.payload["action"]["response"].get("external_effect") == expected

    # Exercise the exact production error envelope as well as the application
    # exception: HTTP clients read the top-level action or details.context.action.
    response = serve_request_runtime.service_error_response(
        failure.value,
        error_payload=serve_request_runtime.build_error_payload_factory(
            err_request_failed="Request failed",
            default_correlation_id_fn=lambda: "direct-effect-fixture",
        ),
        request_correlation_id=lambda _request, **_kwargs: "direct-effect-fixture",
        error_code_for_status=serve_request_runtime.error_code_for_status,
        err_request_failed="Request failed",
    )
    public = json.loads(response.body)
    assert public["action"]["response"]["external_effect"] == expected
    assert public["details"]["context"]["action"]["response"]["external_effect"] == expected
    encoded = json.dumps(public)
    for forbidden in (
        "claim_token",
        "idempotency_key",
        "source_ref_jsonb",
        "encrypted_payload",
        "__confirmed_replay_invocation",
        "request_fingerprint",
        "request_payload",
        forged_id,
        "provider-forged-lease",
        str(row["claim_token"]),
        row["idempotency_key"],
        context.direct_action_idempotency_key,
    ):
        assert forbidden not in encoded
    assert _count(case) == 1


@pytest.mark.parametrize(
    "installation_state, expected_status", [("active", 200), ("disabled", 404), ("foreign", 404)]
)
def test_scoped_summary_uses_real_descriptor_install_projection(
    direct_case, disposable_db, installation_state, expected_status
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.responses import JSONResponse

    from core.application.extensions.direct_effect_recovery import DirectEffectRecoveryService
    from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router
    from core.infrastructure.db.platform_stores import PlatformIdentityStore

    case = direct_case
    effect = case.unknown(case.context())
    release = str(uuid4())
    with case.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version, "
            "descriptor_jsonb, account_id) VALUES (%s, %s, '1.0.0', %s::jsonb, %s)",
            (
                release,
                case.extension_id,
                json.dumps({"extension_id": case.extension_id}),
                case.account,
            ),
        )
        cur.execute(
            "UPDATE extension_installs SET release_id = %s, state = %s WHERE install_id = %s",
            (release, "disabled" if installation_state == "disabled" else "active", case.install),
        )
    logger = PlatformIdentityStore(disposable_db.dsn)
    try:
        projection = logger.get_extension_install_for_operator(
            case.install, account_id=case.account
        )
        assert projection["enabled"] is (installation_state != "disabled")
        assert "state" not in projection
        assert projection["account_id"] == case.account
        service = DirectEffectRecoveryService(
            get_operation_store=lambda: case.store,
            get_install_store=lambda: logger,
            get_project_by_id=case.store.get_project,
        )
        scope = ScopeContext(
            account_id=case.account,
            project_id=case.project,
            principal_type="operator",
            actor_id="real-install-reader",
        )
        app = FastAPI()
        app.include_router(
            create_reactions_extensions_router(
                event_query_service=lambda: None,
                event_command_service=lambda: None,
                job_query_service=lambda: None,
                extension_query_service=lambda: None,
                extension_binding_service=lambda: service,
                tool_execution_service=lambda: None,
                command_dispatch_service=lambda: None,
                request_scope=lambda _request, **_kwargs: scope,
                service_error_response=lambda error, *_args: JSONResponse(
                    {"error": str(error)},
                    status_code=error.status_code,
                ),
                api_error_response=lambda _error, **_kwargs: JSONResponse(
                    {"error": "Unexpected error"},
                    status_code=500,
                ),
            )
        )
        install = case.install
        if installation_state == "foreign":
            install = case.context(account_id=case.tenants.b.account_id).extension[
                "extension_install_id"
            ]
            assert (
                logger.get_extension_install_for_operator(install, account_id=case.account) is None
            )
        with TestClient(app) as client:
            response = client.get(
                f"/api/projects/{case.project}/extensions/{install}/external-effects"
            )
        assert response.status_code == expected_status, response.text
        if expected_status == 200:
            summary = response.json()
            assert len(summary["effects"]) == 1
            assert summary["effects"][0]["effect_id"] == str(effect["effect_id"])
            assert summary["effects"][0]["decision_allowed"] is True
            assert summary["effects"][0]["observed_version"] == 1
            assert "claim_token" not in response.text
            assert "idempotency_key" not in response.text
        else:
            assert response.json() == {"error": "Extension install not found"}
    finally:
        logger.conn.close()
        with case.conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET release_id = NULL WHERE install_id = %s",
                (case.install,),
            )
            cur.execute("DELETE FROM extension_releases WHERE release_id = %s", (release,))
