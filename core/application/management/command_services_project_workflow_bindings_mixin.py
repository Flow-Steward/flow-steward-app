# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


class ProjectCommandServiceWorkflowBindingsMixin:
    def _persist_authoritative_project_workflow_bindings(
        self,
        *,
        bl: Any,
        project_id: str,
        workflow_ids: Sequence[str],
        operator_default_workflow_id: str | None,
        account_id: str,
        actor_id: str,
        transaction_conn: Any | None,
        preserve_default: bool = False,
    ) -> str:
        """Save the project's workflow list and its operator default.

        ``preserve_default`` keeps the default the project already has, as long
        as that workflow is still linked. It is not re-validated: linking another
        workflow says nothing about the default, and a default that was disabled
        later must not stop every link to the project.
        """
        normalized_ids = list(
            dict.fromkeys(str(item).strip() for item in workflow_ids if str(item).strip())
        )
        extension_ids = self._extension_workflow_ids(account_id=account_id)
        self._validate_project_workflow_ids(
            bl=bl,
            workflow_ids=normalized_ids,
            extension_workflow_ids=extension_ids,
            account_id=account_id,
        )
        if preserve_default:
            current_default = str(operator_default_workflow_id or "").strip()
            resolved_default = current_default if current_default in normalized_ids else ""
        else:
            resolved_default = self._resolve_project_workflow_default(
                operator_default_workflow_id,
                normalized_ids,
            )
        self._ensure_extension_workflow_anchors(
            bl=bl,
            workflow_ids=[item for item in normalized_ids if item in extension_ids],
            account_id=account_id,
            actor_id=actor_id,
            transaction_conn=transaction_conn,
        )
        if not callable(getattr(bl, "save_project_workflows", None)):
            raise ServiceUnavailableError("Project workflow bindings are unavailable")
        bl.save_project_workflows(
            project_id,
            normalized_ids,
            operator_default_workflow_id=resolved_default,
        )
        return resolved_default

    def _validate_project_workflow_ids(
        self,
        *,
        bl: Any,
        workflow_ids: Sequence[str],
        extension_workflow_ids: set[str],
        account_id: str,
    ) -> None:
        choices = [
            dict(item)
            for item in self._list_workflow_choices(
                enabled_only=False,
                account_id=account_id,
            )
            if isinstance(item, dict)
        ]
        available_ids = {
            str(item.get("workflow_id") or "").strip()
            for item in choices
            if str(item.get("workflow_id") or "").strip()
        }
        available_ids = {
            workflow_id
            for workflow_id in available_ids
            if not workflow_id.startswith("extension.") or workflow_id in extension_workflow_ids
        }
        available_ids.update(extension_workflow_ids)
        missing_ids = [item for item in workflow_ids if item not in available_ids]
        if missing_ids:
            raise ServiceValidationError(f"Unknown workflow_id(s): {', '.join(missing_ids)}")
        get_workflow = getattr(bl, "get_workflow", None)
        if callable(get_workflow):
            missing_ids = [
                item
                for item in workflow_ids
                if item not in extension_workflow_ids
                and (item.startswith("extension.") or not get_workflow(item))
            ]
            if missing_ids:
                raise ServiceValidationError(f"Unknown workflow_id(s): {', '.join(missing_ids)}")

    def _resolve_project_workflow_default(
        self,
        selected: str | None,
        workflow_ids: Sequence[str],
    ) -> str:
        normalized_selected = str(selected or "").strip()
        if not normalized_selected:
            return ""
        if self._resolve_workflow_selection is None:
            raise ServiceUnavailableError(ERR_WORKFLOW_RESOLUTION_UNAVAILABLE)
        try:
            resolved, _ = self._resolve_workflow_selection(
                normalized_selected,
                [{"workflow_id": item, "enabled": True} for item in workflow_ids],
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        return str(resolved or "").strip()

    @staticmethod
    def _project_workflow_registry_store(bl: Any) -> Any | None:
        registry_store = getattr(bl, "db_store", None)
        dsn = str(getattr(bl, "dsn", "") or "").strip()
        if registry_store is None and dsn:
            from core.application.registry.db_store import RegistryDBStore

            registry_store = RegistryDBStore(dsn)
        return registry_store

    def _ensure_extension_workflow_anchors(
        self,
        *,
        bl: Any,
        workflow_ids: Sequence[str],
        account_id: str,
        actor_id: str,
        transaction_conn: Any | None,
    ) -> None:
        registry_store = self._project_workflow_registry_store(bl)
        if not workflow_ids or registry_store is None:
            return
        save_workflow = getattr(registry_store, "save_workflow", None)
        if not callable(save_workflow):
            raise ServiceUnavailableError("Extension workflow anchor persistence is unavailable")
        set_tx_conn = getattr(registry_store, "_set_tx_conn", None)
        if callable(set_tx_conn) and transaction_conn is not None:
            set_tx_conn(transaction_conn)
        try:
            choices = {
                str(row.get("workflow_id") or "").strip(): dict(row)
                for row in self._list_workflow_choices(
                    enabled_only=False,
                    account_id=account_id,
                )
                if isinstance(row, dict) and str(row.get("workflow_id") or "").strip()
            }
            get_workflow = getattr(registry_store, "get_workflow", None)
            for workflow_id in workflow_ids:
                existing = get_workflow(workflow_id) if callable(get_workflow) else None
                choice = choices.get(workflow_id) or {}
                display_name = str(
                    choice.get("display_name")
                    or choice.get("workflow_display_name")
                    or choice.get("name")
                    or workflow_id
                ).strip()
                if existing and str(existing.get("display_name") or "").strip() == display_name:
                    continue
                parts = workflow_id.split(".", 2)
                payload = (
                    {
                        key: existing[key]
                        for key in (
                            "workflow_key",
                            "version",
                            "description",
                            "enabled",
                            "trigger",
                            "settings",
                            "tags",
                            "builder_metadata_jsonb",
                            "account_id",
                            "scope_kind",
                            "source_install_id",
                            "source_release_id",
                            "source_projection_id",
                            "local_workflow_id",
                            "projection_mode",
                            "derived_from_kind",
                            "derived_from_ref",
                            "sync_status",
                            "availability_status",
                            "availability_reason_code",
                            "availability_reason_text",
                            "new_runs_blocked",
                            "visible_in_catalog",
                        )
                        if key in existing
                    }
                    if isinstance(existing, dict)
                    else {}
                )
                payload.update(
                    {
                        "workflow_id": workflow_id,
                        "display_name": display_name,
                        "enabled": bool(choice.get("enabled", payload.get("enabled", True))),
                        "source_install_id": payload.get("source_install_id")
                        or (parts[1] if len(parts) > 2 else ""),
                        "projection_mode": "derived",
                    }
                )
                save_workflow(
                    payload,
                    author=str(actor_id or "api").strip() or "api",
                    **(
                        {"expected_revision": int(existing["revision"])}
                        if existing
                        else {"create_only": True}
                    ),
                )
        finally:
            if callable(set_tx_conn) and transaction_conn is not None:
                set_tx_conn(None)
