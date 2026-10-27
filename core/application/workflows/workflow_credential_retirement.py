"""Conservative current-consumer decision for exact entrypoint credentials.

The single inventory statement is the linearization point for reference-only
saves. A saved literal does not lease usable authority (ADR 0041).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.workflows.project_credential_versions import guard_project_credential_versions
from core.application.workflows.workflow_entrypoint_repository import _row_to_dict

# No ciphertext is read into the application inventory.
_INVENTORY_SQL = """
WITH scope AS (SELECT %s::uuid[] AS projects, %s::uuid[] AS accounts)
SELECT 'version' AS kind,
       jsonb_build_object('id',s.id,'project_id',s.project_id,'account_id',p.account_id,
                          'secret_key',s.secret_key,'active',s.active,'metadata',s.metadata_jsonb) AS data
  FROM project_secrets s JOIN projects p ON p.id=s.project_id, scope
 WHERE s.project_id=ANY(scope.projects)
UNION ALL
SELECT 'entrypoint', to_jsonb(e) FROM workflow_entrypoints e, scope
 WHERE e.project_id=ANY(scope.projects)
UNION ALL
SELECT 'callback', to_jsonb(c) FROM extension_callback_registrations c, scope
 WHERE c.project_id=ANY(scope.projects)
UNION ALL
SELECT 'step', jsonb_build_object('workflow_id',w.workflow_id,'account_id',w.account_id,
                                 'step_kind',s.step_kind,'override',s.override)
  FROM registry_workflow_steps s JOIN registry_workflows w ON w.workflow_id=s.owner_workflow_id, scope
 WHERE w.account_id IS NULL OR w.account_id=ANY(scope.accounts)
UNION ALL
SELECT 'projection', jsonb_build_object('account_id',p.account_id,'definition',p.projection_jsonb)
  FROM extension_registry_projections p, scope
 WHERE p.artifact_kind='workflow' AND p.account_id=ANY(scope.accounts)
UNION ALL
SELECT 'pin', jsonb_build_object('account_id',j.account_id,'project_id',j.project_id,'definition',s.definition_jsonb)
  FROM jobs j LEFT JOIN workflow_definition_snapshots s ON s.id=j.workflow_definition_snapshot_id, scope
 WHERE j.project_id=ANY(scope.projects) AND j.deleted_at IS NULL
   AND lower(coalesce(j.status,'')) <> ALL(ARRAY['cancelled','completed','failed','max_iterations','triage_needed'])
"""


def entrypoint_credential_refs(entrypoint: Mapping[str, Any]) -> set[str]:
    auth = entrypoint.get("auth_config_jsonb")
    values = [entrypoint.get("secret_ref")]
    if isinstance(auth, Mapping):
        values.extend(auth.get(key) for key in ("secret_ref", "password_secret_ref"))
    return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def _known_auth_reference_shape(auth: Any) -> bool:
    if not isinstance(auth, Mapping):
        return False
    return all(
        auth.get(name) is None or isinstance(auth.get(name), str)
        for name in ("secret_ref", "password_secret_ref", "capability_token_ref")
    )


def _contains_ref(value: Any, key: str) -> bool:
    if isinstance(value, str):
        return value.strip() in {key, "secret:" + key}
    if isinstance(value, Mapping):
        return any(_contains_ref(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_ref(item, key) for item in value)
    return False


def _http_consumes(step: Any, key: str) -> bool:
    if not isinstance(step, Mapping):
        return True
    if _contains_ref(step, key):
        return True
    if step.get("step_kind") != "http_request":
        return False
    override = step.get("override")
    if not isinstance(override, Mapping):
        return True
    config = override.get("http_request")
    if not isinstance(config, Mapping):
        return True
    mode, ref = config.get("auth_mode", "none"), config.get("auth_ref", "")
    if not isinstance(mode, str) or not isinstance(ref, str):
        return True
    if mode.strip().lower() in {"", "none"}:
        return False
    if mode.strip().lower() not in {"basic", "bearer"}:
        return True
    parts = ref.partition(":")[::2] if mode.strip().lower() == "basic" else (ref,)
    return any(part.strip().removeprefix("secret:").strip() == key for part in parts)


def _definition_consumes(definition: Any, key: str) -> bool:
    if not isinstance(definition, Mapping) or not isinstance(definition.get("steps"), list):
        return True
    return _contains_ref(definition, key) or any(
        _http_consumes(step, key) for step in definition["steps"]
    )


def _shared(
    rows: Sequence[dict[str, Any]],
    *,
    project: str,
    account: str,
    key: str,
    retired_ids: set[str],
    retired_workflow: str,
) -> bool:
    for item in rows:
        kind, data = item["kind"], item["data"]
        if kind == "version":
            continue
        if kind in {"entrypoint", "callback", "pin"} and str(data.get("project_id")) != project:
            continue
        if kind in {"step", "projection", "pin"} and data.get("account_id") not in (None, account):
            continue
        if kind == "entrypoint":
            if str(data.get("entrypoint_id")) in retired_ids:
                continue
            if not _known_auth_reference_shape(data.get("auth_config_jsonb")) or _contains_ref(
                data, key
            ):
                return True
        elif kind == "callback":
            auth = data.get("auth_config_jsonb")
            if not _known_auth_reference_shape(auth) or _contains_ref(auth, key):
                return True
        elif kind == "step":
            if data.get("workflow_id") != retired_workflow and _http_consumes(data, key):
                return True
        elif kind in {"projection", "pin"} and _definition_consumes(data.get("definition"), key):
            return True
    return False


def retire_entrypoint_credentials(
    cursor: Any, entrypoints: Sequence[dict[str, Any]], *, retired_workflow: str = ""
) -> None:
    candidates = sorted(
        {
            (str(row.get("account_id") or ""), str(row.get("project_id") or ""), ref)
            for row in entrypoints
            for ref in entrypoint_credential_refs(row)
            if row.get("account_id") and row.get("project_id")
        }
    )
    if not candidates:
        return
    for _account, project, key in candidates:
        guard_project_credential_versions(cursor, project_id=project, secret_key=key)
    # Refresh every key after the complete guard set is held. No retry after 55P03.
    for _account, project, key in candidates:
        guard_project_credential_versions(cursor, project_id=project, secret_key=key)
    cursor.execute(
        _INVENTORY_SQL,
        (sorted({p for _, p, _ in candidates}), sorted({a for a, _, _ in candidates})),
    )
    rows = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
    retired_ids = {str(row["entrypoint_id"]) for row in entrypoints}
    accepted = []
    for account, project, key in candidates:
        versions = [
            row["data"]
            for row in rows
            if row["kind"] == "version"
            and str(row["data"].get("project_id")) == project
            and row["data"].get("secret_key") == key
        ]
        # Any incompatible retained version protects the key, even when inactive.
        if not versions or any(
            v.get("metadata") != {} or str(v.get("account_id")) != account for v in versions
        ):
            continue
        if any(
            not _known_auth_reference_shape(row.get("auth_config_jsonb"))
            for row in entrypoints
            if str(row.get("project_id")) == project
        ):
            continue
        if _shared(
            rows,
            project=project,
            account=account,
            key=key,
            retired_ids=retired_ids,
            retired_workflow=retired_workflow,
        ):
            continue
        accepted.extend(str(v["id"]) for v in versions if v.get("active") is True)
    if accepted:
        cursor.execute(
            "UPDATE project_secrets SET active=false, updated_at=now() WHERE id=ANY(%s::uuid[]) AND active=true",
            (accepted,),
        )
