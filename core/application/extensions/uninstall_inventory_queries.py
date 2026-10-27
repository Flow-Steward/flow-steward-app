from __future__ import annotations

from typing import Any

from core.application.settings.llm_provider_extension_service import credential_ref_key


def uninstall_inventory_row(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row, strict=False))


def _append_query_rows(
    *, bl: Any, cur: Any, remove: list[dict[str, Any]], queries: tuple[Any, ...]
) -> None:
    for table_name, kind, query, params, columns in queries:
        if not getattr(bl, "_table_exists", lambda *_args: False)(table_name):
            continue
        cur.execute(query, params)
        for raw in cur.fetchall() or []:
            item = uninstall_inventory_row(raw, columns)
            stable_id = str(item.get("id") or "").strip()
            if stable_id:
                remove.append(
                    {
                        "kind": kind,
                        "id": stable_id,
                        "display_name": str(item.get("display_name") or stable_id).strip(),
                        "count": 1,
                    }
                )


def _collect_standard_rows(
    *,
    bl: Any,
    cur: Any,
    extension_id: str,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    remove: list[dict[str, Any]],
) -> None:
    usage_scope = "" if include_consumer_accounts else "account_id::text = %s AND "
    usage_params = (install_id,) if include_consumer_accounts else (account_id, install_id)
    queries = (
        (
            "extension_entitlements",
            "entitlement_record",
            "SELECT entitlement_id::text AS id, status AS display_name FROM extension_entitlements WHERE account_id::text = %s AND install_id = %s",
            (account_id, install_id),
            ("id", "display_name"),
        ),
        (
            "extension_usage_events",
            "usage_record",
            f"SELECT usage_event_id::text AS id, event_kind AS display_name FROM extension_usage_events WHERE {usage_scope}install_id = %s",  # nosec B608 -- scope is selected from fixed internal predicates.
            usage_params,
            ("id", "display_name"),
        ),
        (
            "extension_bundles",
            "bundle_record",
            "SELECT id::text AS id, display_name FROM extension_bundles WHERE extension_key = %s",
            (extension_id,),
            ("id", "display_name"),
        ),
        (
            "extension_versions",
            "version_record",
            "SELECT ev.id::text AS id, ev.version AS display_name FROM extension_versions ev JOIN extension_bundles eb ON eb.id = ev.bundle_id WHERE eb.extension_key = %s",
            (extension_id,),
            ("id", "display_name"),
        ),
        (
            "enabled_extensions",
            "enabled_state_record",
            "SELECT ee.id::text AS id, ee.enabled::text AS display_name FROM enabled_extensions ee JOIN extension_bundles eb ON eb.id = ee.bundle_id WHERE eb.extension_key = %s",
            (extension_id,),
            ("id", "display_name"),
        ),
        (
            "extension_invocation_logs",
            "invocation_log",
            "SELECT eil.id::text AS id, eil.invocation_type AS display_name FROM extension_invocation_logs eil JOIN extension_bundles eb ON eb.id = eil.bundle_id WHERE eb.extension_key = %s",
            (extension_id,),
            ("id", "display_name"),
        ),
    )
    _append_query_rows(bl=bl, cur=cur, remove=remove, queries=queries)


def _collect_credentials(
    *,
    bl: Any,
    cur: Any,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    remove: list[dict[str, Any]],
) -> None:
    if not getattr(bl, "_table_exists", lambda *_args: False)("secret_refs"):
        return
    clause = "" if include_consumer_accounts else "account_id::text = %s AND "
    base_ref = credential_ref_key(install_id)
    params = (
        (base_ref, base_ref, base_ref)
        if include_consumer_accounts
        else (account_id, base_ref, base_ref, base_ref)
    )
    cur.execute(
        f"SELECT COUNT(*) AS credential_count FROM secret_refs WHERE {clause}(ref_key = %s OR left(ref_key, length(%s) + 1) = %s || '_')",  # nosec B608 -- clause is selected from fixed internal predicates.
        params,
    )
    row = cur.fetchone()
    count = int((row.get("credential_count") if isinstance(row, dict) else row[0]) or 0)
    if count:
        remove.append(
            {
                "kind": "credential_ref",
                "id": f"{install_id}:llm_provider",
                "display_name": "LLM provider credential",
                "count": count,
            }
        )


