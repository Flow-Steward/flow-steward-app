"""
Canonical TaskEnvelope contract used by task-oriented entry paths.

This model is intentionally strict for first release: no older aliases.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import uuid4

_VALID_PRIORITIES = {"low", "medium", "high", "critical"}

#: Task sources that use the persisted Test Run continuation contract.
TEST_RUN_SOURCES = frozenset({"workflow_test_run", "step_test_run"})


def _normalized_task_type(raw: object) -> str:
    return str(raw or "").strip().lower()


def _normalized_priority(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value in _VALID_PRIORITIES:
        return value
    return "medium"


def _as_dict(raw: object) -> dict:
    return dict(raw) if isinstance(raw, dict) else {}


def _as_files(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _entry_path_for_source(source_system: str) -> str:
    source = str(source_system or "").strip().lower()
    if source in {"manual", "web", "ui"}:
        return "manual_ui"
    if source in {"api", "manual_api"}:
        return "manual_api"
    if source in {"schedule", "scheduled"}:
        return "schedule"
    return "task_ingress"


@dataclass
class TaskEnvelope:
    envelope_id: str = ""
    entry_path: str = "manual_ui"
    source_system: str = "manual"
    source_event_id: str = ""
    source_object_id: str = ""
    task_type: str = ""
    title: str = ""
    description: str = ""
    priority: str = "medium"
    project_id: str = ""
    requested_workflow_id: str = ""
    inputs: dict = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    entry_context: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_system = str(self.source_system or "manual").strip() or "manual"
        self.entry_path = str(self.entry_path or "").strip() or _entry_path_for_source(
            self.source_system
        )
        self.source_event_id = str(self.source_event_id or "").strip()
        self.source_object_id = str(self.source_object_id or "").strip()
        self.task_type = _normalized_task_type(self.task_type)
        self.title = str(self.title or "").strip()
        self.description = str(self.description or "").strip()
        self.priority = _normalized_priority(self.priority)
        self.project_id = str(self.project_id or "").strip()
        self.requested_workflow_id = str(self.requested_workflow_id or "").strip()
        self.metadata = _as_dict(self.metadata)
        self.entry_context = _as_dict(self.entry_context)
        self.files = _as_files(self.files)
        self.inputs = _as_dict(self.inputs)
        if not self.inputs:
            self.inputs = {
                "title": self.title,
                "description": self.description,
                "priority": self.priority,
            }
        if not self.envelope_id:
            self.envelope_id = (
                self.source_event_id
                or self.source_object_id
                or str(self.entry_context.get("request_id") or "").strip()
                or uuid4().hex
            )
        self.envelope_id = str(self.envelope_id).strip() or uuid4().hex

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TaskEnvelope:
        data = _as_dict(d)
        return cls(
            envelope_id=str(data.get("envelope_id") or "").strip(),
            entry_path=str(data.get("entry_path") or "").strip(),
            source_system=str(data.get("source_system") or "").strip() or "manual",
            source_event_id=str(data.get("source_event_id") or "").strip(),
            source_object_id=str(data.get("source_object_id") or "").strip(),
            task_type=_normalized_task_type(data.get("task_type")),
            title=str(data.get("title") or "").strip(),
            description=str(data.get("description") or "").strip(),
            priority=_normalized_priority(data.get("priority")),
            project_id=str(data.get("project_id") or "").strip(),
            requested_workflow_id=str(data.get("requested_workflow_id") or "").strip(),
            inputs=_as_dict(data.get("inputs")),
            files=_as_files(data.get("files")),
            metadata=_as_dict(data.get("metadata")),
            entry_context=_as_dict(data.get("entry_context")),
        )
