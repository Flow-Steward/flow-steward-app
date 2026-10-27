"""Pure schema-version planning shared by extension validation and migration execution."""

from __future__ import annotations

from typing import Any


def plan_data_migration_steps(
    lifecycle: dict[str, Any],
    *,
    source_version: int,
    target_version: int,
) -> list[dict[str, Any]]:
    source = int(source_version)
    target = int(target_version)
    if source < 1 or target < source:
        raise ValueError("Invalid data schema version range")
    if source == target:
        return []
    by_source: dict[int, dict[str, Any]] = {}
    for raw_step in (lifecycle or {}).get("data_migrations") or []:
        step = dict(raw_step or {})
        step_source = int(step.get("source_version") or 0)
        if step_source in by_source:
            raise ValueError("Data migration chain branches from one source version")
        by_source[step_source] = step
    planned: list[dict[str, Any]] = []
    current = source
    while current < target:
        step = by_source.get(current)
        if step is None:
            raise ValueError(f"Missing data migration from schema version {current}")
        next_version = int(step.get("target_version") or 0)
        action_id = str(step.get("action_id") or "").strip()
        if not action_id or next_version <= current or next_version > target:
            raise ValueError("Invalid data migration chain")
        planned.append(
            {
                "source_version": current,
                "target_version": next_version,
                "action_id": action_id,
            }
        )
        current = next_version
    if current != target:
        raise ValueError("Incomplete data migration chain")
    return planned
