"""Host-owned runtime rows for install-scoped extension projections."""

from __future__ import annotations

import logging
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.extensions.projection_dependency_availability import (
    active_extension_ids_for_account,
    active_extension_versions_for_account,
    missing_extension_describer,
    resolve_projection_dependency_availability,
)
from core.application.registry.catalog_categories import (
    DEFAULT_CATALOG_CATEGORY,
    known_catalog_category,
)

logger = logging.getLogger(__name__)


#: What a blocked workflow projection carries, as every availability payload names it.
_AVAILABILITY_KEYS = (
    "availability_status",
    "availability_reason_code",
    "availability_reason_text",
    "missing_extensions",
    "missing_extension_details",
    "new_runs_blocked",
    "visible_in_catalog",
)


def _projection_payload(projection: Any) -> dict[str, Any]:
    if hasattr(projection, "model_dump"):
        payload = projection.model_dump(mode="json")
        return payload if isinstance(payload, dict) else {}
    return dict(projection) if isinstance(projection, dict) else {}


def _runtime_store(db_store: Any) -> Any | None:
    runtime_store = getattr(db_store, "extension_runtime_store", None)
    if runtime_store is not None:
        return runtime_store

    dsn = str(getattr(db_store, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        from core.application.extensions.service_support import (
            get_default_extension_runtime_store,
        )
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        return get_default_extension_runtime_store(get_logger=lambda: PlatformIdentityStore(dsn))
    except Exception as exc:
        logger.debug("Projected artifact store unavailable: %s", exc)
        return None


def _active_projections(
    runtime_store: Any, *, account_id: str = DEFAULT_ACCOUNT_ID
) -> list[dict[str, Any]]:
    try:
        projections = runtime_store.list_projections(account_id=account_id, only_active=True)
    except Exception as exc:
        logger.debug("Projected artifact lookup failed: %s", exc)
        return []
    return [_projection_payload(projection) for projection in projections or []]


def _local_id(payload: dict[str, Any], id_key: str) -> str:
    snapshot = payload.get("projection")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    return str(snapshot.get(id_key) or payload.get("artifact_id") or "").strip()


def _durable_projection_install_id(row: dict[str, Any]) -> str:
    direct = str(row.get("source_install_id") or "").strip()
    if direct:
        return direct
    metadata = row.get("metadata_jsonb")
    metadata = metadata if isinstance(metadata, dict) else {}
    bridge = metadata.get("projection_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    install_id = str(bridge.get("install_id") or "").strip()
    if install_id:
        return install_id
    validation = row.get("validation_result_jsonb")
    validation = validation if isinstance(validation, dict) else {}
    bridge = validation.get("projection_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    return str(bridge.get("install_id") or "").strip()


def _install_reference_maps(
    rows: list[dict[str, Any]], install_id: str
) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    for artifact_kind, id_key in (
        ("agent", "agent_id"),
        ("skill", "skill_id"),
        ("tool", "tool_id"),
        ("workflow", "workflow_id"),
        ("dataset", "dataset_id"),
        ("saved_query", "query_template_id"),
    ):
        maps[artifact_kind] = {
            local_id: projection_bridge_id(install_id, local_id)
            for payload in rows
            if str(payload.get("artifact_kind") or "").strip() == artifact_kind
            if str(payload.get("install_id") or "").strip() == install_id
            for local_id in [_local_id(payload, id_key)]
            if local_id
        }
    return maps


def _scoped_reference(value: Any, references: dict[str, str]) -> str:
    normalized = str(value or "").strip()
    return references.get(normalized, normalized)


def _projected_agent_skills(values: list[Any], references: dict[str, str]) -> list[dict[str, Any]]:
    assignments: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        item = dict(value) if isinstance(value, dict) else {}
        raw_id = (item.get("skill_id") or item.get("id") or item.get("name")) if item else value
        skill_id = str(raw_id or "").strip()
        if not skill_id:
            continue
        assignments.append(
            {
                "skill_id": _scoped_reference(skill_id, references),
                "skill_prompt": str(item.get("skill_prompt") or ""),
                "sort_order": int(item.get("sort_order", index) or index),
            }
        )
    return assignments


def _projected_agent_tools(values: list[Any], references: dict[str, str]) -> list[dict[str, Any]]:
    assignments: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        item = dict(value) if isinstance(value, dict) else {}
        raw_id = (item.get("tool_id") or item.get("id") or item.get("name")) if item else value
        tool_id = str(raw_id or "").strip()
        if not tool_id:
            continue
        assignments.append(
            {
                "tool_id": _scoped_reference(tool_id, references),
                "sort_order": int(item.get("sort_order", index) or index),
            }
        )
    return assignments


def projected_artifact_rows(
    db_store: Any,
    artifact_kind: str,
    id_key: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> list[dict[str, Any]]:
    """Return active projections in the shape expected by the registry manifests.

    Only references which resolve to an active projection from the same install are
    namespaced.  A reference to a platform-owned artifact deliberately stays raw.

    A projection whose required extensions are missing blocks new runs. An agent,
    tool or dataset that cannot be used is left out. A workflow stays listed and
    resolvable by id, disabled and marked unavailable with the reason, so the
    user sees what to install; the run pin refuses it with that reason. Dropping
    it made the workflow vanish from the list and its page answer 404, with
    nothing anywhere saying why.
    """
    runtime_store = _runtime_store(db_store)
    if runtime_store is None:
        return []

    normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    all_rows = _active_projections(runtime_store, account_id=normalized_account_id)
    active_extension_ids = active_extension_ids_for_account(
        db_store, account_id=normalized_account_id
    )
    active_extension_versions = active_extension_versions_for_account(
        db_store, account_id=normalized_account_id
    )
    describe_missing = missing_extension_describer(db_store, account_id=normalized_account_id)
    rows: list[dict[str, Any]] = []
    for payload in all_rows:
        if str(payload.get("artifact_kind") or "").strip() != artifact_kind:
            continue
        snapshot = payload.get("projection")
        snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        install_id = str(payload.get("install_id") or "").strip()
        local_id = str(snapshot.get(id_key) or payload.get("artifact_id") or "").strip()
        if not install_id or not local_id:
            continue

        availability = resolve_projection_dependency_availability(
            snapshot,
            active_extension_ids=active_extension_ids,
            active_extension_versions=active_extension_versions,
            describe_missing=describe_missing,
        )
        blocked = bool(availability.get("new_runs_blocked"))
        if blocked and artifact_kind != "workflow":
            continue
        references = _install_reference_maps(all_rows, install_id)
        snapshot[id_key] = projection_bridge_id(install_id, local_id)
        snapshot["account_id"] = normalized_account_id
        snapshot["source_install_id"] = install_id
        if artifact_kind in {"dataset", "saved_query"}:
            snapshot["source_release_id"] = payload.get("release_id")
        else:
            snapshot.setdefault("source_release_id", payload.get("release_id"))
        if blocked:
            snapshot["enabled"] = False
            for key in _AVAILABILITY_KEYS:
                if key in availability:
                    snapshot[key] = availability[key]

        if artifact_kind in {"agent", "tool", "skill"}:
            # An extension files what it ships under the shared list too; a
            # category outside it reads as the default rather than leaking out.
            snapshot["category"] = (
                known_catalog_category(snapshot.get("category")) or DEFAULT_CATALOG_CATEGORY
            )
        if artifact_kind == "agent":
            raw_skills = list(snapshot.get("skills") or [])
            if not raw_skills and snapshot.get("base_skill"):
                raw_skills = [snapshot["base_skill"]]
            skills = _projected_agent_skills(raw_skills, references["skill"])
            if snapshot.get("base_skill"):
                snapshot["base_skill"] = _scoped_reference(
                    snapshot["base_skill"], references["skill"]
                )
            tools = _projected_agent_tools(list(snapshot.get("tools") or []), references["tool"])
            snapshot["_skills"] = skills
            snapshot["skills"] = skills
            snapshot["_tools"] = tools
            snapshot["tools"] = tools
        elif artifact_kind == "workflow":
            steps = snapshot.get("steps")
            if isinstance(steps, list):
                snapshot["steps"] = _projected_workflow_steps(steps, references, install_id)
        elif artifact_kind == "saved_query" and snapshot.get("dataset_id"):
            snapshot["dataset_id"] = _scoped_reference(
                snapshot["dataset_id"],
                references["dataset"],
            )

        rows.append(snapshot)
    return rows


def _projected_workflow_steps(
    steps: list[Any],
    references: dict[str, dict[str, str]],
    install_id: str,
) -> list[dict[str, Any]]:
    """Namespace the install-local references of a projected workflow's own steps."""
    projected_steps: list[dict[str, Any]] = []
    for step_index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        projected_step = {**step, "sort_order": step.get("sort_order", step_index)}
        if projected_step.get("agent_id"):
            projected_step["agent_id"] = _scoped_reference(
                projected_step["agent_id"], references["agent"]
            )
        if projected_step.get("agent"):
            projected_step["agent"] = _scoped_reference(
                projected_step["agent"], references["agent"]
            )
        if projected_step.get("skill_id"):
            projected_step["skill_id"] = _scoped_reference(
                projected_step["skill_id"], references["skill"]
            )
        if projected_step.get("workflow_id"):
            projected_step["workflow_id"] = _scoped_reference(
                projected_step["workflow_id"], references["workflow"]
            )
        override = projected_step.get("override")
        if isinstance(override, dict):
            invoke = override.get("invoke_workflow")
            if isinstance(invoke, dict) and invoke.get("workflow_id"):
                invoke = dict(invoke)
                invoke["workflow_id"] = _scoped_reference(
                    invoke["workflow_id"], references["workflow"]
                )
                projected_step["override"] = {**override, "invoke_workflow": invoke}
        if projected_step.get("connector_id"):
            connector_id = str(projected_step["connector_id"])
            connector_local_id = connector_id.removeprefix("connector.")
            resolved_connector_id = _scoped_reference(connector_local_id, references["tool"])
            projected_step["connector_id"] = resolved_connector_id
            if connector_local_id in references["tool"] or projected_step.get(
                "connector_install_id"
            ):
                projected_step["connector_install_id"] = install_id
        elif projected_step.get("connector_install_id"):
            projected_step["connector_install_id"] = install_id
        projected_steps.append(projected_step)
    return projected_steps


def _dataset_host_fields(
    projection: dict[str, Any], durable: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep package content, restoring only a matching host bridge and assignments."""
    row = dict(projection)
    metadata = row.get("metadata_jsonb") or row.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.pop("projection_bridge", None)
    metadata["account_id"] = row["account_id"]
    row.pop("metadata", None)
    row.update(
        workspace_id=None,
        availability_mode="specific_projects_only",
        project_ids=[],
        metadata_jsonb=metadata,
    )
    durable = durable or {}
    durable_metadata = durable.get("metadata_jsonb") or durable.get("metadata")
    bridge = (
        durable_metadata.get("projection_bridge") if isinstance(durable_metadata, dict) else None
    )
    if not isinstance(bridge, dict):
        return row
    install_id = str(row.get("source_install_id") or "").strip()
    artifact_id = str(bridge.get("artifact_id") or "").strip()
    release_id = str(row.get("source_release_id") or "").strip()
    if (
        bridge.get("immutable") is True
        and bridge.get("artifact_kind") == "dataset"
        and bridge.get("install_id") == install_id
        and release_id
        and bridge.get("release_id") == release_id
        and durable.get("account_id") == row["account_id"]
        and durable.get("dataset_id") == row["dataset_id"]
        and artifact_id
        and projection_bridge_id(install_id, artifact_id) == row["dataset_id"]
    ):
        metadata["projection_bridge"] = dict(bridge)
        row["project_ids"] = list(durable.get("project_ids") or [])
    return row


def projection_aware_catalog_rows(
    db_store: Any,
    artifact_kind: str,
    id_key: str,
    manual_rows: list[Any] | None,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Merge host-owned rows with active install-scoped projection rows."""
    projected_rows = projected_artifact_rows(
        db_store,
        artifact_kind,
        id_key,
        account_id=account_id,
    )
    active_projection_ids = {
        str(row.get(id_key) or "").strip()
        for row in projected_rows
        if str(row.get(id_key) or "").strip()
    }
    rows_by_id: dict[str, dict[str, Any]] = {}
    for payload in manual_rows or []:
        row = dict(payload) if isinstance(payload, dict) else {}
        artifact_id = str(row.get(id_key) or "").strip()
        if _durable_projection_install_id(row) and artifact_id not in active_projection_ids:
            continue
        if artifact_id:
            rows_by_id[artifact_id] = row
    for row in projected_rows:
        artifact_id = str(row.get(id_key) or "").strip()
        if artifact_id:
            rows_by_id[artifact_id] = (
                _dataset_host_fields(row, rows_by_id.get(artifact_id))
                if artifact_kind == "dataset"
                else dict(row)
            )
    return [
        row for row in rows_by_id.values() if not enabled_only or bool(row.get("enabled", True))
    ]


def projection_aware_catalog_row(
    db_store: Any,
    artifact_kind: str,
    id_key: str,
    artifact_id: str,
    manual_row: Any,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict[str, Any] | None:
    """Resolve one host-owned or active install-scoped public catalog row."""
    normalized_id = str(artifact_id or "").strip()
    return next(
        (
            row
            for row in projection_aware_catalog_rows(
                db_store,
                artifact_kind,
                id_key,
                [manual_row] if manual_row else [],
                account_id=account_id,
            )
            if str(row.get(id_key) or "").strip() == normalized_id
        ),
        None,
    )


def projected_catalog_row_matches(
    row: dict[str, Any],
    query: str,
    *,
    fields: tuple[str, ...],
) -> bool:
    """Match projected catalog rows which are absent from host-table search indexes."""
    if not str(row.get("source_install_id") or "").strip() and not any(
        str(row.get(key) or "").startswith("extension.")
        for key in ("workflow_id", "agent_id", "skill_id", "tool_id", "dataset_id")
    ):
        return False
    normalized_query = str(query or "").strip().casefold()
    if not normalized_query:
        return True
    return normalized_query in " ".join(str(row.get(field) or "") for field in fields).casefold()


def guardrail_assignable_catalog_rows(
    db_store: Any,
    visible_rows: list[dict[str, Any]],
    *,
    account_id: str,
) -> list[dict[str, Any]]:
    """Restrict an already scoped, merged and searched agent catalog to FK targets.

    Display provenance is not eligibility: active projections can replace a
    durable row's display content, while virtual-only projections have no FK.
    """
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id or not visible_rows:
        return []
    candidate_ids = [str(row.get("agent_id") or "").strip() for row in visible_rows]
    eligible_ids = db_store.guardrail_assignable_agent_ids(
        candidate_ids, account_id=normalized_account_id
    )
    return [row for row in visible_rows if str(row.get("agent_id") or "").strip() in eligible_ids]
