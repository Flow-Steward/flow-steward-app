"""The immutable definition a run executes, and its content digest.

Before the first dispatch a job pins the definition it runs: steps, Groups,
For each containers, the execution graph and the workflow settings that configure them.
Runs of the same revision share one archived snapshot; the job and every
checkpoint carry only its id, revision and digest. Editing the workflow never
changes a run, and a run's history stays readable after the workflow changes
or is deleted.

The snapshot holds no runtime state, inputs, files or secret values: secret
references stay references.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING
from core.application.workflows.workflow_definition_service import definition_from_workflow
from core.application.workflows.workflow_portability_policy import WorkflowPortabilityPolicyMixin

DEFINITION_SNAPSHOT_SCHEMA_VERSION = 1


def frozen_definition(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """The definition part of a loaded workflow row, as a run executes it."""
    definition = definition_from_workflow(workflow)
    settings = workflow.get("settings")
    settings = dict(settings) if isinstance(settings, Mapping) else {}
    for key in (EXECUTION_GRAPH_SETTING, "canvas_annotations", "canvas_annotations_revision"):
        settings.pop(key, None)
    return {
        "workflow_id": str(workflow.get("workflow_id") or "").strip(),
        # Whether the run's definition is a protected author graph: Job Details
        # then shows it read-only and redacted, as the canvas does.
        "protected": WorkflowPortabilityPolicyMixin(None).export_policy_for_workflow(
            dict(workflow)
        )["protected"],
        "steps": definition["steps"],
        "groups": definition["groups"],
        "for_each": definition["for_each"],
        "execution_graph": definition["execution_graph"],
        "settings": settings,
    }


def canonical_json(value: Any) -> str:
    """UTF-8 JSON with sorted object keys, array order kept, no extra whitespace.

    Non-finite numbers are refused: they have no JSON form, and a digest over a
    lossy serialization would let two different definitions share an identity.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def definition_digest(
    definition: Mapping[str, Any], *, schema_version: int = DEFINITION_SNAPSHOT_SCHEMA_VERSION
) -> str:
    """SHA-256 of the canonical definition, including its schema version."""
    payload = canonical_json({"schema_version": schema_version, "definition": definition})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def definition_ref(snapshot_id: str, revision: int, digest: str) -> dict[str, Any]:
    """The reference a checkpoint carries; the job row holds the canonical FK."""
    return {"snapshot_id": str(snapshot_id), "revision": int(revision), "digest": str(digest)}


__all__ = [
    "DEFINITION_SNAPSHOT_SCHEMA_VERSION",
    "canonical_json",
    "definition_digest",
    "definition_ref",
    "frozen_definition",
]
