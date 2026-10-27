"""Hook bindings and where-used as shared operator commands (API, MCP, CLI).

Reads need the workflow to be linked to the project in the caller's scope;
writes need deployment-admin authority, as every other workflow change does.
All of them delegate to the one binding service and the one where-used query.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .errors import ServiceError, ServiceForbiddenError, ServiceNotFoundError


def _issues_error(exc: Any, status_code: int = 409) -> ServiceError:
    return ServiceError(
        str(exc),
        status_code=status_code,
        payload={"message": str(exc), "issues": list(getattr(exc, "issues", []))},
    )


class WorkflowOperatorHooksMixin:
    """Mixed into ``WorkflowOperatorService``; uses its scope and registry helpers."""

    _get_logger: Callable[[], Any]
    _project_query: Callable[[], Any]

    def _registry_db(self) -> Any:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _require_project_workflow(
        self,
        project_id: Any,
        workflow_id: str,
        *,
        scope: Any = None,
        allow_disabled: bool = False,
    ) -> str:  # pragma: no cover - provided by the service
        raise NotImplementedError

    def _hook_service(self, scope: Any):
        from core.application.workflows.workflow_hook_binding_service import (
            WorkflowHookBindingService,
        )

        return WorkflowHookBindingService(
            self._registry_db(),
            account_id=str(getattr(scope, "account_id", "") or ""),
            actor=str(getattr(scope, "actor_name", "") or ""),
        )

    def _require_hook_admin(self, scope: Any, action: str) -> None:
        """A binding change needs write authority.

        HTTP and MCP reach these writes only through admin-gated routes
        (``AUTH_ADMIN_ONLY_PATTERNS``, ``admin:all`` tokens); the CLI uses its
        deployment-admin scope. The read-only operator scope never writes.
        """
        principal = str(getattr(scope, "principal_type", "") or "")
        if scope is None or principal in {"", "system_actor"}:
            raise ServiceForbiddenError(f"{action} requires deployment-admin authority.")

    def _release_workflow(self, workflow_id: str, release_id: str) -> Mapping[str, Any] | None:
        """The release's own artifact of an extension workflow, for a staged binding."""
        from core.application.extensions.service_support import (
            _get_default_extension_runtime_store,
        )

        row = self._registry_db().get_workflow(workflow_id)
        local_id = str((row or {}).get("local_workflow_id") or "").strip()
        if not local_id:
            return None
        store = _get_default_extension_runtime_store(get_logger=self._get_logger)
        for snapshot in store.list_artifact_snapshots(release_id) or []:
            if (
                str(getattr(snapshot, "artifact_kind", "")) == "workflow"
                and str(getattr(snapshot, "artifact_id", "")) == local_id
            ):
                return dict(getattr(snapshot, "snapshot", {}) or {})
        return None

    def hooks_list(self, *, project_id: Any, workflow_id: str, scope: Any) -> dict[str, Any]:
        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        hooks = self._hook_service(scope).describe(workflow_id, str(project_id))
        return {"project_id": str(project_id), "workflow_id": workflow_id, "hooks": hooks}

    def hook_get(
        self, *, project_id: Any, workflow_id: str, hook_id: str, scope: Any
    ) -> dict[str, Any]:
        listed = self.hooks_list(project_id=project_id, workflow_id=workflow_id, scope=scope)
        for hook in listed["hooks"]:
            if hook["hook_id"] == hook_id:
                return {"project_id": listed["project_id"], "workflow_id": workflow_id, **hook}
        raise ServiceNotFoundError(f"Workflow declares no hook '{hook_id}'")

    def _binding_request(self, *, project_id: Any, workflow_id: str, **fields: Any):
        from core.application.workflows.workflow_hook_binding_service import HookBindingRequest

        return HookBindingRequest(
            owner_workflow_id=workflow_id,
            project_id=str(project_id),
            hook_id=str(fields["hook_id"]),
            expected_revision=int(fields["expected_revision"]),
            target_workflow_id=fields.get("target_workflow_id") or None,
            input_mapping=dict(fields.get("input_mapping") or {}),
            output_mapping=dict(fields.get("output_mapping") or {}),
            hook_contract_version=fields.get("hook_contract_version"),
            hook_semantic_digest=fields.get("hook_semantic_digest") or None,
            release_id=fields.get("release_id") or None,
        )

    def hook_preview(
        self, *, project_id: Any, workflow_id: str, scope: Any, **fields: Any
    ) -> dict[str, Any]:
        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        request = self._binding_request(project_id=project_id, workflow_id=workflow_id, **fields)
        release = (
            self._release_workflow(workflow_id, request.release_id) if request.release_id else None
        )
        return self._hook_service(scope).preview(
            request, release_workflow=release, sample=fields.get("sample")
        )

    def hook_bind(
        self, *, project_id: Any, workflow_id: str, scope: Any, **fields: Any
    ) -> dict[str, Any]:
        from core.application.workflows.workflow_hook_binding_repository import (
            HookBindingConflictError,
        )
        from core.application.workflows.workflow_hook_binding_service import HookBindingError

        self._require_hook_admin(scope, "Connecting a workflow to a hook")
        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        request = self._binding_request(project_id=project_id, workflow_id=workflow_id, **fields)
        release = (
            self._release_workflow(workflow_id, request.release_id) if request.release_id else None
        )
        try:
            saved = self._hook_service(scope).save(request, release_workflow=release)
        except HookBindingConflictError as exc:
            raise ServiceError(str(exc), status_code=409) from exc
        except HookBindingError as exc:
            raise _issues_error(exc, status_code=400) from exc
        return {
            "project_id": str(project_id),
            "workflow_id": workflow_id,
            "hook_id": request.hook_id,
            "binding_revision": saved.revision,
            "revision_id": saved.id,
            "target_workflow_id": saved.target_workflow_id,
            "staged_release_id": request.release_id,
        }

    def hook_unbind(
        self,
        *,
        project_id: Any,
        workflow_id: str,
        hook_id: str,
        expected_revision: int,
        scope: Any,
    ) -> dict[str, Any]:
        """Clear the binding: the hook is empty again for new runs of the project."""
        return self.hook_bind(
            project_id=project_id,
            workflow_id=workflow_id,
            scope=scope,
            hook_id=hook_id,
            expected_revision=expected_revision,
            target_workflow_id=None,
        )

    def hook_create_and_bind(
        self,
        *,
        project_id: Any,
        workflow_id: str,
        hook_id: str,
        expected_revision: int,
        child_workflow_id: str,
        display_name: str = "",
        scope: Any,
    ) -> dict[str, Any]:
        from core.application.workflows.workflow_hook_binding_repository import (
            HookBindingConflictError,
        )
        from core.application.workflows.workflow_hook_binding_service import HookBindingError

        self._require_hook_admin(scope, "Adding actions to a hook")
        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        try:
            saved = self._hook_service(scope).create_child_and_bind(
                owner_workflow_id=workflow_id,
                project_id=str(project_id),
                hook_id=hook_id,
                expected_revision=int(expected_revision),
                child_workflow_id=child_workflow_id,
                display_name=display_name,
            )
        except HookBindingConflictError as exc:
            raise ServiceError(str(exc), status_code=409) from exc
        except HookBindingError as exc:
            raise _issues_error(exc, status_code=400) from exc
        return {
            "project_id": str(project_id),
            "workflow_id": workflow_id,
            "hook_id": hook_id,
            "binding_revision": saved.revision,
            "revision_id": saved.id,
            "target_workflow_id": saved.target_workflow_id,
        }

    def where_used(
        self, *, project_id: Any, workflow_id: str, scope: Any, locale: str = ""
    ) -> dict[str, Any]:
        """Every direct Invoke and hook binding of the caller's account that calls it."""
        from core.application.workflows.workflow_where_used import (
            WorkflowConsumer,
            workflow_usage_view,
        )

        workflow_id = self._require_project_workflow(
            project_id, workflow_id, scope=scope, allow_disabled=True
        )
        allowed_workflows = frozenset(getattr(scope, "allowed_workflow_ids", ()) or ())
        allowed_projects = frozenset(getattr(scope, "allowed_project_ids", ()) or ())
        linked_ids: dict[str, set[str]] = {}

        def project_workflows(candidate_project: str) -> set[str]:
            if candidate_project not in linked_ids:
                try:
                    bindings = self._project_query().get_workflow_bindings(
                        candidate_project, scope=scope
                    )
                except (ServiceForbiddenError, ServiceNotFoundError):
                    linked_ids[candidate_project] = set()
                else:
                    linked_ids[candidate_project] = {
                        str(row.get("workflow_id") or "").strip()
                        for row in bindings.project_workflows or []
                        if isinstance(row, Mapping)
                    }
            return linked_ids[candidate_project]

        def caller_visible(consumer: WorkflowConsumer) -> bool:
            caller_id = consumer.caller_workflow_id
            if allowed_workflows and caller_id not in allowed_workflows:
                return False
            if consumer.project_id:
                if allowed_projects and consumer.project_id not in allowed_projects:
                    return False
                return caller_id in project_workflows(consumer.project_id)
            # Direct Invoke dependencies are account-wide and have no project id.
            # A project/workspace restricted principal only gets labels for known links.
            if allowed_projects:
                return any(caller_id in project_workflows(project) for project in allowed_projects)
            if getattr(scope, "allowed_workspace_ids", ()):
                return caller_id in project_workflows(str(project_id))
            return True

        return {
            "project_id": str(project_id),
            **workflow_usage_view(
                self._registry_db(),
                account_id=getattr(scope, "account_id", ""),
                workflow_id=workflow_id,
                caller_visible=caller_visible,
                locale=locale,
            ),
        }


__all__ = ["WorkflowOperatorHooksMixin"]
