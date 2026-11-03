"""Catalog secrets in workflow inputs: binding, status and approval (ADR 0055, 2b).

The workflow half of `SecretCatalogService`, kept in its own module; the service
provides `_transaction`, `_owner_scope`, `_secret_in_scope` and `_audit`.
"""

from __future__ import annotations

import json
from typing import Any

from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.secrets import repository
from core.application.secrets.authority import (
    actor_identity,
    require_approval_authority,
    require_secret_bind,
    require_secret_read_metadata,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _validated_field(value: Any) -> str:
    from core.application.secrets.service import _validated_field as validated

    return validated(value)


def secret_available_to(secret: dict[str, Any], *, project_id: str, workspace_id: str) -> bool:
    from core.application.secrets.service import secret_available_to as available

    return available(secret, project_id=project_id, workspace_id=workspace_id)


class WorkflowSecretInputsMixin:
    """Bind, unbind, list and approve the catalog secrets of workflow secret inputs."""

    _transaction: Any
    _owner_scope: Any
    _secret_in_scope: Any
    _audit: Any

    def _workflow_in_project(
        self, conn: Any, cur: Any, scope: ScopeContext, workflow_id: Any, project_id: Any
    ) -> dict[str, Any]:
        from core.application.secrets.workflow_gate import current_definition, workflow_row

        _workspace, owner_project = self._owner_scope(
            cur, scope, project_id=project_id, workspace_id=None
        )
        cur.execute(
            """
            SELECT 1 FROM project_workflows
             WHERE project_id::text = %s AND workflow_id = %s AND deleted_at IS NULL
            """,
            (owner_project, _text(workflow_id)),
        )
        linked = cur.fetchone() is not None
        workflow = workflow_row(cur, account_id=scope.account_id, workflow_id=_text(workflow_id))
        if not linked or workflow is None:
            raise ServiceNotFoundError("Workflow not found")
        # The approved document is the one a run pins: steps, Groups, graph, settings.
        definition = current_definition(
            conn, account_id=scope.account_id, workflow_id=_text(workflow_id)
        )
        if definition is None:
            raise ServiceValidationError("This workflow cannot run, so it cannot be approved")
        return {"workflow": workflow, "definition": definition, "project_id": owner_project}

    @staticmethod
    def _secret_input_names(workflow: dict[str, Any]) -> set[str]:
        settings_value = workflow.get("settings")
        settings = settings_value if isinstance(settings_value, dict) else {}
        callable_value = settings.get("callable")
        callable_block = callable_value if isinstance(callable_value, dict) else {}
        rows = callable_block.get("expected_inputs") if isinstance(callable_block, dict) else []
        return {
            _text(row.get("name"))
            for row in rows or []
            if isinstance(row, dict) and _text(row.get("type")).lower() == "secret"
        }

    def bind_workflow_input(
        self, workflow_id: Any, scope: ScopeContext, *, project_id: Any, field: Any, secret_id: Any
    ) -> dict[str, Any]:
        """Choose a catalog secret for one secret input; only this operation sets a binding."""
        require_secret_bind(scope)
        validated_field = _validated_field(field)
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            found = self._workflow_in_project(conn, cur, scope, workflow_id, project_id)
            if validated_field not in self._secret_input_names(found["workflow"]):
                raise ServiceValidationError(
                    f"'{validated_field}' is not a secret input of this workflow"
                )
            secret = self._secret_in_scope(cur, scope, secret_id, lock=True)
            project = repository.project_scope(
                cur, account_id=scope.account_id, project_id=found["project_id"]
            )
            if not project or not secret_available_to(
                secret, project_id=project["project_id"], workspace_id=project["workspace_id"]
            ):
                raise ServiceValidationError(
                    "This secret is not available to the workflow's project"
                )
            cur.execute(
                """
                INSERT INTO catalog_secret_bindings (
                    account_id, secret_id, consumer_kind, workflow_id, consumer_project_id,
                    field_name, created_by
                ) VALUES (%s, %s, 'workflow_input', %s, %s, %s, %s)
                ON CONFLICT (workflow_id, field_name)
                DO UPDATE SET secret_id = EXCLUDED.secret_id,
                              consumer_project_id = EXCLUDED.consumer_project_id,
                              created_at = now(), created_by = EXCLUDED.created_by
                """,
                (
                    scope.account_id,
                    secret["id"],
                    _text(workflow_id),
                    found["project_id"],
                    validated_field,
                    actor,
                ),
            )
            self._audit(
                cur,
                scope,
                "bind",
                {
                    "secret_id": secret["id"],
                    "workflow_id": _text(workflow_id),
                    "field": validated_field,
                },
            )
        return self.workflow_approval_status(workflow_id, scope, project_id=project_id)

    def unbind_workflow_input(
        self, workflow_id: Any, scope: ScopeContext, *, project_id: Any, field: Any
    ) -> dict[str, Any]:
        require_secret_bind(scope)
        validated_field = _validated_field(field)
        with self._transaction() as (conn, cur):
            self._workflow_in_project(conn, cur, scope, workflow_id, project_id)
            cur.execute(
                """
                DELETE FROM catalog_secret_bindings
                 WHERE consumer_kind = 'workflow_input' AND workflow_id = %s
                   AND field_name = %s AND account_id::text = %s
                """,
                (_text(workflow_id), validated_field, scope.account_id),
            )
            if not getattr(cur, "rowcount", 0):
                raise ServiceNotFoundError("No secret is bound to this input")
            self._audit(
                cur, scope, "unbind", {"workflow_id": _text(workflow_id), "field": validated_field}
            )
        return self.workflow_approval_status(workflow_id, scope, project_id=project_id)

    @staticmethod
    def _receivers(cur: Any, scope: ScopeContext, found: dict[str, Any]) -> list[dict[str, Any]]:
        """The connector receivers of the definition, selected for this project."""
        from core.application.orchestration.connector_receiver import definition_receivers
        from core.application.secrets import workflow_gate

        if not workflow_gate.connector_steps(found["definition"]):
            return []
        cur.execute(
            "SELECT tenant_id::text FROM projects WHERE id::text = %s AND account_id::text = %s",
            (found["project_id"], scope.account_id),
        )
        row = cur.fetchone()
        tenant = row[0] if row and not isinstance(row, dict) else (row or {}).get("tenant_id")
        lineage = {
            "account_id": scope.account_id,
            "project_id": _text(found["project_id"]),
            "tenant_id": _text(tenant),
        }
        return definition_receivers(found["definition"], lineage=lineage)

    @staticmethod
    def _workflow_status_view(
        cur: Any, scope: ScopeContext, found: dict[str, Any]
    ) -> dict[str, Any]:
        from core.application.secrets import workflow_gate

        workflow_id = _text(found["workflow"].get("workflow_id"))
        definition = found["definition"]
        bindings = workflow_gate.workflow_bindings(
            cur, account_id=scope.account_id, workflow_id=workflow_id
        )
        projection = workflow_gate.current_projection(
            cur, account_id=scope.account_id, workflow_id=workflow_id, definition=definition
        )
        fingerprint = workflow_gate.workflow_fingerprint(projection)
        approval = workflow_gate.workflow_approval(
            cur, account_id=scope.account_id, workflow_id=workflow_id
        )
        static_rules, dynamic_steps = workflow_gate.http_destinations(definition)
        receivers = WorkflowSecretInputsMixin._receivers(cur, scope, found)
        approved = (
            approval is not None
            and approval.get("fingerprint") == fingerprint
            and workflow_gate.receivers_approved(approval, receivers)
        )
        secret_inputs = sorted(WorkflowSecretInputsMixin._secret_input_names(found["workflow"]))
        if approved:
            status = "approved"
        elif bindings:
            status = "approval_required"
        elif secret_inputs:
            # No binding of its own, but a parent may pass it a catalog secret: that
            # run needs this workflow's own approval too. Inline values never do.
            status = "delegation_optional"
        else:
            status = "not_required"
        return {
            "workflow_id": workflow_id,
            "secret_inputs": secret_inputs,
            "status": status,
            "fingerprint": workflow_gate.approval_token(fingerprint, receivers),
            "secrets": [{"field": row["field"], "secret_id": row["secret_id"]} for row in bindings],
            "destinations": static_rules,
            "dynamic_http_steps": dynamic_steps,
            # What each connector step's input goes to, for this project; an
            # "unpinned" step never receives a catalog secret.
            "receivers": receivers,
            "approved_destinations": workflow_gate.approved_rules(approval) if approved else [],
        }

    def project_workflow_statuses(
        self, project_id: Any, scope: ScopeContext
    ) -> list[dict[str, Any]]:
        """Every linked workflow of a project that declares a secret input, with its status.

        Readable from viewer up: it is the operator's path to bind and approve without
        the builder or the project's admin settings.
        """
        require_secret_read_metadata(scope)
        with self._transaction() as (conn, cur):
            _workspace, owner_project = self._owner_scope(
                cur, scope, project_id=project_id, workspace_id=None
            )
            cur.execute(
                """
                SELECT pw.workflow_id, COALESCE(w.display_name, pw.workflow_id) AS display_name
                  FROM project_workflows AS pw
                  JOIN registry_workflows AS w ON w.workflow_id = pw.workflow_id
                 WHERE pw.project_id::text = %s AND pw.deleted_at IS NULL
                   AND w.deleted_at IS NULL
                   AND (w.account_id IS NULL OR w.account_id::text = %s)
                 ORDER BY 2, 1
                """,
                (owner_project, scope.account_id),
            )
            linked = [
                (str(row[0]), str(row[1]))
                if not isinstance(row, dict)
                else (str(row["workflow_id"]), str(row["display_name"]))
                for row in cur.fetchall() or []
            ]
            views = []
            for workflow_id, display_name in linked:
                try:
                    found = self._workflow_in_project(conn, cur, scope, workflow_id, owner_project)
                except (ServiceNotFoundError, ServiceValidationError):
                    continue
                view = self._workflow_status_view(cur, scope, found)
                if view["secret_inputs"]:
                    views.append({**view, "display_name": display_name})
            return views

    def workflow_approval_status(
        self, workflow_id: Any, scope: ScopeContext, *, project_id: Any
    ) -> dict[str, Any]:
        require_secret_read_metadata(scope)
        with self._transaction() as (conn, cur):
            found = self._workflow_in_project(conn, cur, scope, workflow_id, project_id)
            return self._workflow_status_view(cur, scope, found)

    def approve_workflow(
        self,
        workflow_id: Any,
        scope: ScopeContext,
        *,
        project_id: Any,
        fingerprint: Any,
        destinations: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Approve the definition shown, with the destinations a person allowed for it.

        Literal HTTP step URLs are allowed automatically; a step whose URL is computed
        needs explicit rules, and without them the definition cannot be approved.
        """
        from core.application.secrets import workflow_gate

        require_approval_authority(scope)
        try:
            explicit = [workflow_gate.normalize_rule(rule) for rule in destinations or []]
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from None
        _kind, actor = actor_identity(scope)
        with self._transaction() as (conn, cur):
            found = self._workflow_in_project(conn, cur, scope, workflow_id, project_id)
            definition = found["definition"]
            projection = workflow_gate.current_projection(
                cur,
                account_id=scope.account_id,
                workflow_id=_text(workflow_id),
                definition=definition,
            )
            current = workflow_gate.workflow_fingerprint(projection)
            receivers = self._receivers(cur, scope, found)
            if workflow_gate.approval_token(current, receivers) != _text(fingerprint):
                raise ServiceConflictError(
                    "The workflow changed after it was shown; review it again"
                )
            previous = workflow_gate.workflow_approval(
                cur, account_id=scope.account_id, workflow_id=_text(workflow_id)
            )
            static_rules, dynamic_steps = workflow_gate.http_destinations(definition)
            if dynamic_steps and not explicit:
                raise ServiceValidationError(
                    "HTTP steps with a computed URL need explicit destinations: "
                    + ", ".join(dynamic_steps)
                )
            rules = [*static_rules, *(rule for rule in explicit if rule not in static_rules)]
            cur.execute(
                """
                INSERT INTO catalog_secret_approvals (
                    account_id, consumer_kind, workflow_id, fingerprint, projection_jsonb,
                    approved_by, approved_by_kind
                ) VALUES (%s, 'workflow', %s, %s, %s::jsonb, %s, 'human')
                ON CONFLICT (workflow_id)
                DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                              projection_jsonb = EXCLUDED.projection_jsonb,
                              approved_by = EXCLUDED.approved_by,
                              approved_by_kind = 'human', approved_at = now()
                """,
                (
                    scope.account_id,
                    _text(workflow_id),
                    current,
                    json.dumps(
                        {
                            **projection,
                            "approved_destinations": rules,
                            "receivers": workflow_gate.merged_receivers(
                                previous, current, receivers
                            ),
                        },
                        sort_keys=True,
                    ),
                    actor,
                ),
            )
            self._audit(
                cur,
                scope,
                "approve",
                {"workflow_id": _text(workflow_id)},
                fingerprint=current,
                destinations=rules,
                receivers=receivers,
            )
            return self._workflow_status_view(cur, scope, found)


__all__ = ["WorkflowSecretInputsMixin"]
