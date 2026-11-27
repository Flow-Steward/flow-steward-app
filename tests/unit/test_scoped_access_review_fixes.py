"""Regressions of the independent review of ADR 0076 section 8b and the secret backends.

Each test fails on the code before its fix:

1. a run of a workflow a limited person does not reach in a workflow-only project is
   refused by every job detail read and by the jobs WebSocket (which also checks the
   token's workflow allowlist);
2. the run's workflow decides a permission: approvals, job reports, bulk delete and
   a project-path write that names a workflow in its body;
3. workspace grants are enforced on the workspace routes' service and on trash;
4. a token limited to projects reaches only their workspaces;
5. lists under a workflow-only project keep only granted workflows' rows;
6. global search hides workflows that name no project from a limited person;
7. a workflow grant counts only while the workflow is linked to its project;
8. a page entitlement list may not be empty;
9-11. secret backends: no silent overwrite, probe limits with audit, deployment
   policy per account.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth import scoped_assignments
from core.application.auth.permissions import OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS
from core.application.auth.project_scope_limit import (
    PROJECT_ROLE_REFUSAL_MESSAGE,
    principal_places_payload,
    scope_limit_route_refusal,
    workflow_id_from_body,
)
from core.application.management.approval_assignees import may_decide_in_project
from core.application.management.command_services_job import _run_permission_refusal
from core.application.management.command_services_trash_workspace_mixin import (
    _require_trash_workspace,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
)
from core.application.management.guards import rows_in_workflow_scope_limit
from core.application.management.scope import ScopeContext, scope_from_principal
from core.application.reports.reporting_service import _require_report_workflow_in_scope
from core.application.reports.ui_page_registry import _page_entitlement
from core.application.search.global_search_service import _filter_item_for_scope
from core.application.secrets import backend_repository, backend_service, repository
from core.application.secrets.backend_service import SecretBackendAdminService
from core.application.settings import (
    model_provider_verification_rate_limit as rate_limit,
)
from core.application.workspaces.service import WorkspaceService
from core.entrypoints.ws.jobs import _project_access_denied
from core.infrastructure.security import (
    secret_backend_policy,
    secret_backends,
    stored_secrets,
)
from core.infrastructure.security.secret_backends import (
    SecretBackendHealth,
    SecretBackendPort,
    SecretBackendTargetExists,
)
from tests.unit.test_multilevel_scoped_roles import (
    ACCOUNT,
    ACCOUNTING,
    CAMPAIGNS,
    INVOICE,
    LEDGER,
    MARKETING,
    MIXED,
    PAYROLL,
    PAYSLIP,
    REPORT,
    SALES,
    _job,
    _job_service,
    _places,
    _project,
    _scope,
)

# --- 1. job detail reads and the WebSocket --------------------------------------------


def _job_store(project: str, workflow: str) -> Any:
    bl = MagicMock()
    bl.get_job.return_value = {**_job(project, workflow), "id": "job-1"}
    bl.get_project.return_value = _project(project)
    return bl


@pytest.mark.parametrize(
    ("project", "workflow", "visible"),
    [
        (CAMPAIGNS, INVOICE, True),
        (CAMPAIGNS, REPORT, False),
        (PAYROLL, "wf-anything", True),
    ],
)
def test_job_detail_reads_follow_the_workflow_grants(project, workflow, visible) -> None:
    service = _job_service(_job_store(project, workflow))

    if visible:
        service._job_context("job-1", scope=_scope())
    else:
        with pytest.raises(ServiceForbiddenError):
            service._job_context("job-1", scope=_scope())


def _principal(**extra: Any) -> dict[str, Any]:
    return {
        "id": "6a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "account_id": ACCOUNT,
        "roles": ["operator"],
        "auth_source": "local",
        **principal_places_payload(_places()),
        **extra,
    }


def test_the_jobs_websocket_refuses_a_run_of_an_ungranted_workflow() -> None:
    assert _project_access_denied(
        _job_store(CAMPAIGNS, REPORT), _principal(), _job(CAMPAIGNS, REPORT)
    )
    assert not _project_access_denied(
        _job_store(CAMPAIGNS, INVOICE), _principal(), _job(CAMPAIGNS, INVOICE)
    )


def test_the_jobs_websocket_checks_the_token_workflow_allowlist() -> None:
    token = {
        "id": "u",
        "account_id": ACCOUNT,
        "roles": ["operator"],
        "auth_source": "pat",
        "allowed_workflow_ids": [INVOICE],
    }

    assert _project_access_denied(_job_store(LEDGER, REPORT), token, _job(LEDGER, REPORT))
    assert not _project_access_denied(_job_store(LEDGER, INVOICE), token, _job(LEDGER, INVOICE))


# --- 2. the run's workflow decides ----------------------------------------------------


def test_deciding_an_approval_is_checked_in_the_run_s_workflow() -> None:
    scope = _scope()

    # Operator in Payroll, viewer on its Payslip workflow.
    assert may_decide_in_project(scope, PAYROLL, "wf-other") is True
    assert may_decide_in_project(scope, PAYROLL, PAYSLIP) is False
    assert may_decide_in_project(scope, CAMPAIGNS, REPORT) is False


def test_a_job_report_is_checked_in_the_run_s_workflow() -> None:
    from core.application.reports.reporting_service import ReportingService

    service = ReportingService.__new__(ReportingService)
    bl = MagicMock()
    bl.get_job.return_value = _job(PAYROLL, PAYSLIP)
    service._logger_or_raise = lambda: bl  # type: ignore[method-assign]

    with pytest.raises(ServiceForbiddenError, match="role in this project"):
        service._require_report_permission(_scope(), "job_summary", {"job_id": "job-1"})
    bl.get_job.return_value = _job(PAYROLL, "wf-other")
    service._require_report_permission(_scope(), "job_summary", {"job_id": "job-1"})


def _manager_scope() -> ScopeContext:
    """``jobs.manage`` in Payroll, not in its Payslip workflow; Campaigns is Invoice only."""
    manage = OPERATOR_PERMISSIONS | {"jobs.manage"}
    return ScopeContext(
        account_id=ACCOUNT,
        user_id="u",
        allowed_project_ids=frozenset({PAYROLL, CAMPAIGNS}),
        project_permissions={PAYROLL: manage, CAMPAIGNS: VIEWER_PERMISSIONS},
        workflow_permissions={
            PAYROLL: {PAYSLIP: VIEWER_PERMISSIONS},
            CAMPAIGNS: {INVOICE: manage},
        },
        project_workflow_ids={CAMPAIGNS: frozenset({INVOICE})},
    )


@pytest.mark.parametrize(
    ("project", "workflow", "expected"),
    [
        (PAYROLL, "wf-other", None),
        (PAYROLL, PAYSLIP, "forbidden"),
        (CAMPAIGNS, INVOICE, None),
        (CAMPAIGNS, REPORT, "not_found"),
    ],
)
def test_bulk_delete_checks_each_run_in_its_workflow(project, workflow, expected) -> None:
    refusal = _run_permission_refusal(
        _manager_scope(), _project(project), _job(project, workflow), "jobs.manage"
    )

    assert refusal == expected


def test_a_project_write_naming_a_workflow_in_its_body_is_checked_there() -> None:
    places = _places()
    args = (places.project_permissions, places.workflow_permissions, places.project_workflow_ids)
    path = f"/api/projects/{PAYROLL}/scheduled-jobs"
    body = json.dumps({"workflow_id": PAYSLIP, "cron": "* * * * *"}).encode()

    named = workflow_id_from_body("POST", path, body)

    assert named == PAYSLIP
    assert scope_limit_route_refusal("POST", path, "workflows.run", *args) is None
    assert (
        scope_limit_route_refusal("POST", path, "workflows.run", *args, body_workflow_id=named)
        == PROJECT_ROLE_REFUSAL_MESSAGE
    )
    assert workflow_id_from_body("GET", path, body) is None
    assert workflow_id_from_body("POST", path, b"not json") is None
    assert workflow_id_from_body("POST", f"/api/projects/{PAYROLL}/workflows/x/y", body) is None


# --- 3. workspace grants -----------------------------------------------------------------


class _WorkspaceStore:
    def __init__(self) -> None:
        self.rows = {
            ws: {"id": ws, "workspace_key": ws[:4], "display_name": ws}
            for ws in (ACCOUNTING, MARKETING, SALES)
        }

    def list_workspaces(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.rows.values())

    def get_workspace(self, workspace_id: str, **_kwargs: Any) -> dict[str, Any] | None:
        return self.rows.get(workspace_id)

    def list_workspace_projects(self, workspace_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": LEDGER}, {"id": PAYROLL}, {"id": "other-project"}]

    def create_workspace(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return dict(payload)


def test_the_workspace_service_follows_the_workspace_grants() -> None:
    service = WorkspaceService(_WorkspaceStore())
    scope = _scope()

    assert [row["id"] for row in service.list_workspaces(scope=scope)] == [ACCOUNTING, MARKETING]
    assert service.get_workspace(ACCOUNTING, scope=scope)["id"] == ACCOUNTING
    with pytest.raises(KeyError):
        service.get_workspace(SALES, scope=scope)
    with pytest.raises(KeyError):
        service.restore_workspace(ACCOUNTING, scope=scope)
    assert [row["id"] for row in service.list_projects(ACCOUNTING, scope=scope)] == [
        LEDGER,
        PAYROLL,
    ]
    with pytest.raises(PermissionError):
        service.create_workspace({"display_name": "New"}, scope=scope)


def test_an_unlimited_admin_keeps_every_workspace() -> None:
    service = WorkspaceService(_WorkspaceStore())
    admin = scope_from_principal({"id": "a", "account_id": ACCOUNT, "roles": ["admin"]})

    assert len(service.list_workspaces(scope=admin)) == 3
    service.get_workspace(SALES, scope=admin)
    service.create_workspace({"display_name": "New"}, scope=admin)


def test_trashing_a_workspace_needs_its_grant() -> None:
    with pytest.raises(ServiceNotFoundError):
        _require_trash_workspace(_scope(), SALES)
    with pytest.raises(ServiceNotFoundError):
        _require_trash_workspace(_scope(), ACCOUNTING)


# --- 4. a project-scoped token -----------------------------------------------------------


def test_a_project_scoped_token_reaches_only_its_projects_workspaces() -> None:
    places = _places(token_project_ids=[CAMPAIGNS])

    assert places.allowed_project_ids == [CAMPAIGNS]
    assert places.allowed_workspace_ids == [MARKETING]
    assert set(places.workspace_permissions) == {MARKETING}


def test_a_person_without_a_token_keeps_their_workspace_grants() -> None:
    assert _places().allowed_workspace_ids == sorted([ACCOUNTING, MARKETING])
    assert MIXED.workspace_roles


# --- 5. lists under a workflow-only project ---------------------------------------------


def test_rows_of_a_workflow_only_project_keep_only_granted_workflows() -> None:
    rows = [{"workflow_id": INVOICE}, {"workflow_id": REPORT}, {"workflow_id": None}]

    assert rows_in_workflow_scope_limit(_scope(), CAMPAIGNS, rows) == [{"workflow_id": INVOICE}]
    assert rows_in_workflow_scope_limit(_scope(), PAYROLL, rows) == rows


def test_notification_deliveries_of_a_workflow_only_project_are_cut() -> None:
    from core.application.management.query_services import ProjectQueryService

    bl = MagicMock()
    bl.get_project.return_value = _project(CAMPAIGNS)
    bl.list_notification_delivery_logs.return_value = [
        {"delivery_id": "1", "workflow_id": INVOICE},
        {"delivery_id": "2", "workflow_id": REPORT},
    ]
    service = ProjectQueryService(
        get_logger=lambda: bl,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda _project_id: 0,
        get_ready_queue_depth=lambda: 0,
    )

    view = service.list_notification_deliveries(CAMPAIGNS, scope=_scope())

    assert [row["delivery_id"] for row in view.notification_deliveries] == ["1"]


def test_a_project_report_of_a_workflow_only_project_needs_a_granted_workflow() -> None:
    _require_report_workflow_in_scope(_scope(), CAMPAIGNS, INVOICE, not_found=True)
    _require_report_workflow_in_scope(_scope(), PAYROLL, None, not_found=True)
    with pytest.raises(ServiceNotFoundError):
        _require_report_workflow_in_scope(_scope(), CAMPAIGNS, None, not_found=True)
    with pytest.raises(ServiceForbiddenError):
        _require_report_workflow_in_scope(_scope(), CAMPAIGNS, REPORT, not_found=False)


# --- 6. global search ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("workflow", "visible"),
    [(INVOICE, True), (PAYSLIP, True), (REPORT, False), (None, False)],
)
def test_a_workflow_without_a_project_is_shown_only_when_granted(workflow, visible) -> None:
    scope = _scope()
    item = {"group": "workflows", "project_id": None, "workflow_id": workflow}

    assert (
        _filter_item_for_scope(item, scope=scope, project_id_set=None, selected_project_id=None)
        is visible
    )


def test_an_unlimited_person_still_finds_workflows_without_a_project() -> None:
    scope = scope_from_principal({"id": "u", "account_id": ACCOUNT, "roles": ["operator"]})
    item = {"group": "workflows", "project_id": None, "workflow_id": REPORT}

    assert _filter_item_for_scope(item, scope=scope, project_id_set=None, selected_project_id=None)


# --- 7. stale workflow grants ----------------------------------------------------------


def test_a_workflow_grant_needs_its_project_link() -> None:
    sql = " ".join(scoped_assignments._GRANTS_SQL.split())

    assert "FROM public.project_workflows pw" in sql
    assert "pw.project_id = ur.project_id" in sql
    assert "pw.workflow_id = ur.workflow_id" in sql
    assert "pw.account_id = ur.workflow_account_id" in sql
    assert "pw.account_id = p.account_id" in sql
    assert "pw.deleted_at IS NULL" in sql


# --- 8. page entitlements -------------------------------------------------------------------


@pytest.mark.parametrize("value", [[], ()])
def test_an_empty_page_entitlement_list_is_refused(value) -> None:
    with pytest.raises(ValueError, match="required_entitlement"):
        _page_entitlement(value, "billing")
    assert _page_entitlement(["a", "a", "b"], "billing") == ["a", "b"]


# --- 9-11. secret backends -----------------------------------------------------------------

SECOND_ACCOUNT = "11111111-1111-1111-1111-111111111111"
FEATURE = "acme.secret_backend.vault"


class _Vault:
    kind = "acme.vault"
    feature = FEATURE
    capabilities = frozenset({"write"})

    def __init__(self) -> None:
        self.values: dict[str, str] = {"taken": "theirs"}
        self.policies: list[Any] = []

    def validate_config(self, config: Mapping[str, Any], policy: Any = None) -> Mapping[str, Any]:
        self.policies.append(policy)
        return dict(config)

    def validate_locator(self, locator: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"name": str(locator["name"])}

    def read(self, context: Any, locator: Mapping[str, Any]) -> str:
        name = str(locator["name"])
        if name not in self.values:
            raise stored_secrets.StoredSecretMissing(name)
        return self.values[name]

    def health(self, context: Any) -> SecretBackendHealth:
        return SecretBackendHealth(healthy=True)

    def write(self, context: Any, locator: Mapping[str, Any], value: str, **kwargs: Any) -> Any:
        self.values[str(locator["name"])] = value
        return dict(locator)


class _CreateOnlyVault(_Vault):
    kind = "acme.createonly"
    capabilities = frozenset({"write", "create_only"})

    def write(self, context: Any, locator: Mapping[str, Any], value: str, **kwargs: Any) -> Any:
        if kwargs.get("create_only") and str(locator["name"]) in self.values:
            raise SecretBackendTargetExists()
        return super().write(context, locator, value)


class _Entitled:
    def is_entitled(self, account_id: str, feature: str) -> bool:
        return True


@pytest.fixture
def backends() -> Iterator[tuple[_Vault, _CreateOnlyVault]]:
    checkpoint = secret_backends.loading_checkpoint()
    secret_backends.restore_loading_checkpoint(None)
    vault, create_only = _Vault(), _CreateOnlyVault()
    port = SecretBackendPort()
    with port.platform_module_scope("acme.vault-module", ["secret_backend"]):
        port.register_secret_backend(vault)
        port.register_secret_backend(create_only)
    yield vault, create_only
    secret_backends.restore_loading_checkpoint(checkpoint)


@pytest.fixture
def rows(monkeypatch) -> dict[str, Any]:
    data: dict[str, Any] = {
        "configs": {},
        "locators": {},
        "secrets": {},
        "local": {},
        "audit": [],
        "probes": {},
    }

    def get_config(cur, *, account_id, config_id, for_update=False):
        row = data["configs"].get(config_id)
        return dict(row) if row and row["account_id"] == account_id else None

    def get_locator(cur, *, account_id, ref_key):
        return data["locators"].get(ref_key)

    def insert_locator(cur, *, account_id, ref_key, config_id, backend_kind, locator, actor):
        data["locators"][ref_key] = {
            "ref_key": ref_key,
            "config_id": config_id,
            "backend_kind": backend_kind,
            "locator": dict(locator),
        }

    def locator_holder(cur, *, account_id, config_id, locator, exclude_ref_key=None):
        for ref_key, row in data["locators"].items():
            if (
                ref_key != exclude_ref_key
                and row["config_id"] == config_id
                and all(row["locator"].get(k) == v for k, v in locator.items())
            ):
                return ref_key
        return None

    def get_secret(cur, *, account_id, secret_id, for_update=False):
        return data["secrets"].get(secret_id)

    def record_audit(cur, *, account_id, command_type, actor_kind, actor_id, target, payload):
        data["audit"].append((command_type, dict(target), dict(payload or {})))

    def consume(conn, *, keys, max_attempts, window_seconds, now=None, **_kwargs):
        for key in keys:
            data["probes"][key] = data["probes"].get(key, 0) + 1
            if data["probes"][key] > max_attempts:
                return False, 7
        return True, 0

    class _Local:
        def __init__(self, conn, **_kwargs):
            pass

        def read(self, context, ref_key):
            return data["local"][ref_key]

    monkeypatch.setattr(backend_repository, "get_config", get_config)
    monkeypatch.setattr(backend_repository, "get_locator", get_locator)
    monkeypatch.setattr(backend_repository, "insert_locator", insert_locator)
    monkeypatch.setattr(backend_repository, "locator_holder", locator_holder)
    monkeypatch.setattr(
        backend_repository, "delete_local_value", lambda cur, *, account_id, ref_key: None
    )
    monkeypatch.setattr(repository, "get_secret", get_secret)
    monkeypatch.setattr(repository, "touch_value", lambda cur, **_k: None)
    monkeypatch.setattr(repository, "record_audit", record_audit)
    monkeypatch.setattr(
        repository,
        "insert_secret",
        lambda cur, *, account_id, name, description, actor: {
            "id": "s-new",
            "name": name,
            "description": description,
            "storage_ref_key": "catalog_secret:s-new",
        },
    )
    monkeypatch.setattr(backend_service, "LocalStoredSecretReader", _Local)
    monkeypatch.setattr(rate_limit, "consume_attempt_window", consume)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        yield object(), object()

    monkeypatch.setattr(SecretBackendAdminService, "_transaction", transaction)
    monkeypatch.setenv(
        "FS_SECRET_BACKEND_POLICY",
        json.dumps(
            {
                SECOND_ACCOUNT: {
                    "acme.vault": {"allowed_values": {"vault": ["team-a"]}},
                    "acme.createonly": {"allowed_values": {}},
                }
            }
        ),
    )
    return data


def _admin(account_id: str = SECOND_ACCOUNT) -> ScopeContext:
    return ScopeContext(
        account_id=account_id, principal_type="admin", auth_source="local", actor_id="ana"
    )


def _service() -> SecretBackendAdminService:
    return SecretBackendAdminService(lambda: object(), entitlement_service=_Entitled)


def _add_config(rows: dict[str, Any], kind: str, config: dict[str, Any] | None = None) -> str:
    config_id = f"c-{len(rows['configs']) + 1}"
    rows["configs"][config_id] = {
        "id": config_id,
        "account_id": SECOND_ACCOUNT,
        "backend_kind": kind,
        "name": config_id,
        "non_secret_config": dict(config or {"vault": "team-a"}),
        "enabled": True,
    }
    return config_id


def _add_local_secret(rows: dict[str, Any], secret_id: str) -> None:
    rows["secrets"][secret_id] = {
        "id": secret_id,
        "account_id": SECOND_ACCOUNT,
        "storage_ref_key": f"catalog_secret:{secret_id}",
    }
    rows["local"][f"catalog_secret:{secret_id}"] = "mine"


def test_a_move_never_takes_a_name_another_catalog_secret_is_kept_at(backends, rows) -> None:
    config_id = _add_config(rows, "acme.vault")
    rows["locators"]["catalog_secret:other"] = {
        "config_id": config_id,
        "backend_kind": "acme.vault",
        "locator": {"name": "shared", "version": "3"},
    }
    _add_local_secret(rows, "s1")

    with pytest.raises(ServiceConflictError, match="Another catalog secret"):
        _service().move_to_backend(_admin(), "s1", config_id=config_id, locator={"name": "shared"})


def test_a_first_move_never_overwrites_an_existing_remote_secret(backends, rows) -> None:
    vault, create_only = backends
    _add_local_secret(rows, "s1")
    _add_local_secret(rows, "s2")
    plain = _add_config(rows, "acme.vault")
    atomic = _add_config(rows, "acme.createonly")
    create_only.values["taken"] = "theirs"

    for config_id in (plain, atomic):
        with pytest.raises(ServiceConflictError, match="already exists"):
            _service().move_to_backend(
                _admin(), "s1", config_id=config_id, locator={"name": "taken"}
            )
    assert vault.values["taken"] == "theirs" and create_only.values["taken"] == "theirs"

    _service().move_to_backend(
        _admin(), "s2", config_id=plain, locator={"name": "taken"}, replace=True
    )
    assert vault.values["taken"] == "mine"
    assert rows["audit"][-1][2]["replace"] is True


def test_adopt_and_health_are_rate_limited_per_account_and_audited(
    backends, rows, monkeypatch
) -> None:
    monkeypatch.setattr(backend_service, "PROBE_MAX_ATTEMPTS", 2)
    config_id = _add_config(rows, "acme.vault")
    service = _service()

    with pytest.raises(ServiceError):
        service.adopt_secret(_admin(), name="A", config_id=config_id, locator={"name": "none"})
    service.adopt_secret(_admin(), name="B", config_id=config_id, locator={"name": "taken"})
    with pytest.raises(ServiceError) as limited:
        service.adopt_secret(_admin(), name="C", config_id=config_id, locator={"name": "taken"})

    assert limited.value.status_code == 429
    outcomes = [p.get("outcome") for c, _t, p in rows["audit"] if c == "secret.storage_adopt"]
    assert outcomes == ["invalid_or_missing", "adopted", "rate_limited"]

    service.config_health(_admin(), config_id)
    service.config_health(_admin(), config_id)
    with pytest.raises(ServiceError) as health_limited:
        service.config_health(_admin(), config_id)
    assert health_limited.value.status_code == 429
    health = [p.get("outcome") for c, _t, p in rows["audit"] if c == "secret.storage_health"]
    assert health == ["healthy", "healthy", "rate_limited"]


def test_adopting_a_name_another_catalog_secret_is_kept_at_is_refused(backends, rows) -> None:
    config_id = _add_config(rows, "acme.vault")
    rows["locators"]["catalog_secret:other"] = {
        "config_id": config_id,
        "backend_kind": "acme.vault",
        "locator": {"name": "taken"},
    }

    with pytest.raises(ServiceConflictError):
        _service().adopt_secret(
            _admin(), name="Twin", config_id=config_id, locator={"name": "taken"}
        )


def test_the_deployment_policy_bounds_an_account_s_configurations(backends, rows) -> None:
    vault, _create_only = backends
    service = _service()

    with pytest.raises(ServiceForbiddenError, match="not allowed for this account"):
        service._validated_config(vault, {"vault": "team-b"}, _admin())
    assert service._validated_config(vault, {"vault": "team-a"}, _admin()) == {"vault": "team-a"}
    # The backend receives the policy entry when its validate_config takes it.
    assert vault.policies[-1] == {"allowed_values": {"vault": ["team-a"]}}

    config_id = _add_config(rows, "acme.vault", {"vault": "team-b"})
    _add_local_secret(rows, "s1")
    with pytest.raises(ServiceForbiddenError):
        service.move_to_backend(_admin(), "s1", config_id=config_id, locator={"name": "x"})
    assert service.config_health(_admin(), config_id)["code"] == "not_allowed"


def test_an_account_the_policy_does_not_list_uses_no_external_backend(
    backends, rows, monkeypatch
) -> None:
    vault, _create_only = backends
    other = "22222222-2222-2222-2222-222222222222"

    with pytest.raises(ServiceForbiddenError, match="has not allowed"):
        _service()._validated_config(vault, {"vault": "team-a"}, _admin(other))

    monkeypatch.delenv("FS_SECRET_BACKEND_POLICY")
    with pytest.raises(ServiceForbiddenError, match="has not allowed"):
        _service()._validated_config(vault, {"vault": "team-a"}, _admin(SECOND_ACCOUNT))
    # A single-account installation (the installation account) is unaffected.
    installation = secret_backend_policy.INSTALLATION_ACCOUNT_ID
    assert _service()._validated_config(vault, {"vault": "x"}, _admin(installation)) == {
        "vault": "x"
    }


def test_the_reader_refuses_a_configuration_outside_the_policy(backends, rows) -> None:
    located = {
        "backend_kind": "acme.vault",
        "config_id": "c-1",
        "config": {"vault": "team-b"},
        "locator": {"name": "taken"},
        "enabled": True,
    }

    with pytest.raises(stored_secrets.StoredSecretStorageRefused):
        secret_backends.read_remote_value(SECOND_ACCOUNT, "ref", located, use_cache=False)
    allowed = {**located, "config": {"vault": "team-a"}}
    assert secret_backends.read_remote_value(SECOND_ACCOUNT, "ref", allowed, use_cache=False)


def test_an_unreadable_policy_refuses_every_external_configuration(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_BACKEND_POLICY", "{not json")

    with pytest.raises(secret_backend_policy.SecretBackendPolicyRefused):
        secret_backend_policy.kind_policy(secret_backend_policy.INSTALLATION_ACCOUNT_ID, "k")


def test_create_only_needs_write() -> None:
    class _Broken(_Vault):
        kind = "acme.broken"
        capabilities = frozenset({"create_only"})

    port = SecretBackendPort()
    checkpoint = secret_backends.loading_checkpoint()
    try:
        with (
            pytest.raises(ValueError, match="create_only"),
            port.platform_module_scope("acme.vault-module", ["secret_backend"]),
        ):
            port.register_secret_backend(_Broken())
    finally:
        secret_backends.restore_loading_checkpoint(checkpoint)
