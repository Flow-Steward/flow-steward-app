"""Stable-ID internal project context operations."""

from __future__ import annotations

from core.application.memory.models import EvidenceRef, MemoryRecordInput
from core.application.memory.runtime import build_memory_services


def load_project_context(*, account_id: str, project_id: str) -> str:
    """Load the exact project-owned canonical profile, without account fallback."""
    normalized_account_id = str(account_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    if not normalized_account_id or not normalized_project_id:
        return ""
    record = build_memory_services().retrieval.exact_project_profile(
        account_id=normalized_account_id,
        project_id=normalized_project_id,
    )
    return f"## Project Profile\n{record.content}" if record is not None else ""


def sync_project_context(
    *,
    account_id: str,
    project_id: str,
    project_name: str,
    project_rules: str,
    assigned_data_connections: list[dict[str, object]],
) -> None:
    """Create and promote the canonical profile for one exact project scope."""
    normalized_account_id = str(account_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    normalized_name = str(project_name or "").strip()
    if not normalized_account_id or not normalized_project_id:
        raise ValueError("account_id and project_id are required for project context sync")

    connection_rows = [
        dict(item)
        for item in assigned_data_connections
        if isinstance(item, dict)
        and str(item.get("connection_id") or item.get("display_name") or "").strip()
    ]
    parts = [f"Project: {normalized_name}"]
    if connection_rows:
        lines: list[str] = []
        for connection in connection_rows:
            display_name = str(
                connection.get("display_name")
                or connection.get("connection_id")
                or "Data connection"
            ).strip()
            engine_hint = str(
                connection.get("engine_hint") or connection.get("source_kind") or ""
            ).strip()
            status = str(
                connection.get("status") or connection.get("health_status") or "unknown"
            ).strip()
            line = f"  - {display_name}"
            if engine_hint:
                line += f" [{engine_hint}]"
            lines.append(f"{line} ({status or 'unknown'})")
        parts.append("Assigned Data Connections:\n" + "\n".join(lines))
    normalized_rules = str(project_rules or "")
    if normalized_rules:
        parts.append(f"Project Rules / Conventions:\n{normalized_rules[:2000]}")

    services = build_memory_services()
    record = services.writer.create_candidate(
        MemoryRecordInput(
            account_id=normalized_account_id,
            tenant_id=None,
            project_id=normalized_project_id,
            created_by="project_profile_sync",
            memory_type="docs_note",
            visibility="project",
            lifecycle_status="active",
            title=f"{normalized_name} project profile",
            summary=f"Project profile for {normalized_name}",
            content="\n\n".join(parts)[:12000],
            source_type="project_profile_sync",
            source_id=f"project:{normalized_project_id}:profile",
            source_trust="canonical_doc",
            importance=1.0,
            tags=["project_profile", "project_kb"],
            metadata={
                "project_name": normalized_name,
                "project_rules": normalized_rules[:2000],
                "assigned_data_connections": connection_rows,
            },
            evidence_refs=[
                EvidenceRef(
                    ref_type="project",
                    ref_id=normalized_project_id,
                    label=normalized_name,
                )
            ],
        )
    )
    services.writer.promote(record.id, actor_id="project_profile_sync", force=True)
