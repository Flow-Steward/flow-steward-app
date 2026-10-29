from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.extension_runtime import (
    ExtensionRegistryProjectionRecord,
    ExtensionReleaseRecord,
)
from core.application.extensions.install import DBExtensionInstallStore
from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore
from core.application.management.errors import ServiceError
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.projected_artifact_rows import projection_bridge_id

pytestmark = pytest.mark.integration


def _install_virtual_agent_with_skill(
    disposable_db, conn, suffix: str, *, account_id: str = DEFAULT_ACCOUNT_ID
):
    extension_id = f"fixture.surfagent-{suffix}"
    release_id = f"pr_{uuid.uuid4().hex[:24]}"
    descriptor = {
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": "1.0.0",
        "description": "Projected skill clone fixture",
        "entrypoint": {"mode": "in_process"},
        "runtime": {"contract_version": "extension_host_v2"},
    }
    runtime = DBExtensionRuntimeStore(conn)
    release = runtime.create_release(
        ExtensionReleaseRecord(
            release_id=release_id,
            extension_id=extension_id,
            release_version="1.0.0",
            descriptor=descriptor,
            source_kind="extension_scan",
            source_ref=extension_id,
            account_id=account_id,
        )
    )
    install = DBExtensionInstallStore(conn).install(
        extension_id=extension_id,
        version="1.0.0",
        account_id=account_id,
        release_id=release.release_id,
        descriptor_source="extension_scan",
    )
    skill_local_id = "summarize_support_ticket"
    agent_local_id = "showcase_summarizer"
    skill_projection = {
        "skill_id": skill_local_id,
        "display_name": "Summarize support ticket",
        "description": "Installed skill contract fixture.",
        "version": "1.0.0",
        "enabled": True,
        "instructions": ["This executable instruction remains in the install projection."],
        "prompt_template": "Do not copy this prompt into the durable anchor.",
        "expected_inputs": [
            {
                "name": "ticket",
                "type": "object",
                "description": "Ticket payload",
                "required": True,
            }
        ],
        "expected_outputs": [
            {
                "name": "summary",
                "type": "text",
                "description": "Concise ticket summary",
                "required": True,
            }
        ],
    }
    agent_projection = {
        "agent_id": agent_local_id,
        "display_name": "Showcase Summarizer",
        "description": "Projected agent with a virtual installed skill.",
        "version": "1.0.0",
        "enabled": True,
        "prompts": {
            "system": "Use the installed skill.",
            "skill_prompts": {
                skill_local_id: "Summarize the ticket into a concise customer-facing ticket_summary."
            },
        },
        "skills": [{"skill_id": skill_local_id, "skill_prompt": ""}],
        "tools": [],
        "inputs": {"required": [], "optional": []},
        "outputs": [],
        "structured_inputs": [
            {
                "name": "review_note",
                "type": "text",
                "description": "Authored by the projected agent",
                "required": False,
            }
        ],
        "structured_outputs": [],
        "depends_on": {"soft": [], "hard": []},
        "config": {},
        "quality": {},
        "builder_metadata_jsonb": {},
        "category": "support",
        "tags": [],
    }
    for kind, artifact_id, projection in (
        ("skill", skill_local_id, skill_projection),
        ("agent", agent_local_id, agent_projection),
    ):
        runtime.upsert_projection(
            ExtensionRegistryProjectionRecord(
                account_id=account_id,
                install_id=install.install_id,
                release_id=release.release_id,
                extension_id=extension_id,
                artifact_kind=kind,
                artifact_id=artifact_id,
                projection=projection,
                is_active=True,
            )
        )
    conn.commit()
    store = RegistryDBStore(disposable_db.dsn)
    store.extension_runtime_store = runtime
    return store, runtime, install, release, agent_local_id, skill_local_id


def _cleanup(
    conn, store, runtime, install, release, ids: list[str], *, account_id: str = DEFAULT_ACCOUNT_ID
) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM registry_changelog WHERE entity_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM registry_agents WHERE agent_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM registry_skills WHERE skill_id = ANY(%s)", (ids,))
    conn.commit()
    runtime.delete_projections_for_install(install.install_id)
    DBExtensionInstallStore(conn).uninstall(
        extension_id=install.extension_id, account_id=account_id
    )
    runtime.delete_release(release.release_id)
    conn.commit()


