"""Tests for ProjectConnectionService — project-scoped extension/connector config."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import (
    ProjectConnectionCreateInput,
    ProjectConnectionUpdateInput,
)
from core.application.management.command_services import ProjectConnectionService
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.project_connection import ProjectConnection
from core.application.management.requests import (
    ProjectConnectionCreateRequest,
    ProjectConnectionUpdateRequest,
)
from core.application.management.scope import ScopeContext


def _make_cursor_ctx():
    """Create a mock cursor that works as a context manager.

    ``with bl.cursor() as cur:`` → cur is the inner mock with .execute/.fetchone.
    """
    inner = MagicMock()
    inner.fetchone = MagicMock(return_value=None)
    inner.fetchall = MagicMock(return_value=[])
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=inner)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, inner


def _make_service(bl=None, install_store=None, runtime_store=None):
    mock_bl = bl or MagicMock()
    ctx, inner = _make_cursor_ctx()
    mock_bl.cursor = MagicMock(return_value=ctx)
    mock_bl.get_project = MagicMock(
        side_effect=lambda project_id: {
            "id": int(project_id),
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Demo",
        }
    )
    # Expose the inner cursor for tests that need to set .fetchone / .description
    mock_bl._cursor_inner = inner
    kwargs = {"get_logger": lambda: mock_bl}
    if install_store is not None:
        kwargs["get_extension_install_store"] = lambda: install_store
    if runtime_store is not None:
        kwargs["get_extension_runtime_store"] = lambda: runtime_store
    service = ProjectConnectionService(**kwargs)

    def _fixture_schemas(*, extension_id: str, connection_type: str = "", **_kwargs):
        if extension_id == "acme.notifier":
            return (
                {
                    "type": "object",
                    "properties": {"parse_mode": {"type": "string"}},
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"bot_token": {"type": "string"}},
                    "required": ["bot_token"],
                    "additionalProperties": False,
                },
            )
        if extension_id == "acme.showcase":
            if connection_type == "showcase_api":
                return (
                    {
                        "type": "object",
                        "properties": {
                            "base_url": {"type": "string", "format": "uri"},
                            "username": {"type": "string"},
                        },
                        "required": ["base_url"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "password": {"type": "string"},
                            "api_token": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                )
            if connection_type == "showcase_oauth_account":
                return (
                    {"type": "object", "additionalProperties": False},
                    {
                        "type": "object",
                        "properties": {"access_token": {"type": "string"}},
                        "additionalProperties": False,
                    },
                )
            raise ServiceValidationError(f"unknown connection_type '{connection_type}'")
        return {}, {}

    service._extension_connection_schemas = _fixture_schemas
    service._oauth_platform_secret_ref_names = lambda **_kwargs: {"access_token"}
    return service, mock_bl


def _trusted_scope(
    principal_type: str = "operator", *, account_id: str = DEFAULT_ACCOUNT_ID
) -> ScopeContext:
    return ScopeContext(
        account_id=account_id,
        actor_id=f"test-{principal_type}",
        principal_type=principal_type,
    )


def test_delete_connection_secret_refs_postgres_removes_only_owned_refs(pg_conn):
    from core.application.management.project_connection_secrets import (
        delete_connection_secret_refs,
    )

    suffix = uuid.uuid4().hex
    connection_id = f"conn_{suffix}"
    other_connection_id = f"connX{suffix}"
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO secret_refs (account_id, ref_key, encrypted_value)
                VALUES
                    (%s, %s, 'encrypted-password'),
                    (%s, %s, 'encrypted-token'),
                    (%s, %s, 'other-connection')
                """,
                (
                    DEFAULT_ACCOUNT_ID,
                    f"project_connection:{connection_id}:password",
                    DEFAULT_ACCOUNT_ID,
                    f"project_connection:{connection_id}:token",
                    DEFAULT_ACCOUNT_ID,
                    f"project_connection:{other_connection_id}:password",
                ),
            )

        deleted = delete_connection_secret_refs(
            pg_conn,
            account_id=DEFAULT_ACCOUNT_ID,
            connection_id=connection_id,
        )

        assert deleted == 2
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT ref_key
                  FROM secret_refs
                 WHERE account_id::text = %s
                   AND ref_key LIKE %s
                 ORDER BY ref_key
                """,
                (DEFAULT_ACCOUNT_ID, "project_connection:%"),
            )
            remaining = [row[0] for row in cur.fetchall()]
        assert f"project_connection:{connection_id}:password" not in remaining
        assert f"project_connection:{connection_id}:token" not in remaining
        assert f"project_connection:{other_connection_id}:password" in remaining
    finally:
        pg_conn.rollback()


def test_delete_connection_secret_refs_uses_exact_prefix_not_like():
    from core.application.management.project_connection_secrets import (
        delete_connection_secret_refs,
    )

    class _Cursor:
        rowcount = 0

        def __init__(self) -> None:
            self.sql = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            self.sql = sql
            self.params = params

    class _Db:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def cursor(self):
            return self.cursor_instance

    db = _Db()

    delete_connection_secret_refs(
        db,
        account_id=DEFAULT_ACCOUNT_ID,
        connection_id="conn_abc",
    )

    assert "LIKE" not in db.cursor_instance.sql
    assert "left(ref_key, length(%s)) = %s" in db.cursor_instance.sql
    assert db.cursor_instance.params == (
        DEFAULT_ACCOUNT_ID,
        "project_connection:conn_abc:",
        "project_connection:conn_abc:",
    )


class TestProjectConnectionModel:
    def test_connection_id_auto_generated(self):
        conn = ProjectConnection(
            project_id=1,
            extension_id="acme.notifier",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert conn.connection_id.startswith("conn_")
        assert len(conn.connection_id) > 5

    def test_default_connection_kind_is_extension(self):
        conn = ProjectConnection(
            project_id=1,
            extension_id="acme.notifier",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert conn.connection_kind == "extension"

    def test_mcp_connector_kind(self):
        conn = ProjectConnection(
            project_id=1,
            extension_id="docs_lookup",
            account_id=DEFAULT_ACCOUNT_ID,
            connection_kind="mcp_connector",
        )
        assert conn.connection_kind == "mcp_connector"

    def test_no_binding_in_field_names(self):
        """Verify naming constraint: no 'binding' in any field names."""
        field_names = list(ProjectConnection.model_fields.keys())
        for name in field_names:
            assert "binding" not in name.lower(), f"Field '{name}' contains 'binding'"

    def test_connection_config_serialization(self):
        conn = ProjectConnection(
            project_id=1,
            extension_id="acme.notifier",
            account_id=DEFAULT_ACCOUNT_ID,
            connection_config={"bot_token_ref": "secret_123"},
            secret_refs=["secret_123"],
            workflow_mappings={"weekly_report": "list_456"},
        )
        data = conn.model_dump(mode="json")
        assert data["connection_config"]["bot_token_ref"] == "secret_123"
        assert data["secret_refs"] == {"refs": {"secret_123": "secret_123"}}
        assert data["workflow_mappings"]["weekly_report"] == "list_456"


class TestProjectConnectionService:
    @pytest.mark.parametrize("principal_type", [None, "viewer", "user", "unknown"])
    @pytest.mark.parametrize(
        ("method_name", "args", "kwargs"),
        [
            (
                "create_connection",
                (1, ProjectConnectionCreateInput(connection_kind="extension_runtime")),
                {},
            ),
            (
                "update_connection",
                ("conn_123", ProjectConnectionUpdateInput(enabled=False)),
                {"project_id": 1},
            ),
            (
                "save_connection_secrets",
                ("conn_123", {"api_key": "secret"}),
                {"project_id": 1},
            ),
            ("delete_connection", ("conn_123",), {"project_id": 1}),
        ],
    )
    def test_mutations_require_operator_before_store_access(
        self,
        principal_type,
        method_name,
        args,
        kwargs,
    ):
        get_logger = MagicMock()
        svc = ProjectConnectionService(get_logger=get_logger)
        scope = (
            ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="untrusted-caller",
                principal_type=principal_type,
            )
            if principal_type is not None
            else None
        )

        _raises_callable_318_1 = getattr(svc, method_name)
        with pytest.raises(ServiceForbiddenError, match=r"Authentication required|Operator role"):
            _raises_callable_318_1(*args, scope=scope, **kwargs)

        get_logger.assert_not_called()

    @pytest.mark.parametrize("principal_type", ["operator", "service_account", "admin"])
    @pytest.mark.parametrize(
        ("method_name", "args", "kwargs"),
        [
            (
                "create_connection",
                (1, ProjectConnectionCreateInput(connection_kind="extension_runtime")),
                {},
            ),
            (
                "update_connection",
                ("conn_123", ProjectConnectionUpdateInput(enabled=False)),
                {"project_id": 1},
            ),
            (
                "save_connection_secrets",
                ("conn_123", {"api_key": "secret"}),
                {"project_id": 1},
            ),
            ("delete_connection", ("conn_123",), {"project_id": 1}),
        ],
    )
    def test_trusted_principals_pass_mutation_role_gate(
        self,
        principal_type,
        method_name,
        args,
        kwargs,
    ):
        get_logger = MagicMock(return_value=None)
        svc = ProjectConnectionService(get_logger=get_logger)

        _raises_callable_355_1 = getattr(svc, method_name)
        _raises_input_355_2 = _trusted_scope(principal_type)
        with pytest.raises(ServiceUnavailableError, match="Database"):
            _raises_callable_355_1(*args, scope=_raises_input_355_2, **kwargs)

        get_logger.assert_called_once_with()

    def test_create_request_rejects_a_noncanonical_extension_id(self):
        with pytest.raises(ValidationError, match="extension_id"):
            ProjectConnectionCreateRequest(extension_id="acme_showcase")

    def test_create_input_rejects_a_noncanonical_extension_id(self):
        with pytest.raises(ValidationError, match="extension_id"):
            ProjectConnectionCreateInput(extension_id="acme_showcase")

    def test_create_connection_enforces_project_scope(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(extension_id="acme.notifier")
        _raises_input_375_1 = _trusted_scope(account_id="acc_other")
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            svc.create_connection(1, inp, scope=_raises_input_375_1)

    @pytest.mark.parametrize("principal_type", ["operator", "service_account", "admin"])
    def test_create_connection(self, principal_type):
        from core.application.extensions.install import ExtensionInstallStore

        install_store = ExtensionInstallStore()
        install = install_store.install(
            extension_id="acme.notifier",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        svc, bl = _make_service(install_store=install_store)
        inp = ProjectConnectionCreateInput(
            extension_id="acme.notifier",
            extension_install_id=install.install_id,
            connection_config={"parse_mode": "HTML"},
            secret_refs=["bot_token"],
        )
        # Mock the insert to succeed
        bl._cursor_inner.execute = MagicMock()
        result = svc.create_connection(1, inp, scope=_trusted_scope(principal_type))
        assert result.extension_id == "acme.notifier"
        assert result.connection_config == {"parse_mode": "HTML"}
        assert result.connection_kind == "extension"
        assert result.secret_refs == {
            "refs": {"bot_token": f"project_connection:{result.connection_id}:bot_token"}
        }

    def test_fixture_oauth_connection_requires_exact_install_binding(self):
        svc, _ = _make_service()

        _raises_input_408_1 = ProjectConnectionCreateInput(
            extension_id="fixture.extension-oauth",
            connection_kind="extension",
            connection_type="fixture_oauth_account",
        )
        _raises_input_408_2 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="extension_install_id is required"):
            svc.create_connection(1, _raises_input_408_1, scope=_raises_input_408_2)

    def test_extension_connection_persists_validated_exact_install_binding(self):
        from core.application.extensions.install import ExtensionInstallStore

        install_store = ExtensionInstallStore()
        install = install_store.install(
            extension_id="fixture.extension-oauth",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        svc, bl = _make_service(install_store=install_store)

        result = svc.create_connection(
            1,
            ProjectConnectionCreateInput(
                extension_id="fixture.extension-oauth",
                extension_install_id=install.install_id,
                connection_kind="extension",
                connection_type="fixture_oauth_account",
            ),
            scope=_trusted_scope(),
        )

        assert result.extension_install_id == install.install_id
        assert result.connection_config == {}
        assert result.health_status == ""
        assert result.runtime_status == ""
        persisted = bl.create_project_connection.call_args.args[0]
        assert persisted["extension_install_id"] == install.install_id

    def test_create_connection_rejects_invalid_config_by_extension_schema(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.notifier",
            connection_kind="mcp_connector",
            connection_config={"unknown_field": "x"},
            secret_refs=["bot_token"],
        )
        _raises_input_456_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="schema validation"):
            svc.create_connection(1, inp, scope=_raises_input_456_1)

    def test_create_connection_rejects_user_supplied_oauth_auth_policy(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.notifier",
            connection_kind="mcp_connector",
            connection_config={
                "oauth_auth": {
                    "type": "oauth2",
                    "token_endpoint": "https://evil.example.test/token",
                }
            },
            secret_refs=["bot_token"],
        )

        _raises_input_473_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="oauth_auth is platform-owned"):
            svc.create_connection(1, inp, scope=_raises_input_473_1)

    def test_create_connection_uses_contract_v2_connection_type_schema(self):
        svc, bl = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.showcase",
            connection_kind="mcp_connector",
            connection_type="showcase_oauth_account",
            connection_config={},
            secret_refs={},
        )

        result = svc.create_connection(1, inp, scope=_trusted_scope())

        assert result.extension_id == "acme.showcase"
        assert result.connection_type == "showcase_oauth_account"
        assert result.connection_config == {}
        bl.create_project_connection.assert_called_once()

    def test_create_connection_rejects_invalid_contract_v2_connection_type_config(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.showcase",
            connection_kind="mcp_connector",
            connection_type="showcase_api",
            connection_config={"username": "demo"},
            secret_refs={},
        )

        _raises_input_503_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match=r"connection_config\.base_url"):
            svc.create_connection(1, inp, scope=_raises_input_503_1)

    def test_create_connection_rejects_unknown_contract_v2_connection_type(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.showcase",
            connection_kind="mcp_connector",
            connection_type="missing_type",
            connection_config={},
            secret_refs={},
        )

        _raises_input_516_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="unknown connection_type"):
            svc.create_connection(1, inp, scope=_raises_input_516_1)

    def test_create_connection_rejects_missing_required_secret_ref(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.notifier",
            connection_kind="mcp_connector",
            connection_config={"parse_mode": "HTML"},
            secret_refs=[],
        )
        _raises_input_527_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="missing required secret 'bot_token'"):
            svc.create_connection(1, inp, scope=_raises_input_527_1)

    def test_create_connection_rejects_foreign_secret_ref_value(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.notifier",
            connection_kind="mcp_connector",
            connection_config={"parse_mode": "HTML"},
            secret_refs={
                "refs": {
                    "bot_token": "project_connection:conn_other_project:bot_token",
                }
            },
        )

        _raises_input_543_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="raw secret ref values"):
            svc.create_connection(1, inp, scope=_raises_input_543_1)

    def test_create_connection_rejects_public_oauth_access_token_ref(self):
        svc, _ = _make_service()
        inp = ProjectConnectionCreateInput(
            extension_id="acme.showcase",
            connection_kind="mcp_connector",
            connection_type="showcase_oauth_account",
            connection_config={},
            secret_refs={"refs": {"access_token": "access_token"}},
        )

        _raises_input_556_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="platform-owned OAuth secret refs"):
            svc.create_connection(1, inp, scope=_raises_input_556_1)

    def test_update_connection_rejects_invalid_config_by_extension_schema(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "acme.notifier",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "enabled": True,
            "connection_config": {"parse_mode": "HTML"},
            "secret_refs": ["bot_token"],
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_574_1 = ProjectConnectionUpdateInput(connection_config={"bad_extra": True})
        _raises_input_574_2 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            pytest.raises(ServiceValidationError, match="schema validation"),
        ):
            svc.update_connection(
                "conn_123", _raises_input_574_1, project_id=1, scope=_raises_input_574_2
            )

        update_row.assert_not_called()

    def test_update_extension_runtime_connection_rejects_oauth_auth_policy_override(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_runtime",
            "project_id": 1,
            "extension_id": None,
            "extension_install_id": "pi_runtime_1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension_runtime",
            "runtime_mode": "local",
            "enabled": True,
            "connection_config": {},
            "secret_refs": {},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_605_1 = ProjectConnectionUpdateInput(
            connection_config={
                "oauth_auth": {
                    "type": "oauth2",
                    "token_endpoint": "https://evil.example.test/token",
                }
            }
        )
        _raises_input_605_2 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            pytest.raises(ServiceValidationError, match="oauth_auth is platform-owned"),
        ):
            svc.update_connection(
                "conn_runtime", _raises_input_605_1, project_id=1, scope=_raises_input_605_2
            )

        update_row.assert_not_called()

    def test_update_connection_type_revalidates_contract_v2_schema_and_bumps_revision(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_showcase",
            "project_id": 1,
            "extension_id": "acme.showcase",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "connection_type": "showcase_oauth_account",
            "credential_revision": 5,
            "enabled": True,
            "connection_config": {},
            "secret_refs": {},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_643_1 = ProjectConnectionUpdateInput(connection_type="showcase_api")
        _raises_input_643_2 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            pytest.raises(ServiceValidationError, match=r"connection_config\.base_url"),
        ):
            svc.update_connection(
                "conn_showcase", _raises_input_643_1, project_id=1, scope=_raises_input_643_2
            )

        update_row.assert_not_called()

    def test_update_connection_ignores_client_supplied_credential_revision(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "acme.notifier",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "credential_revision": 3,
            "enabled": True,
            "connection_config": {"parse_mode": "HTML"},
            "secret_refs": {"refs": {"bot_token": "old_ref"}},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        updated = {**existing, "display_name": "Renamed"}
        with (
            patch.object(svc, "_get_connection_row", side_effect=[existing, updated]),
            patch.object(svc, "_update_connection_row") as update_row,
            patch.object(svc, "_invalidate_connection_sessions") as invalidate_sessions,
        ):
            result = svc.update_connection(
                "conn_123",
                ProjectConnectionUpdateInput(display_name="Renamed", credential_revision=99),
                project_id=1,
                scope=_trusted_scope(),
            )

        update_row.assert_called_once()
        updates = update_row.call_args.args[2]
        assert updates == {"display_name": "Renamed"}
        invalidate_sessions.assert_not_called()
        assert result.credential_revision == 3

    def test_update_connection_preserves_a_persisted_runtime_mode_when_the_editor_sends_blank(self):
        assert (
            ProjectConnectionUpdateRequest(
                extension_install_id="",
                runtime_mode="",
            ).model_dump(exclude_none=True)
            == {}
        )

        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_runtime",
            "project_id": 1,
            "extension_id": "acme.notifier",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "runtime_mode": "local",
            "enabled": True,
            "connection_config": {"parse_mode": "HTML"},
            "secret_refs": {"refs": {"bot_token": "old_ref"}},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        updated = {**existing, "display_name": "Renamed"}
        with (
            patch.object(svc, "_get_connection_row", side_effect=[existing, updated]),
            patch.object(svc, "_update_connection_row") as update_row,
        ):
            svc.update_connection(
                "conn_runtime",
                ProjectConnectionUpdateInput(
                    display_name="Renamed",
                    extension_install_id="",
                    runtime_mode="",
                ),
                project_id=1,
                scope=_trusted_scope(),
            )

        assert update_row.call_args.args[2] == {"display_name": "Renamed"}

    def test_extension_upsert_uses_connection_id_conflict_target(self):
        svc, bl = _make_service()
        conn = ProjectConnection(
            project_id=1,
            extension_id="acme.notifier",
            account_id=DEFAULT_ACCOUNT_ID,
            connection_kind="extension",
            connection_config={"parse_mode": "HTML"},
        )
        svc._insert_connection(bl, conn)
        insert_sql = [
            call.args[0]
            for call in bl._cursor_inner.execute.call_args_list
            if isinstance(call.args[0], str) and "INSERT INTO project_connections" in call.args[0]
        ]
        assert insert_sql, "expected project_connections INSERT to run"
        assert "ON CONFLICT (connection_id)" in insert_sql[-1]

    def test_one_install_many_connections(self):
        """Same extension_id can have multiple named connections in one project."""
        conn1 = ProjectConnection(
            project_id=1,
            extension_id="acme.notifier",
            account_id=DEFAULT_ACCOUNT_ID,
            display_name="Fixture Notification Dev",
            connection_config={"parse_mode": "HTML"},
        )
        conn2 = ProjectConnection(
            project_id=1,
            extension_id="acme.notifier",
            account_id=DEFAULT_ACCOUNT_ID,
            display_name="Fixture Notification Prod",
            connection_config={"parse_mode": "Markdown"},
        )
        assert conn1.extension_id == conn2.extension_id
        assert conn1.project_id == conn2.project_id
        assert conn1.display_name != conn2.display_name
        assert conn1.connection_config != conn2.connection_config
        assert conn1.connection_id != conn2.connection_id

    def test_delete_not_found_raises(self):
        svc, bl = _make_service()
        bl._cursor_inner.execute = MagicMock()
        bl._cursor_inner.fetchone = MagicMock(return_value=None)
        _raises_input_778_1 = _trusted_scope()
        with pytest.raises(ServiceNotFoundError):
            svc.delete_connection("conn_nonexistent", project_id=1, scope=_raises_input_778_1)

    def test_no_logger_raises_unavailable(self):
        svc = ProjectConnectionService(get_logger=lambda: None)
        inp = ProjectConnectionCreateInput(extension_id="acme.test")
        _raises_input_784_1 = _trusted_scope()
        with pytest.raises(ServiceUnavailableError):
            svc.create_connection(1, inp, scope=_raises_input_784_1)

    def test_get_connection_requires_project_id(self):
        """Scope gap: get_connection without project_id must fail."""
        svc, _ = _make_service()
        with pytest.raises(ServiceValidationError, match="project_id"):
            svc.get_connection("conn_123", scope=None)

    def test_get_connection_rejects_wrong_project(self):
        """Scope gap: cross-project read must fail even if connection_id is valid."""
        svc, bl = _make_service()
        # Simulate a row that belongs to project 99
        fake_row = (
            "conn_123",
            99,
            "ext_1",
            "acc_1",
            "extension",
            True,
            "{}",
            "[]",
            "[]",
            "{}",
            "{}",
            None,
            None,
        )
        fake_desc = [
            ("connection_id",),
            ("project_id",),
            ("extension_id",),
            ("account_id",),
            ("connection_kind",),
            ("enabled",),
            ("connection_config",),
            ("secret_refs",),
            ("connection_routes",),
            ("ingress_config",),
            ("workflow_mappings",),
            ("created_at",),
            ("updated_at",),
        ]
        cursor_mock = bl._cursor_inner
        cursor_mock.fetchone = MagicMock(return_value=fake_row)
        cursor_mock.description = fake_desc
        with pytest.raises(ServiceNotFoundError, match="not found in project 1"):
            svc.get_connection("conn_123", project_id=1, scope=None)

    def test_delete_connection_requires_project_id(self):
        """Scope gap: delete without project_id must fail."""
        svc, _ = _make_service()
        _raises_input_836_1 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="project_id"):
            svc.delete_connection("conn_123", scope=_raises_input_836_1)

    def test_delete_connection_rejects_wrong_project(self):
        """Scope gap: cross-project delete must fail."""
        svc, bl = _make_service()
        fake_row = (
            "conn_123",
            99,
            "ext_1",
            "acc_1",
            "extension",
            True,
            "{}",
            "[]",
            "[]",
            "{}",
            "{}",
            None,
            None,
        )
        fake_desc = [
            ("connection_id",),
            ("project_id",),
            ("extension_id",),
            ("account_id",),
            ("connection_kind",),
            ("enabled",),
            ("connection_config",),
            ("secret_refs",),
            ("connection_routes",),
            ("ingress_config",),
            ("workflow_mappings",),
            ("created_at",),
            ("updated_at",),
        ]
        cursor_mock = bl._cursor_inner
        cursor_mock.fetchone = MagicMock(return_value=fake_row)
        cursor_mock.description = fake_desc
        _raises_input_875_1 = _trusted_scope()
        with pytest.raises(ServiceNotFoundError, match="not found in project 1"):
            svc.delete_connection("conn_123", project_id=1, scope=_raises_input_875_1)

    def test_delete_connection_removes_owned_secret_refs_before_connection(self):
        svc, bl = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "secret_refs": {"refs": {"password": "project_connection:conn_123:password"}},
        }
        svc._get_connection_row = MagicMock(return_value=existing)
        with patch(
            "core.application.management.command_services_project_connection.cleanup_project_connection_owned_data"
        ) as cleanup:
            result = svc.delete_connection("conn_123", project_id=1, scope=_trusted_scope())

        assert result == {"status": "deleted", "connection_id": "conn_123"}
        cleanup.assert_called_once_with(bl, connection=existing)

    def test_db_read_propagates_errors(self):
        """DB helpers must propagate exceptions, not swallow them."""
        svc, bl = _make_service()
        bl._cursor_inner.execute = MagicMock(side_effect=RuntimeError("table does not exist"))
        with pytest.raises(RuntimeError, match="table does not exist"):
            svc.get_connection("conn_123", project_id=1, scope=None)

    def test_db_list_propagates_errors(self):
        """DB list helpers must propagate exceptions."""
        svc, bl = _make_service()
        bl._cursor_inner.execute = MagicMock(side_effect=RuntimeError("table does not exist"))
        with pytest.raises(RuntimeError, match="table does not exist"):
            svc.list_connections(1, scope=None)

    def test_list_connections_normalizes_nullable_runtime_fields(self):
        svc, _ = _make_service()
        fake_rows = [
            {
                "connection_id": "conn_123",
                "project_id": 1,
                "extension_id": "acme.notifier",
                "account_id": DEFAULT_ACCOUNT_ID,
                "connection_kind": "extension",
                "extension_install_id": None,
                "runtime_mode": None,
                "remote_base_url": None,
                "health_status": None,
                "runtime_status": None,
                "last_seen_ok_at": None,
                "last_error": None,
                "enabled": True,
                "connection_config": None,
                "secret_refs": None,
                "connection_routes": None,
                "ingress_config": None,
                "workflow_mappings": None,
                "created_at": None,
                "updated_at": None,
            }
        ]
        with patch.object(svc, "_list_connection_rows", return_value=fake_rows):
            results = svc.list_connections(1, scope=None)

        assert len(results) == 1
        assert results[0].extension_install_id == ""
        assert results[0].runtime_mode == ""
        assert results[0].remote_base_url == ""
        assert results[0].health_status == ""
        assert results[0].runtime_status == ""
        assert results[0].last_error == ""
        assert results[0].connection_config == {}
        assert results[0].secret_refs == {}
        assert results[0].connection_routes == []
        assert results[0].ingress_config == {}
        assert results[0].workflow_mappings == {}

    def test_extension_runtime_requires_extension_install_id(self):
        svc, _ = _make_service()
        _raises_input_955_1 = ProjectConnectionCreateInput(
            connection_kind="extension_runtime", runtime_mode="local"
        )
        _raises_input_955_2 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="extension_install_id"):
            svc.create_connection(1, _raises_input_955_1, scope=_raises_input_955_2)

    def test_extension_runtime_rejects_unsupported_runtime_mode(self):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        release = runtime_store.create_release(
            ExtensionReleaseRecord(
                extension_id="acme.runtime-extension",
                release_version="1.0.0",
                descriptor={
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "acme.runtime-extension",
                    "release_version": "1.0.0",
                    "distribution": {"execution_modes": ["local"]},
                    "runtime": {"contract_version": "extension_host_v2"},
                },
                execution_modes=["local"],
                contract_version="extension_host_v2",
                source_kind="file",
                source_ref="/tmp/descriptor.json",
                account_id=DEFAULT_ACCOUNT_ID,
                created_by="tester",
            )
        )
        install = install_store.install(
            extension_id="acme.runtime-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            runtime_mode="local",
        )
        svc, _ = _make_service(install_store=install_store, runtime_store=runtime_store)
        _raises_input_999_1 = ProjectConnectionCreateInput(
            connection_kind="extension_runtime",
            extension_install_id=install.install_id,
            runtime_mode="remote_https",
        )
        _raises_input_999_2 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="not supported"):
            svc.create_connection(1, _raises_input_999_1, scope=_raises_input_999_2)

    def test_extension_runtime_remote_https_requires_base_url(self):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        release = runtime_store.create_release(
            ExtensionReleaseRecord(
                extension_id="acme.runtime-extension",
                release_version="1.0.0",
                descriptor={
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "acme.runtime-extension",
                    "release_version": "1.0.0",
                    "distribution": {"execution_modes": ["remote_https"]},
                    "runtime": {"contract_version": "extension_host_v2"},
                },
                execution_modes=["remote_https"],
                contract_version="extension_host_v2",
                source_kind="file",
                source_ref="/tmp/descriptor.json",
                account_id=DEFAULT_ACCOUNT_ID,
                created_by="tester",
            )
        )
        install = install_store.install(
            extension_id="acme.runtime-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            runtime_mode="remote_https",
        )
        svc, _ = _make_service(install_store=install_store, runtime_store=runtime_store)
        _raises_input_1044_1 = ProjectConnectionCreateInput(
            connection_kind="extension_runtime",
            extension_install_id=install.install_id,
            runtime_mode="remote_https",
        )
        _raises_input_1044_2 = _trusted_scope()
        with pytest.raises(ServiceValidationError, match="remote_base_url"):
            svc.create_connection(1, _raises_input_1044_1, scope=_raises_input_1044_2)

    def test_extension_runtime_create_success(self, monkeypatch):
        # remote_base_url is validated against SSRF rules at store time; allow
        # the placeholder runtime host to resolve in the offline test env.
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        release = runtime_store.create_release(
            ExtensionReleaseRecord(
                extension_id="acme.runtime-extension",
                release_version="1.0.0",
                descriptor={
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "acme.runtime-extension",
                    "release_version": "1.0.0",
                    "distribution": {"execution_modes": ["remote_https"]},
                    "runtime": {"contract_version": "extension_host_v2"},
                },
                execution_modes=["remote_https"],
                contract_version="extension_host_v2",
                source_kind="file",
                source_ref="/tmp/descriptor.json",
                account_id=DEFAULT_ACCOUNT_ID,
                created_by="tester",
            )
        )
        install = install_store.install(
            extension_id="acme.runtime-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            runtime_mode="remote_https",
            remote_base_url="https://runtime.example.com",
            health_status="ok",
            runtime_status="compatible",
        )
        svc, _ = _make_service(install_store=install_store, runtime_store=runtime_store)
        created = svc.create_connection(
            1,
            ProjectConnectionCreateInput(
                connection_kind="extension_runtime",
                extension_install_id=install.install_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com",
            ),
            scope=_trusted_scope(),
        )
        assert created.connection_kind == "extension_runtime"
        assert created.extension_install_id == install.install_id
        assert created.runtime_mode == "remote_https"
        assert created.remote_base_url == "https://runtime.example.com"

    def test_extension_runtime_global_install_is_visible_to_consumer_account(self, monkeypatch):
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        owner_account_id = "00000000-0000-0000-0000-000000000099"
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        release = runtime_store.create_release(
            ExtensionReleaseRecord(
                extension_id="acme.global-runtime",
                release_version="1.0.0",
                descriptor={
                    "extension_id": "acme.global-runtime",
                    "distribution": {"execution_modes": ["remote_https"]},
                },
                execution_modes=["remote_https"],
                source_kind="remote",
                source_ref="https://runtime.example.com/descriptor.json",
                account_id=owner_account_id,
                created_by="owner",
            )
        )
        install = install_store.install(
            extension_id="acme.global-runtime",
            version="1.0.0",
            account_id=owner_account_id,
            visibility="global",
            release_id=release.release_id,
            runtime_mode="remote_https",
        )
        svc, _ = _make_service(install_store=install_store, runtime_store=runtime_store)

        created = svc.create_connection(
            1,
            ProjectConnectionCreateInput(
                connection_kind="extension_runtime",
                extension_install_id=install.install_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com",
            ),
            scope=_trusted_scope(),
        )

        assert created.extension_install_id == install.install_id

    def test_extension_runtime_upsert_uses_connection_id_conflict_target(self):
        svc, bl = _make_service()
        conn = ProjectConnection(
            project_id=1,
            extension_id=None,
            account_id=DEFAULT_ACCOUNT_ID,
            connection_kind="extension_runtime",
            extension_install_id="pi_runtime_1",
            runtime_mode="local",
            health_status="ok",
            runtime_status="compatible",
        )
        svc._insert_connection(bl, conn)
        insert_sql = [
            call.args[0]
            for call in bl._cursor_inner.execute.call_args_list
            if isinstance(call.args[0], str) and "INSERT INTO project_connections" in call.args[0]
        ]
        assert insert_sql, "expected project_connections INSERT to run"
        assert "ON CONFLICT (connection_id)" in insert_sql[-1]

    def test_update_connection_bumps_credential_revision_on_secret_ref_change(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "acme.notifier",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "credential_revision": 3,
            "enabled": True,
            "connection_config": {"parse_mode": "HTML"},
            "secret_refs": {"refs": {"bot_token": "project_connection:conn_123:bot_token"}},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        updated = {
            **existing,
            "credential_revision": 4,
            "secret_refs": {"refs": {"bot_token": "project_connection:conn_123:bot_token"}},
        }
        with (
            patch.object(svc, "_get_connection_row", side_effect=[existing, updated]),
            patch.object(svc, "_update_connection_row") as update_row,
            patch.object(svc, "_invalidate_connection_sessions") as invalidate_sessions,
        ):
            result = svc.update_connection(
                "conn_123",
                ProjectConnectionUpdateInput(
                    secret_refs={"refs": {"bot_token": "project_connection:conn_123:bot_token"}}
                ),
                project_id=1,
                scope=_trusted_scope(),
            )

        update_row.assert_called_once()
        assert update_row.call_args.args[2]["credential_revision"] == 4
        invalidate_sessions.assert_called_once_with(update_row.call_args.args[0], "conn_123")
        assert result.credential_revision == 4
        assert result.secret_refs == {
            "refs": {"bot_token": "project_connection:conn_123:bot_token"}
        }

    def test_update_connection_rejects_foreign_secret_ref_value(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "acme.notifier",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "credential_revision": 3,
            "enabled": True,
            "connection_config": {"parse_mode": "HTML"},
            "secret_refs": {"refs": {"bot_token": "project_connection:conn_123:bot_token"}},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_1237_1 = ProjectConnectionUpdateInput(
            secret_refs={"refs": {"bot_token": "project_connection:conn_other_project:bot_token"}}
        )
        _raises_input_1237_2 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            pytest.raises(ServiceValidationError, match="raw secret ref values"),
        ):
            svc.update_connection(
                "conn_123", _raises_input_1237_1, project_id=1, scope=_raises_input_1237_2
            )

        update_row.assert_not_called()

    def test_update_connection_preserves_internal_secret_refs_server_side(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "acme.showcase",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "connection_type": "showcase_oauth_account",
            "credential_revision": 3,
            "enabled": True,
            "connection_config": {},
            "secret_refs": {
                "refs": {
                    "access_token": "project_connection:conn_123:access_token",
                    "_oauth_refresh_token": "project_connection:conn_123:_oauth_refresh_token",
                }
            },
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        updated = {
            **existing,
            "credential_revision": 4,
        }
        with (
            patch.object(svc, "_get_connection_row", side_effect=[existing, updated]),
            patch.object(svc, "_update_connection_row") as update_row,
            patch.object(svc, "_invalidate_connection_sessions"),
        ):
            svc.update_connection(
                "conn_123",
                ProjectConnectionUpdateInput(secret_refs={}),
                project_id=1,
                scope=_trusted_scope(),
            )

        updates = update_row.call_args.args[2]
        assert updates["secret_refs"] == {
            "refs": {
                "access_token": "project_connection:conn_123:access_token",
                "_oauth_refresh_token": "project_connection:conn_123:_oauth_refresh_token",
            }
        }

    def test_update_connection_bumps_credential_revision_on_connection_type_change(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_showcase",
            "project_id": 1,
            "extension_id": "acme.showcase",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "connection_type": "showcase_api",
            "credential_revision": 2,
            "enabled": True,
            "connection_config": {"base_url": "https://api.example.test"},
            "secret_refs": {},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        updated = {
            **existing,
            "connection_type": "showcase_oauth_account",
            "connection_config": {},
            "credential_revision": 3,
        }
        with (
            patch.object(svc, "_get_connection_row", side_effect=[existing, updated]),
            patch.object(svc, "_update_connection_row") as update_row,
            patch.object(svc, "_invalidate_connection_sessions") as invalidate_sessions,
        ):
            result = svc.update_connection(
                "conn_showcase",
                ProjectConnectionUpdateInput(
                    connection_type="showcase_oauth_account",
                    connection_config={},
                ),
                project_id=1,
                scope=_trusted_scope(),
            )

        updates = update_row.call_args.args[2]
        assert updates["connection_type"] == "showcase_oauth_account"
        assert updates["credential_revision"] == 3
        invalidate_sessions.assert_called_once_with(update_row.call_args.args[0], "conn_showcase")
        assert result.credential_revision == 3

    def test_save_connection_secrets_stores_refs_bumps_revision_and_invalidates_sessions(self):
        svc, bl = _make_service()
        existing = {
            "connection_id": "conn_123",
            "project_id": 1,
            "extension_id": "acme.notifier",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "credential_revision": 2,
            "enabled": True,
            "connection_config": {"parse_mode": "HTML"},
            "secret_refs": {},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        updated = {
            **existing,
            "credential_revision": 3,
            "secret_refs": {
                "refs": {
                    "bot_token": "project_connection:conn_123:bot_token",
                }
            },
        }
        with (
            patch.object(svc, "_get_connection_row", side_effect=[existing, updated]),
            patch.object(svc, "_update_connection_row") as update_row,
            patch.object(svc, "_invalidate_connection_sessions") as invalidate_sessions,
            patch(
                "core.application.management.project_connection_secrets.save_connection_secrets",
                return_value={"refs": {"bot_token": "project_connection:conn_123:bot_token"}},
            ) as save_secrets,
        ):
            result = svc.save_connection_secrets(
                "conn_123",
                {"bot_token": "secret-value"},
                project_id=1,
                scope=_trusted_scope(),
            )

        save_secrets.assert_called_once_with(
            bl,
            account_id=DEFAULT_ACCOUNT_ID,
            connection_id="conn_123",
            secrets={"bot_token": "secret-value"},
        )
        update_row.assert_called_once()
        updates = update_row.call_args.args[2]
        assert updates["credential_revision"] == 3
        assert updates["secret_refs"] == {
            "refs": {
                "bot_token": "project_connection:conn_123:bot_token",
            }
        }
        invalidate_sessions.assert_called_once_with(bl, "conn_123")
        assert result.credential_revision == 3
        assert result.secret_refs == updated["secret_refs"]

    def test_save_connection_secrets_rejects_platform_owned_oauth_secret(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_oauth",
            "project_id": 1,
            "extension_id": "acme.showcase",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "connection_type": "showcase_oauth_account",
            "credential_revision": 2,
            "enabled": True,
            "connection_config": {},
            "secret_refs": {},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_1423_1 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            patch(
                "core.application.management.project_connection_secrets.save_connection_secrets"
            ) as save_secrets,
            pytest.raises(ServiceValidationError, match="platform-owned OAuth secret refs"),
        ):
            svc.save_connection_secrets(
                "conn_oauth",
                {"_oauth_refresh_token": "refresh-token"},
                project_id=1,
                scope=_raises_input_1423_1,
            )

        save_secrets.assert_not_called()
        update_row.assert_not_called()

    def test_save_connection_secrets_rejects_platform_owned_oauth_access_token(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_oauth",
            "project_id": 1,
            "extension_id": "acme.showcase",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "connection_type": "showcase_oauth_account",
            "credential_revision": 2,
            "enabled": True,
            "connection_config": {},
            "secret_refs": {},
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_1458_1 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            patch(
                "core.application.management.project_connection_secrets.save_connection_secrets"
            ) as save_secrets,
            pytest.raises(ServiceValidationError, match="platform-owned OAuth secret refs"),
        ):
            svc.save_connection_secrets(
                "conn_oauth",
                {"access_token": "access-token"},
                project_id=1,
                scope=_raises_input_1458_1,
            )

        save_secrets.assert_not_called()
        update_row.assert_not_called()

    def test_save_connection_secrets_rejects_unknown_secret_for_oauth_connection(self):
        svc, _ = _make_service()
        existing = {
            "connection_id": "conn_oauth",
            "project_id": 1,
            "extension_id": "acme.showcase",
            "account_id": DEFAULT_ACCOUNT_ID,
            "connection_kind": "extension",
            "connection_type": "showcase_oauth_account",
            "credential_revision": 2,
            "enabled": True,
            "connection_config": {},
            "secret_refs": {
                "refs": {
                    "_oauth_refresh_token": "project_connection:conn_oauth:_oauth_refresh_token",
                }
            },
            "connection_routes": [],
            "ingress_config": {},
            "workflow_mappings": {},
        }
        _raises_input_1497_1 = _trusted_scope()
        with (
            patch.object(svc, "_get_connection_row", return_value=existing),
            patch.object(svc, "_update_connection_row") as update_row,
            patch(
                "core.application.management.project_connection_secrets.save_connection_secrets"
            ) as save_secrets,
            pytest.raises(ServiceValidationError, match="unknown secret 'undeclared'"),
        ):
            svc.save_connection_secrets(
                "conn_oauth", {"undeclared": "value"}, project_id=1, scope=_raises_input_1497_1
            )

        save_secrets.assert_not_called()
        update_row.assert_not_called()
