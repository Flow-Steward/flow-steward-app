from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.registry import seed_builder_metadata_sync
from core.application.registry.seed_builder_metadata_sync import (
    sync_registry_seed_builder_metadata,
)


def _metadata(purpose: str) -> dict:
    return {
        "purpose": purpose,
        "use_cases": ["weekly reporting"],
        "anti_use_cases": ["mutating external systems"],
        "composition_role": "content_generation",
        "determinism_level": "hybrid",
        "safe_for_auto_use": True,
    }


@pytest.fixture
def in_memory_seed(tmp_path, monkeypatch) -> Path:
    seed_path = tmp_path / "registry_seed.json"
    seed_path.write_text(
        json.dumps(
            {
                "agents": [
                    {"agent_id": "writer", "builder_metadata_jsonb": _metadata("Write reports")}
                ],
                "skills": [
                    {
                        "skill_id": "draft_summary",
                        "builder_metadata_jsonb": _metadata("Draft a summary"),
                    },
                    # Seed rows without a purpose never overwrite anything.
                    {"skill_id": "no_purpose", "builder_metadata_jsonb": {}},
                ],
                "tools": [
                    {
                        "tool_id": "report_tool",
                        "builder_metadata_jsonb": _metadata("Read reports"),
                    }
                ],
                "workflows": [
                    {
                        "workflow_id": "weekly_report",
                        "builder_metadata_jsonb": _metadata("Produce the weekly report"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed_builder_metadata_sync, "_REGISTRY_SEED_PATH", seed_path)
    return seed_path


class _FakeStore:
    def __init__(self) -> None:
        self.rows = {
            "agent": {"writer": {"builder_metadata_jsonb": {}}},
            "skill": {
                "draft_summary": {
                    "builder_metadata_jsonb": {
                        "purpose": "keep existing",
                        "use_cases": ["existing"],
                        "anti_use_cases": ["none"],
                        "composition_role": "existing_role",
                        "determinism_level": "hybrid",
                        "safe_for_auto_use": False,
                    }
                },
                "no_purpose": {"builder_metadata_jsonb": {}},
            },
            "tool": {"report_tool": {"builder_metadata_jsonb": {}}},
            "workflow": {"weekly_report": {"builder_metadata_jsonb": {}}},
        }

    def set_entity_builder_metadata(
        self,
        entity_type: str,
        entity_id: str,
        builder_metadata: dict,
        *,
        author: str = "admin",
        only_if_empty: bool = False,
    ) -> bool:
        row = self.rows.get(entity_type, {}).get(entity_id)
        if not row:
            return False
        current = row.get("builder_metadata_jsonb") or {}
        if only_if_empty and str(current.get("purpose") or "").strip():
            return False
        row["builder_metadata_jsonb"] = dict(builder_metadata)
        row["updated_by"] = author
        return True


def test_seed_builder_metadata_sync_only_backfills_empty_rows(in_memory_seed) -> None:
    store = _FakeStore()

    changed = sync_registry_seed_builder_metadata(store)

    assert changed["agent"] == ["writer"]
    assert "skill" not in changed
    assert changed["tool"] == ["report_tool"]
    assert changed["workflow"] == ["weekly_report"]

    assert store.rows["agent"]["writer"]["builder_metadata_jsonb"]["purpose"] == "Write reports"
    assert store.rows["tool"]["report_tool"]["builder_metadata_jsonb"]["purpose"] == "Read reports"
    assert (
        store.rows["workflow"]["weekly_report"]["builder_metadata_jsonb"]["purpose"]
        == "Produce the weekly report"
    )
    assert (
        store.rows["skill"]["draft_summary"]["builder_metadata_jsonb"]["purpose"] == "keep existing"
    )
    assert store.rows["skill"]["no_purpose"]["builder_metadata_jsonb"] == {}


def test_seed_builder_metadata_sync_is_a_noop_for_an_empty_catalog(tmp_path, monkeypatch) -> None:
    seed_path = tmp_path / "registry_seed.json"
    seed_path.write_text(json.dumps({"agents": [], "skills": [], "workflows": []}))
    monkeypatch.setattr(seed_builder_metadata_sync, "_REGISTRY_SEED_PATH", seed_path)
    store = _FakeStore()

    assert sync_registry_seed_builder_metadata(store) == {}
    assert store.rows["agent"]["writer"]["builder_metadata_jsonb"] == {}


@pytest.mark.parametrize(
    "entity_type, section, id_key",
    [
        ("agent", "agents", "agent_id"),
        ("skill", "skills", "skill_id"),
        ("tool", "tools", "tool_id"),
        ("workflow", "workflows", "workflow_id"),
    ],
)
def test_seed_sync_ignores_alternate_serialized_metadata(entity_type, section, id_key, monkeypatch):
    monkeypatch.setattr(
        seed_builder_metadata_sync,
        "_load_seed_payload",
        lambda: {
            section: [
                {id_key: "alternate", "builder_metadata": _metadata("Alternate guidance")},
                {
                    id_key: "empty",
                    "builder_metadata_jsonb": {},
                    "builder_metadata": _metadata("Alternate guidance"),
                },
                {id_key: "current", "builder_metadata_jsonb": _metadata("Current guidance")},
            ]
        },
    )

    entries = seed_builder_metadata_sync._seed_builder_metadata_by_entity()

    assert list(entries[entity_type]) == ["current"]
    assert entries[entity_type]["current"]["purpose"] == "Current guidance"
