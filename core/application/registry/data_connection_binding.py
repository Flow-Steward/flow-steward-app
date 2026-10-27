"""Whether a data connection is backed by a live Data Connector extension.

Core stores abstract connections, datasets, saved queries and bindings; it holds
no database drivers and executes no SQL. A connection is therefore query-capable
only when it resolves to an **installed and enabled** Data Connector extension,
whose own configuration selects the concrete driver.

This mirrors `dataset_query`'s `_extension_for_connection` exactly, because a
readiness answer that is more generous than the runtime is a false GREEN:

* the identities it resolves through are `install_id` first, then `extension_key`,
  `extension_id`, `source_extension_id` — and `source_install_id` is **not** one of
  them, so a connection carrying only that is not bound;
* when `install_id` is present the runtime returns on it and never tries the other
  identities, so neither does this;
* resolving to nothing, or to a disabled extension, is a failure at run time, so it
  is a failure here too.

The lookup is injected rather than imported so the rule stays testable and the
registry layer keeps no runtime dependency of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.extensions.projected_dataset_bridge import projection_bridge_id

#: Resolved in this order, and `install_id` short-circuits — see the module docstring.
INSTALL_BINDING_KEY = "install_id"
EXTENSION_BINDING_KEYS: tuple[str, ...] = (
    "install_id",
    "extension_key",
    "extension_id",
    "source_extension_id",
)

ExtensionResolver = Callable[..., Any]


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def connection_connector_ref(connection: Any) -> dict[str, Any]:
    """Return a connection's connector reference regardless of which column holds it."""
    payload = _as_dict(connection)
    return _as_dict(payload.get("connector_ref_jsonb") or payload.get("connector_ref"))


def connection_extension_candidates(connection: Any) -> list[tuple[str, str]]:
    """Return the identities the runtime would try, in the order it tries them.

    ``install_id`` short-circuits: the runtime returns on it and never falls back.
    Otherwise it walks `extension_key`, `extension_id`, `source_extension_id` and
    takes the first that resolves — so a stale earlier identity alongside a working
    later one is a connection the runtime runs, and readiness must agree.
    """
    ref = connection_connector_ref(connection)
    install_id = str(ref.get(INSTALL_BINDING_KEY) or "").strip()
    if install_id:
        return [("install_id", install_id)]
    return [
        ("extension", value)
        for key in EXTENSION_BINDING_KEYS
        if key != INSTALL_BINDING_KEY
        for value in [str(ref.get(key) or "").strip()]
        if value
    ]


def connection_extension_binding(connection: Any) -> tuple[str, str]:
    """Return the first identity the runtime would try, as ``(kind, value)``.

    Both are empty when the connection declares no identity the runtime reads.
    Prefer `connection_extension_candidates` where the fallback order matters.
    """
    candidates = connection_extension_candidates(connection)
    return candidates[0] if candidates else ("", "")


def _connection_label(connection: dict[str, Any]) -> str:
    label = str(connection.get("display_name") or connection.get("connection_id") or "").strip()
    return label or "This data connection"


def connection_matches_scope(connection: Any, scope: dict[str, str]) -> bool:
    """Return whether an enabled connection belongs to the runtime scope."""
    payload = _as_dict(connection)
    if not payload or payload.get("enabled") is False:
        return False
    return not any(
        actual and actual != _text(scope.get(field))
        for field in ("account_id", "tenant_id", "workspace_id")
        for actual in [_text(payload.get(field))]
    )


def connection_extension_binding_issue(
    connection: Any,
    *,
    resolve_extension: ExtensionResolver,
    require_healthy: bool = False,
) -> str:
    """Explain why a connection cannot run queries, or return an empty string.

    The message is what an operator sees before the workflow starts, so it names
    the connection and the remedy rather than the runtime symptom.
    """
    payload = _as_dict(connection)
    if not payload:
        return "Data connection was not found."
    label = _connection_label(payload)
    if payload.get("enabled") is False:
        return f"Data connection '{label}' is disabled."
    if require_healthy and _text(payload.get("health_status")).lower() != "healthy":
        return f"Data connection '{label}' must pass its health check before it can run queries."

    candidates = connection_extension_candidates(payload)
    if not candidates:
        return (
            f"Data connection '{label}' is not bound to a Data Connector extension, so no "
            "driver can run its queries. Install and enable a Data Connector extension and "
            "rebind the connection to one of its drivers."
        )

    account_id = str(payload.get("account_id") or "").strip()
    resolved: dict[str, Any] | None = None
    for kind, value in candidates:
        extension = _as_dict(resolve_extension(kind=kind, value=value, account_id=account_id))
        if extension:
            # The runtime takes the first identity that resolves and then fails it
            # for being disabled; it does not keep looking for an enabled one.
            resolved = extension
            break
    if resolved is None:
        return (
            f"Data connection '{label}' is bound to a Data Connector extension that is not "
            "installed for this account, so no driver can run its queries. Install and enable "
            "it, or rebind the connection to an installed one."
        )
    if resolved.get("enabled") is False:
        return (
            f"Data connection '{label}' is bound to a Data Connector extension that is "
            "disabled. Ask an admin to enable it."
        )
    return ""


def is_query_capable_connection(connection: Any, *, resolve_extension: ExtensionResolver) -> bool:
    """True when this connection can actually run a saved query."""
    return not connection_extension_binding_issue(connection, resolve_extension=resolve_extension)


