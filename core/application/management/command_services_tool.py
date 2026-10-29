# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.registry.active_workflow_dependencies import (
    enabled_workflow_dependencies,
)
from core.application.registry.identifier_keys import require_tool_identifier, unique_text_key
from core.application.registry.projected_artifact_rows import projection_aware_catalog_row

from .command_services_common import *  # NOSONAR
from .mcp_projected_tool_lifecycle import (
    McpProjectedToolLifecycleService,
    mcp_projected_tool_connector_id,
)

_TOOL_AVAILABILITY_FIELDS = (
    "availability_status",
    "availability_reason_code",
    "availability_reason_text",
    "new_runs_blocked",
    "visible_in_catalog",
)


def _require_tool_write_identifier(tool_id: str) -> str:
    try:
        return require_tool_identifier(tool_id)
    except ValueError as exc:
        raise ServiceValidationError(str(exc)) from exc


def _is_mcp_projection_payload(payload: Mapping[str, Any]) -> bool:
    implementation = payload.get("implementation") or {}
    if not isinstance(implementation, Mapping):
        return False
    return str(implementation.get("type") or implementation.get("kind") or "").strip() == "mcp"


def _reject_public_mcp_projection_write(payload: Mapping[str, Any]) -> None:
    if _is_mcp_projection_payload(payload):
        raise ServiceValidationError(
            "MCP projected tools are managed only by their owning connector sync"
        )


def _require_tool_available_for_enable(row: dict[str, Any], tool_id: str) -> None:
    status = str(row.get("availability_status") or "available").strip().lower() or "available"
    if status == "available":
        return
    reason_code = (
        str(row.get("availability_reason_code") or "availability_blocked").strip()
        or "availability_blocked"
    )
    reason_text = str(row.get("availability_reason_text") or "").strip()
    reason = f"{reason_code}: {reason_text}" if reason_text else reason_code
    raise ServiceConflictError(
        f"Tool '{tool_id}' cannot be enabled while availability is '{status}' ({reason})."
    )


def _tool_row_in_scope(row: Any, scope: ScopeContext | None) -> bool:
    """True if *row* is visible to *scope* under account **and** project isolation.

    Account-owned rows must match the caller's account; for a project-restricted
    principal (non-empty ``allowed_project_ids``), a project-bound row must be in
    the allowed set while account-global (``project_id`` NULL) rows stay visible.
    """
    if not isinstance(row, dict):
        return True
    row_account = str(row.get("account_id") or "").strip()
    if row_account:
        scope_account = str(getattr(scope, "account_id", "") or "").strip()
        if not scope_account or row_account != scope_account:
            return False
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
    row_project = str(row.get("project_id") or "").strip()
    scoped_project = str(getattr(scope, "project_id", "") or "").strip()
    if scoped_project and row_project != scoped_project:
        return False
    if (
        allowed_project_ids
        and row_project
        and row_project not in {str(p).strip() for p in allowed_project_ids}
    ):
        return False
    implementation = row.get("implementation") or {}
    if isinstance(implementation, str):
        try:
            implementation = json.loads(implementation)
        except Exception:
            implementation = {}
    implementation_kind = str(
        (implementation or {}).get("type") or (implementation or {}).get("kind") or ""
    ).strip()
    if implementation_kind != "mcp":
        return True
    row_tenant = str((implementation or {}).get("owner_tenant_id") or "").strip()
    scoped_tenant = str(getattr(scope, "tenant_id", "") or "").strip()
    return not scoped_tenant or row_tenant == scoped_tenant


