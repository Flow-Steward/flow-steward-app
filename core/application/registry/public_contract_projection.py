from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from core.application.registry.public_contract_projection_helpers import (
    STRUCTURED_VALUE_TYPES,
    _default_schema_for_value_type,
    _extract_connector_projection_spec,
    _extract_workflow_callable_contract,
    _extract_workflow_callable_interface,
    _humanize_field_key,
    _list_workflows_safe,
    _normalize_value_type,
    _register_public_field_source,
    _stable_hash,
)

logger = logging.getLogger(__name__)


@dataclass
class ProjectionSummary:
    created: int = 0
    updated: int = 0
    disabled: int = 0
    orphaned: int = 0
    unchanged: int = 0
    errors: int = 0
    manual_preserved: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def ensure_public_contract_projection(db) -> dict[str, Any] | None:
    counts = _projection_counts(db)
    if all(
        int(counts.get(target_key) or 0) > 0
        for target_key in ("fields", "data_contracts", "published_blocks")
    ):
        return None
    source_counts = _projection_source_counts(db)
    needs_sync = any(
        int(source_counts.get(source_key) or 0) > 0 and int(counts.get(target_key) or 0) <= 0
        for source_key, target_key in (
            ("fields", "fields"),
            ("data_contracts", "data_contracts"),
            ("published_blocks", "published_blocks"),
        )
    )
    if not needs_sync:
        return None
    return reconcile_public_contract_projection(db, apply=True)


def sync_skill_projection(db, skill_id: str) -> dict[str, Any]:
    _ = skill_id
    return reconcile_public_contract_projection(db, apply=True)


def sync_agent_projection(db, agent_id: str) -> dict[str, Any]:
    _ = agent_id
    return reconcile_public_contract_projection(db, apply=True)


def sync_agent_skill_pair_projection(db, agent_id: str, skill_id: str) -> dict[str, Any]:
    _ = (agent_id, skill_id)
    return reconcile_public_contract_projection(db, apply=True)


def sync_workflow_projection(db, workflow_id: str) -> dict[str, Any]:
    _ = workflow_id
    return reconcile_public_contract_projection(db, apply=True)


def reconcile_public_contract_projection(db, *, apply: bool = True) -> dict[str, Any]:
    now = datetime.now(UTC)
    desired = _build_desired_projection_state(db, now=now)
    summary = ProjectionSummary()

    current_fields = {
        str(row.get("field_key") or "").strip(): row
        for row in (db.list_fields(enabled_only=False, ensure_projection=False) or [])
        if str(row.get("field_key") or "").strip()
    }
    current_contracts = {
        str(row.get("contract_id") or "").strip(): row
        for row in (db.list_data_contracts(enabled_only=False, ensure_projection=False) or [])
        if str(row.get("contract_id") or "").strip()
    }
    current_blocks = {
        str(row.get("block_key") or "").strip(): row
        for row in (
            db.list_published_block_contracts(enabled_only=False, ensure_projection=False) or []
        )
        if str(row.get("block_key") or "").strip()
    }

    if not apply:
        _summarize_data_contract_diff(current_contracts, desired["contracts"], summary)
        _summarize_field_diff(current_fields, desired["fields"], summary)
        _summarize_block_diff(current_blocks, desired["blocks"], summary)
        return {
            "applied": False,
            "counts": _projection_counts(db),
            "summary": summary.to_dict(),
        }

    _sync_data_contracts(db, current_contracts, desired["contracts"], summary)
    _apply_skill_contract_backrefs(db, desired["contract_backrefs"])
    _sync_fields(db, current_fields, desired["fields"], summary)
    _sync_blocks(db, current_blocks, desired["blocks"], summary)

    return {
        "applied": True,
        "counts": _projection_counts(db),
        "summary": summary.to_dict(),
    }


