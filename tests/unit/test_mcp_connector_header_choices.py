"""MCP connector headers that send a stored catalog secret (ADR 0057)."""

from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.management import mcp_connector_header_secrets as header_secrets
from core.application.management.command_models import (
    McpConnectorCreateInput,
    McpConnectorUpdateInput,
)
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.mcp_connector_services import McpConnectorCommandService
from core.application.management.scope import ScopeContext
from core.application.secrets import reference_consumers
from core.infrastructure.security import stored_secrets

CONNECTOR = "mcp_choice"
TYPED_KEY = f"mcp_connector:{CONNECTOR}:header:gen1:0"
CATALOG_KEY = "catalog_secret:s1"


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._result: Any = None
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        text = " ".join(str(query).split())
        self._conn.statements.append((text, tuple(params)))
        self._result = None
        if text.startswith("SELECT 1 FROM catalog_secrets"):
            self._result = (1,) if params[1] in self._conn.catalog_keys else None
        elif text.startswith("SELECT encrypted_value FROM secret_refs"):
            value = self._conn.typed_values.get(params[3])
            self._result = (value,) if value else None
        elif text.startswith("SELECT 1 FROM mcp_connectors"):
            self._result = (1,)

    def fetchone(self) -> Any:
        return self._result

    def fetchall(self) -> list[Any]:
        return []


class _Conn:
    def __init__(
        self,
        *,
        catalog_keys: set[str] | None = None,
        typed_values: dict[str, str] | None = None,
    ) -> None:
        self.catalog_keys = set(catalog_keys or ())
        self.typed_values = dict(typed_values or {})
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Reader:
    def __init__(self, values: dict[str, str] | None = None, error: Exception | None = None):
        self.values = dict(values or {})
        self.error = error
        self.reads: list[tuple[str, str]] = []

    def read(self, context: stored_secrets.SecretReadContext, ref_key: str) -> str:
        self.reads.append((context.account_id, ref_key))
        if self.error is not None:
            raise self.error
        return self.values[ref_key]


def _row(refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": CONNECTOR,
        "account_id": "acct",
        "project_id": "proj",
        "tenant_id": "tenant",
        "display_name": "Docs MCP",
        "description": "",
        "transport": "streamable_http",
        "base_url": "https://mcp.example.test/mcp",
        "auth_mode": "bearer",
        "allowlisted_tools": [],
        "namespace_prefix": "",
        "status": "unchecked",
        "auth_status": "not_configured",
        "discovered_tools_jsonb": [],
        "oauth_metadata_jsonb": {},
        "oauth_secret_refs_jsonb": {},
        "oauth_connected_identity": "",
        "enabled": True,
        header_secrets.HEADER_SECRET_REFS_FIELD: {"refs": refs} if refs else {},
    }


def _operator(**fields: Any) -> ScopeContext:
    return ScopeContext(
        account_id="acct",
        project_id="proj",
        tenant_id="tenant",
        principal_type="operator",
        auth_source="local",
        actor_id="ops",
        **fields,
    )


