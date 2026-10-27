"""Serializable presentation data shared by note authoring and bundle import."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ANNOTATIONS_SETTING = "canvas_annotations"
ANNOTATIONS_REVISION_SETTING = "canvas_annotations_revision"


class WorkflowAnnotationsRevisionConflictError(RuntimeError):
    """A note writer used a presentation version that is no longer current."""

    def __init__(self, workflow_id: str, expected_revision: int):
        self.workflow_id = workflow_id
        self.expected_annotations_revision = expected_revision
        super().__init__(
            f"Workflow '{workflow_id}' annotations revision conflict (expected {expected_revision})"
        )


ANNOTATION_IMPORT_ERROR = (
    "Canvas notes need unique identifiers and valid text, position and size. "
    "Attach images after importing; local artifact references cannot be imported."
)


class CanvasAnnotation(BaseModel):
    """Artifact references are service-owned and are not editable note fields."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(max_length=20000)
    x: float = Field(ge=-1000000, le=1000000)
    y: float = Field(ge=-1000000, le=1000000)
    width: float = Field(ge=160, le=4000)
    height: float = Field(ge=100, le=4000)


def normalize_portable_annotations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError(ANNOTATION_IMPORT_ERROR)
    notes = [CanvasAnnotation.model_validate(note).model_dump() for note in value]
    if len({note["id"] for note in notes}) != len(notes):
        raise ValueError(ANNOTATION_IMPORT_ERROR)
    return notes
