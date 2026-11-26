"""HTTP surface and settings page of the reference notes module (app role only).

Everything Core-owned comes from the runtime container Core hands ``register``:
the loaded identity, the auth helpers (``require_authenticated_user`` and
``require_entitlement``), the ``db`` service factory, the route permission
registration point and the page registries. The only Core import is the page
registry function, which the app source slice ships.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fs_reference_notes._features import PINNED_COLUMN
from fs_reference_notes.repository import (
    NoteRepository,
    NotesUnavailableError,
    module_schema,
    valid_note_id,
)

#: The feature an account needs for every route and for the page.
FEATURE = "reference_notes"
#: Core permission the write routes need. Reads fall to Core's generic API read
#: rule (``platform.read``), so a viewer can list notes but not change them.
WRITE_PERMISSION = "extensions.use"
#: Page visibility: the settings page is shown to operators and admins.
PAGE_PERMISSION = "operator"
PAGE_KEY = "reference_notes"
PAGE_PATH = "/settings/reference-notes"

_logger = logging.getLogger(__name__)


class NoteFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=10_000)


class PinnedNoteFields(NoteFields):
    pinned: bool | None = None


#: The request body of this version: 1.1.0 adds ``pinned``.
NoteInput = PinnedNoteFields if PINNED_COLUMN else NoteFields


def install(app: FastAPI, container: Any) -> None:
    identity = container.metadata["platform_module"]
    module_id = str(identity["id"])
    prefix = f"/api/modules/{module_id}"
    repository = NoteRepository(
        container.service_factories["db"],
        schema=module_schema(module_id),
        pinned_column=PINNED_COLUMN,
    )
    require_entitlement = container.auth_helpers["require_entitlement"]
    router = build_router(
        prefix=prefix,
        require_user=container.auth_helpers["require_authenticated_user"],
        require_feature=require_entitlement(FEATURE),
        repository=repository,
    )
    app.include_router(router)

    routes = container.registries["module_route_permissions"]
    routes.register(methods=("POST",), pattern="notes", permission=WRITE_PERMISSION)
    routes.register(methods=("PUT", "DELETE"), pattern="notes/[^/]+", permission=WRITE_PERMISSION)

    _register_page(container.registries, prefix=prefix)
    _logger.info(
        "Platform module %s %s mounted %s", module_id, identity.get("version", "?"), prefix
    )


def build_router(
    *,
    prefix: str,
    require_user: Callable[..., Any],
    require_feature: Callable[[Request], None],
    repository: NoteRepository,
) -> APIRouter:
    def current_account(request: Request) -> str:
        try:
            user = require_user(request)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail="Authentication required") from exc
        account_id = str(user.get("account_id") or "").strip() if isinstance(user, Mapping) else ""
        if not account_id:
            raise HTTPException(status_code=403, detail="The caller has no account")
        return account_id

    def call(operation: Callable[..., Any], *args: Any) -> Any:
        try:
            return operation(*args)
        except NotesUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Notes are unavailable") from exc

    # Authentication first, so an anonymous caller gets 401 rather than 403.
    router = APIRouter(
        prefix=prefix, dependencies=[Depends(current_account), Depends(require_feature)]
    )

    @router.get("/notes")
    def list_notes(account_id: str = Depends(current_account)) -> dict[str, Any]:
        return {"items": call(repository.list_notes, account_id)}

    @router.post("/notes", status_code=201)
    def create_note(
        payload: NoteInput, account_id: str = Depends(current_account)
    ) -> dict[str, Any]:
        return {"item": call(repository.create_note, account_id, payload.model_dump())}

    @router.put("/notes/{note_id}")
    def update_note(
        note_id: str, payload: NoteInput, account_id: str = Depends(current_account)
    ) -> dict[str, Any]:
        note = call(repository.update_note, account_id, note_id, payload.model_dump())
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found")
        return {"item": note}

    @router.delete("/notes/{note_id}")
    def delete_note(note_id: str, account_id: str = Depends(current_account)) -> dict[str, Any]:
        if not valid_note_id(note_id) or not call(repository.delete_note, account_id, note_id):
            raise HTTPException(status_code=404, detail="Note not found")
        return {"deleted": True, "id": note_id}

    return router


def _fields() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = [
        {"key": "title", "label": "Title", "type": "text", "required": True},
        {"key": "body", "label": "Text", "type": "textarea"},
    ]
    if PINNED_COLUMN:
        fields.append({"key": "pinned", "label": "Pinned", "type": "checkbox", "default": False})
    return fields


def _register_page(registries: Any, *, prefix: str) -> None:
    from core.application.reports.ui_page_registry import register_settings_page

    register_settings_page(
        registries,
        page_key=PAGE_KEY,
        title="Reference notes",
        path=PAGE_PATH,
        nav_section="settings_hub",
        render_mode="react_shell",
        required_permission=PAGE_PERMISSION,
        required_entitlement=FEATURE,
        settings_tab=True,
        sort_order=900,
        control_surface={
            "kind": "resource_admin",
            "description": "Notes of your account, kept by the reference notes module.",
            "resources": [
                {
                    "key": "notes",
                    "title": "Notes",
                    "id_field": "id",
                    "display_field": "title",
                    "list_path": f"{prefix}/notes",
                    "item_path": f"{prefix}/notes/{{note_id}}",
                    "collection_key": "items",
                    "record_key": "item",
                    "summary_fields": ["body", *(["pinned"] if PINNED_COLUMN else [])],
                    "fields": _fields(),
                }
            ],
        },
    )
