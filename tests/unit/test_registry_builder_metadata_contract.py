"""Registry persistence and manifests accept one serialized metadata contract."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from core.application.registry.agent_manifest import AgentManifest
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.skill_manifest import SkillManifest
from core.application.registry.workflow_manifest import WorkflowManifest


@pytest.mark.parametrize(
    "manifest_type, id_key",
    [(AgentManifest, "agent_id"), (SkillManifest, "skill_id"), (WorkflowManifest, "workflow_id")],
)
@pytest.mark.parametrize(
    "current_metadata, expected_purpose",
    [(None, ""), ({}, ""), ({"purpose": "Current guidance"}, "Current guidance")],
)
def test_registry_manifests_ignore_alternate_serialized_metadata(
    manifest_type, id_key, current_metadata, expected_purpose
):
    row = {
        id_key: "current",
        "display_name": "Current",
        "builder_metadata": {"purpose": "Alternate guidance"},
    }
    if current_metadata is not None:
        row["builder_metadata_jsonb"] = current_metadata

    manifest = manifest_type.from_db_row(row)

    assert manifest.builder_metadata["purpose"] == expected_purpose
    exported = manifest.to_dict()
    assert exported["builder_metadata_jsonb"]["purpose"] == expected_purpose
    assert "builder_metadata" not in exported


@pytest.mark.parametrize("entity_type", ["agent", "skill", "workflow"])
@pytest.mark.parametrize(
    "current_metadata, expected_purpose",
    [(None, ""), ({}, ""), ({"purpose": "Current guidance"}, "Current guidance")],
)
def test_registry_saves_ignore_alternate_serialized_metadata(
    entity_type, current_metadata, expected_purpose
):
    db = RegistryDBStore("postgres://unused")
    db._exists = MagicMock(return_value=False)
    db._column_exists = MagicMock(
        side_effect=lambda _table, column: column == "builder_metadata_jsonb"
    )
    db._lock_agent_relationship_parent = MagicMock(return_value={"account_id": "account"})
    db._require_relationship_capabilities_in_scope = MagicMock()
    db._sync_workflow_runtime_dependencies = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    db._log_change_cur = MagicMock()
    # This is a new manual skill, so the readonly-anchor preflight reads no row.
    db.get_skill = MagicMock(return_value=None)
    # A workflow save records the row it stored, read back in its transaction.
    db.get_workflow = MagicMock(return_value={"workflow_id": "current", "steps": []})
    writes = []

    class Cursor:
        def execute(self, _sql, params=None):
            if isinstance(params, dict) and "builder_metadata_jsonb" in params:
                writes.append(deepcopy(params))

    @contextmanager
    def transaction():
        yield Cursor()

    db._transaction = transaction
    payload = {
        f"{entity_type}_id": "current",
        "display_name": "Current",
        "prompts": {"system": "Current instructions"},
        "builder_metadata": {"purpose": "Alternate guidance"},
    }
    if current_metadata is not None:
        payload["builder_metadata_jsonb"] = current_metadata

    getattr(db, f"save_{entity_type}")(payload)

    if entity_type == "skill":
        db.get_skill.assert_called_once_with("current")
    assert len(writes) == 1
    assert json.loads(writes[0]["builder_metadata_jsonb"])["purpose"] == expected_purpose


def test_skill_metadata_save_keeps_projected_anchor_read_only():
    db = RegistryDBStore("postgres://unused")
    skill_id = "extension.install.current"
    db.get_skill = MagicMock(return_value={"skill_id": skill_id, "source_install_id": "install"})
    db._transaction = MagicMock()

    with pytest.raises(ValueError, match="Install-owned projected skills are read-only"):
        db.save_skill({"skill_id": skill_id, "builder_metadata_jsonb": {"purpose": "Changed"}})

    db.get_skill.assert_called_once_with(skill_id)
    db._transaction.assert_not_called()


@pytest.mark.parametrize(
    "current_key, expected_updated", [("builder_metadata", True), ("builder_metadata_jsonb", False)]
)
def test_metadata_backfill_compares_only_current_serialized_snapshot(current_key, expected_updated):
    db = RegistryDBStore("postgres://unused")
    db._get_entity_snapshot = MagicMock(
        return_value={"agent_id": "current", current_key: {"purpose": "Existing guidance"}}
    )
    db._log_change_cur = MagicMock()
    writes = []

    class Cursor:
        def execute(self, _sql, params=None):
            writes.append(params)

    @contextmanager
    def transaction():
        yield Cursor()

    db._transaction = transaction

    assert (
        db.set_entity_builder_metadata(
            "agent", "current", {"purpose": "Seed guidance"}, only_if_empty=True
        )
        is expected_updated
    )
    assert len(writes) == int(expected_updated)
    if expected_updated:
        assert json.loads(writes[0][0])["purpose"] == "Seed guidance"