@pytest.fixture(autouse=True)
def _stable_dns(monkeypatch):
    monkeypatch.setattr(
        "core.application.management.mcp_connector_http._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )
    monkeypatch.setattr(
        "core.application.extensions.url_safety._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )


@pytest.fixture
def reader(monkeypatch) -> _Reader:
    fake = _Reader({CATALOG_KEY: "tok-123"})
    monkeypatch.setattr(stored_secrets, "stored_secret_reader", lambda _conn: fake)
    monkeypatch.setattr(header_secrets, "decrypt_secret", lambda value: f"plain:{value}")
    return fake


# -- resolve ------------------------------------------------------------------------


def test_a_catalog_header_is_read_account_scoped_and_sent_with_its_prefix(reader) -> None:
    conn = _Conn(catalog_keys={CATALOG_KEY}, typed_values={TYPED_KEY: "enc-tenant"})
    row = _row(
        {
            "X-Tenant": TYPED_KEY,
            "Authorization": header_secrets.catalog_ref_entry(CATALOG_KEY, "Bearer "),
        }
    )

    headers = header_secrets.resolve_connector_headers(conn, row)

    assert headers == {"X-Tenant": "plain:enc-tenant", "Authorization": "Bearer tok-123"}
    assert reader.reads == [("acct", CATALOG_KEY)]
    # The typed value is read project-scoped; the catalog value never is.
    typed_reads = [p for q, p in conn.statements if q.startswith("SELECT encrypted_value")]
    assert typed_reads == [("acct", "proj", "tenant", TYPED_KEY)]


def test_a_key_that_is_not_a_catalog_secret_of_the_account_fails_closed(reader) -> None:
    conn = _Conn(catalog_keys=set())
    row = _row({"Authorization": header_secrets.catalog_ref_entry("llm_provider:key", "Bearer ")})

    with pytest.raises(ServiceUnavailableError) as raised:
        header_secrets.resolve_connector_headers(conn, row)

    assert raised.value.payload["error"] == "mcp_connector_header_secret_missing"
    assert raised.value.payload["header"] == "Authorization"
    assert reader.reads == []


def test_a_missing_catalog_value_fails_closed(monkeypatch) -> None:
    fake = _Reader(error=stored_secrets.StoredSecretMissing(CATALOG_KEY))
    monkeypatch.setattr(stored_secrets, "stored_secret_reader", lambda _conn: fake)
    conn = _Conn(catalog_keys={CATALOG_KEY})
    row = _row({"Authorization": header_secrets.catalog_ref_entry(CATALOG_KEY)})

    with pytest.raises(ServiceUnavailableError) as raised:
        header_secrets.resolve_connector_headers(conn, row)

    assert raised.value.payload["error"] == "mcp_connector_header_secret_missing"
    assert "tok" not in str(raised.value)


def test_a_catalog_entry_under_the_connector_prefix_is_never_read(reader) -> None:
    conn = _Conn(catalog_keys={TYPED_KEY})
    row = _row({"Authorization": header_secrets.catalog_ref_entry(TYPED_KEY)})

    with pytest.raises(ServiceUnavailableError):
        header_secrets.resolve_connector_headers(conn, row)
    assert reader.reads == []


def test_names_cover_typed_and_chosen_headers_but_typed_refs_only_typed() -> None:
    row = _row(
        {"X-Tenant": TYPED_KEY, "Authorization": header_secrets.catalog_ref_entry(CATALOG_KEY)}
    )

    assert set(header_secrets.connector_header_names(row)) == {"X-Tenant", "Authorization"}
    assert header_secrets.connector_header_typed_refs(row) == {"X-Tenant": TYPED_KEY}
    assert header_secrets.connector_header_catalog_refs(row) == {"Authorization": (CATALOG_KEY, "")}


def test_deleting_typed_values_never_touches_a_key_outside_the_connector_prefix() -> None:
    conn = _Conn()

    header_secrets.delete_connector_header_secret_refs(
        conn,
        connector_id=CONNECTOR,
        account_id="acct",
        project_id="proj",
        tenant_id="tenant",
        ref_keys=[TYPED_KEY, CATALOG_KEY],
    )

    deleted = [params[-1] for _query, params in conn.statements]
    assert deleted == [TYPED_KEY]


# -- create and update --------------------------------------------------------------


class _Bindings:
    """Stand-in for reference_consumers.bind / unbind / bound_fields."""

    def __init__(self, bound: dict[str, str] | None = None) -> None:
        self.bound = dict(bound or {})
        self.events: list[tuple[str, str, str]] = []

    def bind(self, _cur, scope, *, kind, consumer_id, field, secret_id) -> str:
        from core.application.secrets.authority import require_secret_bind

        require_secret_bind(scope)
        assert kind == "mcp_connector"
        self.events.append(("bind", field, secret_id))
        self.bound[field] = secret_id
        return f"catalog_secret:{secret_id}"

    def unbind(self, _cur, *, account_id, kind, consumer_id, field) -> bool:
        assert kind == "mcp_connector"
        self.events.append(("unbind", field, ""))
        self.bound.pop(field, None)
        return True

    def bound_fields(self, _cur, *, account_id, kind, consumer_id) -> dict[str, str]:
        return dict(self.bound)


@pytest.fixture
def bindings(monkeypatch) -> _Bindings:
    fake = _Bindings()
    monkeypatch.setattr(reference_consumers, "bind", fake.bind)
    monkeypatch.setattr(reference_consumers, "unbind", fake.unbind)
    monkeypatch.setattr(reference_consumers, "bound_fields", fake.bound_fields)
    return fake


def _service(monkeypatch, conn: _Conn, *, existing: dict[str, Any] | None = None):
    monkeypatch.setattr(
        "core.application.management.mcp_connector_services.transaction_connection",
        lambda bl: nullcontext(bl),
    )
    saves: list[dict[str, str]] = []

    def _save(_conn, *, connector_id, headers, **_lineage) -> dict[str, Any]:
        saves.append(dict(headers or {}))
        refs = {
            name: f"mcp_connector:{connector_id}:header:gen2:{index}"
            for index, name in enumerate(sorted(headers or {}))
        }
        return {"refs": refs} if refs else {}

    monkeypatch.setattr(
        "core.application.management.mcp_connector_services.save_connector_header_secrets",
        _save,
    )
    svc = McpConnectorCommandService(get_logger=lambda: conn)
    order: list[str] = []
    rows: dict[str, dict[str, Any]] = {}
    if existing is not None:
        rows[existing["connector_id"]] = dict(existing)

    def _insert(_bl, row):
        order.append("insert")
        rows[row["connector_id"]] = dict(row)

    def _update(_bl, connector_id, updates, **_kwargs):
        order.append("update")
        rows[connector_id].update(updates)

    svc._insert_connector = MagicMock(side_effect=_insert)
    svc._update_connector_row = MagicMock(side_effect=_update)
    svc._get_connector_row = MagicMock(
        side_effect=lambda _bl, connector_id, **_kwargs: (
            dict(rows[connector_id]) if connector_id in rows else None
        )
    )
    return svc, rows, saves, order


def test_create_binds_the_chosen_header_after_the_row_and_keeps_typed_values(
    monkeypatch, bindings
) -> None:
    svc, rows, saves, order = _service(monkeypatch, _Conn())
    events = bindings.events
    original_bind = bindings.bind

    def _bind(*args, **kwargs):
        order.append("bind")
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(reference_consumers, "bind", _bind)

    view = svc.create_connector(
        McpConnectorCreateInput(
            display_name="Docs MCP",
            base_url="https://mcp.example.test/mcp",
            auth_mode="bearer",
            headers={"X-Tenant": "acme"},
            header_secret_choices={"Authorization": {"secret_id": "s1", "prefix": "Bearer "}},
        ),
        scope=_operator(),
    )

    assert order == ["insert", "bind", "update"]
    assert saves == [{"X-Tenant": "acme"}]
    assert events == [("bind", "Authorization", "s1")]
    [row] = rows.values()
    refs = row[header_secrets.HEADER_SECRET_REFS_FIELD]["refs"]
    assert refs["Authorization"] == {"catalog_ref": "catalog_secret:s1", "prefix": "Bearer "}
    assert refs["X-Tenant"].startswith(f"mcp_connector:{row['connector_id']}:header:")
    body = view.dump()
    assert body["header_secret_choices"] == {
        "Authorization": {"secret_id": "s1", "prefix": "Bearer "}
    }
    assert "catalog_secret:" not in json.dumps(body, default=str)
    assert "acme" not in json.dumps(body, default=str)


def test_create_with_a_choice_needs_bind_authority(monkeypatch, bindings) -> None:
    svc, rows, _saves, _order = _service(monkeypatch, _Conn())
    token = ScopeContext(
        account_id="acct",
        project_id="proj",
        tenant_id="tenant",
        principal_type="operator",
        auth_source="pat",
        token_scopes=frozenset({"admin:all"}),
    )

    with pytest.raises(ServiceForbiddenError):
        svc.create_connector(
            McpConnectorCreateInput(
                display_name="Docs MCP",
                base_url="https://mcp.example.test/mcp",
                header_secret_choices={"Authorization": {"secret_id": "s1"}},
            ),
            scope=token,
        )
    assert rows == {}
    assert bindings.events == []


def test_one_header_cannot_be_typed_and_chosen(monkeypatch, bindings) -> None:
    svc, rows, _saves, _order = _service(monkeypatch, _Conn())

    with pytest.raises(ServiceValidationError):
        svc.create_connector(
            McpConnectorCreateInput(
                display_name="Docs MCP",
                base_url="https://mcp.example.test/mcp",
                headers={"authorization": "Bearer typed"},
                header_secret_choices={"Authorization": {"secret_id": "s1"}},
            ),
            scope=_operator(),
        )
    assert rows == {}


@pytest.mark.parametrize("prefix", ["Bearer\r\n", "x" * 33])
def test_a_prefix_is_short_and_has_no_line_breaks(prefix) -> None:
    with pytest.raises(ValueError):
        McpConnectorCreateInput(
            display_name="Docs MCP",
            base_url="https://mcp.example.test/mcp",
            header_secret_choices={"Authorization": {"secret_id": "s1", "prefix": prefix}},
        )


def _existing_with_choice() -> dict[str, Any]:
    return _row(
        {
            "X-Tenant": TYPED_KEY,
            "Authorization": header_secrets.catalog_ref_entry(CATALOG_KEY, "Bearer "),
        }
    )


def test_update_changes_the_chosen_secret_and_keeps_typed_values(monkeypatch, bindings) -> None:
    bindings.bound = {"Authorization": "s1"}
    svc, rows, saves, _order = _service(monkeypatch, _Conn(), existing=_existing_with_choice())

    view = svc.update_connector(
        CONNECTOR,
        McpConnectorUpdateInput(
            header_secret_choices={"Authorization": {"secret_id": "s2", "prefix": "Token "}}
        ),
        scope=_operator(),
    )

    assert saves == []
    assert bindings.events == [("bind", "Authorization", "s2")]
    refs = rows[CONNECTOR][header_secrets.HEADER_SECRET_REFS_FIELD]["refs"]
    assert refs == {
        "X-Tenant": TYPED_KEY,
        "Authorization": {"catalog_ref": "catalog_secret:s2", "prefix": "Token "},
    }
    assert view.header_secret_choices == {"Authorization": {"secret_id": "s2", "prefix": "Token "}}


def test_update_with_no_choices_stops_using_the_secret(monkeypatch, bindings) -> None:
    bindings.bound = {"Authorization": "s1"}
    svc, rows, _saves, _order = _service(monkeypatch, _Conn(), existing=_existing_with_choice())

    view = svc.update_connector(
        CONNECTOR, McpConnectorUpdateInput(header_secret_choices={}), scope=_operator()
    )

    assert bindings.events == [("unbind", "Authorization", "")]
    assert rows[CONNECTOR][header_secrets.HEADER_SECRET_REFS_FIELD] == {
        "refs": {"X-Tenant": TYPED_KEY}
    }
    assert view.header_secret_choices == {}


def test_replacing_typed_headers_keeps_the_choice_without_rebinding(monkeypatch, bindings) -> None:
    bindings.bound = {"Authorization": "s1"}
    svc, rows, saves, _order = _service(monkeypatch, _Conn(), existing=_existing_with_choice())

    svc.update_connector(
        CONNECTOR,
        McpConnectorUpdateInput(headers_mode="replace", headers={"X-Tenant": "beta"}),
        scope=_operator(),
    )

    assert saves == [{"X-Tenant": "beta"}]
    assert bindings.events == []
    refs = rows[CONNECTOR][header_secrets.HEADER_SECRET_REFS_FIELD]["refs"]
    assert refs["Authorization"] == {"catalog_ref": CATALOG_KEY, "prefix": "Bearer "}
    assert refs["X-Tenant"] != TYPED_KEY


def test_choosing_a_secret_for_a_typed_header_deletes_only_its_typed_value(
    monkeypatch, bindings
) -> None:
    conn = _Conn()
    svc, rows, saves, _order = _service(
        monkeypatch, conn, existing=_row({"Authorization": TYPED_KEY})
    )

    svc.update_connector(
        CONNECTOR,
        McpConnectorUpdateInput(
            header_secret_choices={"Authorization": {"secret_id": "s1", "prefix": "Bearer "}}
        ),
        scope=_operator(),
    )

    assert saves == []
    deletes = [p for q, p in conn.statements if q.startswith("DELETE FROM secret_refs")]
    assert deletes == [("acct", "proj", "tenant", TYPED_KEY)]
    assert rows[CONNECTOR][header_secrets.HEADER_SECRET_REFS_FIELD] == {
        "refs": {"Authorization": {"catalog_ref": "catalog_secret:s1", "prefix": "Bearer "}}
    }


def test_clear_removes_typed_values_and_choices(monkeypatch, bindings) -> None:
    bindings.bound = {"Authorization": "s1"}
    svc, rows, _saves, _order = _service(monkeypatch, _Conn(), existing=_existing_with_choice())

    svc.update_connector(
        CONNECTOR, McpConnectorUpdateInput(headers_mode="clear"), scope=_operator()
    )

    assert ("unbind", "Authorization", "") in bindings.events
    assert rows[CONNECTOR][header_secrets.HEADER_SECRET_REFS_FIELD] == {}


def test_changing_a_choice_needs_bind_authority(monkeypatch, bindings) -> None:
    bindings.bound = {"Authorization": "s1"}
    svc, rows, _saves, _order = _service(monkeypatch, _Conn(), existing=_existing_with_choice())
    before = json.dumps(rows, sort_keys=True, default=str)
    token = ScopeContext(
        account_id="acct",
        project_id="proj",
        tenant_id="tenant",
        principal_type="operator",
        auth_source="pat",
        token_scopes=frozenset({"admin:all"}),
    )

    with pytest.raises(ServiceForbiddenError):
        svc.update_connector(
            CONNECTOR, McpConnectorUpdateInput(header_secret_choices={}), scope=token
        )
    assert json.dumps(rows, sort_keys=True, default=str) == before
    assert bindings.events == []
