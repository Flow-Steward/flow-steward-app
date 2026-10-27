#!/usr/bin/env python3
"""Fail a release candidate when smoke-owned product or queue residue remains."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

FIXTURE_REGISTRY = Path(__file__).with_name("fixture_families.json")


def _required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"required residue-check input is missing: {name}")
    return value


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"residue probe failed ({command[2]}): {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def _load_fixture_registry() -> dict[str, list[str]]:
    payload = json.loads(FIXTURE_REGISTRY.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "flow-steward-smoke-fixtures-v1":
        raise RuntimeError("unsupported smoke fixture registry schema")
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    if any(
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value for value in values)
        for values in fields.values()
    ):
        raise RuntimeError("smoke fixture registry fields must be non-empty string arrays")
    return fields


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _prefix_predicate(column: str, prefixes: list[str]) -> str:
    return (
        "("
        + " or ".join(f"{column} like {_sql_literal(prefix + '%')}" for prefix in prefixes)
        + ")"
    )


def _value_predicate(column: str, values: list[str]) -> str:
    return f"{column} in (" + ",".join(_sql_literal(value) for value in values) + ")"


def _database_counts() -> dict[str, int]:
    registry = _load_fixture_registry()
    fixture_project = _prefix_predicate("name", registry["project_name_prefixes"])
    joined_fixture_project = _prefix_predicate("p.name", registry["project_name_prefixes"])
    fixture_jobs = (
        "("
        + " or ".join(
            (
                _prefix_predicate("task_id", registry["job_task_id_prefixes"]),
                _prefix_predicate("task_summary", registry["job_task_summary_prefixes"]),
            )
        )
        + ")"
    )
    fixture_connections = _prefix_predicate("pc.connection_id", registry["connection_id_prefixes"])
    fixture_token_labels = _prefix_predicate("label", registry["personal_token_label_prefixes"])
    verifier_token_hash = hashlib.sha256(
        _required("FS_SMOKE_BEARER_TOKEN").encode("utf-8")
    ).hexdigest()
    fixture_service_accounts = _prefix_predicate(
        "sa.service_account_key", registry["service_account_key_prefixes"]
    )
    fixture_namespaces = _value_predicate("eps.namespace", registry["extension_state_namespaces"])
    registry_entities = (
        (
            "registry_workflows",
            "registry_workflows",
            _prefix_predicate("workflow_key", registry["registry_workflow_id_prefixes"]),
        ),
        (
            "registry_agents",
            "registry_agents",
            _prefix_predicate("agent_id", registry["registry_agent_id_prefixes"]),
        ),
        (
            "registry_skills",
            "registry_skills",
            _prefix_predicate("skill_id", registry["registry_skill_id_prefixes"]),
        ),
        (
            "registry_tools",
            "registry_tools",
            _prefix_predicate("tool_id", registry["registry_tool_id_prefixes"]),
        ),
        (
            "registry_guardrails",
            "registry_guardrails",
            _prefix_predicate("display_name", registry["registry_guardrail_name_prefixes"]),
        ),
        (
            "registry_data_contracts",
            "registry_data_contracts",
            _prefix_predicate(
                "contract_id",
                registry["registry_data_contract_id_prefixes"],
            ),
        ),
    )
    registry_counts_sql = "".join(
        (
            f",'{name}_active',(select count(*) from {table} "
            f"where {predicate} and deleted_at is null),"
            f"'{name}_trashed',(select count(*) from {table} "
            f"where {predicate} and deleted_at is not null)"
        )
        for name, table, predicate in registry_entities
    )
    sql = (
        "select json_build_object("
        f"'projects',(select count(*) from projects where {fixture_project}),"
        f"'jobs',(select count(*) from jobs where {fixture_jobs}),"
        f"'workflow_links',(select count(*) from project_workflows pw join projects p on p.id=pw.project_id where {joined_fixture_project}),"
        f"'dataset_links',(select count(*) from project_datasets pd join projects p on p.id=pd.project_id where {joined_fixture_project}),"
        f"'connections',(select count(*) from project_connections pc join projects p on p.id=pc.project_id where {joined_fixture_project} or {fixture_connections}),"
        f"'callbacks',(select count(*) from extension_callback_registrations cb join projects p on p.id=cb.project_id where {joined_fixture_project}),"
        f"'tokens',(select count(*) from personal_access_tokens where {fixture_token_labels} "
        f"and token_hash <> {_sql_literal(verifier_token_hash)} and not revoked),"
        f"'machine_tokens',(select count(*) from machine_tokens mt join service_accounts sa on sa.id=mt.service_account_id where {fixture_service_accounts} and not mt.revoked),"
        f"'extension_state',(select count(*) from extension_project_state eps left join projects p on p.id=eps.project_id where {fixture_namespaces} or {joined_fixture_project})"
        f"{registry_counts_sql}"
        ")::text"
    )
    raw = _run(
        [
            "docker",
            "exec",
            _required("FS_SMOKE_PG_CONTAINER"),
            "psql",
            "-U",
            str(os.environ.get("FS_SMOKE_PG_USER") or "flow_steward"),
            "-d",
            str(os.environ.get("FS_SMOKE_PG_DATABASE") or "flow_steward"),
            "-tAc",
            sql,
        ]
    )
    return {str(key): int(value) for key, value in json.loads(raw or "{}").items()}


def _queue_counts() -> dict[str, int]:
    raw = _run(
        [
            "docker",
            "exec",
            _required("FS_SMOKE_RABBITMQ_CONTAINER"),
            "rabbitmqctl",
            "list_queues",
            "-q",
            "name",
            "messages",
            "messages_unacknowledged",
        ]
    )
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[1].isdigit() or not fields[2].isdigit():
            continue
        rows.append(
            {
                "name": fields[0],
                "messages": int(fields[1]),
                "messages_unacknowledged": int(fields[2]),
            }
        )
    return {
        "queue_messages": sum(int(row.get("messages") or 0) for row in rows),
        "queue_unacknowledged": sum(int(row.get("messages_unacknowledged") or 0) for row in rows),
    }


def _artifact_count() -> int:
    raw = _run(
        [
            "docker",
            "exec",
            _required("FS_SMOKE_APP_CONTAINER"),
            "sh",
            "-c",
            'root="${FS_ARTIFACT_ROOT:-}"; '
            'if [ -z "$root" ] || [ ! -d "$root" ]; then printf 0; '
            'else find "$root" -type f | wc -l | tr -d " "; fi',
        ]
    )
    return int(raw or 0)


def main() -> int:
    deadline = time.monotonic() + int(os.environ.get("FS_SMOKE_CLEANUP_TIMEOUT_SECONDS") or 30)
    counts: dict[str, int] = {}
    while True:
        counts = {**_database_counts(), **_queue_counts(), "artifact_files": _artifact_count()}
        if not any(counts.values()):
            print(json.dumps({"status": "clean", "counts": counts}, sort_keys=True))
            return 0
        if time.monotonic() >= deadline:
            print(json.dumps({"status": "residue", "counts": counts}, sort_keys=True))
            return 1
        time.sleep(2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "probe_failed", "reason": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
