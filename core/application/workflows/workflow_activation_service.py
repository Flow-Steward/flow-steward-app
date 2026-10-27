"""Validate and activate the same locked workflow revision."""

from collections.abc import Callable
from typing import Any


class WorkflowActivationValidationError(ValueError):
    def __init__(self, validation: dict[str, Any]) -> None:
        super().__init__("Workflow cannot be enabled")
        self.validation = validation


def toggle_workflow_enabled(
    db: Any,
    workflow_id: str,
    *,
    authorize: Callable[[dict[str, Any]], None],
    validate: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    """Serialize the existing toggle with all definition writes and validation."""
    with db._transaction() as cursor:
        cursor.execute(
            "SELECT revision FROM registry_workflows "
            "WHERE workflow_id = %s AND deleted_at IS NULL FOR UPDATE",
            (workflow_id,),
        )
        if cursor.fetchone() is None:
            raise KeyError(workflow_id)
        workflow = db.get_workflow(workflow_id)
        if not workflow:
            raise KeyError(workflow_id)
        authorize(workflow)
        if not workflow.get("enabled", False):
            validation = validate(workflow)
            if not validation.get("valid", False):
                raise WorkflowActivationValidationError(validation)
        return bool(db.toggle_workflow(workflow_id))