@pytest.mark.integration
def test_clone_materializes_virtual_installed_skill_reference_and_contract(
    disposable_db, disposable_db_conn
):
    from core.application.management.command_services_agent import AgentCommandService
    from core.application.registry.workflow_lookup import get_registry_artifact_row

    suffix = uuid.uuid4().hex
    store, runtime, install, release, agent_local_id, skill_local_id = (
        _install_virtual_agent_with_skill(disposable_db, disposable_db_conn, suffix)
    )
    source_id = projection_bridge_id(install.install_id, agent_local_id)
    skill_id = projection_bridge_id(install.install_id, skill_local_id)
    target_id = f"clone_virtual_skill_{suffix}"
    try:
        assert store.get_agent(source_id) is None
        assert store.get_skill(skill_id) is None
        result = AgentCommandService(
            get_db=lambda: store, reload_registry=lambda: None
        ).clone_agent(
            source_id,
            target_agent_id=target_id,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="projected-skill-test"),
        )

        saved = store.get_agent(target_id)
        assert result["agent_id"] == target_id
        assert saved is not None
        assert [row["skill_id"] for row in saved["skills"]] == [skill_id]
        assert saved["prompts"]["skill_prompts"] == {
            skill_id: "Summarize the ticket into a concise customer-facing ticket_summary."
        }
        assert [
            (row["name"], row["type"], row["source"]) for row in store.get_agent_inputs(target_id)
        ] == [
            ("ticket", "object", f"skill:{skill_id}"),
            ("review_note", "text", "own"),
        ]
        assert [
            (row["name"], row["type"], row["source"]) for row in store.get_agent_outputs(target_id)
        ] == [("summary", "text", f"skill:{skill_id}")]
        anchor = store.get_skill(skill_id)
        assert anchor is not None
        assert anchor["source_install_id"] == install.install_id
        assert str(anchor["account_id"]) == DEFAULT_ACCOUNT_ID
        assert anchor["enabled"] is False
        assert anchor["instructions"] == []
        assert anchor["prompt_template"] == ""
        active_skill = get_registry_artifact_row(
            store,
            artifact_kind="skill",
            id_key="skill_id",
            artifact_id=skill_id,
            platform_getter="get_skill",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert active_skill["instructions"] == [
            "This executable instruction remains in the install projection."
        ]
        assert "Do not copy this prompt" in active_skill["prompt_template"]
        from core.application.extensions.uninstall_projected_skill_dependents import (
            collect_projected_skill_dependents,
        )

        assert [
            row["id"]
            for row in collect_projected_skill_dependents(
                store, install_id=install.install_id, account_id=DEFAULT_ACCOUNT_ID
            )
        ] == [target_id]

        inactive = next(
            row
            for row in runtime.list_projections(account_id=DEFAULT_ACCOUNT_ID, only_active=False)
            if row.artifact_kind == "skill"
        )
        runtime.upsert_projection(inactive.model_copy(update={"is_active": False}))
        assert (
            get_registry_artifact_row(
                store,
                artifact_kind="skill",
                id_key="skill_id",
                artifact_id=skill_id,
                platform_getter="get_skill",
                account_id=DEFAULT_ACCOUNT_ID,
            )
            is None
        )
    finally:
        _cleanup(disposable_db_conn, store, runtime, install, release, [target_id, skill_id])


@pytest.mark.integration
def test_clone_files_a_projection_with_a_legacy_category_under_other(
    disposable_db, disposable_db_conn
):
    from core.application.management.command_services_agent import AgentCommandService

    suffix = uuid.uuid4().hex
    store, runtime, install, release, agent_local_id, skill_local_id = (
        _install_virtual_agent_with_skill(disposable_db, disposable_db_conn, suffix)
    )
    source_id = projection_bridge_id(install.install_id, agent_local_id)
    skill_id = projection_bridge_id(install.install_id, skill_local_id)
    target_id = f"clone_source_specialist_override_{suffix}"
    try:
        agent_projection = next(
            row
            for row in runtime.list_projections(account_id=DEFAULT_ACCOUNT_ID, only_active=True)
            if row.artifact_kind == "agent" and row.artifact_id == agent_local_id
        )
        legacy_projection = dict(agent_projection.projection)
        legacy_projection["category"] = "analysis"
        runtime.upsert_projection(
            agent_projection.model_copy(update={"projection": legacy_projection})
        )
        clone_service = AgentCommandService(get_db=lambda: store, reload_registry=lambda: None)

        result = clone_service.clone_agent(
            source_id,
            target_agent_id=target_id,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="projected-skill-test"),
        )

        saved = store.get_agent(target_id)
        assert result["agent_id"] == target_id
        assert saved is not None
        assert saved["category"] == "other"
        assert saved["enabled"] is False
        assert [row["skill_id"] for row in saved["skills"]] == [skill_id]
        assert store.get_agent_inputs(target_id)[0]["source"] == f"skill:{skill_id}"
        assert store.get_agent_outputs(target_id)[0]["name"] == "summary"
        assert store.get_agent(source_id) is None
    finally:
        _cleanup(disposable_db_conn, store, runtime, install, release, [target_id, skill_id])


