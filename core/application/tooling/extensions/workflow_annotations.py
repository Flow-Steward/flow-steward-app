"""Apply the install-time note contract during developer bundle validation."""

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.application.extensions.extension_descriptor import ExtensionDescriptorArtifacts
from core.application.extensions.lifecycle_descriptor_io import materialize_descriptor_artifacts
from core.application.workflows.workflow_canvas_annotation_contract import ANNOTATIONS_SETTING


def workflow_annotation_errors(descriptor: Any, bundle_root: Path) -> list[str]:
    artifacts = descriptor.get("artifacts") if isinstance(descriptor, dict) else None
    workflows = artifacts.get("workflows") if isinstance(artifacts, dict) else None
    if not isinstance(workflows, list) or not workflows:
        return []
    materialized, errors = materialize_descriptor_artifacts(
        {"artifacts": {"workflows": workflows}}, bundle_root=bundle_root
    )
    if errors:
        return [f"{item['path']}: {item['message']}" for item in errors]
    annotated = []
    for entry in materialized["artifacts"]["workflows"]:
        snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
        settings = snapshot.get("settings") if isinstance(snapshot, dict) else None
        if isinstance(settings, dict) and ANNOTATIONS_SETTING in settings:
            annotated.append(entry)
    if not annotated:
        return []
    try:
        ExtensionDescriptorArtifacts.model_validate({"workflows": annotated})
    except ValidationError as exc:
        # Match install-time validation without a second copy of the note grammar.
        return [f"Workflow canvas_annotations: {error['msg']}" for error in exc.errors()]
    return []
