"""Shared world for the scenarios of people who work in several companies (ADR 0084, 0085).

Three companies (accounts A, B and C) share one installation. Each has a workspace,
two projects and a workflow with the same key, so a check that confuses one company's
row with another's fails. People are added per test with a role per company.

Requests go through a small FastAPI app that runs Core's own authentication
middleware (``local_auth_middleware``: session, personal token, role of the acting
account, scope limits, the route permission table) and Core's own auth routers
(sign-in, switch account, users, invitations). A few probe routes stand in for the
project, job, run and approval routes: each applies the guard the real service
applies (``require_project_access``, ``require_project_permission``,
``job_row_in_scope``, the approvals queue service) to the real rows. Everything runs
on the disposable audit database inside one transaction that is rolled back; the
full ``create_app()`` opens pooled connections of its own and cannot join it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from core.application.auth import request_principal_runtime, resolve
from core.application.auth.auth_http_runtime import (
    local_auth_middleware,
    read_stored_scope_limit,
)
from core.application.auth.inbound_oauth_policy import INBOUND_OAUTH_SETTING_KEY, secret_digest
from core.application.auth.installation_admin import is_installation_admin
from core.application.auth.password import session_token_hash
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.scoped_role_ports import ScopedRolesPort
from core.application.auth.user_admin.account_invitations import invitation_token_hash, invite
from core.application.jobs.query_scope import job_row_in_scope
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access, require_project_permission
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext, scope_from_principal
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.workflow_account import workflow_account_scope
from core.entrypoints.api.auth_sessions import create_auth_sessions_router
from core.entrypoints.api.auth_users import create_auth_users_router
from core.infrastructure.db import job_admin_store
from core.infrastructure.db.logger_extensions_accounts_common import (
    PlatformLoggerExtensionsAccountsCommonMixin,
)
from core.infrastructure.db.logger_extensions_accounts_pat_mixin import (
    PlatformLoggerPersonalAccessTokensMixin,
)
from core.infrastructure.db.logger_extensions_accounts_service_tokens_mixin import (
    PlatformLoggerServiceTokensMixin,
)
from core.infrastructure.db.platform_logger_common import SQL_AND, SQL_PROJECT_ACCOUNT_ID_EQ
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter
from core.infrastructure.runtime.serve_request_runtime import (
    build_api_error_response_factory,
    build_error_payload_factory,
)

TENANT_ID = "00000000-0000-0000-0000-000000000001"
PASSWORD = "correct horse battery staple"
COOKIE = "fs_session"

#: The production answer to an unexpected error: logged, never shown (no exception text).
GENERIC_ERROR_RESPONSE = build_api_error_response_factory(
    logger=logging.getLogger("tests.db.companies"),
    error_payload=build_error_payload_factory(
        err_request_failed="Request failed", default_correlation_id_fn=lambda: "test"
    ),
    request_correlation_id=lambda _request: "test",
)
APPROVAL_STEP = "sign_off"


def new_id() -> str:
    return str(uuid.uuid4())


class TxConnection:
    """The test transaction as the stores see it: savepoints, never a commit.

    Every nested ``transaction()`` is a savepoint of the outer rolled-back transaction;
    an explicit ``commit``/``rollback`` and an ``autocommit`` change are ignored, so no
    route ends the test's transaction.
    """

    def __init__(self, conn: Any) -> None:
        object.__setattr__(self, "_adapter", ConnectionAdapter(conn))

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.pop("cursor_factory", None) is not None:
            kwargs.setdefault("row_factory", dict_row)
        return self._adapter.cursor(*args, **kwargs)

    @property
    def autocommit(self) -> bool:
        return False

    @autocommit.setter
    def autocommit(self, _value: bool) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)


class Store(
    PlatformLoggerServiceTokensMixin,
    PlatformLoggerPersonalAccessTokensMixin,
    PlatformLoggerExtensionsAccountsCommonMixin,
):
    """What the middleware and the auth routes read: Core's own queries, this transaction."""

    def __init__(self, conn: Any) -> None:
        self.conn = TxConnection(conn)
        self._raw = conn

    def _column_exists(self, table: str, column: str) -> bool:
        return bool(
            self._raw.execute(
                """SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s AND column_name = %s""",
                (table, column),
            ).fetchone()
        )

    def _table_exists(self, table: str) -> bool:
        return bool(self._raw.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0])

    def get_awaiting_approval_jobs(self, *, account_id: str | None = None) -> list[dict]:
        return job_admin_store.get_awaiting_approval_jobs(
            self.conn,
            account_id=account_id,
            sql_project_account_id_eq=SQL_PROJECT_ACCOUNT_ID_EQ,
            sql_and=SQL_AND,
        )