@pytest.mark.integration
def test_projected_skill_release_refresh_replaces_inherited_contract(
    disposable_db, disposable_db_conn
):
    from core.application.extensions.projected_skill_bridge import (
        materialize_projected_skill_anchors,
    )
    from core.application.management.command_services_agent import AgentCommandService

    suffix = uuid.uuid4().hex
    store, runtime, install, release, agent_local_id, skill_local_id = (
        _install_virtual_agent_with_skill(disposable_db, disposable_db_conn, suffix)
    )
    source_id = projection_bridge_id(install.install_id, agent_local_id)
    skill_id = projection_bridge_id(install.install_id, skill_local_id)
    target_id = f"clone_virtual_skill_refresh_{suffix}"
    try:
        AgentCommandService(get_db=lambda: store, reload_registry=lambda: None).clone_agent(
            source_id,
            target_agent_id=target_id,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="projected-skill-test"),
        )
        prior = next(
            row
            for row in runtime.list_projections(account_id=DEFAULT_ACCOUNT_ID, only_active=False)
            if row.artifact_kind == "skill"
        )
        runtime.upsert_projection(
            prior.model_copy(
                update={
                    "projection": {
                        **prior.projection,
                        "expected_inputs": [
                            {"name": "updated_ticket", "type": "json", "required": True}
                        ],
                        "expected_outputs": [],
                    }
                }
            )
        )
        materialize_projected_skill_anchors(
            store,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=install.install_id,
            release_id=release.release_id,
            projections=runtime.list_projections(account_id=DEFAULT_ACCOUNT_ID, only_active=False),
        )
        assert [
            (row["name"], row["type"], row["source"]) for row in store.get_agent_inputs(target_id)
        ] == [
            ("updated_ticket", "json", f"skill:{skill_id}"),
            ("review_note", "text", "own"),
        ]
        assert store.get_agent_outputs(target_id) == []
        assert store.get_skill_inputs(skill_id)[0]["name"] == "updated_ticket"
        assert store.get_skill_outputs(skill_id) == []
    finally:
        _cleanup(disposable_db_conn, store, runtime, install, release, [target_id, skill_id])


