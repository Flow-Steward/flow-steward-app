from __future__ import annotations

import uuid

import pytest

from core.application.registry.db_store import RegistryDBStore
from tests._db_env import resolve_test_db_dsn


@pytest.mark.integration
def test_guardrail_crud_and_audit_are_account_scoped(pg_conn) -> None:
    suffix = uuid.uuid4().hex
    owner_account = "00000000-0000-0000-0000-000000000002"
    foreign_account = "00000000-0000-0000-0000-000000000099"
    owner_agent = f"guardrail_owner_{suffix}"
    foreign_agent = f"guardrail_foreign_{suffix}"
    store = RegistryDBStore(resolve_test_db_dsn())
    guardrail_id = ""

    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT id FROM agent_types ORDER BY id LIMIT 1")
            agent_type_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO registry_agents
                   (agent_id, display_name, agent_type_id, account_id)
                   VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)""",
                (
                    owner_agent,
                    "Guardrail owner agent",
                    agent_type_id,
                    owner_account,
                    foreign_agent,
                    "Guardrail foreign agent",
                    agent_type_id,
                    foreign_account,
                ),
            )
        pg_conn.commit()

        guardrail_id = store.save_guardrail(
            {
                "display_name": "Account boundary regression",
                "mode": "deterministic",
                "rule_type": "account_boundary_regression",
                "severity": "warning",
                "agents": [owner_agent],
            },
            author="owner@example.test",
            account_id=owner_account,
            actor_id="user-owner",
            actor_type="user",
        )

        assert store.get_guardrail(guardrail_id, account_id=owner_account) is not None
        assert store.get_guardrail(guardrail_id, account_id=foreign_account) is None
        assert guardrail_id in {
            str(row["id"]) for row in store.list_guardrails(account_id=owner_account)
        }
        assert guardrail_id not in {
            str(row["id"]) for row in store.list_guardrails(account_id=foreign_account)
        }
        assert (
            store.toggle_guardrail(
                guardrail_id,
                account_id=foreign_account,
                actor_id="user-foreign",
                actor_type="user",
            )
            is None
        )
        assert not store.delete_guardrail(
            guardrail_id,
            author="foreign@example.test",
            account_id=foreign_account,
            actor_id="user-foreign",
            actor_type="user",
        )
        with pytest.raises(KeyError):
            store.set_guardrail_agents(
                guardrail_id,
                [foreign_agent],
                account_id=foreign_account,
                actor_id="user-foreign",
                actor_type="user",
            )

        owner_row = store.get_guardrail(guardrail_id, account_id=owner_account)
        assert owner_row["created_by"] == "user-owner"
        assert owner_row["updated_by"] == "user-owner"
        history = store.get_entity_history(
            "guardrail",
            guardrail_id,
            account_id=owner_account,
        )
        assert history[0]["account_id"] == uuid.UUID(owner_account)
        assert history[0]["actor_id"] == "user-owner"
        assert history[0]["actor_type"] == "user"
        assert (
            store.get_entity_history(
                "guardrail",
                guardrail_id,
                account_id=foreign_account,
            )
            == []
        )

        assert store.delete_guardrail(
            guardrail_id,
            author="owner@example.test",
            account_id=owner_account,
            actor_id="user-owner",
            actor_type="user",
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                """SELECT account_id, created_by, updated_by, deleted_by
                     FROM registry_guardrails WHERE id::text = %s""",
                (guardrail_id,),
            )
            persisted = cur.fetchone()
        assert str(persisted[0]) == owner_account
        assert tuple(persisted[1:]) == ("user-owner", "user-owner", "user-owner")
    finally:
        with pg_conn.cursor() as cur:
            if guardrail_id:
                cur.execute(
                    "DELETE FROM registry_changelog WHERE entity_type = 'guardrail' AND entity_id = %s",
                    (guardrail_id,),
                )
                cur.execute(
                    "DELETE FROM registry_guardrail_agents WHERE guardrail_id::text = %s",
                    (guardrail_id,),
                )
                cur.execute("DELETE FROM registry_guardrails WHERE id::text = %s", (guardrail_id,))
            cur.execute(
                "DELETE FROM registry_agents WHERE agent_id = ANY(%s)",
                ([owner_agent, foreign_agent],),
            )
        pg_conn.commit()
        if store._pool is not None:
            store._pool.closeall()