def _collect_callbacks(
    *,
    bl: Any,
    cur: Any,
    extension_id: str,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    remove: list[dict[str, Any]],
) -> None:
    if not getattr(bl, "_table_exists", lambda *_args: False)("extension_callback_registrations"):
        return
    clause, scope_params = callback_ownership_predicate(
        include_consumer_accounts=include_consumer_accounts,
        install_id=install_id,
        account_id=account_id,
    )
    params = (extension_id, *scope_params)
    cur.execute(
        f"SELECT c.id::text AS id, c.callback_id, c.project_id::text AS project_id, p.name AS project_name FROM extension_callback_registrations c JOIN projects p ON p.id = c.project_id WHERE c.extension_id = %s {clause}",  # nosec B608 -- clause is selected from fixed internal predicates.
        params,
    )
    for raw in cur.fetchall() or []:
        item = uninstall_inventory_row(raw, ("id", "callback_id", "project_id", "project_name"))
        remove.append(
            {
                "kind": "callback_registration",
                "id": str(item.get("id") or "").strip(),
                "display_name": str(item.get("callback_id") or "").strip(),
                "project_id": str(item.get("project_id") or "").strip(),
                "project_name": str(item.get("project_name") or "").strip(),
                "count": 1,
            }
        )


def callback_ownership_predicate(
    *, include_consumer_accounts: bool, install_id: str, account_id: str
) -> tuple[str, tuple[str, ...]]:
    """Return the shared exact-install callback inventory/cleanup predicate."""
    if not include_consumer_accounts:
        return "AND p.account_id::text = %s", (account_id,)
    return (
        """AND (
               EXISTS (
                   SELECT 1
                     FROM project_connections AS owned_connection
                    WHERE owned_connection.connection_id = c.connection_id
                      AND owned_connection.extension_install_id = %s
               )
               OR NOT EXISTS (
                   SELECT 1
                     FROM extension_installs AS surviving_install
                    WHERE surviving_install.extension_id = c.extension_id
                      AND surviving_install.account_id = p.account_id
                      AND surviving_install.install_id <> %s
               )
           )""",
        (install_id, install_id),
    )


def _collect_provider_sources(
    *,
    bl: Any,
    cur: Any,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    remove: list[dict[str, Any]],
) -> None:
    if not getattr(bl, "_table_exists", lambda *_args: False)("llm_provider_sources"):
        return
    clause = "" if include_consumer_accounts else "account_id = %s AND"
    params = (install_id,) if include_consumer_accounts else (account_id, install_id)
    cur.execute(
        f"SELECT id::text AS id, provider_key, display_label FROM llm_provider_sources WHERE {clause} extension_install_id = %s",  # nosec B608 -- clause is selected from fixed internal predicates.
        params,
    )
    for raw in cur.fetchall() or []:
        item = uninstall_inventory_row(raw, ("id", "provider_key", "display_label"))
        remove.append(
            {
                "kind": "llm_provider_source",
                "id": str(item.get("id") or "").strip(),
                "display_name": str(
                    item.get("display_label") or item.get("provider_key") or ""
                ).strip(),
                "count": 1,
            }
        )


def _collect_project_state(
    *, bl: Any, cur: Any, install_id: str, remove: list[dict[str, Any]]
) -> None:
    if not getattr(bl, "_table_exists", lambda *_args: False)("extension_project_state"):
        return
    cur.execute(
        "SELECT s.state_id::text AS state_id, s.namespace, s.state_key, s.project_id::text AS project_id, p.name AS project_name FROM extension_project_state s LEFT JOIN projects p ON p.id = s.project_id WHERE s.extension_install_id = %s",
        (install_id,),
    )
    for raw in cur.fetchall() or []:
        item = uninstall_inventory_row(
            raw, ("state_id", "namespace", "state_key", "project_id", "project_name")
        )
        display_name = "/".join(
            filter(
                None,
                (
                    str(item.get("namespace") or "").strip(),
                    str(item.get("state_key") or "").strip(),
                ),
            )
        )
        remove.append(
            {
                "kind": "project_state",
                "id": str(item.get("state_id") or "").strip(),
                "display_name": display_name,
                "project_id": str(item.get("project_id") or "").strip(),
                "project_name": str(item.get("project_name") or "").strip(),
                "count": 1,
            }
        )