class ToolCommandService:
    def __init__(
        self,
        *,
        get_db: Callable[[], Any],
        reload_registry: Callable[[], None],
        get_tool_validation: Callable[[Any, str], dict[str, Any]],
        format_validation_error_detail: Callable[..., dict[str, Any]],
    ):
        self._get_db = get_db
        self._reload_registry = reload_registry
        self._get_tool_validation = get_tool_validation
        self._format_validation_error_detail = format_validation_error_detail

    def _db(self) -> Any:
        return self._get_db()

    def _require_tool_in_scope(self, db: Any, tool_id: str, scope: ScopeContext | None) -> dict:
        """Load a tool and enforce caller account row-scope, else 404. Mutations key
        only on ``tool_id``; without this an admin in account A could alter account
        B's tool contract."""
        tool = db.get_tool(tool_id)
        if not tool or not _tool_row_in_scope(tool, scope):
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found")
        return tool

    def _validation_error(
        self, *, tool_id: str, action: str, validation: dict[str, Any]
    ) -> ServiceValidationError:
        return ServiceValidationError(
            "Contract validation failed",
            payload=self._format_validation_error_detail(
                entity_type="tool",
                entity_id=tool_id,
                action=action,
                validation=validation,
            ),
        )

    def create_tool(
        self,
        command: ToolSaveInput,
        *,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        tool_id = unique_text_key(
            command.tool_id,
            lambda candidate: db.get_tool(candidate) is not None,
        )
        payload = command.model_dump()
        _reject_public_mcp_projection_write(payload)
        payload["tool_id"] = tool_id
        # Ownership comes from authenticated transport context, never from the
        # client-controlled ToolSave contract. Without this, non-default-account
        # creates fall back to the registry store's default account and become
        # immediately invisible to their creator.
        payload["account_id"] = resolved.account_id
        with db._transaction():
            db.save_tool(payload, author=author)
            validation = self._get_tool_validation(db, tool_id)
            if bool(payload.get("enabled", False)) and not bool(
                (validation or {}).get("valid", False)
            ):
                raise self._validation_error(tool_id=tool_id, action="save", validation=validation)
        self._reload_registry()
        return ToolMutationResultView(status="created", tool_id=tool_id, validation=validation)

    def update_tool(
        self,
        tool_id: str,
        command: ToolSaveInput,
        *,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        existing = db.get_tool(tool_id)
        if existing and not _tool_row_in_scope(existing, resolved):
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found")
        _require_tool_write_identifier(tool_id)
        data = command.model_dump()
        if _is_mcp_projection_payload(existing or {}) or _is_mcp_projection_payload(data):
            _reject_public_mcp_projection_write(
                data if _is_mcp_projection_payload(data) else existing
            )
        data["tool_id"] = tool_id
        if isinstance(existing, dict):
            data["account_id"] = existing.get("account_id") or resolved.account_id
            for field in ("project_id", "tenant_id", "scope_kind"):
                if field in existing:
                    data[field] = existing[field]
            for field in _TOOL_AVAILABILITY_FIELDS:
                if field in existing:
                    data[field] = existing[field]
            if bool(data.get("enabled", False)):
                _require_tool_available_for_enable(existing, tool_id)
        with db._transaction():
            db.save_tool(data, author=author)
            validation = self._get_tool_validation(db, tool_id)
            if bool(data.get("enabled", False)) and not bool(
                (validation or {}).get("valid", False)
            ):
                raise self._validation_error(tool_id=tool_id, action="save", validation=validation)
        self._reload_registry()
        return ToolMutationResultView(status="updated", tool_id=tool_id, validation=validation)

    def patch_tool(
        self,
        tool_id: str,
        command: ToolPatchInput,
        *,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        existing = self._require_tool_in_scope(db, tool_id, resolved)

        _require_tool_write_identifier(tool_id)
        updates = command.model_dump(exclude_none=True)
        if not updates:
            return ToolMutationResultView(
                status="no_change",
                tool_id=tool_id,
                validation=self._get_tool_validation(db, tool_id),
            )

        if bool(existing.get("enabled", False)) and updates.get("enabled") is False:
            dependencies = enabled_workflow_dependencies(
                db,
                agent_ids=db.get_agents_using_tool(tool_id),
                tool_id=tool_id,
            )
            if dependencies:
                raise ServiceConflictError(
                    f"Tool '{tool_id}' cannot be disabled while enabled workflows depend "
                    f"on it: {', '.join(dependencies)}. Disable or update those workflows first."
                )

        merged = {**existing, **updates}
        merged["tool_id"] = tool_id
        if _is_mcp_projection_payload(existing) or _is_mcp_projection_payload(merged):
            _reject_public_mcp_projection_write(merged)
        if bool(merged.get("enabled", False)):
            _require_tool_available_for_enable(existing, tool_id)
        with db._transaction():
            db.save_tool(merged, author=author)
            validation = self._get_tool_validation(db, tool_id)
            if bool(merged.get("enabled", False)) and not bool(
                (validation or {}).get("valid", False)
            ):
                raise self._validation_error(tool_id=tool_id, action="save", validation=validation)
        self._reload_registry()
        return ToolMutationResultView(status="updated", tool_id=tool_id, validation=validation)

    def toggle_tool(
        self, tool_id: str, *, scope: ScopeContext | None = None
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        current = self._require_tool_in_scope(db, tool_id, resolved)
        _reject_public_mcp_projection_write(current)
        if bool(current.get("enabled", False)):
            dependencies = enabled_workflow_dependencies(
                db,
                agent_ids=db.get_agents_using_tool(tool_id),
                tool_id=tool_id,
            )
            if dependencies:
                raise ServiceConflictError(
                    f"Tool '{tool_id}' cannot be disabled while enabled workflows depend "
                    f"on it: {', '.join(dependencies)}. Disable or update those workflows first."
                )
        if not bool(current.get("enabled", False)):
            _require_tool_available_for_enable(current, tool_id)
            validation = self._get_tool_validation(db, tool_id)
            if not bool((validation or {}).get("valid", False)):
                raise self._validation_error(
                    tool_id=tool_id, action="enable", validation=validation
                )
        try:
            new_state = db.toggle_tool(tool_id)
        except Exception as exc:
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found") from exc
        validation = self._get_tool_validation(db, tool_id)
        self._reload_registry()
        return ToolMutationResultView(
            status="toggled",
            tool_id=tool_id,
            enabled=bool(new_state),
            validation=validation,
        )

    def delete_tool(
        self,
        tool_id: str,
        *,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        current = self._require_tool_in_scope(db, tool_id, resolved)
        if mcp_projected_tool_connector_id(current):
            raise ServiceConflictError(
                "MCP projected tool deletion requires a confirmed workflow-impact plan",
                payload={
                    "error": "mcp_projected_tool_delete_plan_required",
                    "tool_id": tool_id,
                    "delete_plan_url": f"/api/tools/{tool_id}/delete-plan",
                },
            )
        try:
            db.delete_tool(tool_id, author=author)
        except Exception as exc:
            if isinstance(exc, KeyError):
                detail = str(exc.args[0] if exc.args else exc)
                raise ServiceNotFoundError(detail) from exc
            if "violates foreign key" in str(exc):
                raise ServiceConflictError(
                    f"Tool '{tool_id}' is still assigned to agents or skills. Remove associations first."
                ) from exc
            raise ServiceError(str(exc), status_code=500) from exc
        self._reload_registry()
        return ToolMutationResultView(status="deleted", tool_id=tool_id)

    def preview_delete_tool(
        self,
        tool_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        resolved = _service_scope(scope)
        db = self._db()
        row = self._require_tool_in_scope(db, tool_id, resolved)
        implementation = row.get("implementation") or {}
        project_id = str(row.get("project_id") or implementation.get("owner_project_id") or "")
        tenant_id = str(implementation.get("owner_tenant_id") or "")
        return McpProjectedToolLifecycleService(
            db_store=db,
            reload_registry=self._reload_registry,
        ).preview_delete(
            tool_id,
            account_id=str(resolved.account_id),
            project_id=project_id,
            tenant_id=tenant_id,
        )

    def delete_tool_confirmed(
        self,
        tool_id: str,
        *,
        revision: str,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        row = self._require_tool_in_scope(db, tool_id, resolved)
        implementation = row.get("implementation") or {}
        project_id = str(row.get("project_id") or implementation.get("owner_project_id") or "")
        tenant_id = str(implementation.get("owner_tenant_id") or "")
        McpProjectedToolLifecycleService(
            db_store=db,
            reload_registry=self._reload_registry,
        ).delete_confirmed(
            tool_id,
            account_id=str(resolved.account_id),
            project_id=project_id,
            tenant_id=tenant_id,
            deleted_by=author,
            revision=revision,
        )
        return ToolMutationResultView(status="deleted", tool_id=tool_id)

    def clone_tool(
        self,
        tool_id: str,
        command: ToolCloneInput,
        *,
        author: str = "admin",
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        source = projection_aware_catalog_row(
            db,
            "tool",
            "tool_id",
            tool_id,
            db.get_tool(tool_id),
            account_id=str(resolved.account_id or ""),
        )
        if not source or not _tool_row_in_scope(source, resolved):
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found")
        if str(source.get("source_install_id") or "").strip():
            message = "This installed tool cannot be copied while its runtime is owned by the extension install."
            raise ServiceConflictError(
                message,
                payload={
                    "error": message,
                    "code": "tool_clone_runtime_dependency_unavailable",
                    "tool_id": tool_id,
                },
            )
        _reject_public_mcp_projection_write(source)
        target_id = unique_text_key(
            _require_tool_write_identifier(command.tool_id or f"{tool_id}_copy"),
            lambda candidate: db.get_tool(candidate) is not None,
        )

        clone_data = {
            "tool_id": target_id,
            "account_id": resolved.account_id,
            "display_name": command.display_name or f"{source.get('display_name', tool_id)} (Copy)",
            "description": source.get("description", ""),
            "category": source.get("category"),
            "prompt": source.get("prompt", ""),
            "implementation": dict(source.get("implementation") or {}),
            "interface": dict(source.get("interface") or {}),
            "config": dict(source.get("config") or {}),
            "enabled": False,
            "version": source.get("version", "1.0.0"),
            "tags": list(source.get("tags") or []),
        }
        try:
            db.save_tool(clone_data, author=author)
        except Exception as exc:
            raise ServiceError(str(exc), status_code=500) from exc
        self._reload_registry()
        return ToolMutationResultView(status="cloned", tool_id=target_id, source_tool_id=tool_id)

    def restore_tool_revision(
        self,
        tool_id: str,
        command: ToolRestoreRevisionInput,
        *,
        author: str = "admin_restore",
        restore_validation_error_cls: type[Exception] | None = None,
        scope: ScopeContext | None = None,
    ) -> ToolMutationResultView:
        resolved = _service_scope(scope)
        db = self._db()
        current = self._require_tool_in_scope(db, tool_id, resolved)
        _reject_public_mcp_projection_write(current)
        restore_validation_error_cls = restore_validation_error_cls or ValueError

        def _validate_restored(
            _entity_type: str,
            _entity_id: str,
            restored: Mapping[str, Any],
        ) -> dict[str, Any]:
            _reject_public_mcp_projection_write(restored)
            return self._get_tool_validation(db, tool_id)

        try:
            outcome = db.restore_entity_revision(
                entity_type="tool",
                entity_id=tool_id,
                entry_id=str(command.entry_id),
                author=author,
                validate_restored=_validate_restored,
                account_id=str(current.get("account_id") or resolved.account_id),
            )
        except Exception as exc:
            if restore_validation_error_cls is not None and isinstance(
                exc, restore_validation_error_cls
            ):
                raw_validation = getattr(exc, "validation", None)
                validation = (
                    raw_validation
                    if isinstance(raw_validation, dict)
                    else {
                        "valid": False,
                        "errors": [],
                        "warnings": [],
                    }
                )
                raise self._validation_error(
                    tool_id=tool_id, action="restore", validation=validation
                ) from exc
            if isinstance(exc, KeyError):
                raise ServiceNotFoundError(str(exc)) from exc
            if isinstance(exc, ValueError):
                raise ServiceValidationError(str(exc)) from exc
            raise ServiceError(str(exc), status_code=500) from exc
        validation = outcome.get("validation") if isinstance(outcome, dict) else None
        if not isinstance(validation, dict):
            validation = self._get_tool_validation(db, tool_id)
        self._reload_registry()
        return ToolMutationResultView(
            status="restored",
            tool_id=tool_id,
            restored_from_entry_id=str(command.entry_id),
            validation=validation,
            snapshot=(outcome or {}).get("restored") if isinstance(outcome, dict) else None,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
