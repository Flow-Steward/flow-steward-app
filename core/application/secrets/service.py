"""Use cases of the secret catalog (ADR 0055).

Authorization is decided here, before any value is written or read: the action
authority from `authority`, the account and project scope from `ScopeContext`, and
the availability rule (a project secret serves its project, a workspace secret its
workspace) from the server-side rows. Values only ever flow in: nothing here returns
or logs one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.secrets import repository
from core.application.secrets.authority import (
    actor_identity,
    require_approval_authority,
    require_secret_bind,
    require_secret_read_metadata,
    require_secret_write,
)
from core.application.secrets.projection import (
    connection_projection,
    projection_changes,
    projection_fingerprint,
)
from core.application.secrets.service_workflows import WorkflowSecretInputsMixin

_MAX_NAME = 200
_MAX_DESCRIPTION = 2000
_MAX_VALUE_BYTES = 64 * 1024
OAUTH_GRANT_PREFIX = "_oauth_"  # pragma: allowlist secret - a name prefix


def _text(value: Any) -> str:
    return str(value or "").strip()


def _validated_name(value: Any) -> str:
    name = _text(value)
    if not name or len(name) > _MAX_NAME:
        raise ServiceValidationError(f"name: 1 to {_MAX_NAME} characters are required")
    return name


def _validated_description(value: Any) -> str:
    description = str(value or "")
    if len(description) > _MAX_DESCRIPTION:
        raise ServiceValidationError(f"description: at most {_MAX_DESCRIPTION} characters")
    return description


def _validated_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceValidationError("value: a string is required")
    if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise ServiceValidationError("value: too large")
    return value


def _validated_field(value: Any) -> str:
    field = _text(value)
    if not field or len(field) > _MAX_NAME:
        raise ServiceValidationError("field: a credential field name is required")
    if field.startswith(OAUTH_GRANT_PREFIX):
        raise ServiceValidationError(f"field: '{field}' is an OAuth grant value, not a credential")
    return field


def public_secret(row: dict[str, Any], *, usage_count: int | None = None) -> dict[str, Any]:
    """Metadata only. There is no value field to leak."""
    view = {
        "id": str(row.get("id") or ""),
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "owner": "project" if row.get("project_id") else "workspace",
        "workspace_id": str(row.get("workspace_id") or ""),
        "project_id": str(row.get("project_id") or "") or None,
        "configured": True,
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by") or "",
        "metadata_updated_at": row.get("metadata_updated_at"),
        "metadata_updated_by": row.get("metadata_updated_by") or "",
        "value_updated_at": row.get("value_updated_at"),
        "value_updated_by": row.get("value_updated_by") or "",
    }
    if usage_count is not None:
        view["usage_count"] = int(usage_count)
    return view


def secret_available_to(secret: dict[str, Any], *, project_id: str, workspace_id: str) -> bool:
    if _text(secret.get("project_id")):
        return _text(secret.get("project_id")) == _text(project_id)
    return _text(secret.get("workspace_id")) == _text(workspace_id)


def _bindings_from_refs(secret_refs: Any) -> dict[str, str]:
    catalog = (secret_refs or {}).get("catalog") if isinstance(secret_refs, dict) else None
    return {str(k): str(v) for k, v in (catalog or {}).items()} if isinstance(catalog, dict) else {}


def connection_projection_for(cur: Any, connection: dict[str, Any]) -> dict[str, Any]:
    """The current executable projection of one connection, from server-side rows."""
    bindings = repository.connection_bindings(
        cur,
        account_id=_text(connection.get("account_id")),
        connection_id=_text(connection.get("connection_id")),
    )
    release = repository.install_release(
        cur, install_id=_text(connection.get("extension_install_id"))
    )
    return connection_projection(connection, bindings=bindings, release=release)


class SecretCatalogService(WorkflowSecretInputsMixin):
    """Create, change, bind and approve catalog secrets for one installation."""

    def __init__(
        self,
        get_logger: Callable[[], Any],
        *,
        encrypt: Callable[[str], str] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._encrypt = encrypt

    # -- plumbing ------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError("Database is not configured")
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(bl) as conn, conn.cursor() as cur:
            yield conn, cur

    def _encrypted(self, value: str) -> str:
        if self._encrypt is not None:
            return self._encrypt(value)
        from core.infrastructure.security.secret_codec import encrypt_secret

        return encrypt_secret(value)

    def _store_value(self, conn: Any, *, account_id: str, storage_ref_key: str, value: str) -> None:
        from core.infrastructure.db.model_provider_store import upsert_secret_ref

        upsert_secret_ref(
            conn,
            account_id=account_id,
            ref_key=storage_ref_key,
            encrypted_value=self._encrypted(value),
        )

    @staticmethod
    def _audit(
        cur: Any, scope: ScopeContext, command: str, target: dict[str, Any], **payload: Any
    ) -> None:
        kind, actor = actor_identity(scope)
        repository.record_audit(
            cur,
            account_id=scope.account_id,
            command_type=f"secret.{command}",
            actor_kind=kind,
            actor_id=actor,
            target=target,
            payload=payload,
        )

    @staticmethod
    def _owner_scope(
        cur: Any, scope: ScopeContext, *, project_id: Any, workspace_id: Any
    ) -> tuple[str, str | None]:
        """(workspace_id, project_id or None) after checking the scope may reach them."""
        from core.application.management.guards import require_project_access

        if _text(project_id):
            project = repository.project_scope(
                cur, account_id=scope.account_id, project_id=_text(project_id)
            )
            if not project:
                raise ServiceNotFoundError("Project not found")
            require_project_access(
                scope,
                {
                    "id": project["project_id"],
                    "account_id": scope.account_id,
                    "workspace_id": project["workspace_id"],
                },
            )
            return _text(project["workspace_id"]), _text(project["project_id"])
        if not _text(workspace_id) or not repository.workspace_exists(
            cur, account_id=scope.account_id, workspace_id=_text(workspace_id)
        ):
            raise ServiceNotFoundError("Workspace not found")
        if scope.allowed_project_ids or (
            scope.allowed_workspace_ids and _text(workspace_id) not in scope.allowed_workspace_ids
        ):
            # A project-restricted caller cannot reach what every project of the
            # workspace may use.
            raise ServiceNotFoundError("Workspace not found")
        return _text(workspace_id), None

    def _secret_in_scope(
        self, cur: Any, scope: ScopeContext, secret_id: Any, *, lock: bool = False
    ) -> dict[str, Any]:
        secret = repository.get_secret(
            cur, account_id=scope.account_id, secret_id=_text(secret_id), for_update=lock
        )
        if not secret:
            raise ServiceNotFoundError("Secret not found")
        project_id = _text(secret.get("project_id"))
        workspace_id = _text(secret.get("workspace_id"))
        if scope.allowed_project_ids and project_id not in scope.allowed_project_ids:
            raise ServiceNotFoundError("Secret not found")
        if scope.allowed_workspace_ids and workspace_id not in scope.allowed_workspace_ids:
            raise ServiceNotFoundError("Secret not found")
        return secret

    # -- catalog -------------------------------------------------------------

    def list_secrets(
        self, scope: ScopeContext, *, project_id: Any = None, workspace_id: Any = None
    ) -> list[dict[str, Any]]:
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            owner_workspace, owner_project = self._owner_scope(
                cur, scope, project_id=project_id, workspace_id=workspace_id
            )
            rows = repository.list_secrets(
                cur,
                account_id=scope.account_id,
                workspace_id=owner_workspace,
                project_id=owner_project,
            )
        return [public_secret(row, usage_count=row.get("usage_count")) for row in rows]

    def get_secret(self, secret_id: Any, scope: ScopeContext) -> dict[str, Any]:
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id)
            usages = repository.bindings_for_secret(
                cur, account_id=scope.account_id, secret_id=secret["id"]
            )
        view = public_secret(secret, usage_count=len(usages))
        view["usages"] = [
            {
                "consumer_kind": usage["consumer_kind"],
                "connection_id": usage["project_connection_id"],
                "connection_name": usage["connection_name"] or "",
                "workflow_id": usage["workflow_id"],
                "workflow_name": usage["workflow_name"] or "",
                "project_id": usage["project_id"],
                "project_name": usage["project_name"] or "",
                "field": usage["field_name"],
            }
            for usage in usages
        ]
        return view

    def create_secret(
        self,
        scope: ScopeContext,
        *,
        name: Any,
        value: Any,
        description: Any = "",
        project_id: Any = None,
        workspace_id: Any = None,
    ) -> dict[str, Any]:
        require_secret_write(scope)
        validated_name = _validated_name(name)
        validated_value = _validated_value(value)
        validated_description = _validated_description(description)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            owner_workspace, owner_project = self._owner_scope(
                cur, scope, project_id=project_id, workspace_id=workspace_id
            )
            secret = repository.insert_secret(
                cur,
                account_id=scope.account_id,
                workspace_id=owner_workspace,
                project_id=owner_project,
                name=validated_name,
                description=validated_description,
                actor=actor,
            )
            self._store_value(
                conn,
                account_id=scope.account_id,
                storage_ref_key=secret["storage_ref_key"],
                value=validated_value,
            )
            self._audit(cur, scope, "create", {"secret_id": secret["id"]})
        return public_secret(secret, usage_count=0)

    def update_metadata(
        self, secret_id: Any, scope: ScopeContext, *, name: Any, description: Any = ""
    ) -> dict[str, Any]:
        require_secret_write(scope)
        validated_name = _validated_name(name)
        validated_description = _validated_description(description)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (_conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id, lock=True)
            updated = repository.update_metadata(
                cur,
                account_id=scope.account_id,
                secret_id=secret["id"],
                name=validated_name,
                description=validated_description,
                actor=actor,
            )
            self._audit(cur, scope, "update_metadata", {"secret_id": secret["id"]})
        return public_secret(updated or secret)

    def replace_value(self, secret_id: Any, scope: ScopeContext, *, value: Any) -> dict[str, Any]:
        require_secret_write(scope)
        validated_value = _validated_value(value)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id, lock=True)
            self._store_value(
                conn,
                account_id=scope.account_id,
                storage_ref_key=secret["storage_ref_key"],
                value=validated_value,
            )
            repository.touch_value(
                cur, account_id=scope.account_id, secret_id=secret["id"], actor=actor
            )
            self._audit(cur, scope, "replace_value", {"secret_id": secret["id"]})
        return public_secret(secret)

    def delete_secret(self, secret_id: Any, scope: ScopeContext) -> dict[str, Any]:
        require_secret_write(scope)
        with self._transaction() as (_conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id, lock=True)
            usages = repository.bindings_for_secret(
                cur, account_id=scope.account_id, secret_id=secret["id"]
            )
            if usages:
                raise ServiceConflictError(
                    f"Secret is used by {len(usages)} consumer(s); remove those bindings first"
                )
            # The FK on bindings still refuses a binding created concurrently
            # after the check above; the transaction then fails as a whole.
            repository.delete_secret(
                cur,
                account_id=scope.account_id,
                secret_id=secret["id"],
                storage_ref_key=secret["storage_ref_key"],
            )
            self._audit(cur, scope, "delete", {"secret_id": secret["id"]})
        return {"deleted": True, "id": str(secret["id"])}

    # -- connection bindings -------------------------------------------------

    def _locked_connection(
        self, cur: Any, scope: ScopeContext, connection_id: Any, *, project_id: Any = None
    ) -> dict[str, Any]:
        from core.application.management.guards import require_project_access

        connection = repository.lock_connection(
            cur, account_id=scope.account_id, connection_id=_text(connection_id)
        )
        if not connection or (
            _text(project_id) and _text(connection.get("project_id")) != _text(project_id)
        ):
            raise ServiceNotFoundError("Connection not found")
        require_project_access(
            scope, {"id": connection["project_id"], "account_id": scope.account_id}
        )
        return connection

    @staticmethod
    def _write_projection(
        cur: Any, connection: dict[str, Any], *, bump_revision: bool = True
    ) -> None:
        """Rewrite the connection's refs projection from its bindings, in this transaction."""
        secret_refs = dict(connection.get("secret_refs") or {})
        refs = dict(secret_refs.get("refs") or {})
        previous = _bindings_from_refs(secret_refs)
        bindings = repository.connection_bindings(
            cur,
            account_id=_text(connection.get("account_id")),
            connection_id=_text(connection.get("connection_id")),
        )
        for field in set(previous) - set(bindings):
            refs.pop(field, None)
        for field, secret_id in bindings.items():
            secret = repository.get_secret(
                cur, account_id=_text(connection.get("account_id")), secret_id=secret_id
            )
            assert secret is not None
            refs[field] = secret["storage_ref_key"]
        secret_refs["refs"] = refs
        if bindings:
            secret_refs["catalog"] = dict(sorted(bindings.items()))
        else:
            secret_refs.pop("catalog", None)
        repository.write_connection_secret_refs(
            cur,
            connection_id=_text(connection.get("connection_id")),
            secret_refs=secret_refs,
            bump_revision=bump_revision,
        )
        connection["secret_refs"] = secret_refs

    def bind_connection_field(
        self, connection_id: Any, scope: ScopeContext, *, field: Any, secret_id: Any
    ) -> dict[str, Any]:
        require_secret_bind(scope)
        validated_field = _validated_field(field)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (_conn, cur):
            connection = self._locked_connection(cur, scope, connection_id)
            secret = self._secret_in_scope(cur, scope, secret_id, lock=True)
            project = repository.project_scope(
                cur, account_id=scope.account_id, project_id=connection["project_id"]
            )
            if not project or not secret_available_to(
                secret, project_id=project["project_id"], workspace_id=project["workspace_id"]
            ):
                raise ServiceValidationError(
                    "This secret is not available to the connection's project"
                )
            repository.bind_connection_field(
                cur,
                account_id=scope.account_id,
                secret_id=secret["id"],
                connection_id=connection["connection_id"],
                field_name=validated_field,
                actor=actor,
            )
            self._write_projection(cur, connection)
            self._audit(
                cur,
                scope,
                "bind",
                {
                    "secret_id": secret["id"],
                    "connection_id": connection["connection_id"],
                    "field": validated_field,
                },
            )
        return self.connection_approval_status(connection_id, scope)

    def unbind_connection_field(
        self, connection_id: Any, scope: ScopeContext, *, field: Any
    ) -> dict[str, Any]:
        require_secret_bind(scope)
        validated_field = _validated_field(field)
        with self._transaction() as (_conn, cur):
            connection = self._locked_connection(cur, scope, connection_id)
            removed = repository.unbind_connection_field(
                cur,
                account_id=scope.account_id,
                connection_id=connection["connection_id"],
                field_name=validated_field,
            )
            if not removed:
                raise ServiceNotFoundError("No secret is bound to this field")
            self._write_projection(cur, connection)
            self._audit(
                cur,
                scope,
                "unbind",
                {"connection_id": connection["connection_id"], "field": validated_field},
            )
        return self.connection_approval_status(connection_id, scope)

    def save_connection_values(
        self, connection_id: Any, scope: ScopeContext, *, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Values typed into a connection's credential fields.

        A bound field gets a new value for its secret; an unbound one gets a new
        project secret bound to it. Either way it is one catalog operation, never a
        private copy, so it needs both write and bind authority.
        """
        require_secret_write(scope)
        require_secret_bind(scope)
        validated = {
            _validated_field(field): _validated_value(value) for field, value in values.items()
        }
        if not validated:
            raise ServiceValidationError("values: at least one credential is required")
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            connection = self._locked_connection(cur, scope, connection_id)
            project = repository.project_scope(
                cur, account_id=scope.account_id, project_id=connection["project_id"]
            )
            if not project:
                raise ServiceNotFoundError("Project not found")
            bindings = repository.connection_bindings(
                cur, account_id=scope.account_id, connection_id=connection["connection_id"]
            )
            label = _text(connection.get("display_name")) or connection["connection_id"]
            for field, value in sorted(validated.items()):
                if field in bindings:
                    secret = self._secret_in_scope(cur, scope, bindings[field], lock=True)
                    self._store_value(
                        conn,
                        account_id=scope.account_id,
                        storage_ref_key=secret["storage_ref_key"],
                        value=value,
                    )
                    repository.touch_value(
                        cur, account_id=scope.account_id, secret_id=secret["id"], actor=actor
                    )
                    self._audit(cur, scope, "replace_value", {"secret_id": secret["id"]})
                    continue
                secret = repository.insert_secret(
                    cur,
                    account_id=scope.account_id,
                    workspace_id=project["workspace_id"],
                    project_id=project["project_id"],
                    name=f"{label} / {field}"[:_MAX_NAME],
                    description="",
                    actor=actor,
                )
                self._store_value(
                    conn,
                    account_id=scope.account_id,
                    storage_ref_key=secret["storage_ref_key"],
                    value=value,
                )
                repository.bind_connection_field(
                    cur,
                    account_id=scope.account_id,
                    secret_id=secret["id"],
                    connection_id=connection["connection_id"],
                    field_name=field,
                    actor=actor,
                )
                self._audit(cur, scope, "create", {"secret_id": secret["id"]})
                self._audit(
                    cur,
                    scope,
                    "bind",
                    {
                        "secret_id": secret["id"],
                        "connection_id": connection["connection_id"],
                        "field": field,
                    },
                )
            self._write_projection(cur, connection)
        return self.connection_approval_status(connection_id, scope)

    # -- approvals -----------------------------------------------------------

    @staticmethod
    def _status_view(cur: Any, connection: dict[str, Any]) -> dict[str, Any]:
        current = connection_projection_for(cur, connection)
        fingerprint = projection_fingerprint(current)
        approval = repository.connection_approval(
            cur,
            account_id=_text(connection.get("account_id")),
            connection_id=_text(connection.get("connection_id")),
        )
        if not current["bindings"]:
            status = "not_required"
        elif approval and approval.get("fingerprint") == fingerprint:
            status = "approved"
        else:
            status = "approval_required"
        secrets = []
        for field, secret_id in current["bindings"].items():
            secret = repository.get_secret(
                cur, account_id=_text(connection.get("account_id")), secret_id=secret_id
            )
            secrets.append(
                {"field": field, "secret_id": secret_id, "name": (secret or {}).get("name", "")}
            )
        approved_projection = (approval or {}).get("projection_jsonb")
        return {
            "connection_id": _text(connection.get("connection_id")),
            "status": status,
            "fingerprint": fingerprint,
            "secrets": secrets,
            "destinations": current["destinations"],
            "release": current["release"],
            "changes": []
            if status != "approval_required"
            else projection_changes(approved_projection, current),
            "approved_at": (approval or {}).get("approved_at"),
            "approved_by": (approval or {}).get("approved_by") or "",
            "approved_by_kind": (approval or {}).get("approved_by_kind") or "",
            "previous_release": (approved_projection or {}).get("release") if approval else None,
        }

    def connection_approval_status(
        self, connection_id: Any, scope: ScopeContext, *, project_id: Any = None
    ) -> dict[str, Any]:
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            connection = self._locked_connection(cur, scope, connection_id, project_id=project_id)
            return self._status_view(cur, connection)

    def project_approval_statuses(
        self, project_id: Any, scope: ScopeContext, *, extension_install_id: Any = None
    ) -> list[dict[str, Any]]:
        """Approval status of every connection of a project that uses catalog secrets."""
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            self._owner_scope(cur, scope, project_id=project_id, workspace_id=None)
            cur.execute(
                """
                SELECT DISTINCT pc.connection_id
                  FROM project_connections AS pc
                  JOIN catalog_secret_bindings AS b ON b.project_connection_id = pc.connection_id
                 WHERE pc.project_id::text = %s AND pc.account_id::text = %s
                   AND (%s = '' OR pc.extension_install_id = %s)
                 ORDER BY pc.connection_id
                """,
                (
                    _text(project_id),
                    scope.account_id,
                    _text(extension_install_id),
                    _text(extension_install_id),
                ),
            )
            connection_ids = [str(row[0]) for row in cur.fetchall() or []]
            views = []
            for connection_id in connection_ids:
                connection = self._locked_connection(cur, scope, connection_id)
                view = self._status_view(cur, connection)
                view["display_name"] = connection.get("display_name") or ""
                view["extension_install_id"] = connection.get("extension_install_id") or ""
                views.append(view)
        return views

    def approve_connections(
        self,
        scope: ScopeContext,
        *,
        approvals: dict[str, str],
        release_id: Any = None,
        project_id: Any = None,
    ) -> list[dict[str, Any]]:
        """Approve exactly the fingerprints the person was shown, all or none.

        `approvals` maps connection id to the fingerprint on screen. When
        `release_id` is given, every connection must currently run that release.
        """
        require_approval_authority(scope)
        if not approvals:
            raise ServiceValidationError("approvals: choose at least one connection")
        _kind, actor = actor_identity(scope)
        results = []
        with self._transaction() as (_conn, cur):
            for connection_id in sorted(approvals):
                connection = self._locked_connection(
                    cur, scope, connection_id, project_id=project_id
                )
                current = connection_projection_for(cur, connection)
                fingerprint = projection_fingerprint(current)
                if not current["bindings"]:
                    raise ServiceConflictError(
                        f"Connection '{connection_id}' uses no stored secret; nothing to approve"
                    )
                if fingerprint != _text(approvals[connection_id]) or (
                    _text(release_id) and current["release"]["release_id"] != _text(release_id)
                ):
                    raise ServiceConflictError(
                        f"Connection '{connection_id}' changed after it was shown; review it again"
                    )
                repository.record_connection_approval(
                    cur,
                    account_id=scope.account_id,
                    connection_id=connection["connection_id"],
                    fingerprint=fingerprint,
                    projection=current,
                    actor=actor,
                    actor_kind="human",
                )
                self._audit(
                    cur,
                    scope,
                    "approve",
                    {"connection_id": connection["connection_id"]},
                    fingerprint=fingerprint,
                    release_id=current["release"]["release_id"],
                )
                results.append(self._status_view(cur, connection))
        return results


__all__ = [
    "SecretCatalogService",
    "connection_projection_for",
    "public_secret",
    "secret_available_to",
]
