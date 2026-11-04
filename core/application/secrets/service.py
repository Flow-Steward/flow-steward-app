"""Use cases of the secret catalog (ADR 0057).

Authorization is decided here, before any value is written or read: the action
authority from `authority` and the account and project scope from `ScopeContext`.
A secret belongs to the installation: every project may use it, and only the
consumer's own project is checked. Values only ever flow in: nothing here returns
or logs one.
"""

from __future__ import annotations

import hashlib
import json
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
_NOT_FOUND_MESSAGE = "Secret not found"
_STALE_PLAN_MESSAGE = "The secret's uses changed after you reviewed them; review them again"
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


def _require_unrestricted(scope: ScopeContext) -> None:
    """A secret serves every project, so a caller limited to some projects may not change it."""
    if scope.allowed_project_ids or scope.allowed_workspace_ids:
        raise ServiceNotFoundError(_NOT_FOUND_MESSAGE)


def _usage_view(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "consumer_kind": usage["consumer_kind"],
        "connection_id": usage["project_connection_id"],
        "connection_name": usage["connection_name"] or "",
        "workflow_id": usage["workflow_id"],
        "workflow_name": usage["workflow_name"] or "",
        "project_id": usage["project_id"],
        "project_name": usage["project_name"] or "",
        "field": usage["field_name"],
        # A data connection, MCP connector or LLM provider install: its id and name.
        "consumer_id": usage.get("data_connection_id")
        or usage.get("mcp_connector_id")
        or usage.get("llm_provider_install_id"),
        "consumer_name": usage.get("consumer_name") or "",
    }


