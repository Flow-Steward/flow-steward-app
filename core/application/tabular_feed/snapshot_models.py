"""Scoped snapshot references and durable publication results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SnapshotScope:
    """The existing account/project/workflow/profile snapshot namespace.

    Workflow fallback and profile slug rules deliberately match object metadata
    keys. Job/run/step identity never participates in the publication head.
    """

    account_id: str
    project_id: str
    workflow_id: str | None
    profile_key: str

    def __post_init__(self) -> None:
        for name in ("account_id", "project_id"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError("snapshot_scope_required")
            object.__setattr__(self, name, value)
        workflow = str(self.workflow_id or "").strip() or "workflow"
        profile = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.profile_key or "").strip())
        profile = profile.strip("._-")[:120]
        if not profile:
            raise ValueError("profile_key is required")
        object.__setattr__(self, "workflow_id", workflow)
        object.__setattr__(self, "profile_key", profile)

    def as_tuple(self) -> tuple[str, str, str, str]:
        return self.account_id, self.project_id, str(self.workflow_id), self.profile_key


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    revision: int
    snapshot_artifact_id: str = ""

    def __post_init__(self) -> None:
        artifact = self.snapshot_artifact_id
        if (
            type(self.revision) is not int
            or self.revision < 0
            or not isinstance(artifact, str)
            or artifact != artifact.strip()
            or (self.revision == 0) != (artifact == "")
        ):
            raise ValueError("invalid_snapshot_ref")

    def as_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "snapshot_artifact_id": self.snapshot_artifact_id}


@dataclass(frozen=True, slots=True)
class SnapshotHead:
    ref: SnapshotRef
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SnapshotCommitReceipt:
    ref: SnapshotRef
    previous_snapshot_artifact_id: str
    metadata: dict[str, Any]
