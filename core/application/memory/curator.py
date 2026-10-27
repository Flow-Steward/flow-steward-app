from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import CuratedBatch, CuratedRecordCandidate, EvidenceRef, MemoryRecordInput


def _first_sentence(text: str, *, fallback: str) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return fallback
    for delimiter in (". ", "\n", "• "):
        if delimiter in normalized:
            head = normalized.split(delimiter, 1)[0].strip()
            if head:
                return head[:180]
    return normalized[:180]


def _scope_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value or "").strip()
    return normalized or None


class MemoryCurator:
    def curate_completed_job(self, job_payload: Mapping[str, Any]) -> CuratedBatch:
        row = dict(job_payload or {})
        job_id = str(row.get("id") or row.get("job_id") or "").strip()
        account_id = str(row.get("account_id") or "acc_default").strip() or "acc_default"
        project_id = _scope_id(row.get("project_id"))
        task_id = str(row.get("task_id") or "").strip()
        title = str(row.get("task_summary") or "").strip() or f"Job {job_id}"
        final_report = str(row.get("final_report") or row.get("error") or "").strip()
        summary = _first_sentence(final_report, fallback=title)
        content_parts = [f"Task summary: {title}"]
        if final_report:
            content_parts.append(f"Final report: {final_report}")
        metadata = {
            "task_id": task_id,
            "final_status": str(row.get("status") or "").strip() or "completed",
        }
        evidence = [
            EvidenceRef(ref_type="job", ref_id=job_id, label=title),
        ]
        if row.get("thread_id"):
            evidence.append(
                EvidenceRef(ref_type="thread", ref_id=str(row.get("thread_id")), label="job thread")
            )

        candidate = CuratedRecordCandidate(
            record=MemoryRecordInput(
                account_id=account_id,
                tenant_id=str(row.get("tenant_id") or "").strip() or None,
                project_id=project_id,
                workflow_id=str(row.get("selected_workflow") or "").strip() or None,
                job_id=_scope_id(job_id),
                created_by="memory_curator",
                memory_type="lesson",
                visibility="project" if project_id is not None else "account",
                lifecycle_status="candidate",
                title=title[:240],
                summary=summary[:600],
                content="\n\n".join(content_parts)[:12000],
                source_type="job_completion",
                source_id=job_id or task_id or title,
                source_trust="system_generated",
                importance=0.7,
                tags=["task_record", "job_completion", "project_kb"],
                metadata=metadata,
                evidence_refs=evidence,
            ),
            decision="ADD",
            recommended_action="ADD",
            evidence_refs=evidence,
        )
        return CuratedBatch(
            source_kind="job",
            source_id=job_id or task_id or title,
            candidates=[candidate],
        )

    def curate_agent_messages(self, message_rows: Iterable[Mapping[str, Any]]) -> CuratedBatch:
        candidates: list[CuratedRecordCandidate] = []
        source_id = ""
        for row in message_rows:
            item = dict(row or {})
            source_id = source_id or str(item.get("job_id") or item.get("id") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if not summary:
                continue
            status = str(item.get("status") or "").strip().lower()
            memory_type = (
                "verification_result" if status in {"pass", "fail"} else "workflow_pattern"
            )
            evidence = [
                EvidenceRef(
                    ref_type="agent_message",
                    ref_id=str(item.get("id") or item.get("correlation_id") or ""),
                    label=str(item.get("from_agent") or "agent message"),
                )
            ]
            candidates.append(
                CuratedRecordCandidate(
                    record=MemoryRecordInput(
                        account_id=str(item.get("account_id") or "acc_default"),
                        project_id=_scope_id(item.get("project_id")),
                        job_id=_scope_id(item.get("job_id")),
                        workflow_id=str(item.get("workflow_id") or "").strip() or None,
                        created_by="memory_curator",
                        memory_type=memory_type,
                        visibility="project" if item.get("project_id") is not None else "account",
                        lifecycle_status="candidate",
                        title=_first_sentence(summary, fallback="Agent memory"),
                        summary=summary[:600],
                        content=summary[:4000],
                        source_type="agent_message",
                        source_id=str(item.get("id") or item.get("correlation_id") or ""),
                        source_trust="tool_observed",
                        importance=0.35,
                        tags=["agent_message", str(item.get("from_agent") or "").strip()],
                        metadata={
                            "status": status,
                            "from_agent": str(item.get("from_agent") or "").strip(),
                            "skill": str(item.get("skill") or "").strip(),
                        },
                        evidence_refs=evidence,
                    ),
                    decision="ADD",
                    recommended_action="ADD",
                    evidence_refs=evidence,
                )
            )
        return CuratedBatch(
            source_kind="agent_messages", source_id=source_id, candidates=candidates
        )
