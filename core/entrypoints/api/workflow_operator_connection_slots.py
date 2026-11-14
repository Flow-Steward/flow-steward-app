"""Thin HTTP adapters of a project's connection choices for workflow slots (ADR 0068).

Every route calls the one ``WorkflowOperatorService`` method; the slot service
behind it is the one the pin at job submission uses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceError

_ProjectId = Annotated[str, Path(description="Provisioned project identifier.")]
_WorkflowId = Annotated[str, Path(description="Identifier of the workflow linked to the project.")]
_Slot = Annotated[str, Path(description="Name of a connection slot the workflow declares.")]


class ConnectionSlotOption(BaseModel):
    connection_id: str
    display_name: str
    connection_type: str = ""
    extension_install_id: str = ""


class ConnectionSlotState(BaseModel):
    slot: str
    label: str
    extension_id: str
    connection_type_ids: list[str] = Field(default_factory=list)
    connection_id: str | None = None
    choice: str = Field(description="'chosen', 'automatic' (the only usable one) or 'none'.")
    ready: bool
    reason: str = ""
    options: list[ConnectionSlotOption] = Field(default_factory=list)


class ConnectionSlotsResult(BaseModel):
    workflow_id: str
    project_id: str
    ready: bool
    slots: list[ConnectionSlotState] = Field(default_factory=list)


class ConnectionSlotChoiceBody(BaseModel):
    """The connection for one slot; an empty value clears the project's choice."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"connection_id": "conn_5f2c1d"}},
    )

    connection_id: str | None = None
    release_id: str = Field(
        default="",
        description="Choose for a slot of this pending release, before the update activates.",
    )


_EXAMPLE = {
    "workflow_id": "extension.inst_2b1f.sync_orders",
    "project_id": "df0d2afe-7064-4206-941b-61d2d4db425c",
    "ready": True,
    "slots": [
        {
            "slot": "store",
            "label": "Store account",
            "extension_id": "acme.store",
            "connection_type_ids": ["api_token"],
            "connection_id": "conn_5f2c1d",
            "choice": "automatic",
            "ready": True,
            "reason": "",
            "options": [
                {
                    "connection_id": "conn_5f2c1d",
                    "display_name": "Main store",
                    "connection_type": "api_token",
                    "extension_install_id": "inst_2b1f",
                }
            ],
        }
    ],
}


def _json(description: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": description, "content": {"application/json": {"example": _EXAMPLE}}}
    }


def register_workflow_connection_slot_routes(
    router: APIRouter,
    *,
    workflow_operator_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> None:
    def _call(request: Request, project_id: str, method: str, **arguments: Any):
        try:
            scope = request_scope(request, project_id=project_id)
            return getattr(workflow_operator_service(), method)(
                project_id=project_id, scope=scope, **arguments
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/connection-slots",
        response_model=ConnectionSlotsResult,
        operation_id="listProjectWorkflowConnectionSlots",
        summary="List the connections a workflow needs.",
        description=(
            "Every connection slot the extension workflow declares, the connection that "
            "fills it in the project and, when none does, the reason the workflow is not ready."
        ),
        responses=_json("Slots with the project's connections."),
    )
    async def api_workflow_connection_slots(
        request: Request, project_id: _ProjectId, workflow_id: _WorkflowId
    ):
        return _call(request, project_id, "connection_slots_get", workflow_id=workflow_id)

    @router.put(
        "/api/projects/{project_id}/workflows/{workflow_id}/connection-slots/{slot}",
        response_model=ConnectionSlotsResult,
        operation_id="setProjectWorkflowConnectionSlot",
        summary="Choose the connection of a workflow slot.",
        description=(
            "Store the project's connection for one slot; new runs, webhooks and schedules "
            "use it. An empty connection_id clears the choice."
        ),
        responses=_json("Slots after the change."),
    )
    async def api_workflow_connection_slot_set(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        slot: _Slot,
        body: ConnectionSlotChoiceBody,
    ):
        return _call(
            request,
            project_id,
            "connection_slot_set",
            workflow_id=workflow_id,
            slot=slot,
            connection_id=body.connection_id,
            release_id=body.release_id,
        )


__all__ = ["register_workflow_connection_slot_routes"]
