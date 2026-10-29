from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from core.application.registry.db_store import RegistryDBStore
from core.application.registry.db_store_agents_mixin import AgentRelationshipValidationError
from tests._db_env import resolve_test_db_dsn


@pytest.mark.integration
def test_relationship_replace_rejects_foreign_account_skills_and_tools(pg_conn) -> None:
    suffix = uuid.uuid4().hex
    agent_id = f"scope_agent_{suffix}"
    skill_id = f"scope_skill_{suffix}"
    tool_id = f"scope_tool_{suffix}"
    agent_account = "00000000-0000-0000-0000-000000000002"
    foreign_account = "00000000-0000-0000-0000-000000000099"
    store = RegistryDBStore(resolve_test_db_dsn())
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO registry_agents
                   (agent_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (agent_id, "Scope agent", agent_account),
            )
            cur.execute(
                """INSERT INTO registry_skills (skill_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (skill_id, "Foreign skill", foreign_account),
            )
            cur.execute(
                """INSERT INTO registry_tools (tool_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (tool_id, "Foreign tool", foreign_account),
            )
        pg_conn.commit()

        with pytest.raises(AgentRelationshipValidationError):
            store.replace_agent_relationships(
                agent_id,
                expected_skill_ids=[],
                expected_tool_ids=[],
                skill_ids=[skill_id],
                tool_ids=[tool_id],
            )

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_skills WHERE agent_id = %s",
                (agent_id,),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_tools WHERE agent_id = %s",
                (agent_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM registry_agent_tools WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
        pg_conn.commit()
        if store._pool is not None:
            store._pool.closeall()


@pytest.mark.integration
def test_legacy_relationship_writer_waits_for_the_shared_agent_lock(pg_conn) -> None:
    suffix = uuid.uuid4().hex
    agent_id = f"lock_agent_{suffix}"
    skill_id = f"lock_skill_{suffix}"
    account_id = "00000000-0000-0000-0000-000000000002"
    store = RegistryDBStore(resolve_test_db_dsn())
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO registry_agents
                   (agent_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (agent_id, "Lock agent", account_id),
            )
            cur.execute(
                """INSERT INTO registry_skills (skill_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (skill_id, "Lock skill", account_id),
            )
            cur.execute(
                """INSERT INTO registry_agent_skills (agent_id, skill_id, sort_order)
                   VALUES (%s, %s, 0)""",
                (agent_id, skill_id),
            )
        pg_conn.commit()

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id FROM registry_agents WHERE agent_id = %s FOR UPDATE",
                (agent_id,),
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            removal = pool.submit(store.remove_agent_skill, agent_id, skill_id)
            with pytest.raises(TimeoutError):
                removal.result(timeout=0.25)
            pg_conn.commit()
            removal.result(timeout=5)

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM registry_agent_skills WHERE agent_id = %s",
                (agent_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM registry_skills WHERE skill_id = %s", (skill_id,))
        pg_conn.commit()
        if store._pool is not None:
            store._pool.closeall()


@pytest.mark.integration
def test_patch_revision_restore_preserves_relationship_added_after_stale_read(pg_conn) -> None:
    suffix = uuid.uuid4().hex
    agent_id = f"revision_agent_{suffix}"
    first_skill_id = f"revision_skill_a_{suffix}"
    concurrent_skill_id = f"revision_skill_b_{suffix}"
    account_id = "00000000-0000-0000-0000-000000000002"
    store = RegistryDBStore(resolve_test_db_dsn())
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO registry_agents
                   (agent_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (agent_id, "Revision agent", account_id),
            )
            cur.executemany(
                """INSERT INTO registry_skills (skill_id, display_name, account_id)
                   VALUES (%s, %s, %s)""",
                (
                    (first_skill_id, "Initial skill", account_id),
                    (concurrent_skill_id, "Concurrent skill", account_id),
                ),
            )
            cur.execute(
                """INSERT INTO registry_agent_skills (agent_id, skill_id, sort_order)
                   VALUES (%s, %s, 0)""",
                (agent_id, first_skill_id),
            )
        pg_conn.commit()

        stale_patch_snapshot = store.get_agent(agent_id)
        assert stale_patch_snapshot is not None
        store.add_agent_skill(agent_id, concurrent_skill_id)

        stale_patch_snapshot["description"] = "Scalar PATCH after concurrent relationship update"
        store.save_agent(
            stale_patch_snapshot,
            author="patch_revision_test",
            sync_relationships=False,
        )
        patch_entry = next(
            entry
            for entry in store.get_entity_history("agent", agent_id)
            if entry.get("author") == "patch_revision_test"
        )

        store.restore_entity_revision(
            "agent",
            agent_id,
            entry_id=str(patch_entry["id"]),
            author="restore_revision_test",
        )

        assert [row["skill_id"] for row in store.get_agent_skills(agent_id)] == [
            first_skill_id,
            concurrent_skill_id,
        ]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM registry_changelog WHERE entity_type = 'agent' AND entity_id = %s",
                (agent_id,),
            )
            cur.execute("DELETE FROM registry_agent_skills WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))
            cur.execute(
                "DELETE FROM registry_skills WHERE skill_id = ANY(%s)",
                ([first_skill_id, concurrent_skill_id],),
            )
        pg_conn.commit()
        if store._pool is not None:
            store._pool.closeall()