@pytest.mark.integration
def test_projected_skill_clone_readiness_is_bound_to_agent_account(
    disposable_db, disposable_db_conn
):
    from core.application.management.command_services_agent import AgentCommandService
    from core.application.registry.contract_validation_entities import (
        validate_agent_readiness,
        validate_agent_skill_compatibility,
    )
    from core.application.registry.workflow_lookup import get_registry_artifact_row

    account_id = str(uuid.uuid4())
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (id, account_key, display_name) VALUES (%s, %s, %s)",
            (account_id, f"surfagent-{account_id[:8]}", "Projected skill clone account"),
        )
    disposable_db_conn.commit()
    suffix = uuid.uuid4().hex
    store, runtime, install, release, agent_local_id, skill_local_id = (
        _install_virtual_agent_with_skill(
            disposable_db, disposable_db_conn, suffix, account_id=account_id
        )
    )
    source_id = projection_bridge_id(install.install_id, agent_local_id)
    skill_id = projection_bridge_id(install.install_id, skill_local_id)
    target_id = f"clone_virtual_skill_account_{suffix}"
    try:
        AgentCommandService(get_db=lambda: store, reload_registry=lambda: None).clone_agent(
            source_id,
            target_agent_id=target_id,
            scope=ScopeContext(account_id=account_id, actor_id="projected-skill-test"),
        )
        assert validate_agent_readiness(store, target_id)["valid"] is True
        assert (
            validate_agent_skill_compatibility(store, target_id, skill_id, account_id=account_id)[
                "valid"
            ]
            is True
        )
        assert (
            validate_agent_readiness(store, target_id, account_id=DEFAULT_ACCOUNT_ID)["valid"]
            is False
        )
        assert (
            validate_agent_skill_compatibility(
                store, target_id, skill_id, account_id=DEFAULT_ACCOUNT_ID
            )["valid"]
            is False
        )
        assert (
            get_registry_artifact_row(
                store,
                artifact_kind="skill",
                id_key="skill_id",
                artifact_id=skill_id,
                platform_getter="get_skill",
                account_id=DEFAULT_ACCOUNT_ID,
            )
            is None
        )
    finally:
        _cleanup(
            disposable_db_conn,
            store,
            runtime,
            install,
            release,
            [target_id, skill_id],
            account_id=account_id,
        )
        with disposable_db_conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        disposable_db_conn.commit()


@pytest.mark.integration
def test_virtual_skill_anchor_collision_and_clone_failure_roll_back(
    disposable_db, disposable_db_conn
):
    from core.application.management.command_services_agent import AgentCommandService

    suffix = uuid.uuid4().hex
    store, runtime, install, release, agent_local_id, skill_local_id = (
        _install_virtual_agent_with_skill(disposable_db, disposable_db_conn, suffix)
    )
    source_id = projection_bridge_id(install.install_id, agent_local_id)
    skill_id = projection_bridge_id(install.install_id, skill_local_id)
    target_id = f"clone_virtual_skill_rollback_{suffix}"
    try:
        store.save_skill(
            {"skill_id": skill_id, "display_name": "User-owned collision", "enabled": False},
            author="projected_skill_collision_test",
        )
        with pytest.raises(ServiceError):
            AgentCommandService(get_db=lambda: store, reload_registry=lambda: None).clone_agent(
                source_id,
                target_agent_id=target_id,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="projected-skill-test"),
            )
        assert store.get_skill(skill_id)["display_name"] == "User-owned collision"
        assert store.get_agent(target_id) is None
    finally:
        _cleanup(disposable_db_conn, store, runtime, install, release, [target_id, skill_id])


@pytest.mark.integration
def test_virtual_skill_anchor_and_agent_creation_roll_back_together(
    disposable_db, disposable_db_conn
):
    from core.application.management.command_services_agent import AgentCommandService

    suffix = uuid.uuid4().hex
    store, runtime, install, release, agent_local_id, skill_local_id = (
        _install_virtual_agent_with_skill(disposable_db, disposable_db_conn, suffix)
    )
    source_id = projection_bridge_id(install.install_id, agent_local_id)
    skill_id = projection_bridge_id(install.install_id, skill_local_id)
    target_id = f"clone_virtual_skill_rollback_{suffix}"
    original_materializer = store.materialize_agent_contract

    def fail_after_materialization(agent_id: str) -> None:
        original_materializer(agent_id)
        raise RuntimeError("injected clone failure after contract materialization")

    try:
        with (
            patch.object(
                store, "materialize_agent_contract", side_effect=fail_after_materialization
            ),
            pytest.raises(ServiceError),
        ):
            AgentCommandService(get_db=lambda: store, reload_registry=lambda: None).clone_agent(
                source_id,
                target_agent_id=target_id,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="projected-skill-test"),
            )
        assert store.get_agent(target_id) is None
        assert store.get_skill(skill_id) is None
    finally:
        _cleanup(disposable_db_conn, store, runtime, install, release, [target_id, skill_id])
