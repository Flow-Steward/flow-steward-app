"""Host-side processing for extension response envelope side effects."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Any

from core.application.extensions import extension_project_state
from core.application.extensions.connection_config_mutation import (
    CONDITIONAL_CONFIG_MUTATION,
    ConnectionConfigMutationRejected,
    apply_conditional_connection_config,
)
from core.application.extensions.connection_partition_validation import (
    ConnectionPartitionsRejected,
    require_valid_connection_partitions,
)
from core.application.extensions.project_connection_state import carried_connection_fields
from core.application.extensions.runtime_host_grants import trusted_mutation_grant


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


class _CursorAdapter:
    def __init__(self, db: Any) -> None:
        self._db = db

    def cursor(self) -> Any:
        return self._db._cursor()


def _state_db(db: Any) -> Any | None:
    if db is None:
        return None
    if callable(getattr(db, "cursor", None)):
        return db
    if callable(getattr(db, "_cursor", None)):
        return _CursorAdapter(db)
    return None


def _project_state_declaration(extension: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    for candidate in (
        payload.get("project_state"),
        compiled.get("project_state"),
        _as_dict(payload.get("manifest")).get("project_state"),
        _as_dict(compiled.get("manifest")).get("project_state"),
    ):
        row = _as_dict(candidate)
        if row:
            return row
    return {}


def _declared_write_rows(extension: dict[str, Any] | None) -> list[dict[str, Any]]:
    declaration = _project_state_declaration(extension)
    rows = declaration.get("writes") or declaration.get("write") or []
    if isinstance(rows, dict):
        rows = [rows]
    return [_as_dict(row) for row in _as_list(rows) if _as_dict(row)]


def project_state_write_declared(
    extension: dict[str, Any] | None,
    *,
    namespace: str,
    state_key: str,
) -> bool:
    normalized_namespace = _text(namespace)
    normalized_key = _text(state_key)
    if not normalized_namespace or not normalized_key:
        return False
    for row in _declared_write_rows(extension):
        row_namespace = _text(row.get("namespace"))
        row_key = _text(row.get("state_key") or row.get("key"))
        if row_namespace not in {"*", normalized_namespace}:
            continue
        if row_key and row_key not in {"*", normalized_key}:
            continue
        return True
    return False


def _response_project_state_updates(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    rows = response.get("project_state_updates")
    if not isinstance(rows, list):
        rows = result.get("project_state_updates")
    return [_as_dict(row) for row in _as_list(rows) if _as_dict(row)]


def response_host_mutations(
    invocation: dict[str, Any], *, strict_types: bool = False
) -> list[dict[str, Any]]:
    """Extract host mutations using the canonical response/result precedence."""

    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    response_rows = response.get("host_mutations")
    result_rows = result.get("host_mutations")
    if strict_types:
        for container, rows in ((response, response_rows), (result, result_rows)):
            if "host_mutations" in container and not isinstance(rows, list):
                raise ValueError("host_mutations must be a list")
        for rows in (response_rows, result_rows):
            if isinstance(rows, list) and any(not isinstance(row, dict) for row in rows):
                raise ValueError("host_mutations entries must be objects")
    rows = response_rows
    if not isinstance(rows, list):
        rows = result_rows
    return [_as_dict(row) for row in _as_list(rows) if _as_dict(row)]


def _declared_host_mutation_kinds(
    extension: dict[str, Any] | None,
    *,
    operation_type: str,
    operation_id: str,
) -> set[str]:
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    key = "query_definitions" if operation_type == "query" else "action_definitions"
    id_key = "query_id" if operation_type == "query" else "action_id"
    rows = payload.get(key) or compiled.get(key) or []
    for raw in _as_list(rows):
        row = _as_dict(raw)
        if _text(row.get(id_key) or row.get("id")) != operation_id:
            continue
        handler = _as_dict(row.get("handler"))
        config = _as_dict(handler.get("config"))
        return {_text(item) for item in _as_list(config.get("host_mutations")) if _text(item)}
    return set()


class _TransactionDb:
    """Rebind logger mixin methods to one transaction-pinned connection."""

    def __init__(self, db: Any, conn: Any) -> None:
        self._db = db
        self.conn = conn

    def __getattr__(self, name: str) -> Any:
        try:
            descriptor = inspect.getattr_static(type(self._db), name)
        except AttributeError:
            return getattr(self._db, name)
        if isinstance(descriptor, (staticmethod, classmethod)):
            return descriptor.__get__(None, type(self._db))
        if hasattr(descriptor, "__get__"):
            return descriptor.__get__(self, type(self._db))
        return getattr(self._db, name)


@contextmanager
def _host_mutation_transaction(db: Any):
    if getattr(db, "conn", None) is None:
        yield db
        return
    from core.infrastructure.db.db_pool import transaction_connection

    with transaction_connection(db) as conn:
        yield _TransactionDb(db, conn)


def _provider_key(row: Any) -> str:
    return _text(_as_dict(row).get("provider_key"))


def _validate_host_mutation_batch(
    mutations: list[dict[str, Any]],
    *,
    extension: dict[str, Any] | None,
    declared: set[str],
    operation_id: str,
    extension_id: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared: list[dict[str, Any]] = []
    for index, mutation in enumerate(mutations):
        kind = _text(mutation.get("kind"))
        if kind not in declared:
            return [], [
                {
                    "code": "host_mutation_undeclared",
                    "message": f"Host mutation '{kind}' is not declared for {operation_id}",
                    "index": index,
                }
            ]
        grant = trusted_mutation_grant(extension, kind, extension_id=extension_id)
        if grant is None:
            return [], [
                {
                    "code": "host_mutation_not_granted",
                    "message": (
                        f"Host mutation '{kind}' is not declared in the extension's runtime_grants"
                    ),
                    "index": index,
                }
            ]
        payload = _as_dict(mutation.get("payload"))
        provider_key = _text(grant.get("provider_key"))
        if provider_key:
            if kind == "notification_routes_save":
                routes = _as_list(payload.get("routes"))
                if any(_provider_key(route) != provider_key for route in routes):
                    return [], [
                        {
                            "code": "host_mutation_provider_forbidden",
                            "message": f"Host mutation '{kind}' may only manage provider '{provider_key}'",
                            "index": index,
                        }
                    ]
            elif _provider_key(payload) != provider_key:
                return [], [
                    {
                        "code": "host_mutation_provider_forbidden",
                        "message": f"Host mutation '{kind}' may only use provider '{provider_key}'",
                        "index": index,
                    }
                ]
        prepared.append({"kind": kind, "payload": payload, "grant": grant, "index": index})
    return prepared, []


def _require_expected_poll_versions(
    payload: dict[str, Any], poll_context: Any, db: Any, connection_id: str
) -> bool:
    """A state write may carry the versions it was computed from; they must match.

    Inside a connection poll they are compared with what the poll started from
    (its caller already locked and judged the row); outside one, with the row as
    locked in this transaction. Returns whether versions were carried.
    """
    if all(
        payload.get(key) in (None, "")
        for key in ("expected_state_version", "expected_config_generation")
    ):
        return False
    current_row: dict[str, Any] = {}
    if poll_context is None:
        with db.conn.cursor() as cur:
            cur.execute(
                "SELECT state_version, config_generation FROM project_connections "
                "WHERE connection_id = %s FOR UPDATE",
                (connection_id,),
            )
            row = cur.fetchone()
        if row:
            current_row = (
                dict(row)
                if isinstance(row, dict)
                else {"state_version": row[0], "config_generation": row[1]}
            )
    carried = False
    for key, attribute in (
        ("expected_state_version", "state_version"),
        ("expected_config_generation", "config_generation"),
    ):
        if payload.get(key) in (None, ""):
            continue
        carried = True
        current = (
            getattr(poll_context, attribute)
            if poll_context is not None
            else current_row.get(attribute)
        )
        try:
            matches = int(payload[key]) == int(current)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise ValueError("host_mutation_poll_version_mismatch")
    return carried


def _poll_mode_changed(
    extension: dict[str, Any] | None, before: dict[str, Any], after: dict[str, Any]
) -> bool:
    from core.application.extensions.connection_poll_commit import background_poll_mode_fields

    return any(
        _text(before.get(field)) != _text(after.get(field))
        for field in background_poll_mode_fields(extension)
    )


def _bump_connection_generation(db: Any, connection_id: str) -> None:
    from core.application.extensions.connection_poll_commit import bump_config_generation

    with db.conn.cursor() as cur:
        bump_config_generation(cur, connection_id)


def _advance_connection_state_version(db: Any, connection_id: str) -> None:
    from core.application.extensions.connection_poll_commit import advance_state_version

    with db.conn.cursor() as cur:
        advance_state_version(cur, connection_id)


def process_host_mutations(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any] | None,
    extension_id: str,
    db: Any,
    account_id: str,
    project_id: str,
    connection_id: str | None = None,
    extension_install_id: str | None = None,
    poll_context: Any = None,
) -> dict[str, Any]:
    mutations = response_host_mutations(invocation)
    if not mutations:
        return {"applied": [], "errors": []}
    operation_type = _text(invocation.get("operation_type"))
    operation_id = _text(invocation.get("operation_id"))
    declared = _declared_host_mutation_kinds(
        extension,
        operation_type=operation_type,
        operation_id=operation_id,
    )
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if db is None or not account_id or not project_id or not extension_id:
        errors.append(
            {
                "code": "host_mutation_scope_missing",
                "message": "Host mutations require database, account, project, and extension scope",
            }
        )
    prepared: list[dict[str, Any]] = []
    if not errors:
        prepared, errors = _validate_host_mutation_batch(
            mutations,
            extension=extension,
            declared=declared,
            operation_id=operation_id,
            extension_id=extension_id,
        )
    try:
        if errors:
            raise ValueError(_text(errors[0].get("code")))
        with _host_mutation_transaction(db) as transaction_db:
            for item in prepared:
                kind = item["kind"]
                payload = item["payload"]
                index = item["index"]
                if kind == CONDITIONAL_CONFIG_MUTATION:
                    apply_conditional_connection_config(
                        payload,
                        db=transaction_db,
                        extension=_as_dict(extension),
                        grant=item["grant"],
                        account_id=account_id,
                        project_id=project_id,
                        extension_id=extension_id,
                        connection_id=connection_id,
                        extension_install_id=extension_install_id,
                    )
                elif kind == "project_connection_upsert":
                    payload_connection_id = _text(payload.get("connection_id"))
                    payload_install_id = _text(payload.get("extension_install_id"))
                    requested_connection_id = _text(connection_id)
                    requested_install_id = _text(extension_install_id) or _text(
                        _as_dict(extension).get("extension_install_id")
                    )
                    if not requested_install_id:
                        raise ValueError("host_mutation_install_scope_missing")
                    if payload_install_id and payload_install_id != requested_install_id:
                        raise ValueError("host_mutation_install_scope_mismatch")
                    lookup = {
                        "project_id": project_id,
                        "extension_id": extension_id,
                        "extension_install_id": requested_install_id,
                        "enabled_only": False,
                    }
                    if requested_connection_id:
                        lookup["connection_id"] = requested_connection_id
                    existing = transaction_db.get_project_connection(**lookup) or {}
                    if not existing:
                        raise ValueError("host_mutation_connection_not_found")
                    existing_connection_id = _text(existing.get("connection_id"))
                    if payload_connection_id and payload_connection_id != existing_connection_id:
                        raise ValueError("host_mutation_connection_scope_mismatch")
                    if _text(existing.get("project_id")) != _text(project_id):
                        raise ValueError("host_mutation_project_scope_mismatch")
                    if _text(existing.get("extension_id")) != _text(extension_id):
                        raise ValueError("host_mutation_extension_scope_mismatch")
                    if _text(existing.get("extension_install_id")) != requested_install_id:
                        raise ValueError("host_mutation_install_scope_mismatch")
                    if _text(existing.get("account_id")) != _text(account_id):
                        raise ValueError("host_mutation_account_scope_mismatch")
                    versions_carried = _require_expected_poll_versions(
                        payload, poll_context, transaction_db, existing_connection_id
                    )
                    token_to_consume = _text(payload.get("consume_ingress_token"))
                    if token_to_consume:
                        consume = getattr(
                            transaction_db,
                            "consume_project_connection_ingress_token",
                            None,
                        )
                        if not callable(consume) or not consume(
                            project_id=project_id,
                            extension_id=extension_id,
                            account_id=account_id,
                            connection_id=existing_connection_id,
                            extension_install_id=requested_install_id,
                            token=token_to_consume,
                        ):
                            raise ValueError("host_mutation_precondition_failed")
                        applied.append({"kind": kind, "index": index})
                        continue
                    carried = carried_connection_fields(
                        existing,
                        extension_install_id=requested_install_id,
                        account_id=account_id,
                    )
                    carried["connection_type"] = (
                        _text(payload.get("connection_type")) or carried["connection_type"]
                    )
                    carried["display_name"] = (
                        _text(payload.get("display_name")) or carried["display_name"]
                    )
                    if "enabled" in payload:
                        carried["enabled"] = bool(payload["enabled"])
                    for field, normalize in (
                        ("connection_config", _as_dict),
                        ("connection_routes", _as_list),
                        ("ingress_config", _as_dict),
                        ("workflow_mappings", _as_dict),
                    ):
                        if field in payload:
                            carried[field] = normalize(payload[field])
                    require_valid_connection_partitions(
                        _as_dict(extension),
                        connection_type=_text(carried.get("connection_type")),
                        connection_config=_as_dict(carried.get("connection_config")),
                        ingress_config=_as_dict(carried.get("ingress_config")),
                    )
                    transaction_db.upsert_project_connection(
                        project_id=project_id,
                        extension_id=extension_id,
                        **carried,
                    )
                    if poll_context is None and versions_carried:
                        _advance_connection_state_version(transaction_db, existing_connection_id)
                    if poll_context is None and _poll_mode_changed(
                        extension,
                        _as_dict(existing.get("ingress_config")),
                        _as_dict(carried.get("ingress_config")),
                    ):
                        _bump_connection_generation(transaction_db, existing_connection_id)
                elif kind == "notification_delivery_log":
                    delivery_payload = dict(payload)
                    delivery_payload.pop("project_id", None)
                    delivery_payload.pop("account_id", None)
                    transaction_db.log_notification_delivery(
                        project_id=project_id, **delivery_payload
                    )
                elif kind == "notification_routes_save":
                    provider_key = _text(item["grant"].get("provider_key"))
                    existing_routes = list(
                        transaction_db.list_notification_routes(
                            project_id=project_id, enabled_only=False
                        )
                        or []
                    )
                    preserved = [
                        row for row in existing_routes if _provider_key(row) != provider_key
                    ]
                    transaction_db.save_notification_routes(
                        project_id,
                        routes=[*preserved, *_as_list(payload.get("routes"))],
                    )
                else:
                    raise ValueError(f"Unsupported host mutation kind '{kind}'")
                applied.append({"kind": kind, "index": index})
    except Exception as exc:
        if not errors:
            failed_item = prepared[len(applied)] if len(applied) < len(prepared) else {}
            error_code = (
                exc.code
                if isinstance(exc, (ConnectionPartitionsRejected, ConnectionConfigMutationRejected))
                else "host_mutation_failed"
            )
            errors.append(
                {
                    "code": error_code,
                    "message": str(exc),
                    "kind": _text(failed_item.get("kind")),
                    "index": failed_item.get("index"),
                }
            )
            if isinstance(exc, ConnectionConfigMutationRejected):
                errors[-1]["reason"] = exc.reason
                if exc.path:
                    errors[-1]["path"] = exc.path
        applied = []
    result = {"applied": applied, "errors": errors}
    invocation["host_mutations"] = result
    if errors:
        invocation["status"] = "failed"
        invocation["detail"] = _text(errors[0].get("code")) or "host_mutation_failed"
        invocation["errors"] = [*list(invocation.get("errors") or []), *errors]
    return result


def process_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any] | None,
    extension_id: str,
    db: Any,
    account_id: str,
    project_id: str,
    connection_id: str | None = None,
    actor_id: str | None = None,
    extension_install_id: str | None = None,
    poll_context: Any = None,
) -> dict[str, Any]:
    """Apply a response's project-state updates and host mutations, then store
    its ``resume_waits`` for waiting runs (ADR 0071) once both succeeded.

    ``poll_context`` (a ``ConnectionPollContext``) marks a connection poll whose
    caller holds the connection row lock and has judged the result current."""
    result = _apply_project_state_updates(
        invocation,
        extension=extension,
        extension_id=extension_id,
        db=db,
        account_id=account_id,
        project_id=project_id,
        connection_id=connection_id,
        actor_id=actor_id,
        extension_install_id=extension_install_id,
        poll_context=poll_context,
    )
    if invocation.get("status") != "failed":
        from core.application.jobs.workflow_wait_event_delivery import deliver_wait_events

        deliver_wait_events(
            invocation,
            db=db,
            extension_id=extension_id,
            account_id=account_id,
            project_id=project_id,
            extension_install_id=(
                _text(extension_install_id)
                or _text((extension or {}).get("install_id"))
                or _text((extension or {}).get("extension_install_id"))
            ),
        )
    return result


def _apply_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any] | None,
    extension_id: str,
    db: Any,
    account_id: str,
    project_id: str,
    connection_id: str | None = None,
    actor_id: str | None = None,
    extension_install_id: str | None = None,
    poll_context: Any = None,
) -> dict[str, Any]:
    updates = _response_project_state_updates(invocation)
    if not updates:
        process_host_mutations(
            invocation,
            extension=extension,
            extension_id=extension_id,
            db=db,
            account_id=account_id,
            project_id=project_id,
            connection_id=connection_id,
            extension_install_id=extension_install_id,
            poll_context=poll_context,
        )
        return {"applied": [], "errors": []}
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    missing_scope = [
        key
        for key, value in (
            ("account_id", account_id),
            ("project_id", project_id),
            ("extension_id", extension_id),
        )
        if not _text(value)
    ]
    if missing_scope:
        errors.append(
            {
                "code": "project_state_scope_missing",
                "message": "Extension project-state updates require account_id, project_id, and extension_id",
                "missing": missing_scope,
            }
        )
    state_db = _state_db(db)
    if state_db is None:
        errors.append(
            {
                "code": "project_state_store_unavailable",
                "message": "Extension project-state updates require a database handle",
            }
        )
    for idx, update in enumerate(updates):
        namespace = _text(update.get("namespace"))
        state_key = _text(update.get("state_key") or update.get("key"))
        value_jsonb = update.get("value_jsonb")
        if value_jsonb is None:
            value_jsonb = update.get("value")
        requested_connection_id = _text(update.get("connection_id"))
        runtime_connection_id = _text(connection_id)
        row_connection_id = runtime_connection_id or None
        owner_install_id = (
            _text(extension_install_id)
            or _text((extension or {}).get("install_id"))
            or _text((extension or {}).get("extension_install_id"))
        )
        if errors:
            break
        if requested_connection_id and requested_connection_id != runtime_connection_id:
            errors.append(
                {
                    "code": "project_state_connection_override_forbidden",
                    "message": "Extension project-state updates cannot override the runtime connection_id",
                    "index": idx,
                    "namespace": namespace,
                    "state_key": state_key,
                }
            )
            break
        if not isinstance(value_jsonb, dict):
            errors.append(
                {
                    "code": "invalid_project_state_update_value",
                    "message": "Extension project-state update value_jsonb must be an object",
                    "index": idx,
                    "namespace": namespace,
                    "state_key": state_key,
                }
            )
            break
        if not project_state_write_declared(
            extension,
            namespace=namespace,
            state_key=state_key,
        ):
            errors.append(
                {
                    "code": "project_state_write_undeclared",
                    "message": "Extension project-state write is not declared in manifest",
                    "index": idx,
                    "namespace": namespace,
                    "state_key": state_key,
                }
            )
            break
        if not owner_install_id:
            errors.append(
                {
                    "code": "project_state_install_scope_missing",
                    "message": "Extension project-state updates require an exact extension install",
                    "index": idx,
                    "namespace": namespace,
                    "state_key": state_key,
                }
            )
            break
        try:
            row = extension_project_state.put_state(
                state_db,
                account_id=account_id,
                project_id=project_id,
                extension_id=extension_id,
                extension_install_id=owner_install_id,
                namespace=namespace,
                state_key=state_key,
                value_jsonb=dict(value_jsonb),
                connection_id=row_connection_id,
                expected_revision=update.get("expected_revision"),
                expires_at=_text(update.get("expires_at")) or None,
                actor_id=actor_id,
            )
            applied.append(row)
        except extension_project_state.ExtensionProjectStateRevisionConflict as exc:
            errors.append(
                {
                    "code": "project_state_revision_conflict",
                    "message": str(exc),
                    "index": idx,
                    "namespace": namespace,
                    "state_key": state_key,
                }
            )
            break
        except Exception as exc:
            errors.append(
                {
                    "code": "project_state_update_failed",
                    "message": str(exc),
                    "index": idx,
                    "namespace": namespace,
                    "state_key": state_key,
                }
            )
            break
    result = {"applied": applied, "errors": errors}
    if errors:
        invocation["status"] = "failed"
        invocation["detail"] = str(errors[0].get("code") or "project_state_update_failed")
        invocation["errors"] = [*list(invocation.get("errors") or []), *errors]
    invocation["project_state_updates"] = result
    if not errors:
        process_host_mutations(
            invocation,
            extension=extension,
            extension_id=extension_id,
            db=db,
            account_id=account_id,
            project_id=project_id,
            connection_id=connection_id,
            extension_install_id=extension_install_id,
            poll_context=poll_context,
        )
    return result


__all__ = [
    "process_host_mutations",
    "process_project_state_updates",
    "project_state_write_declared",
    "response_host_mutations",
]