@dataclass(frozen=True)
class Company:
    id: str
    workspace: str
    project: str
    second_project: str
    workflow: str


@dataclass
class World:
    conn: Any
    store: Store
    registry: RegistryDBStore
    a: Company
    b: Company
    c: Company
    workflow: str
    audit: list[dict[str, Any]] = field(default_factory=list)

    def companies(self) -> tuple[Company, Company, Company]:
        return (self.a, self.b, self.c)

    def projects(self) -> list[str]:
        return [p for c in self.companies() for p in (c.project, c.second_project)]


def _company(conn: Any, registry: RegistryDBStore, label: str, workflow: str) -> Company:
    account_id, workspace_id = new_id(), new_id()
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"{label}-{suffix}", f"Company {label.upper()} {suffix}"),
    )
    conn.execute(
        """INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name)
           VALUES (%s, %s, %s, %s, 'Workspace')""",
        (workspace_id, TENANT_ID, account_id, f"ws-{suffix}"),
    )
    projects = []
    for name in ("Ledger", "Payroll"):
        project_id = new_id()
        conn.execute(
            """INSERT INTO projects (id, tenant_id, workspace_id, account_id, name, description)
               VALUES (%s, %s, %s, %s, %s, '')""",
            (project_id, TENANT_ID, workspace_id, account_id, f"{name} {suffix}"),
        )
        projects.append(project_id)
    save_workflow(registry, account_id, workflow, f"Invoices of {label.upper()}", author="setup")
    for project_id in projects:
        conn.execute(
            "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
            (project_id, workflow),
        )
    return Company(account_id, workspace_id, projects[0], projects[1], workflow)


def save_workflow(
    registry: RegistryDBStore, account_id: str, key: str, name: str, *, author: str
) -> None:
    with workflow_account_scope(account_id):
        registry.save_workflow(
            {
                "workflow_id": key,
                "display_name": name,
                "enabled": False,
                "steps": [],
                "groups": [],
                "for_each": [],
            },
            author=author,
        )


@contextmanager
def companies_world(dsn: str, conn: Any) -> Iterator[World]:
    """Companies A, B and C in a transaction that is rolled back."""
    with conn.transaction(force_rollback=True):
        store = Store(conn)
        registry = RegistryDBStore(dsn)
        # Every registry read and write joins the test transaction.
        registry._set_tx_conn(store.conn)
        try:
            workflow = f"invoices_{uuid.uuid4().hex[:8]}"
            yield World(
                conn=conn,
                store=store,
                registry=registry,
                a=_company(conn, registry, "a", workflow),
                b=_company(conn, registry, "b", workflow),
                c=_company(conn, registry, "c", workflow),
                workflow=workflow,
            )
        finally:
            registry._set_tx_conn(None)


# --- people, sessions and tokens --------------------------------------------------------


def person(world: World, name: str, home: str, roles: dict[str, str | None]) -> str:
    """A person living in ``home`` with a role per company (``None``: member, no role)."""
    user_id = new_id()
    world.conn.execute(
        "INSERT INTO users (id, username, password_hash, account_id) VALUES (%s, %s, 'x', %s)",
        (user_id, f"{name}-{uuid.uuid4().hex[:8]}@example.invalid", home),
    )
    for account_id, role in roles.items():
        world.conn.execute(
            """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
               VALUES (%s, %s, %s, (SELECT id FROM roles WHERE key = %s))""",
            (user_id, account_id, account_id == home, role),
        )
    return user_id


def username(world: World, user_id: str) -> str:
    return str(
        world.conn.execute("SELECT username FROM users WHERE id = %s", (user_id,)).fetchone()[0]
    )


def session(world: World, user_id: str, account_id: str) -> str:
    """A signed-in browser session of the person, acting in ``account_id``."""
    token = f"session-{uuid.uuid4().hex}"
    world.conn.execute(
        """INSERT INTO auth_sessions (user_id, account_id, session_token_hash, expires_at)
           VALUES (%s, %s, %s, now() + interval '1 hour')""",
        (user_id, account_id, session_token_hash(token)),
    )
    return token


def session_account(world: World, token: str) -> str:
    row = world.conn.execute(
        "SELECT account_id::text FROM auth_sessions WHERE session_token_hash = %s",
        (session_token_hash(token),),
    ).fetchone()
    return str(row[0])


