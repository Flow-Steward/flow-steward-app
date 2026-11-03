"""Released upgrades remove the retired agent type constraint from old databases."""

import hashlib
import json
from pathlib import Path

from core.application.tooling.db import migration_runner

ROOT = Path(__file__).resolve().parents[2]
NAME = "20261103090000_retired_agent_type.sql"


def test_retired_agent_type_migration_is_transactional_idempotent_and_released():
    path = ROOT / "db/migrations" / NAME
    sql = path.read_text(encoding="utf-8")
    assert "ALTER TABLE ONLY public.registry_agents DROP COLUMN IF EXISTS agent_type_id" in sql
    assert "CASCADE" not in sql.upper()
    assert migration_runner.parse_transactional(sql)
    assert migration_runner.find_explicit_transaction_control(sql) == []
    ledger = json.loads((ROOT / "db/released_migrations.json").read_text())
    assert ledger["migrations"][NAME] == hashlib.sha256(path.read_bytes()).hexdigest()