def resolve_connection_dataset_connector(
    connection: Any,
    connector_catalog: dict[str, dict[str, Any]],
    *,
    require_healthy: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Derive a dataset connector solely from a connection and active catalog."""

    def _resolve_catalog_extension(*, kind: str, value: str, account_id: str = "") -> Any:
        for connector in connector_catalog.values():
            if account_id and _text(connector.get("account_id")) not in {"", account_id}:
                continue
            keys = (
                ("source_install_id", "install_id")
                if kind == "install_id"
                else ("extension_key", "extension_id", "source_extension_id")
            )
            if value in {_text(connector.get(key)) for key in keys}:
                return {"enabled": True}
        return None

    issue = connection_extension_binding_issue(
        connection,
        resolve_extension=_resolve_catalog_extension,
        require_healthy=require_healthy,
    )
    if issue:
        return None, issue
    connector_id = _text(connection_connector_ref(connection).get("connector_id"))
    connector = connector_catalog.get(connector_id)
    if not connector:
        return None, "Selected data connection is not backed by an active dataset connector."
    return dict(connector), ""


def resolve_project_scoped_data_connection(
    db: Any,
    dataset: dict[str, Any],
    scope: dict[str, str],
    *,
    connection_candidates: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the one connection a project-scoped dataset would use at runtime."""
    metadata = _as_dict(dataset.get("metadata_jsonb") or dataset.get("metadata"))
    if _text(metadata.get("connection_resolution")) != "project_scope":
        return None, "connection_rebind_required"
    bridge = _as_dict(metadata.get("projection_bridge"))
    install_id = _text(bridge.get("install_id"))
    release_id = _text(bridge.get("release_id"))
    artifact_id = _text(bridge.get("artifact_id"))
    dataset_id = _text(dataset.get("dataset_id"))
    try:
        expected_dataset_id = projection_bridge_id(install_id, artifact_id)
    except ValueError:
        expected_dataset_id = ""
    if (
        bridge.get("immutable") is not True
        or _text(bridge.get("artifact_kind")) != "dataset"
        or not install_id
        or not release_id
        or not artifact_id
        or dataset_id != expected_dataset_id
    ):
        return None, "connection_rebind_required"

    get_projection = getattr(db, "get_extension_registry_projection", None)
    if not callable(get_projection):
        return None, "connection_rebind_required"
    account_id = _text(scope.get("account_id"))
    try:
        projection = _as_dict(
            get_projection(
                account_id=account_id,
                install_id=install_id,
                artifact_kind="dataset",
                artifact_id=artifact_id,
            )
        )
    except Exception:
        return None, "connection_rebind_required"
    if (
        not projection
        or projection.get("is_active") is not True
        or bool(projection.get("new_runs_blocked"))
        or _text(projection.get("availability_status")).lower() != "available"
        or _text(projection.get("account_id")) != account_id
        or _text(projection.get("install_id")) != install_id
        or _text(projection.get("release_id")) != release_id
        or _text(projection.get("artifact_kind")) != "dataset"
        or _text(projection.get("artifact_id")) != artifact_id
    ):
        return None, "connection_rebind_required"
    rows = connection_candidates
    if rows is None:
        list_connections = getattr(db, "list_workspace_data_connections", None)
        if not callable(list_connections):
            return None, "connection_not_found"
        try:
            rows = list_connections(workspace_id=scope["workspace_id"])
        except TypeError:
            rows = list_connections()

    expected_connector = _text(dataset.get("connector_id"))
    if not expected_connector:
        schema = _as_dict(dataset.get("schema_snapshot_jsonb"))
        expected_connector = _text(_as_dict(schema.get("metadata")).get("connector_id"))

    matches: list[dict[str, Any]] = []
    for raw in rows or []:
        connection = _as_dict(raw)
        if not connection_matches_scope(connection, scope):
            continue
        connector_ref = connection_connector_ref(connection)
        connector_aliases = {
            _text(connector_ref.get("local_connector_id")),
            _text(connector_ref.get("connector_id")),
        } - {""}
        if expected_connector and expected_connector not in connector_aliases:
            continue
        matches.append(connection)
    if len(matches) > 1:
        return None, "connection_ambiguous"
    return (matches[0], "") if matches else (None, "connection_not_found")


def runtime_extension_resolver(db: Any) -> ExtensionResolver:
    """Resolve an extension the way `dataset_query` does at run time."""

    def _resolve(*, kind: str, value: str, account_id: str = "") -> Any:
        from core.infrastructure.extension_runtime.extension_runtime_helpers import (
            resolve_extension_runtime_record,
            resolve_extension_runtime_record_by_install_id,
        )

        dsn = str(getattr(db, "dsn", "") or "").strip()
        get_logger_only = None
        if dsn:

            def get_logger_only():
                from core.infrastructure.db.platform_stores import PlatformIdentityStore

                return PlatformIdentityStore(dsn)

        lookup = (
            resolve_extension_runtime_record_by_install_id
            if kind == "install_id"
            else resolve_extension_runtime_record
        )
        extension, _logger = lookup(
            value,
            get_logger_only=get_logger_only,
            account_id=account_id or None,
        )
        return extension

    return _resolve


__all__ = [
    "EXTENSION_BINDING_KEYS",
    "INSTALL_BINDING_KEY",
    "connection_connector_ref",
    "connection_extension_binding",
    "connection_extension_binding_issue",
    "connection_extension_candidates",
    "connection_matches_scope",
    "is_query_capable_connection",
    "resolve_connection_dataset_connector",
    "resolve_project_scoped_data_connection",
    "runtime_extension_resolver",
]
