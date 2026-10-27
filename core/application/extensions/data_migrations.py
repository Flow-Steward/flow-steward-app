"""Host-owned planning and envelopes for extension JSON data migrations."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from core.application.extensions.data_migration_plan import plan_data_migration_steps

MIGRATION_ENVELOPE_VERSION = "extension_data_migration_v1"
SUPPORTED_TARGET_TYPES = frozenset(
    {
        "install_config",
        "project_connection_config",
        "workspace_data_connection_config",
        "project_state",
    }
)


def _row_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, (tuple, list)):
        return dict(zip(columns, row, strict=False))
    return {}


def build_migration_envelope(
    *,
    target_type: str,
    record: dict[str, Any],
    source_version: int,
    target_version: int,
) -> dict[str, Any]:
    normalized_type = str(target_type or "").strip()
    if normalized_type not in SUPPORTED_TARGET_TYPES:
        raise ValueError("Unsupported migration target type")
    root = dict(record or {})
    if normalized_type == "install_config":
        record_id = str(root.get("install_id") or "").strip()
        value = root.get("config")
    elif normalized_type in {"project_connection_config", "workspace_data_connection_config"}:
        record_id = str(root.get("connection_id") or "").strip()
        value = root.get("connection_config")
    else:
        record_id = str(root.get("state_id") or "").strip()
        value = root.get("value")
    if not record_id or not isinstance(value, dict):
        raise ValueError("Migration record requires stable ID and object value")
    envelope = {
        "envelope_version": MIGRATION_ENVELOPE_VERSION,
        "source_version": int(source_version),
        "target_version": int(target_version),
        "target_type": normalized_type,
        "record_id": record_id,
        "value": dict(value),
    }
    namespace = str(root.get("namespace") or "").strip()
    if normalized_type == "project_state" and namespace:
        envelope["namespace"] = namespace
    return envelope


def validate_migration_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {"value"}:
        raise ValueError("Migration output may replace only value")
    value = result.get("value")
    if not isinstance(value, dict):
        raise ValueError("Migration output value must be an object")
    return dict(value)


def validate_candidate_migration_results(
    *,
    candidate_manifest: dict[str, Any],
    migrated: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Validate transformed values against bounded candidate schemas."""
    from .lifecycle_ports import validate_json_schema_value

    schemas = dict(candidate_manifest.get("schemas") or {})
    lifecycle = dict(candidate_manifest.get("lifecycle") or {})
    state_schemas = dict(lifecycle.get("project_state_schemas") or {})
    errors: list[str] = []
    contract = dict(
        dict(candidate_manifest.get("runtime") or {}).get("extension_contract_v2") or {}
    )
    dataset_connectors = [
        dict(item) for item in contract.get("dataset_connectors") or [] if isinstance(item, dict)
    ]

    def connector_contract(expected: dict[str, Any]) -> dict[str, Any]:
        connection_type = str(expected.get("connection_type") or "").strip()
        if not connection_type:
            return {}
        for connector in dataset_connectors:
            connector_ids = {
                str(connector.get("connector_id") or "").strip(),
                str(connector.get("connector_type_id") or "").strip(),
            }
            if connection_type in connector_ids:
                return connector
            for catalog_item in connector.get("connector_catalog") or []:
                catalog = dict(catalog_item or {})
                catalog_type_id = str(catalog.get("connector_type_id") or "").strip()
                if connection_type == catalog_type_id:
                    return connector
        return {}

    for row in migrated:
        target_type = str(row.get("target_type") or "").strip()
        record_id = str(row.get("record_id") or "").strip()
        value = validate_migration_result({"value": row.get("value")})
        schema: dict[str, Any] = {}
        path = target_type
        if target_type == "install_config":
            schema = dict(schemas.get("config") or {})
        elif target_type in {"project_connection_config", "workspace_data_connection_config"}:
            expected = dict(
                dict((snapshot or {}).get("by_id") or {}).get((target_type, record_id)) or {}
            )
            connector = connector_contract(expected)
            if not connector:
                errors.append(
                    f"project_connection_config {record_id}: connector identity cannot be resolved"
                )
                continue
            schema = dict(connector.get("connection_schema") or {})
            secret_schema = dict(connector.get("secret_schema") or {})
            if secret_schema:
                required = {
                    str(item or "").strip()
                    for item in secret_schema.get("required") or []
                    if str(item or "").strip()
                }
                properties = dict(secret_schema.get("properties") or {})
                refs = dict(dict(expected.get("secret_refs") or {}).get("refs") or {})
                names = set(refs)
                missing = sorted(required - names)
                unknown = (
                    sorted(names - set(properties))
                    if secret_schema.get("additionalProperties", True) is False
                    else []
                )
                if missing or unknown:
                    errors.append(
                        f"project_connection_config {record_id}: candidate secret schema is "
                        f"incompatible (missing={missing}, unknown={unknown})"
                    )
        elif target_type == "project_state":
            namespace = str(row.get("namespace") or "").strip()
            schema = dict(state_schemas.get(namespace) or {})
            if not schema:
                errors.append(
                    f"project_state namespace '{namespace}' has no bounded candidate schema"
                )
                continue
        if schema:
            encoded = json.dumps(schema, sort_keys=True)
            if len(encoded.encode("utf-8")) > 262_144 or '"$ref": "http' in encoded.lower():
                errors.append(f"{path}: schema must be bounded and bundle-local")
                continue
            validate_json_schema_value(
                schema=schema,
                value=value,
                path=path,
                errors=errors,
            )
    if errors:
        raise ValueError("Candidate data migration validation failed: " + "; ".join(errors))