def _collect_retained_audit(
    *,
    bl: Any,
    cur: Any,
    install_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    retained: list[dict[str, Any]],
) -> None:
    if not getattr(bl, "_table_exists", lambda *_args: False)("external_effects"):
        return
    clause = "" if include_consumer_accounts else "account_id = %s AND"
    params = (install_id,) if include_consumer_accounts else (account_id, install_id)
    cur.execute(
        f"SELECT COUNT(*) AS audit_count FROM external_effects WHERE {clause} extension_install_id = %s",  # nosec B608 -- clause is selected from fixed internal predicates.
        params,
    )
    row = cur.fetchone()
    count = int((row.get("audit_count") if isinstance(row, dict) else row[0]) or 0)
    if count:
        retained.append(
            {
                "kind": "external_effect_audit",
                "id": install_id,
                "display_name": "Redacted external-effect audit history",
                "count": count,
            }
        )


def collect_database_inventory(
    *,
    bl: Any,
    extension_id: str,
    install_id: str,
    release_id: str,
    account_id: str,
    include_consumer_accounts: bool,
    remove: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, bool]:
    retained: list[dict[str, Any]] = []
    shared_runtime_package = shared_release = False
    if not callable(getattr(bl, "cursor", None)):
        return retained, shared_runtime_package, shared_release
    with bl.cursor() as cur:
        if getattr(bl, "_table_exists", lambda *_args: False)("extension_installs"):
            cur.execute(
                "SELECT extension_id, release_id FROM extension_installs WHERE extension_id = %s AND install_id <> %s",
                (extension_id, install_id),
            )
            rows = [
                uninstall_inventory_row(row, ("extension_id", "release_id"))
                for row in cur.fetchall() or []
            ]
            shared_runtime_package = any(
                str(row.get("extension_id") or "").strip() == extension_id for row in rows
            )
            shared_release = any(
                str(row.get("release_id") or "").strip() == release_id for row in rows
            )
        common = {
            "bl": bl,
            "cur": cur,
            "install_id": install_id,
            "account_id": account_id,
            "include_consumer_accounts": include_consumer_accounts,
        }
        _collect_standard_rows(extension_id=extension_id, remove=remove, **common)
        _collect_credentials(remove=remove, **common)
        _collect_callbacks(extension_id=extension_id, remove=remove, **common)
        _collect_provider_sources(remove=remove, **common)
        _collect_project_state(bl=bl, cur=cur, install_id=install_id, remove=remove)
        _collect_retained_audit(retained=retained, **common)
    return retained, shared_runtime_package, shared_release


def artifact_display_name(payload: Any, fallback: str = "") -> str:
    """Return the name an extension gave one of its artifacts."""
    data = dict(payload or {}) if isinstance(payload, dict) else {}
    return str(
        data.get("display_name") or data.get("name") or data.get("title") or fallback or ""
    ).strip()


def project_assignments(bl: Any, projections: list[Any]) -> dict[str, dict[str, Any]]:
    """Which projects use the workflows/datasets this install projected, by name."""
    from core.application.extensions.projected_dataset_bridge import projection_bridge_id

    names: dict[str, dict[str, str]] = {"workflow": {}, "dataset": {}}
    for row in projections:
        kind = str(getattr(row, "artifact_kind", "") or "").strip()
        install_id = str(getattr(row, "install_id", "") or "").strip()
        artifact_id = str(getattr(row, "artifact_id", "") or "").strip()
        if kind in names and install_id and artifact_id:
            names[kind][projection_bridge_id(install_id, artifact_id)] = artifact_display_name(
                getattr(row, "projection", None)
            )
    result: dict[str, dict[str, Any]] = {
        kind: {"count": 0, "display_name": "", "project_names": []} for kind in names
    }
    if not callable(getattr(bl, "cursor", None)):
        return result
    with bl.cursor() as cur:
        for kind, table in (("workflow", "project_workflows"), ("dataset", "project_datasets")):
            if names[kind] and getattr(bl, "_table_exists", lambda *_args: False)(table):
                column = f"{kind}_id"
                cur.execute(
                    f"SELECT a.{column} AS artifact_ref, COALESCE(p.name, '') AS project_name FROM {table} a LEFT JOIN projects p ON p.id = a.project_id WHERE a.{column} = ANY(%s) ORDER BY p.name, a.{column}",  # nosec B608 -- identifiers come from the fixed kind/table mapping above.
                    (sorted(names[kind]),),
                )
                rows = [
                    uninstall_inventory_row(raw, ("artifact_ref", "project_name"))
                    for raw in cur.fetchall() or []
                ]
                result[kind] = _assignment_summary(rows, names[kind])
    return result