def _projection_counts(db) -> dict[str, int]:
    with db._cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM registry_fields) AS fields,
                (SELECT COUNT(*) FROM registry_data_contracts) AS data_contracts,
                (SELECT COUNT(*) FROM registry_published_block_contracts) AS published_blocks
            """
        )
        row = cur.fetchone() or {}
        return {
            "fields": int(row.get("fields") or 0),
            "data_contracts": int(row.get("data_contracts") or 0),
            "published_blocks": int(row.get("published_blocks") or 0),
        }


def _projection_source_counts(db) -> dict[str, int]:
    desired = _build_desired_projection_state(db, now=datetime.now(UTC))
    return {
        "fields": len(desired["fields"]),
        "data_contracts": len(desired["contracts"]),
        "published_blocks": len(desired["blocks"]),
    }


def _build_desired_projection_state(db, *, now: datetime) -> dict[str, Any]:
    skills = list(db.list_skills(enabled_only=False) or [])
    agents = list(db.list_agents(enabled_only=False) or [])
    workflows = _list_workflows_safe(db)
    tools = list(db.list_tools(enabled_only=False) or [])
    current_contracts = {
        str(row.get("contract_id") or "").strip(): row
        for row in (db.list_data_contracts(enabled_only=False, ensure_projection=False) or [])
        if str(row.get("contract_id") or "").strip()
    }

    desired_fields: dict[str, dict[str, Any]] = {}
    desired_contracts: dict[str, dict[str, Any]] = {}
    contract_backrefs: list[dict[str, str]] = []
    skill_map = {
        str(cap.get("skill_id") or "").strip(): cap
        for cap in skills
        if str(cap.get("skill_id") or "").strip()
    }
    desired_blocks: dict[str, dict[str, Any]] = {}

    for skill in skills:
        skill_id = str(skill.get("skill_id") or "").strip()
        if not skill_id:
            continue
        for io_kind, rows in (
            ("input", list(skill.get("_inputs") or [])),
            ("output", list(skill.get("_outputs") or [])),
        ):
            for row in rows:
                field_key = str(row.get("name") or "").strip()
                if not field_key:
                    continue
                value_type = _normalize_value_type(row.get("type"))
                source_ref = f"{skill_id}:{io_kind}:{field_key}"
                source_entry = {
                    "skill_id": skill_id,
                    "io_kind": io_kind,
                    "field_key": field_key,
                    "description": str(row.get("description") or "").strip(),
                    "value_type": value_type,
                    "required": bool(row.get("required", io_kind == "output")),
                    "source_ref": source_ref,
                }
                bucket = desired_fields.setdefault(
                    field_key,
                    {
                        "field_key": field_key,
                        "display_name": _humanize_field_key(field_key),
                        "description": source_entry["description"],
                        "value_type": value_type,
                        "data_contract_id": None,
                        "created_from_kind": "skill_io",
                        "created_from_ref": source_ref,
                        "source_hints_json": {"sources": []},
                        "manual_allowed": True,
                        "enabled": True,
                        "projection_mode": "derived",
                        "derived_from_kind": "skill_io",
                        "derived_from_ref": source_ref,
                        "sync_status": "active",
                        "last_synced_at": now,
                        "last_sync_error": None,
                    },
                )
                if not bucket.get("description") and source_entry["description"]:
                    bucket["description"] = source_entry["description"]
                if (
                    bucket.get("value_type") not in STRUCTURED_VALUE_TYPES
                    and value_type in STRUCTURED_VALUE_TYPES
                ):
                    bucket["value_type"] = value_type
                bucket_sources = bucket["source_hints_json"].setdefault("sources", [])
                bucket_sources.append(source_entry)

                if value_type in STRUCTURED_VALUE_TYPES:
                    contract_id = (
                        str(row.get("contract_id") or "").strip() or f"auto.{field_key}.v1"
                    )
                    contract_backrefs.append(
                        {
                            "skill_id": skill_id,
                            "io_kind": io_kind,
                            "field_key": field_key,
                            "contract_id": contract_id,
                        }
                    )
                    bucket["data_contract_id"] = contract_id

                    existing_contract = current_contracts.get(contract_id) or {}
                    desired_contracts.setdefault(
                        contract_id,
                        {
                            "contract_id": contract_id,
                            "version": str(existing_contract.get("version") or "1.0.0"),
                            "display_name": str(
                                existing_contract.get("display_name")
                                or _humanize_field_key(field_key)
                            ).strip(),
                            "description": str(
                                existing_contract.get("description") or source_entry["description"]
                            ).strip(),
                            "kind": value_type,
                            "schema_json": _default_schema_for_value_type(value_type),
                            "enabled": True,
                            "tags": list(existing_contract.get("tags") or ["derived", "auto"]),
                            "projection_mode": "derived",
                            "derived_from_kind": "field_shape",
                            "derived_from_ref": field_key,
                            "sync_status": "active",
                            "last_synced_at": now,
                            "last_sync_error": None,
                        },
                    )

    for workflow in workflows:
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        if not workflow_id:
            continue
        workflow_display_name = str(workflow.get("display_name") or workflow_id).strip()
        workflow_description = str(workflow.get("description") or "").strip()
        workflow_enabled = bool(workflow.get("enabled", True))
        callable_interface = _extract_workflow_callable_interface(
            workflow.get("settings"), steps=workflow.get("steps") or []
        )
        callable_enabled, callable_inputs, callable_outputs = _extract_workflow_callable_contract(
            workflow.get("settings")
        )
        callable_artifacts = list(callable_interface.get("returned_files") or [])
        if (
            not callable_enabled
            and not callable_inputs
            and not callable_outputs
            and not callable_artifacts
        ):
            continue

        block_inputs: list[dict[str, Any]] = []
        block_outputs: list[dict[str, Any]] = []

        for idx, row in enumerate(callable_inputs):
            field_key = row["name"]
            source_ref = f"{workflow_id}:callable:input:{field_key}"
            _register_public_field_source(
                desired_fields,
                desired_contracts,
                current_contracts,
                contract_backrefs,
                field_key=field_key,
                display_name=str(row.get("display_name") or "").strip(),
                description=str(row.get("description") or "").strip(),
                value_type=str(row.get("type") or "text").strip(),
                required=bool(row.get("required", False)),
                derived_from_kind="workflow_input",
                derived_from_ref=source_ref,
                source_entry={
                    "workflow_id": workflow_id,
                    "workflow_display_name": workflow_display_name,
                    "io_kind": "input",
                    "field_key": field_key,
                    "description": str(row.get("description") or "").strip(),
                    "value_type": str(row.get("type") or "text").strip(),
                    "required": bool(row.get("required", False)),
                    "source_ref": source_ref,
                },
                now=now,
            )
            block_inputs.append(
                {
                    "field_key": field_key,
                    "required": bool(row.get("required", False)),
                    "multiple_allowed": False,
                    "manual_allowed_override": None,
                    "default_source_hint": "",
                    "notes": "",
                    "sort_order": idx,
                }
            )

        for idx, row in enumerate(callable_outputs):
            field_key = row["name"]
            source_ref = f"{workflow_id}:callable:output:{field_key}"
            _register_public_field_source(
                desired_fields,
                desired_contracts,
                current_contracts,
                contract_backrefs,
                field_key=field_key,
                display_name=str(row.get("display_name") or "").strip(),
                description=str(row.get("description") or "").strip(),
                value_type=str(row.get("type") or "text").strip(),
                required=bool(row.get("required", True)),
                derived_from_kind="workflow_output",
                derived_from_ref=source_ref,
                source_entry={
                    "workflow_id": workflow_id,
                    "workflow_display_name": workflow_display_name,
                    "io_kind": "output",
                    "field_key": field_key,
                    "description": str(row.get("description") or "").strip(),
                    "value_type": str(row.get("type") or "text").strip(),
                    "required": bool(row.get("required", True)),
                    "source_ref": source_ref,
                },
                now=now,
            )
            block_outputs.append(
                {
                    "field_key": field_key,
                    "required_by_default": bool(row.get("required", True)),
                    "exposed_by_default": True,
                    "notes": "",
                    "sort_order": idx,
                }
            )

        output_offset = len(block_outputs)
        for idx, row in enumerate(callable_artifacts):
            if not isinstance(row, dict):
                continue
            field_key = str(row.get("name") or "").strip()
            if not field_key:
                continue
            source_ref = f"{workflow_id}:callable:artifact:{field_key}"
            _register_public_field_source(
                desired_fields,
                desired_contracts,
                current_contracts,
                contract_backrefs,
                field_key=field_key,
                display_name=str(row.get("label") or "").strip(),
                description=str(row.get("description") or "").strip(),
                value_type="json",
                required=bool(row.get("required", True)),
                derived_from_kind="workflow_artifact",
                derived_from_ref=source_ref,
                source_entry={
                    "workflow_id": workflow_id,
                    "workflow_display_name": workflow_display_name,
                    "io_kind": "artifact",
                    "field_key": field_key,
                    "description": str(row.get("description") or "").strip(),
                    "value_type": "json",
                    "required": bool(row.get("required", True)),
                    "source_ref": source_ref,
                },
                now=now,
            )
            block_outputs.append(
                {
                    "field_key": field_key,
                    "required_by_default": bool(row.get("required", True)),
                    "exposed_by_default": True,
                    "notes": "artifact",
                    "sort_order": output_offset + idx,
                }
            )

        block_key = f"workflow.{workflow_id}"
        enabled = bool(workflow_enabled and callable_enabled)
        sync_status = "active" if enabled else "disabled"
        desired_blocks[block_key] = {
            "block_key": block_key,
            "display_name": workflow_display_name,
            "block_type": "workflow",
            "source_kind": "workflow",
            "source_ref": workflow_id,
            "description": workflow_description,
            "family": "workflow",
            "enabled": enabled,
            "inputs": block_inputs,
            "outputs": block_outputs,
            "projection_mode": "derived",
            "derived_from_kind": "callable_workflow",
            "derived_from_ref": workflow_id,
            "sync_status": sync_status,
            "last_synced_at": now,
            "last_sync_error": None,
        }
        desired_blocks[block_key]["derived_hash"] = _stable_hash(
            {
                "block_key": block_key,
                "source_ref": workflow_id,
                "enabled": enabled,
                "inputs": block_inputs,
                "outputs": block_outputs,
                "output_mode": callable_interface.get("output_mode") or "none",
                "returned_files": callable_artifacts,
            }
        )

    for tool in tools:
        connector_spec = _extract_connector_projection_spec(tool)
        if not connector_spec:
            continue
        tool_id = connector_spec["tool_id"]
        tool_display_name = connector_spec["display_name"]
        tool_description = connector_spec["description"]
        tool_enabled = bool(connector_spec["enabled"])
        block_inputs: list[dict[str, Any]] = []
        block_outputs: list[dict[str, Any]] = []

        for idx, row in enumerate(connector_spec["inputs"]):
            field_key = row["name"]
            source_ref = f"{tool_id}:input:{field_key}"
            _register_public_field_source(
                desired_fields,
                desired_contracts,
                current_contracts,
                contract_backrefs,
                field_key=field_key,
                display_name=str(row.get("display_name") or "").strip(),
                description=str(row.get("description") or "").strip(),
                value_type=str(row.get("type") or "text").strip(),
                required=bool(row.get("required", False)),
                derived_from_kind="connector_input",
                derived_from_ref=source_ref,
                source_entry={
                    "tool_id": tool_id,
                    "tool_display_name": tool_display_name,
                    "io_kind": "input",
                    "field_key": field_key,
                    "description": str(row.get("description") or "").strip(),
                    "value_type": str(row.get("type") or "text").strip(),
                    "required": bool(row.get("required", False)),
                    "source_ref": source_ref,
                },
                now=now,
            )
            block_inputs.append(
                {
                    "field_key": field_key,
                    "required": bool(row.get("required", False)),
                    "multiple_allowed": False,
                    "manual_allowed_override": None,
                    "default_source_hint": "",
                    "notes": "",
                    "sort_order": idx,
                }
            )

        for idx, row in enumerate(connector_spec["outputs"]):
            field_key = row["name"]
            source_ref = f"{tool_id}:output:{field_key}"
            _register_public_field_source(
                desired_fields,
                desired_contracts,
                current_contracts,
                contract_backrefs,
                field_key=field_key,
                display_name=str(row.get("display_name") or "").strip(),
                description=str(row.get("description") or "").strip(),
                value_type=str(row.get("type") or "text").strip(),
                required=bool(row.get("required", True)),
                derived_from_kind="connector_output",
                derived_from_ref=source_ref,
                source_entry={
                    "tool_id": tool_id,
                    "tool_display_name": tool_display_name,
                    "io_kind": "output",
                    "field_key": field_key,
                    "description": str(row.get("description") or "").strip(),
                    "value_type": str(row.get("type") or "text").strip(),
                    "required": bool(row.get("required", True)),
                    "source_ref": source_ref,
                },
                now=now,
            )
            block_outputs.append(
                {
                    "field_key": field_key,
                    "required_by_default": bool(row.get("required", True)),
                    "exposed_by_default": True,
                    "notes": "",
                    "sort_order": idx,
                }
            )

        block_key = f"connector.{tool_id}"
        sync_status = "active" if tool_enabled else "disabled"
        desired_blocks[block_key] = {
            "block_key": block_key,
            "display_name": tool_display_name,
            "block_type": "connector",
            "source_kind": "connector",
            "source_ref": tool_id,
            "description": tool_description,
            "family": str(connector_spec.get("family") or "").strip(),
            "enabled": tool_enabled,
            "inputs": block_inputs,
            "outputs": block_outputs,
            "projection_mode": "derived",
            "derived_from_kind": "connector_tool",
            "derived_from_ref": tool_id,
            "sync_status": sync_status,
            "last_synced_at": now,
            "last_sync_error": None,
        }
        desired_blocks[block_key]["derived_hash"] = _stable_hash(
            {
                "block_key": block_key,
                "source_ref": tool_id,
                "enabled": tool_enabled,
                "inputs": block_inputs,
                "outputs": block_outputs,
            }
        )

    for field in desired_fields.values():
        field["source_hints_json"]["source_count"] = len(
            field["source_hints_json"].get("sources") or []
        )
        field["derived_hash"] = _stable_hash(
            {
                "field_key": field["field_key"],
                "value_type": field["value_type"],
                "data_contract_id": field["data_contract_id"],
                "sources": field["source_hints_json"].get("sources") or [],
            }
        )

    for contract in desired_contracts.values():
        contract["derived_hash"] = _stable_hash(
            {
                "contract_id": contract["contract_id"],
                "kind": contract["kind"],
                "schema_json": contract["schema_json"],
                "derived_from_ref": contract["derived_from_ref"],
            }
        )

    for agent in agents:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        agent_display_name = str(agent.get("display_name") or agent_id).strip()
        agent_enabled = bool(agent.get("enabled", True))
        for pair in agent.get("_skills") or []:
            skill_id = str(pair.get("skill_id") or "").strip()
            if not skill_id:
                continue
            skill = skill_map.get(skill_id)
            if skill is None:
                continue
            source_ref = f"{agent_id}::{skill_id}"
            block_key = f"agent.{agent_id}.{skill_id}"
            skill_display_name = str(skill.get("display_name") or skill_id).strip()
            skill_enabled = bool(skill.get("enabled", True))
            block_inputs = [
                {
                    "field_key": str(row.get("name") or "").strip(),
                    "required": bool(row.get("required", False)),
                    "multiple_allowed": False,
                    "manual_allowed_override": None,
                    "default_source_hint": "",
                    "notes": "",
                    "sort_order": idx,
                }
                for idx, row in enumerate(list(skill.get("_inputs") or []))
                if str(row.get("name") or "").strip()
            ]
            block_outputs = [
                {
                    "field_key": str(row.get("name") or "").strip(),
                    "required_by_default": bool(row.get("required", True)),
                    "exposed_by_default": True,
                    "notes": "",
                    "sort_order": idx,
                }
                for idx, row in enumerate(list(skill.get("_outputs") or []))
                if str(row.get("name") or "").strip()
            ]
            enabled = bool(agent_enabled and skill_enabled)
            sync_status = "active" if enabled else "disabled"
            desired_blocks[block_key] = {
                "block_key": block_key,
                "display_name": f"{agent_display_name} / {skill_display_name}",
                "block_type": "agent",
                "source_kind": "agent",
                "source_ref": source_ref,
                "description": str(skill.get("description") or "").strip(),
                "family": str(skill.get("category") or "").strip(),
                "enabled": enabled,
                "inputs": block_inputs,
                "outputs": block_outputs,
                "projection_mode": "derived",
                "derived_from_kind": "agent_skill_pair",
                "derived_from_ref": source_ref,
                "sync_status": sync_status,
                "last_synced_at": now,
                "last_sync_error": None,
            }
            desired_blocks[block_key]["derived_hash"] = _stable_hash(
                {
                    "block_key": block_key,
                    "source_ref": source_ref,
                    "enabled": enabled,
                    "inputs": block_inputs,
                    "outputs": block_outputs,
                }
            )

    return {
        "fields": desired_fields,
        "contracts": desired_contracts,
        "blocks": desired_blocks,
        "contract_backrefs": contract_backrefs,
    }


def _sync_data_contracts(
    db, current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    desired_keys = set(desired_rows)
    for contract_id, payload in desired_rows.items():
        existing = current_rows.get(contract_id)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
            continue
        if existing and _contract_row_matches(existing, payload):
            summary.unchanged += 1
            continue
        summary.created += 1 if not existing else 0
        summary.updated += 0 if not existing else 1
        db.save_data_contract(payload, author="projection_sync")

    for contract_id, row in current_rows.items():
        if str(row.get("projection_mode") or "manual") != "derived":
            continue
        if contract_id in desired_keys:
            continue
        orphaned = {
            **row,
            "enabled": False,
            "sync_status": "orphaned",
            "last_synced_at": datetime.now(UTC),
        }
        if str(row.get("sync_status") or "") == "orphaned" and row.get("enabled") is False:
            summary.unchanged += 1
            continue
        summary.orphaned += 1
        summary.disabled += 1
        db.save_data_contract(orphaned, author="projection_sync")


def _sync_fields(
    db, current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    desired_keys = set(desired_rows)
    for field_key, payload in desired_rows.items():
        existing = current_rows.get(field_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
            continue
        if existing and _field_row_matches(existing, payload):
            summary.unchanged += 1
            continue
        summary.created += 1 if not existing else 0
        summary.updated += 0 if not existing else 1
        db.save_field(payload, author="projection_sync")

    for field_key, row in current_rows.items():
        if str(row.get("projection_mode") or "manual") != "derived":
            continue
        if field_key in desired_keys:
            continue
        orphaned = {
            **row,
            "enabled": False,
            "sync_status": "orphaned",
            "last_synced_at": datetime.now(UTC),
        }
        if str(row.get("sync_status") or "") == "orphaned" and row.get("enabled") is False:
            summary.unchanged += 1
            continue
        summary.orphaned += 1
        summary.disabled += 1
        db.save_field(orphaned, author="projection_sync")


def _sync_blocks(
    db, current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    desired_keys = set(desired_rows)
    for block_key, payload in desired_rows.items():
        existing = current_rows.get(block_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
            continue
        if existing and _block_row_matches(existing, payload):
            summary.unchanged += 1
            continue
        summary.created += 1 if not existing else 0
        summary.updated += 0 if not existing else 1
        db.save_published_block_contract(payload, author="projection_sync")

    for block_key, row in current_rows.items():
        if str(row.get("projection_mode") or "manual") != "derived":
            continue
        if block_key in desired_keys:
            continue
        if str(row.get("sync_status") or "") == "orphaned" and row.get("enabled") is False:
            summary.unchanged += 1
            continue
        full = db.get_published_block_contract(block_key) or row
        orphaned = {
            **full,
            "enabled": False,
            "sync_status": "orphaned",
            "last_synced_at": datetime.now(UTC),
            "inputs": list(full.get("inputs") or full.get("_inputs") or []),
            "outputs": list(full.get("outputs") or full.get("_outputs") or []),
        }
        summary.orphaned += 1
        summary.disabled += 1
        db.save_published_block_contract(orphaned, author="projection_sync")


def _apply_skill_contract_backrefs(db, backrefs: list[dict[str, str]]) -> None:
    if not backrefs:
        return
    affected_skills: set[str] = set()
    with db._transaction() as cur:
        for item in backrefs:
            skill_id = str(item.get("skill_id") or "").strip()
            field_key = str(item.get("field_key") or "").strip()
            contract_id = str(item.get("contract_id") or "").strip()
            io_kind = str(item.get("io_kind") or "").strip().lower()
            if (
                not skill_id
                or not field_key
                or not contract_id
                or io_kind not in {"input", "output"}
            ):
                continue
            affected_skills.add(skill_id)
            table_name = "registry_skill_inputs" if io_kind == "input" else "registry_skill_outputs"
            update_query = "\n".join(
                [
                    "UPDATE ",
                    table_name,
                    "SET contract_id = %s",
                    "WHERE skill_id = %s",
                    "  AND name = %s",
                    "  AND COALESCE(contract_id, '') <> %s",
                ]
            )
            cur.execute(
                update_query,
                (contract_id, skill_id, field_key, contract_id),
            )
        for skill_id in affected_skills:
            schema_builder = getattr(db, "_build_contract_schema", None)
            cur.execute(
                """SELECT name, type, description, required, contract_id
                   FROM registry_skill_inputs
                   WHERE skill_id = %s
                   ORDER BY sort_order""",
                (skill_id,),
            )
            fetchall = getattr(cur, "fetchall", None)
            inputs = [dict(row) for row in ((fetchall() or []) if callable(fetchall) else [])]
            cur.execute(
                """SELECT name, type, description, required, contract_id
                   FROM registry_skill_outputs
                   WHERE skill_id = %s
                   ORDER BY sort_order""",
                (skill_id,),
            )
            outputs = [dict(row) for row in ((fetchall() or []) if callable(fetchall) else [])]
            input_schema = schema_builder(inputs) if callable(schema_builder) else None
            output_schema = schema_builder(outputs) if callable(schema_builder) else None
            if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
                from core.application.registry.db_store import RegistryDBStore

                input_schema = RegistryDBStore._build_contract_schema(inputs)
                output_schema = RegistryDBStore._build_contract_schema(outputs)
            cur.execute(
                """UPDATE registry_skills
                   SET input_schema = %s,
                       output_schema = %s
                   WHERE skill_id = %s""",
                (
                    json.dumps(input_schema),
                    json.dumps(output_schema),
                    skill_id,
                ),
            )


def _summarize_data_contract_diff(
    current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    for contract_id, payload in desired_rows.items():
        existing = current_rows.get(contract_id)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
        elif not existing:
            summary.created += 1
        elif _contract_row_matches(existing, payload):
            summary.unchanged += 1
        else:
            summary.updated += 1
    for contract_id, row in current_rows.items():
        if (
            str(row.get("projection_mode") or "manual") == "derived"
            and contract_id not in desired_rows
        ):
            summary.orphaned += 1
            summary.disabled += 1


def _summarize_field_diff(
    current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    for field_key, payload in desired_rows.items():
        existing = current_rows.get(field_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
        elif not existing:
            summary.created += 1
        elif _field_row_matches(existing, payload):
            summary.unchanged += 1
        else:
            summary.updated += 1
    for field_key, row in current_rows.items():
        if (
            str(row.get("projection_mode") or "manual") == "derived"
            and field_key not in desired_rows
        ):
            summary.orphaned += 1
            summary.disabled += 1


def _summarize_block_diff(
    current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    for block_key, payload in desired_rows.items():
        existing = current_rows.get(block_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
        elif not existing:
            summary.created += 1
        elif _block_row_matches(existing, payload):
            summary.unchanged += 1
        else:
            summary.updated += 1
    for block_key, row in current_rows.items():
        if (
            str(row.get("projection_mode") or "manual") == "derived"
            and block_key not in desired_rows
        ):
            summary.orphaned += 1
            summary.disabled += 1


def _field_row_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        str(existing.get("projection_mode") or "manual") == "derived"
        and str(existing.get("sync_status") or "active")
        == str(payload.get("sync_status") or "active")
        and bool(existing.get("enabled", True)) == bool(payload.get("enabled", True))
        and str(existing.get("derived_hash") or "") == str(payload.get("derived_hash") or "")
        and str(existing.get("data_contract_id") or "")
        == str(payload.get("data_contract_id") or "")
    )


def _contract_row_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        str(existing.get("projection_mode") or "manual") == "derived"
        and str(existing.get("sync_status") or "active")
        == str(payload.get("sync_status") or "active")
        and bool(existing.get("enabled", True)) == bool(payload.get("enabled", True))
        and str(existing.get("derived_hash") or "") == str(payload.get("derived_hash") or "")
        and str(existing.get("kind") or "json") == str(payload.get("kind") or "json")
    )


def _block_row_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        str(existing.get("projection_mode") or "manual") == "derived"
        and str(existing.get("sync_status") or "active")
        == str(payload.get("sync_status") or "active")
        and bool(existing.get("enabled", True)) == bool(payload.get("enabled", True))
        and str(existing.get("derived_hash") or "") == str(payload.get("derived_hash") or "")
    )