def personal_token(world: World, user_id: str, account_id: str) -> str:
    """A personal access token made while acting in ``account_id``."""
    return str(
        world.store.create_pat(user_id=user_id, account_id=account_id, scopes=["admin:all"])[
            "raw_token"
        ]
    )


def oauth_grant(world: World, user_id: str, account_id: str) -> tuple[str, str]:
    """An inbound OAuth (MCP) grant made in ``account_id``: ``(grant id, access token)``."""
    now = datetime.now(UTC)
    client_id = f"https://client-{uuid.uuid4().hex[:8]}.example.invalid/metadata.json"
    world.conn.execute(
        """INSERT INTO oauth_clients (client_id, client_name, redirect_uris_jsonb, metadata_sha256,
                                      fetched_at, cache_expires_at, first_seen_at)
           VALUES (%s, 'Assistant', '["https://client.example.invalid/cb"]'::jsonb, 'x',
                   %s, %s, %s)""",
        (client_id, now, now + timedelta(hours=1), now),
    )
    world.conn.execute(
        """INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb, updated_by)
           VALUES (%s, %s, '{"enabled": true}'::jsonb, 'test')
           ON CONFLICT (account_id, setting_key)
           DO UPDATE SET setting_value_jsonb = EXCLUDED.setting_value_jsonb""",
        (account_id, INBOUND_OAUTH_SETTING_KEY),
    )
    grant_id, token = new_id(), f"fs_oat_{uuid.uuid4().hex}"
    world.conn.execute(
        """INSERT INTO oauth_grants (id, user_id, account_id, client_id, client_name_snapshot,
                                     resource, preset, scopes_jsonb, allowed_project_ids_jsonb,
                                     created_at, expires_at)
           VALUES (%s, %s, %s, %s, 'Assistant', 'https://fs.example.invalid/mcp', 'read',
                   '["projects:read"]'::jsonb, '[]'::jsonb, %s, %s)""",
        (grant_id, user_id, account_id, client_id, now, now + timedelta(days=30)),
    )
    world.conn.execute(
        """INSERT INTO oauth_access_tokens (id, token_hash, grant_id, scopes_jsonb, created_at,
                                            expires_at)
           VALUES (%s, %s, %s, '["projects:read"]'::jsonb, %s, %s)""",
        (new_id(), secret_digest(token), grant_id, now, now + timedelta(hours=1)),
    )
    return grant_id, token


def invitation(world: World, *, account_id: str, inviter: str, email: str, role: str) -> str:
    """An open invitation of ``inviter`` (an administrator there): its single-use token."""
    token = f"link-{uuid.uuid4().hex}"
    with world.conn.cursor() as cur:
        invite(
            cur,
            account_id=account_id,
            email=email,
            role=role,
            invited_by=inviter,
            token_hash=invitation_token_hash(token),
        )
    return token


def limit_to_project(world: World, account_id: str, user_id: str, project_id: str) -> None:
    """Limit the person, in ``account_id`` only, to one project as an operator."""

    @contextmanager
    def connection():
        with world.conn.transaction():
            yield world.conn

    ScopedRolesPort(connection).replace_user_grants(
        account_id,
        user_id,
        [{"scope": "project", "id": project_id, "role": "operator"}],
        created_by="test",
    )


def job(
    world: World,
    company: Company,
    *,
    status: str,
    started_by: str = "",
    project_id: str | None = None,
) -> str:
    """A run of the company's workflow; ``awaiting_approval`` waits at the approval step."""
    job_id = new_id()
    world.conn.execute(
        """INSERT INTO jobs (id, tenant_id, account_id, workspace_id, project_id, task_id,
                             status, selected_workflow, selected_workflow_key, current_step_id,
                             thread_id, meta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   jsonb_build_object('started_by', %s::text))""",
        (
            job_id,
            TENANT_ID,
            company.id,
            company.workspace,
            project_id or company.project,
            f"task-{job_id}",
            status,
            company.workflow,
            company.workflow,
            APPROVAL_STEP if status == "awaiting_approval" else None,
            f"thread-{job_id}",
            started_by,
        ),
    )
    return job_id


def assign_approver(world: World, company: Company, user_id: str) -> None:
    """Make the person an assigned approver of the company's approval step (ADR 0082)."""
    world.conn.execute(
        """INSERT INTO approval_step_approvers (workflow_id, step_id, user_id, created_by,
                                                account_id)
           VALUES (%s, %s, %s, 'test', %s)""",
        (company.workflow, APPROVAL_STEP, user_id, company.id),
    )


