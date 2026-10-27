"""Backfill missing builder metadata from the canonical registry seed."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.application.registry.builder_metadata import normalize_builder_metadata

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGISTRY_SEED_PATH = _REPO_ROOT / "db" / "seeds" / "registry_seed.json"
_ENTITY_SPECS: dict[str, tuple[str, str]] = {
    "agent": ("agents", "agent_id"),
    "skill": ("skills", "skill_id"),
    "tool": ("tools", "tool_id"),
    "workflow": ("workflows", "workflow_id"),
}


def _load_seed_payload() -> dict[str, Any]:
    if not _REGISTRY_SEED_PATH.is_file():
        logger.warning("Registry seed path is missing: %s", _REGISTRY_SEED_PATH)
        return {}
    try:
        return json.loads(_REGISTRY_SEED_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("Could not parse registry seed for builder metadata sync: %s", exc)
        return {}


def _seed_builder_metadata_by_entity() -> dict[str, dict[str, dict[str, Any]]]:
    payload = _load_seed_payload()
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for entity_type, (section_key, id_key) in _ENTITY_SPECS.items():
        rows = payload.get(section_key) or []
        if not isinstance(rows, list):
            continue
        section_entries: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            entity_id = str(row.get(id_key) or "").strip()
            if not entity_id:
                continue
            metadata = normalize_builder_metadata(row.get("builder_metadata_jsonb"))
            if not str(metadata.get("purpose") or "").strip():
                continue
            section_entries[entity_id] = metadata
        if section_entries:
            result[entity_type] = section_entries
    return result


def sync_registry_seed_builder_metadata(db_store: Any) -> dict[str, list[str]]:
    """Populate missing builder metadata for canonical core registry entities.

    This only fills empty builder metadata rows. Existing authored metadata is
    preserved to avoid clobbering admin edits made through the UI.
    """

    changed: dict[str, list[str]] = {}
    seed_entries = _seed_builder_metadata_by_entity()
    for entity_type, entries in seed_entries.items():
        for entity_id, metadata in entries.items():
            try:
                updated = bool(
                    db_store.set_entity_builder_metadata(
                        entity_type,
                        entity_id,
                        metadata,
                        author="system",
                        only_if_empty=True,
                    )
                )
            except Exception as exc:  # pragma: no cover - runtime safety/logging
                logger.warning(
                    "Could not backfill builder metadata for %s '%s': %s",
                    entity_type,
                    entity_id,
                    exc,
                )
                continue
            if updated:
                changed.setdefault(entity_type, []).append(entity_id)
    return changed