def run_candidate_data_migrations(
    *,
    candidate_extension: dict[str, Any],
    lifecycle: dict[str, Any],
    records: list[dict[str, Any]],
    source_version: int,
    target_version: int,
    invoke_action: Callable[..., dict[str, Any]] | None = None,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    if len(records) > 500:
        raise ValueError("Data migration batch exceeds 500 records")
    steps = plan_data_migration_steps(
        lifecycle,
        source_version=source_version,
        target_version=target_version,
    )
    if not steps:
        return [dict(record) for record in records]
    if invoke_action is None:
        from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

        invoke_action = run_extension_action
    external_effect_actions = {
        str(row.get("operation_id") or "").strip()
        for row in candidate_extension.get("external_effects")
        or dict(candidate_extension.get("compiled_manifest") or {}).get("external_effects")
        or []
        if isinstance(row, dict)
    }
    declared_actions = {
        str(row.get("action_id") or "").strip()
        for row in candidate_extension.get("action_definitions")
        or dict(candidate_extension.get("compiled_manifest") or {}).get("action_definitions")
        or []
        if isinstance(row, dict) and str(row.get("action_id") or "").strip()
    }
    current = [
        {
            "target_type": str(item.get("target_type") or "").strip(),
            **build_migration_envelope(
                target_type=str(item.get("target_type") or "").strip(),
                record=dict(item.get("record") or {}),
                source_version=source_version,
                target_version=steps[0]["target_version"],
            ),
        }
        for item in records
    ]
    for step in steps:
        action_id = step["action_id"]
        if action_id not in declared_actions:
            raise ValueError(f"Data migration action '{action_id}' is not declared")
        if action_id in external_effect_actions:
            raise ValueError("Data migration action must not declare external effects")
        next_rows: list[dict[str, Any]] = []
        for row in current:
            envelope = {
                **row,
                "source_version": step["source_version"],
                "target_version": step["target_version"],
            }
            invocation = invoke_action(
                candidate_extension,
                action_id=action_id,
                input_payload=envelope,
                timeout_seconds=timeout_seconds,
                runtime_context=None,
            )
            if str(invocation.get("status") or "").strip() != "succeeded":
                raise ValueError(
                    f"Data migration action '{action_id}' failed: "
                    f"{str(invocation.get('detail') or 'unknown error').strip()}"
                )
            response = invocation.get("response")
            if isinstance(response, dict) and isinstance(response.get("result"), dict):
                response = response["result"]
            value = validate_migration_result(response)
            next_rows.append({**envelope, "value": value})
        current = next_rows
    return current


def snapshot_extension_data(
    db: Any,
    *,
    account_id: str,
    extension_id: str,
    install_id: str,
) -> dict[str, Any]:
    """Read a bounded, non-secret migration snapshot without holding a transaction."""
    snapshot: dict[str, Any] = {
        "records": [],
        "by_id": {},
        "account_id": account_id,
        "extension_id": extension_id,
        "install_id": install_id,
    }
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT install_id, config_jsonb, applied_data_schema_version, visibility
              FROM extension_installs
             WHERE account_id::text = %s AND install_id = %s
            """,
            (account_id, install_id),
        )
        install = _row_dict(
            cur.fetchone(),
            ("install_id", "config_jsonb", "applied_data_schema_version", "visibility"),
        )
        if not install:
            raise ValueError("Extension install disappeared before data migration")
        install_row = {
            "target_type": "install_config",
            "record": {
                "install_id": install_id,
                "account_id": account_id,
                "config": dict(install.get("config_jsonb") or {}),
            },
        }
        snapshot["records"].append(install_row)
        snapshot["by_id"][("install_config", install_id)] = {
            "value": dict(install.get("config_jsonb") or {}),
            "revision": int(install.get("applied_data_schema_version") or 1),
            "account_id": account_id,
        }
        workspace_account_clause = (
            ""
            if str(install.get("visibility") or "").strip() == "global"
            else "AND account_id::text = %s"
        )
        workspace_params: tuple[Any, ...] = (
            (install_id,) if not workspace_account_clause else (install_id, account_id)
        )
        cur.execute(
            """
            SELECT account_id::text AS account_id,
                   connection_id,
                   connection_type,
                   connection_config,
                   secret_refs,
                   credential_revision
              FROM project_connections
             WHERE extension_install_id = %s
             ORDER BY connection_id
             LIMIT 500
            """,
            (install_id,),
        )
        for row in cur.fetchall() or []:
            row = _row_dict(
                row,
                (
                    "account_id",
                    "connection_id",
                    "connection_type",
                    "connection_config",
                    "secret_refs",
                    "credential_revision",
                ),
            )
            if not row:
                continue
            record_id = str(row.get("connection_id") or "").strip()
            value = dict(row.get("connection_config") or {})
            snapshot["records"].append(
                {
                    "target_type": "project_connection_config",
                    "record": {
                        "account_id": str(row.get("account_id") or "").strip(),
                        "connection_id": record_id,
                        "connection_config": value,
                    },
                }
            )
            snapshot["by_id"][("project_connection_config", record_id)] = {
                "value": value,
                "revision": int(row.get("credential_revision") or 1),
                "secret_refs": dict(row.get("secret_refs") or {}),
                "account_id": str(row.get("account_id") or "").strip(),
                "connection_type": str(row.get("connection_type") or "").strip(),
            }
        workspace_connections_sql = """
            SELECT account_id::text AS account_id,
                   connection_id,
                   connector_ref_jsonb,
                   connection_config_jsonb,
                   secret_refs_jsonb,
                   updated_at
             FROM workspace_data_connections
             WHERE deleted_at IS NULL
               AND connector_ref_jsonb->>'install_id' = %s
            """
        # This clause is selected from fixed host-owned account scopes.
        workspace_connections_sql = "\n".join(
            (
                workspace_connections_sql,
                workspace_account_clause,
                """
                 ORDER BY connection_id
             LIMIT 500
                """,
            )
        )
        cur.execute(workspace_connections_sql, workspace_params)
        for row in cur.fetchall() or []:
            row = _row_dict(
                row,
                (
                    "account_id",
                    "connection_id",
                    "connector_ref_jsonb",
                    "connection_config_jsonb",
                    "secret_refs_jsonb",
                    "updated_at",
                ),
            )
            if not row:
                continue
            record_id = str(row.get("connection_id") or "").strip()
            value = dict(row.get("connection_config_jsonb") or {})
            connector_ref = dict(row.get("connector_ref_jsonb") or {})
            snapshot["records"].append(
                {
                    "target_type": "workspace_data_connection_config",
                    "record": {
                        "account_id": str(row.get("account_id") or "").strip(),
                        "connection_id": record_id,
                        "connection_config": value,
                    },
                }
            )
            snapshot["by_id"][("workspace_data_connection_config", record_id)] = {
                "value": value,
                "revision": str(row.get("updated_at") or ""),
                "secret_refs": dict(row.get("secret_refs_jsonb") or {}),
                "connector_ref": connector_ref,
                "account_id": str(row.get("account_id") or "").strip(),
                "connection_type": str(
                    connector_ref.get("local_connector_id")
                    or connector_ref.get("connector_id")
                    or ""
                ).strip(),
            }
        cur.execute(
            """
            SELECT s.account_id::text AS account_id,
                   s.state_id::text AS state_id,
                   s.namespace,
                   s.value_jsonb,
                   s.revision
             FROM extension_project_state s
             WHERE s.extension_id = %s
               AND (
                   s.extension_install_id = %s
                   OR (
                       s.extension_install_id IS NULL
                       AND s.connection_id IN (
                           SELECT connection_id
                             FROM project_connections
                            WHERE extension_install_id = %s
                       )
                   )
               )
             ORDER BY state_id
             LIMIT 500
            """,
            (extension_id, install_id, install_id),
        )
        for row in cur.fetchall() or []:
            row = _row_dict(
                row,
                ("account_id", "state_id", "namespace", "value_jsonb", "revision"),
            )
            if not row:
                continue
            record_id = str(row.get("state_id") or "").strip()
            value = dict(row.get("value_jsonb") or {})
            snapshot["records"].append(
                {
                    "target_type": "project_state",
                    "record": {
                        "account_id": str(row.get("account_id") or "").strip(),
                        "state_id": record_id,
                        "namespace": str(row.get("namespace") or "").strip(),
                        "value": value,
                    },
                }
            )
            snapshot["by_id"][("project_state", record_id)] = {
                "value": value,
                "revision": int(row.get("revision") or 1),
                "account_id": str(row.get("account_id") or "").strip(),
                "extension_id": extension_id,
            }
    if len(snapshot["records"]) > 500:
        raise ValueError("Data migration snapshot exceeds 500 records")
    return snapshot


def apply_extension_data_migrations(
    cur: Any,
    *,
    account_id: str,
    install_id: str,
    snapshot: dict[str, Any],
    migrated: list[dict[str, Any]],
    target_version: int,
) -> dict[str, Any]:
    """Recheck snapshot revisions and atomically persist migration output."""
    by_id = dict(snapshot.get("by_id") or {})
    extension_id = str(snapshot.get("extension_id") or "").strip()
    expected_connection_ids = {
        record_id
        for (target_type, record_id), _record in by_id.items()
        if target_type == "project_connection_config"
    }
    expected_workspace_connection_ids = {
        record_id
        for (target_type, record_id), _record in by_id.items()
        if target_type == "workspace_data_connection_config"
    }
    expected_state_ids = {
        record_id
        for (target_type, record_id), _record in by_id.items()
        if target_type == "project_state"
    }
    cur.execute(
        "SELECT connection_id FROM project_connections WHERE extension_install_id = %s FOR UPDATE",
        (install_id,),
    )
    if {
        str(_row_dict(row, ("connection_id",)).get("connection_id") or "")
        for row in cur.fetchall() or []
    } != expected_connection_ids:
        raise ValueError("Project connections changed during migration")
    cur.execute(
        """
        SELECT connection_id
          FROM workspace_data_connections
         WHERE deleted_at IS NULL
           AND connector_ref_jsonb->>'install_id' = %s
         FOR UPDATE
        """,
        (install_id,),
    )
    if {
        str(_row_dict(row, ("connection_id",)).get("connection_id") or "")
        for row in cur.fetchall() or []
    } != expected_workspace_connection_ids:
        raise ValueError("Workspace data connections changed during migration")
    if extension_id:
        cur.execute(
            """
            SELECT state_id::text AS state_id
              FROM extension_project_state s
             WHERE s.extension_id = %s
               AND (
                   s.extension_install_id = %s
                   OR (
                       s.extension_install_id IS NULL
                       AND s.connection_id IN (
                           SELECT connection_id FROM project_connections WHERE extension_install_id = %s
                       )
                   )
               )
             FOR UPDATE
            """,
            (extension_id, install_id, install_id),
        )
        if {
            str(_row_dict(row, ("state_id",)).get("state_id") or "") for row in cur.fetchall() or []
        } != expected_state_ids:
            raise ValueError("Extension project state changed during migration")
    install_config: dict[str, Any] = {}
    for row in migrated:
        target_type = str(row.get("target_type") or "").strip()
        record_id = str(row.get("record_id") or "").strip()
        expected = dict(by_id.get((target_type, record_id)) or {})
        if not expected:
            raise ValueError("Migration output contains an unknown record")
        value = validate_migration_result({"value": row.get("value")})
        record_account_id = str(expected.get("account_id") or account_id).strip()
        if target_type == "install_config":
            cur.execute(
                """
                SELECT config_jsonb, applied_data_schema_version
                  FROM extension_installs
                 WHERE account_id::text = %s AND install_id = %s
                 FOR UPDATE
                """,
                (record_account_id, install_id),
            )
            current = _row_dict(cur.fetchone(), ("config_jsonb", "applied_data_schema_version"))
            if (
                not current
                or dict(current.get("config_jsonb") or {}) != expected["value"]
                or int(current.get("applied_data_schema_version") or 1) != int(expected["revision"])
            ):
                raise ValueError("Extension install configuration changed during migration")
            cur.execute(
                """
                UPDATE extension_installs
                   SET config_jsonb = %s::jsonb,
                       applied_data_schema_version = %s
                 WHERE account_id::text = %s AND install_id = %s
                """,
                (json.dumps(value), int(target_version), record_account_id, install_id),
            )
            install_config = value
        elif target_type == "project_connection_config":
            cur.execute(
                """
                SELECT connection_config, secret_refs, credential_revision
                  FROM project_connections
                 WHERE account_id::text = %s
                   AND connection_id = %s
                   AND extension_install_id = %s
                 FOR UPDATE
                """,
                (record_account_id, record_id, install_id),
            )
            current = _row_dict(
                cur.fetchone(), ("connection_config", "secret_refs", "credential_revision")
            )
            if (
                not current
                or dict(current.get("connection_config") or {}) != expected["value"]
                or dict(current.get("secret_refs") or {}) != expected["secret_refs"]
                or int(current.get("credential_revision") or 1) != int(expected["revision"])
            ):
                raise ValueError("Project connection changed during migration")
            cur.execute(
                "UPDATE project_connections SET connection_config = %s::jsonb, updated_at = now() WHERE account_id::text = %s AND connection_id = %s AND extension_install_id = %s",
                (json.dumps(value), record_account_id, record_id, install_id),
            )
        elif target_type == "workspace_data_connection_config":
            cur.execute(
                """
                SELECT connection_config_jsonb, secret_refs_jsonb, updated_at
                  FROM workspace_data_connections
                 WHERE account_id::text = %s
                   AND connection_id = %s
                   AND deleted_at IS NULL
                   AND connector_ref_jsonb->>'install_id' = %s
                 FOR UPDATE
                """,
                (record_account_id, record_id, install_id),
            )
            current = _row_dict(
                cur.fetchone(), ("connection_config_jsonb", "secret_refs_jsonb", "updated_at")
            )
            if (
                not current
                or dict(current.get("connection_config_jsonb") or {}) != expected["value"]
                or dict(current.get("secret_refs_jsonb") or {}) != expected["secret_refs"]
                or str(current.get("updated_at") or "") != str(expected["revision"] or "")
            ):
                raise ValueError("Workspace data connection changed during migration")
            cur.execute(
                """
                UPDATE workspace_data_connections
                   SET connection_config_jsonb = %s::jsonb, updated_at = now()
                 WHERE account_id::text = %s
                   AND connection_id = %s
                   AND connector_ref_jsonb->>'install_id' = %s
                """,
                (json.dumps(value), record_account_id, record_id, install_id),
            )
        elif target_type == "project_state":
            extension_id = str(expected.get("extension_id") or "").strip()
            cur.execute(
                "SELECT value_jsonb, revision FROM extension_project_state WHERE account_id::text = %s AND state_id::text = %s AND extension_id = %s FOR UPDATE",
                (record_account_id, record_id, extension_id),
            )
            current = _row_dict(cur.fetchone(), ("value_jsonb", "revision"))
            if (
                not current
                or dict(current.get("value_jsonb") or {}) != expected["value"]
                or int(current.get("revision") or 1) != int(expected["revision"])
            ):
                raise ValueError("Extension project state changed during migration")
            if value != expected["value"]:
                cur.execute(
                    "UPDATE extension_project_state SET value_jsonb = %s::jsonb, revision = revision + 1, updated_at = now() WHERE account_id::text = %s AND state_id::text = %s AND extension_id = %s",
                    (json.dumps(value), record_account_id, record_id, extension_id),
                )
    return install_config