# --- the app ----------------------------------------------------------------------------


def _forbidden(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=403)


def _scope(request: Request) -> ScopeContext:
    scope = scope_from_principal(request.state.auth_user)
    request.app.state.scopes.append(scope)
    return scope


def _project_row(world: World, project_id: str) -> dict[str, Any]:
    row = world.conn.execute(
        "SELECT id::text, account_id::text, workspace_id::text FROM projects WHERE id::text = %s",
        (project_id,),
    ).fetchone()
    return {"id": row[0], "account_id": row[1], "workspace_id": row[2]} if row else {}


def _job_rows(world: World, project_id: str) -> list[dict[str, Any]]:
    with world.conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT id::text AS id, tenant_id::text AS tenant_id, account_id::text AS account_id,
                      workspace_id::text AS workspace_id, project_id::text AS project_id,
                      selected_workflow, status
                 FROM jobs WHERE project_id::text = %s ORDER BY id""",
            (project_id,),
        )
        return list(cur.fetchall())


def _approvals_service(world: World) -> JobQueryService:
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row.get("id") or ""),
            "project_id": str(row.get("project_id") or ""),
            "selected_workflow": row.get("selected_workflow"),
            "thread_id": row.get("thread_id"),
            "status": "awaiting_approval",
        }

    async def _payload(row: dict[str, Any], _bl: Any) -> dict[str, Any]:
        return dict(row)

    return JobQueryService(
        get_logger=lambda: world.store,
        serialize_attention_job_row=dict,
        serialize_awaiting_approval_job_row=_row,
        job_attention_group=lambda *_args: "",
        build_runtime_payload_for_api=_payload,
    )


def _add_probes(app: FastAPI, world: World) -> None:
    @app.get("/api/projects")
    def _projects(request: Request) -> dict[str, Any]:
        scope = _scope(request)
        visible = []
        for project_id in world.projects():
            try:
                require_project_access(scope, _project_row(world, project_id))
            except ServiceForbiddenError:
                continue
            visible.append(project_id)
        return {"account_id": scope.account_id, "projects": visible}

    @app.get("/api/projects/{project_id}/jobs")
    def _jobs(project_id: str, request: Request) -> Any:
        scope = _scope(request)
        try:
            require_project_access(scope, _project_row(world, project_id))
        except ServiceForbiddenError as exc:
            return _forbidden(exc)
        jobs = _job_rows(world, project_id)
        return {"jobs": [row for row in jobs if job_row_in_scope(scope, row)]}

    @app.post("/api/workflows/{workflow_id}/run")
    async def _run(workflow_id: str, request: Request) -> Any:
        scope = _scope(request)
        body = await request.json()
        try:
            require_project_permission(
                scope,
                _project_row(world, str(body.get("project_id") or "")),
                "workflows.run",
                workflow_id=workflow_id,
            )
        except ServiceForbiddenError as exc:
            return _forbidden(exc)
        return {"started": True, "account_id": scope.account_id}

    @app.get("/api/approvals")
    def _approvals(request: Request) -> dict[str, Any]:
        scope = _scope(request)
        jobs = _approvals_service(world).list_awaiting_approval_jobs(scope=scope)
        return {"jobs": [{"id": job.id, "can_decide": job.can_decide} for job in jobs]}


def build_app(world: World) -> FastAPI:
    store = world.store
    audit = world.audit

    def _record(*_args: Any, **kwargs: Any) -> None:
        audit.append(kwargs)

    def _require_user(request: Request, required_roles: Any = None) -> dict:
        return request_principal_runtime.require_authenticated_user(
            request,
            required_roles=required_roles,
            request_auth_user_payload_fn=lambda current: (
                request_principal_runtime.request_auth_user_payload(
                    current, resolved_principal_type=ResolvedPrincipal
                )
            ),
            roles_intersect_fn=request_principal_runtime.roles_intersect,
            err_authentication_required="Authentication required",
        )

    app = FastAPI()
    app.state.scopes = []

    @app.middleware("http")
    async def _authenticate(request: Request, call_next: Any) -> Any:
        return await local_auth_middleware(
            request,
            call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda path: path == "/api/auth/login",
            route_registered_fn=None,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda user, default_auth_source: (
                request_principal_runtime.normalize_live_principal(
                    user,
                    default_auth_source=default_auth_source,
                    normalize_principal_fn=resolve.normalize_principal,
                    resolved_principal_type=ResolvedPrincipal,
                )
            ),
            get_logger_only_fn=lambda: store,
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=resolve.get_user_by_session_token,
            local_auth_bootstrap_required_fn=lambda _bl: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=request_principal_runtime.roles_intersect,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda _path: False,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name=COOKIE,
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables are not available",
            err_authentication_required="Authentication required",
            err_account_disabled="Account disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
            load_scope_limit_fn=read_stored_scope_limit,
            installation_admin_fn=is_installation_admin,
        )

    app.include_router(
        create_auth_sessions_router(
            get_logger_only=lambda: store,
            session_token_hash=session_token_hash,
            auth_cookie_secure=lambda: False,
            log_auth_audit_event=_record,
            local_auth_enabled=lambda: True,
            bootstrap_request_allowed=lambda _request: (False, "done"),
            auth_tables_available=lambda _bl: True,
            resolve_actor_username=lambda _request, _requested, default="auth": default,
            get_user_by_username=resolve.get_user_by_username,
            request_client_ip=lambda _request: "127.0.0.1",
            auth_login_lock_state=lambda _username, _client_ip: (False, 0),
            auth_login_record_failure=lambda _username, _client_ip: (False, 0),
            verify_password=lambda plain, hashed: plain == PASSWORD and hashed == "x",
            password_hash=lambda plain: plain,
            auth_user_column_exists=lambda _bl, _column: False,
            auth_login_clear_failures=lambda _username, _client_ip: None,
            csrf_request_valid=lambda _request: True,
            api_error_response=GENERIC_ERROR_RESPONSE,
            err_database_not_configured="db-missing",
            err_local_auth_disabled="local-disabled",
            err_auth_tables_not_available="auth-missing",
            err_too_many_login_attempts="rate-limited",
            auth_password_min_length=12,
            auth_session_ttl_hours=24,
            auth_remember_session_ttl_hours=168,
            auth_cookie_name=COOKIE,
            auth_csrf_cookie_name="fs_csrf",
            first_admin_service_factory=lambda _bl: None,
        )
    )
    app.include_router(
        create_auth_users_router(
            local_auth_enabled=lambda: True,
            external_auth_enabled=lambda: False,
            get_logger_only=lambda: store,
            auth_tables_available=lambda _bl: True,
            require_authenticated_user=_require_user,
            request_auth_user_payload=lambda request: (
                request_principal_runtime.request_auth_user_payload(
                    request, resolved_principal_type=ResolvedPrincipal
                )
            ),
            api_error_response=GENERIC_ERROR_RESPONSE,
            log_auth_audit_event=_record,
            auth_login_clear_user_failures=lambda *_args, **_kwargs: None,
            get_user_by_id=resolve.get_user_by_id,
            get_user_by_username=resolve.get_user_by_username,
            normalize_requested_roles=request_principal_runtime.normalize_requested_roles,
            seed_default_roles=lambda _bl: None,
            password_hash=lambda plain: plain,
            auth_user_column_exists=lambda _bl, _column: False,
            real_dict_cursor_kwargs=lambda: {"row_factory": dict_row},
            auth_admin_roles={"admin"},
            err_authentication_required="Authentication required",
            err_database_not_configured="db-missing",
            err_auth_tables_not_available="auth-missing",
            err_admin_role_required="Admin role required",
            err_local_auth_disabled="local-disabled",
            err_invalid_json_body="Invalid JSON body",
            auth_csrf_cookie_name="fs_csrf",
            csrf_request_valid=lambda _request: True,
        )
    )
    _add_probes(app, world)
    return app


def client(world: World, *, cookie: str = "", bearer: str = "") -> TestClient:
    """A browser (``cookie``) or an API client (``bearer``) of the scenario app."""
    test_client = TestClient(build_app(world), raise_server_exceptions=True)
    if cookie:
        test_client.cookies.set(COOKIE, cookie)
    if bearer:
        test_client.headers["Authorization"] = f"Bearer {bearer}"
    return test_client


def last_scope(test_client: TestClient) -> ScopeContext:
    """The scope the last probe request ran with (what the middleware decided)."""
    return test_client.app.state.scopes[-1]


__all__ = [
    "APPROVAL_STEP",
    "PASSWORD",
    "Company",
    "Store",
    "TxConnection",
    "World",
    "assign_approver",
    "client",
    "companies_world",
    "invitation",
    "job",
    "last_scope",
    "limit_to_project",
    "new_id",
    "oauth_grant",
    "person",
    "personal_token",
    "save_workflow",
    "session",
    "session_account",
    "username",
]