def _plan_revision(secret: dict[str, Any], usages: list[dict[str, Any]]) -> str:
    """Names the exact set of bindings a person saw before confirming the delete."""
    payload = {
        "secret_id": _text(secret.get("id")),
        "bindings": sorted(_text(usage.get("id")) for usage in usages),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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
    def _project_in_scope(cur: Any, scope: ScopeContext, project_id: Any) -> str:
        """The project id after checking the scope may reach the project."""
        from core.application.management.guards import require_project_access

        project = (
            repository.project_scope(cur, account_id=scope.account_id, project_id=_text(project_id))
            if _text(project_id)
            else None
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
        return _text(project["project_id"])

    def _secret_in_scope(
        self, cur: Any, scope: ScopeContext, secret_id: Any, *, lock: bool = False
    ) -> dict[str, Any]:
        secret = repository.get_secret(
            cur, account_id=scope.account_id, secret_id=_text(secret_id), for_update=lock
        )
        if not secret:
            raise ServiceNotFoundError(_NOT_FOUND_MESSAGE)
        return secret

    # -- catalog -------------------------------------------------------------

    def list_secrets(self, scope: ScopeContext) -> list[dict[str, Any]]:
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            rows = repository.list_secrets(cur, account_id=scope.account_id)
        return [public_secret(row, usage_count=row.get("usage_count")) for row in rows]

    def get_secret(self, secret_id: Any, scope: ScopeContext) -> dict[str, Any]:
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id)
            usages = repository.bindings_for_secret(
                cur, account_id=scope.account_id, secret_id=secret["id"]
            )
        view = public_secret(secret, usage_count=len(usages))
        view["usages"] = [_usage_view(usage) for usage in usages]
        return view

    def delete_plan(self, secret_id: Any, scope: ScopeContext) -> dict[str, Any]:
        """What deleting the secret would unbind, and the revision a confirmed delete names."""
        require_secret_read_metadata(scope)
        with self._transaction() as (_conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id)
            usages = repository.bindings_for_secret(
                cur, account_id=scope.account_id, secret_id=secret["id"]
            )
        return {
            "secret": public_secret(secret, usage_count=len(usages)),
            "usages": [_usage_view(usage) for usage in usages],
            "revision": _plan_revision(secret, usages),
        }

    def create_secret(
        self,
        scope: ScopeContext,
        *,
        name: Any,
        value: Any,
        description: Any = "",
    ) -> dict[str, Any]:
        require_secret_write(scope)
        _require_unrestricted(scope)
        validated_name = _validated_name(name)
        validated_value = _validated_value(value)
        validated_description = _validated_description(description)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            secret = repository.insert_secret(
                cur,
                account_id=scope.account_id,
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
        _require_unrestricted(scope)
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
        _require_unrestricted(scope)
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

    def delete_secret(
        self, secret_id: Any, scope: ScopeContext, *, plan_revision: Any = None
    ) -> dict[str, Any]:
        """Delete a secret. A used one is deleted only with the revision of the plan the
        person confirmed: its bindings are removed first, in the same transaction."""
        require_secret_write(scope)
        _require_unrestricted(scope)
        confirmed = _text(plan_revision)
        if confirmed:
            require_secret_bind(scope)
        with self._transaction() as (_conn, cur):
            secret = self._secret_in_scope(cur, scope, secret_id, lock=True)
            usages = repository.bindings_for_secret(
                cur, account_id=scope.account_id, secret_id=secret["id"]
            )
            if usages and not confirmed:
                raise ServiceConflictError(
                    f"Secret is used by {len(usages)} consumer(s); remove those bindings first"
                )
            if confirmed and confirmed != _plan_revision(secret, usages):
                raise ServiceConflictError(
                    _STALE_PLAN_MESSAGE,
                    payload={"error": _STALE_PLAN_MESSAGE, "details": {"stale_plan": True}},
                )
            self._remove_bindings(cur, scope, usages)
            # The FK on bindings still refuses a binding created concurrently
            # after the check above; the transaction then fails as a whole.
            repository.delete_secret(
                cur,
                account_id=scope.account_id,
                secret_id=secret["id"],
                storage_ref_key=secret["storage_ref_key"],
            )
            self._audit(
                cur, scope, "delete", {"secret_id": secret["id"], "removed_bindings": len(usages)}
            )
        return {"deleted": True, "id": str(secret["id"]), "removed_bindings": len(usages)}

    def _remove_bindings(self, cur: Any, scope: ScopeContext, usages: list[dict[str, Any]]) -> None:
        """Unbind every consumer of a secret that is about to be deleted."""
        from core.application.management.guards import require_project_access
        from core.application.secrets import reference_consumers

        connections: dict[str, list[str]] = {}
        for usage in usages:
            if usage["consumer_kind"] in reference_consumers.KINDS:
                reference_consumers.release(cur, scope, usage)
                self._audit(
                    cur,
                    scope,
                    "unbind",
                    {"consumer_kind": usage["consumer_kind"], "field": usage["field_name"]},
                )
                continue
            if usage["consumer_kind"] == "project_connection":
                connections.setdefault(_text(usage["project_connection_id"]), []).append(
                    usage["field_name"]
                )
                continue
            require_project_access(
                scope, {"id": _text(usage["project_id"]), "account_id": scope.account_id}
            )
            repository.delete_binding(cur, account_id=scope.account_id, binding_id=usage["id"])
            self._audit(
                cur,
                scope,
                "unbind",
                {"workflow_id": _text(usage["workflow_id"]), "field": usage["field_name"]},
            )
        for connection_id, fields in connections.items():
            connection = self._locked_connection(cur, scope, connection_id)
            for field in fields:
                repository.unbind_connection_field(
                    cur,
                    account_id=scope.account_id,
                    connection_id=connection["connection_id"],
                    field_name=field,
                )
                self._audit(
                    cur,
                    scope,
                    "unbind",
                    {"connection_id": connection["connection_id"], "field": field},
                )
            self._write_projection(cur, connection)

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
        secret bound to it. Either way it is one catalog operation, never a
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
            bindings = repository.connection_bindings(
                cur, account_id=scope.account_id, connection_id=connection["connection_id"]
            )
            label = _text(connection.get("display_name")) or connection["connection_id"]
            for field, value in sorted(validated.items()):
                if field in bindings:
                    # The bound secret may serve other projects too.
                    _require_unrestricted(scope)
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
            self._project_in_scope(cur, scope, project_id)
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
]