def _assignment_summary(
    rows: list[dict[str, Any]], artifact_names: dict[str, str]
) -> dict[str, Any]:
    by_artifact: dict[str, list[str]] = {}
    projects: list[str] = []
    for row in rows:
        artifact = artifact_names.get(str(row.get("artifact_ref") or "").strip(), "")
        project = str(row.get("project_name") or "").strip()
        by_artifact.setdefault(artifact, [])
        if project and project not in by_artifact[artifact]:
            by_artifact[artifact].append(project)
        if project and project not in projects:
            projects.append(project)
    parts = [
        f"{artifact} ({', '.join(names)})" if artifact and names else artifact or ", ".join(names)
        for artifact, names in sorted(by_artifact.items())
    ]
    return {
        "count": len(rows),
        "display_name": "; ".join(part for part in parts if part),
        "project_names": projects,
    }


def finalize_inventory(
    *,
    bl: Any,
    extension_id: str,
    install_id: str,
    release_id: str,
    include_consumer_accounts: bool,
    projections: list[Any],
    snapshots: list[Any],
    remove: list[dict[str, Any]],
    retained: list[dict[str, Any]],
    shared_runtime_package: bool,
    shared_release: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shared_kinds = {
        "runtime_package",
        "bundle_record",
        "version_record",
        "enabled_state_record",
        "invocation_log",
    }
    if shared_release:
        shared_kinds.add("release")
    if shared_runtime_package or shared_release:
        remove = [item for item in remove if item.get("kind") not in shared_kinds]
    if shared_runtime_package:
        retained.append(
            {
                "kind": "runtime_package",
                "id": extension_id,
                "display_name": "Shared runtime package",
                "count": 1,
            }
        )
    if shared_release:
        retained.append(
            {"kind": "release", "id": release_id, "display_name": "Shared release", "count": 1}
        )
    if include_consumer_accounts:
        remove = _aggregate_consumer_inventory(remove, install_id)
    assignments = project_assignments(bl, projections)
    remove.extend(
        {
            "kind": f"project_{kind}_assignment",
            "id": install_id,
            "display_name": summary["display_name"],
            "project_names": summary["project_names"],
            "count": summary["count"],
        }
        for kind, summary in assignments.items()
    )
    _append_snapshot_inventory(remove, snapshots)
    return remove, retained


def _aggregate_consumer_inventory(
    remove: list[dict[str, Any]], install_id: str
) -> list[dict[str, Any]]:
    kinds = {
        "callback_registration": "consumer_callback_registrations",
        "llm_provider_source": "consumer_llm_provider_sources",
        "project_state": "consumer_project_state",
        "usage_record": "consumer_usage_records",
    }
    grouped: dict[str, int] = {}
    safe: list[dict[str, Any]] = []
    for item in remove:
        aggregate = kinds.get(str(item.get("kind") or ""))
        if aggregate is None:
            safe.append(item)
        else:
            grouped[aggregate] = grouped.get(aggregate, 0) + int(item.get("count") or 0)
    safe.extend(
        {
            "kind": kind,
            "id": install_id,
            "display_name": kind.replace("_", " ").capitalize(),
            "count": count,
        }
        for kind, count in sorted(grouped.items())
        if count
    )
    return safe


def _append_snapshot_inventory(remove: list[dict[str, Any]], snapshots: list[Any]) -> None:
    for kind in ("workflow", "agent", "skill", "tool", "dataset", "saved_query"):
        for row in snapshots:
            if str(getattr(row, "artifact_kind", "") or "") != kind:
                continue
            artifact_id = str(getattr(row, "artifact_id", "") or "").strip()
            display_name = artifact_display_name(getattr(row, "snapshot", None), artifact_id)
            remove.append(
                {
                    "kind": kind,
                    "id": artifact_id,
                    "display_name": display_name or artifact_id,
                    "count": 1,
                }
            )
