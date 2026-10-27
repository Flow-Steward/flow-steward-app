"""Seed canonical lookup and registry state from checked-in snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.builtin_mcp_tools import sync_builtin_mcp_tools
from core.application.registry.builtin_standard_tools import sync_builtin_standard_tools
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.config import runtime_config as core_config
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.platform_stores import PlatformScheduleStore
from core.infrastructure.db.schema_state import record_canonical_schema_state

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG = getattr(core_config, "CONFIG", {})
SEEDS_DIR = REPO_ROOT / "db" / "seeds"
LOOKUP_SEED_PATH = SEEDS_DIR / "lookup_seed.json"
REGISTRY_SEED_PATH = SEEDS_DIR / "registry_seed.json"


def _resolve_sql_dsn() -> str:
    candidates = (
        CONFIG.get("fs_db_dsn", ""),
        CONFIG.get("langgraph_postgres_uri", ""),
        _runtime_env.get("DATABASE_URL", ""),
        _runtime_env.get("POSTGRES_DSN", ""),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _load_seed(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Seed file is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_seed_published_block_contract(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row or {})
    normalized.pop("id", None)
    for key in ("inputs", "_inputs", "outputs", "_outputs"):
        items = normalized.get(key)
        if not isinstance(items, list):
            continue
        cleaned_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            cleaned = dict(item)
            cleaned.pop("id", None)
            cleaned.pop("field_id", None)
            cleaned_items.append(cleaned)
        normalized[key] = cleaned_items
    if isinstance(normalized.get("inputs"), list):
        normalized.pop("_inputs", None)
    if isinstance(normalized.get("outputs"), list):
        normalized.pop("_outputs", None)
    return normalized


def _seed_guardrail_agents(row: dict[str, Any]) -> list[str]:
    return [
        str(item or "").strip()
        for item in ((row or {}).get("agents") or (row or {}).get("_agents") or [])
        if str(item or "").strip()
    ]


def _canonical_guardrail_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in payload.get("guardrails") or []:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "").strip().lower()
        if "safe to delete" in description:
            continue
        normalized = dict(row)
        normalized["agents"] = _seed_guardrail_agents(normalized)
        if not normalized["agents"]:
            guardrail_id = str(normalized.get("id") or "<unknown>").strip()
            raise ValueError(f"Canonical guardrail '{guardrail_id}' must assign at least one agent")
        for key in ("_agents", "created_at", "updated_at", "deleted_at", "deleted_by"):
            normalized.pop(key, None)
        rows.append(normalized)
    return rows


def _canonical_guardrail_restore_ready(
    store: RegistryDBStore,
    guardrail_rows: list[dict[str, Any]],
) -> bool:
    expected_agents = sorted(
        {agent_id for row in (guardrail_rows or []) for agent_id in _seed_guardrail_agents(row)}
    )
    if not expected_agents:
        return True

    get_agent = getattr(store, "get_agent", None)
    if not callable(get_agent):
        return True

    for agent_id in expected_agents:
        try:
            agent = get_agent(agent_id, include_trashed=True)
        except TypeError:
            agent = get_agent(agent_id)
        except Exception:
            return False
        if not isinstance(agent, dict) or not agent:
            return False
    return True


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _upsert_rows(
    conn,
    *,
    table: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    conflict_columns: list[str],
) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(columns))
    update_columns = [column for column in columns if column not in conflict_columns]
    update_sql = ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_columns)}) DO UPDATE SET {update_sql}"
    )
    values = [
        tuple(_json_value((row or {}).get(column)) for column in columns) for row in (rows or [])
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, values)
    conn.commit()
    return len(values)


def apply_lookup_seed(*, dsn: str | None = None) -> dict[str, int]:
    resolved_dsn = str(dsn or _resolve_sql_dsn()).strip()
    if not resolved_dsn:
        raise RuntimeError("No Postgres DSN configured for lookup seed")

    payload = _load_seed(LOOKUP_SEED_PATH)
    conn = psycopg_runtime.connect(resolved_dsn)
    conn.autocommit = False
    try:
        counts = {
            "accounts": _upsert_rows(
                conn,
                table="accounts",
                rows=list(payload.get("accounts") or []),
                columns=["id", "tenant_id", "account_key", "display_name", "is_active"],
                conflict_columns=["id"],
            ),
            "workspaces": _upsert_rows(
                conn,
                table="workspaces",
                rows=list(payload.get("workspaces") or []),
                columns=[
                    "id",
                    "tenant_id",
                    "account_id",
                    "workspace_key",
                    "display_name",
                    "description",
                    "status",
                ],
                conflict_columns=["id"],
            ),
            "roles": _upsert_rows(
                conn,
                table="roles",
                rows=list(payload.get("roles") or []),
                columns=["key", "label"],
                conflict_columns=["key"],
            ),
            "agent_types": _upsert_rows(
                conn,
                table="agent_types",
                rows=list(payload.get("agent_types") or []),
                columns=["key", "label", "description", "enabled"],
                conflict_columns=["key"],
            ),
            "provider_types": _upsert_rows(
                conn,
                table="provider_types",
                rows=list(payload.get("provider_types") or []),
                columns=["key", "label", "description", "enabled"],
                conflict_columns=["key"],
            ),
        }
    finally:
        conn.close()

    logger = PlatformScheduleStore(resolved_dsn)
    provider_count = 0
    for row in payload.get("providers") or []:
        provider = dict(row or {})
        logger.save_provider(provider)
        provider_count += 1
    counts["providers"] = provider_count
    return counts


def apply_registry_seed(*, dsn: str | None = None, author: str = "system") -> dict[str, int]:
    resolved_dsn = str(dsn or _resolve_sql_dsn()).strip()
    if not resolved_dsn:
        raise RuntimeError("No Postgres DSN configured for registry seed")

    payload = _load_seed(REGISTRY_SEED_PATH)
    store = RegistryDBStore(resolved_dsn)
    counts: dict[str, int] = {}

    for row in payload.get("data_contracts") or []:
        store.save_data_contract(dict(row or {}), author=author)
    counts["data_contracts"] = len(payload.get("data_contracts") or [])

    for row in payload.get("fields") or []:
        store.save_field(dict(row or {}), author=author)
    counts["fields"] = len(payload.get("fields") or [])

    for row in payload.get("tools") or []:
        entity_id = str((row or {}).get("tool_id") or "").strip()
        if entity_id:
            store.apply_entity_snapshot("tool", entity_id, dict(row or {}), author=author)
    counts["tools"] = len(payload.get("tools") or [])

    for row in payload.get("skills") or []:
        entity_id = str((row or {}).get("skill_id") or "").strip()
        if entity_id:
            store.apply_entity_snapshot("skill", entity_id, dict(row or {}), author=author)
    counts["skills"] = len(payload.get("skills") or [])

    sync_builtin_mcp_tools(store)
    sync_builtin_standard_tools(store)

    for row in payload.get("agents") or []:
        entity_id = str((row or {}).get("agent_id") or "").strip()
        if entity_id:
            store.apply_entity_snapshot("agent", entity_id, dict(row or {}), author=author)
    counts["agents"] = len(payload.get("agents") or [])

    for row in payload.get("workflows") or []:
        entity_id = str((row or {}).get("workflow_id") or "").strip()
        if entity_id:
            store.apply_entity_snapshot("workflow", entity_id, dict(row or {}), author=author)
    counts["workflows"] = len(payload.get("workflows") or [])

    for row in payload.get("published_block_contracts") or []:
        store.save_published_block_contract(
            _normalize_seed_published_block_contract(dict(row or {})),
            author=author,
        )
    counts["published_block_contracts"] = len(payload.get("published_block_contracts") or [])

    guardrail_rows = _canonical_guardrail_rows(payload)
    for row in guardrail_rows:
        store.save_guardrail(
            dict(row or {}),
            author=author,
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="system",
            actor_type="system",
        )
    counts["guardrails"] = len(guardrail_rows)
    return counts


def ensure_canonical_guardrails_seeded(
    *, dsn: str | None = None, author: str = "system"
) -> dict[str, int]:
    resolved_dsn = str(dsn or _resolve_sql_dsn()).strip()
    if not resolved_dsn:
        raise RuntimeError("No Postgres DSN configured for canonical guardrail restore")

    payload = _load_seed(REGISTRY_SEED_PATH)
    guardrail_rows = _canonical_guardrail_rows(payload)
    store = RegistryDBStore(resolved_dsn)

    if not _canonical_guardrail_restore_ready(store, guardrail_rows):
        return {
            "expected": len(guardrail_rows),
            "restored": 0,
            "scope_repaired": 0,
        }

    restored = 0
    scope_repaired = 0
    for row in guardrail_rows:
        raw_guardrail_id = row.get("id")
        if raw_guardrail_id is None:
            continue
        guardrail_id = str(raw_guardrail_id or "").strip()
        if not guardrail_id:
            continue
        existing = store.get_guardrail(
            guardrail_id,
            include_trashed=True,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        expected_agents = sorted(set(_seed_guardrail_agents(row)))
        if not isinstance(existing, dict) or not existing:
            store.save_guardrail(
                dict(row),
                author=author,
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="system",
                actor_type="system",
            )
            restored += 1
            continue

        actual_agents = sorted(
            {
                str(item or "").strip()
                for item in (existing.get("_agents") or existing.get("agents") or [])
                if str(item or "").strip()
            }
        )
        if actual_agents == expected_agents:
            continue

        repaired = dict(existing)
        repaired["agents"] = expected_agents
        repaired.pop("_agents", None)
        store.save_guardrail(
            repaired,
            author=author,
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="system",
            actor_type="system",
        )
        scope_repaired += 1

    return {
        "expected": len(guardrail_rows),
        "restored": restored,
        "scope_repaired": scope_repaired,
    }


def apply_bootstrap_seed(
    *, dsn: str | None = None, author: str = "system"
) -> dict[str, dict[str, Any]]:
    resolved_dsn = str(dsn or _resolve_sql_dsn()).strip()
    if not resolved_dsn:
        raise RuntimeError("No Postgres DSN configured for bootstrap seed")

    summary: dict[str, dict[str, Any]] = {
        "lookup": apply_lookup_seed(dsn=resolved_dsn),
        "registry": apply_registry_seed(dsn=resolved_dsn, author=author),
    }
    conn = psycopg_runtime.connect(resolved_dsn)
    conn.autocommit = True
    try:
        schema_state = record_canonical_schema_state(
            conn,
            root=REPO_ROOT,
            applied_by=author,
            metadata={"source": "flow-steward database bootstrap-seed"},
        )
    finally:
        conn.close()
    summary["schema_state"] = {
        "updated": 1,
        "fingerprint": str(schema_state["schema_fingerprint"]),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply canonical database seed data.")
    parser.add_argument("--author", default="system")
    args = parser.parse_args(argv)
    summary = apply_bootstrap_seed(author=args.author)
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
