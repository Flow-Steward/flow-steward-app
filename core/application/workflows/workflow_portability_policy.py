# mypy: ignore-errors
"""Workflow portability service mixin."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from core.application.registry.workflow_secret_defaults import (
    preserve_secret_references,
    strip_workflow_secret_authority,
)
from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING
from core.application.workflows.workflow_hook_authoring_policy import (
    HOOK_DECLARATION_MODE_CODE,
    HOOK_DECLARATION_MODE_MESSAGE,
    hook_declaration_authoring_issues,
)
from core.application.workflows.workflow_portability_common import (
    PROTECTED_WORKFLOW_PATCH_FIELDS,
    TRUSTED_WORKFLOW_DEFAULTS,
    TRUSTED_WORKFLOW_METADATA_FIELDS,
    WorkflowPortabilityError,
    _as_dict,
    _call_db_get,
    _call_db_get_scoped,
    _explicit_full_export_allowed,
    _extension_owned,
    _is_mock,
    _redact_sensitive,
    _text,
    _workflow_settings,
)
from core.application.workflows.workflow_portability_definitions import (
    _portable_extension_identity,
)


class WorkflowPortabilityPolicyMixin:
    def __init__(
        self,
        db: Any,
        *,
        project_connection_store: Any | None = None,
        extension_install_store: Any | None = None,
        account_id: str = "",
        import_transaction_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.db = db
        self.project_connection_store = project_connection_store or db
        # AC17-R67: the admin routes hand this service a RegistryDBStore, which has none of
        # the three install-lookup methods the preflight probes, so every extension
        # dependency stayed blocking on every deployment. The store that does own the lookup
        # is the same integration store already built here for project connections -- the
        # identical defect shape was fixed for connections once already, see the docstring on
        # _platform_logger_for_admin_catalog. The account id is required with it: the lookup
        # skips its account filter when asked with None, which would let a preflight confirm
        # an install belonging to someone else.
        self.extension_install_store = extension_install_store
        self.account_id = str(account_id or "").strip()
        self.import_transaction_factory = import_transaction_factory

    def _import_transaction(self) -> AbstractContextManager[Any]:
        factory = self.import_transaction_factory
        if factory is None:
            factory = getattr(self.db, "workflow_import_transaction", None)
        if not callable(factory):
            raise WorkflowPortabilityError(
                "Workflow import transaction is unavailable.",
                code="workflow_import_transaction_unavailable",
                status_code=503,
            )
        transaction = factory()
        if not hasattr(transaction, "__enter__") or not hasattr(transaction, "__exit__"):
            raise WorkflowPortabilityError(
                "Workflow import transaction is unavailable.",
                code="workflow_import_transaction_unavailable",
                status_code=503,
            )
        return transaction

    def export_policy_for_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """Return export/clone policy for an already-loaded workflow row."""
        return self._export_policy_from_workflow(workflow)

    def export_policy(self, workflow_id: str) -> dict[str, Any]:
        workflow = _call_db_get(self.db, "get_workflow", workflow_id)
        if workflow is None:
            raise WorkflowPortabilityError(
                f"Workflow '{workflow_id}' not found",
                code="workflow_not_found",
                status_code=404,
            )
        return self._export_policy_from_workflow(workflow)

    def _source_extension_identity(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """The extension a workflow comes from, by the names a person reads.

        B13: the header said "Provided by extension: <install UUID>", an id unique
        to one installation. The install is looked up in the account that owns the
        workflow row -- its install foreign key is (install_id, account_id) -- so
        the lookup never answers across tenants, and once per install per service.
        """
        install_id = _text(workflow.get("source_install_id"))
        account_id = _text(workflow.get("account_id")) or _text(getattr(self, "account_id", ""))
        store = getattr(self, "extension_install_store", None)
        if not install_id or not account_id or store is None or _is_mock(store):
            return {}
        cache = self.__dict__.setdefault("_source_extension_identities", {})
        key = (install_id, account_id)
        if key not in cache:
            row = _call_db_get_scoped(
                store, "get_extension_install_for_operator", install_id, account_id=account_id
            )
            cache[key] = _portable_extension_identity(row, install_id=install_id) if row else {}
        return cache[key]

    def _export_policy_from_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _text(workflow.get("workflow_id"))
        extension_owned = _extension_owned(workflow)
        explicit_full = _explicit_full_export_allowed(workflow)
        protected = bool(extension_owned and not explicit_full)
        availability_status = _text(workflow.get("availability_status") or "available")
        reason_code = _text(workflow.get("availability_reason_code") or "none")
        reason_text = _text(workflow.get("availability_reason_text"))
        reason = ""
        if protected:
            reason = "Workflow is provided by an extension and protected by default."
        if reason_text:
            reason = reason_text
        source_extension = self._source_extension_identity(workflow) if extension_owned else {}
        policy = {
            "workflow_id": workflow_id,
            "extension_owned": extension_owned,
            "protected": protected,
            "full_export_allowed": not protected,
            "reference_export_allowed": extension_owned,
            "clone_allowed": not protected,
            # Three separate operations (spec §8.2): anyone allowed to view the
            # workflow reads its structure, redacted when protected; changing
            # it and running one author step in isolation have independent
            # authority. An export/fork license never grants source writes.
            "structure_read_allowed": True,
            "structure_change_allowed": not extension_owned,
            "step_test_run_allowed": not protected,
            "source_install_id": _text(workflow.get("source_install_id")),
            "source_release_id": _text(workflow.get("source_release_id")),
            # The extension by name and version, for people; "" when the install
            # cannot be read. Never an install id in a human field.
            "source_extension_id": _text(source_extension.get("extension_id")),
            "source_extension_name": _text(source_extension.get("display_name")),
            "source_extension_version": _text(source_extension.get("version")),
            "local_workflow_id": _text(workflow.get("local_workflow_id")),
            "projection_mode": _text(workflow.get("projection_mode") or "manual"),
            "availability_status": availability_status,
            "availability_reason_code": reason_code,
            "availability_reason_text": reason_text,
            "new_runs_blocked": bool(workflow.get("new_runs_blocked", False)),
            "visible_in_catalog": bool(workflow.get("visible_in_catalog", True)),
            "reason": reason,
        }
        # The structured reason behind ``availability_reason_text``: which
        # extension is missing, by name, and whether it is installed at all.
        details = workflow.get("missing_extension_details")
        if availability_status != "available" and isinstance(details, list) and details:
            policy["missing_extension_details"] = [
                dict(item) for item in details if isinstance(item, dict)
            ]
        return policy

    def redact_protected_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        policy = self._export_policy_from_workflow(workflow)
        return self._redact_workflow_with_policy(workflow, policy)

    def _redact_workflow_with_policy(
        self,
        workflow: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """A read-only copy: a protected author graph keeps its real structure.

        Steps, Groups, For each containers and the execution graph stay
        readable; sensitive values in their configuration and settings are
        redacted and secret authority is stripped. The copy carries
        ``protected_workflow`` so every adapter shows it read-only.
        """
        redacted = strip_workflow_secret_authority(workflow)
        redacted["portability"] = policy
        if policy.get("protected"):
            for key in ("steps", "groups", "for_each"):
                items = redacted.get(key)
                redacted[key] = (
                    [_redact_sensitive(item) for item in items if isinstance(item, dict)]
                    if isinstance(items, list)
                    else []
                )
            settings = _workflow_settings(redacted)
            redacted["settings"] = {
                key: (
                    value if key == EXECUTION_GRAPH_SETTING else _redact_sensitive(value, key=key)
                )
                for key, value in settings.items()
            }
            redacted["protected_workflow"] = True
        return redacted

    def assert_structure_change_allowed(self, workflow_id: str, *, action: str) -> None:
        """Refuse a change of an installed author graph (nodes, edges, configs, notes)."""
        workflow = _call_db_get(self.db, "get_workflow", workflow_id)
        if workflow is None:
            return
        self.assert_structure_change_allowed_for(workflow, action=action)

    def assert_structure_change_allowed_for(self, workflow: dict[str, Any], *, action: str) -> None:
        policy = self._export_policy_from_workflow(workflow)
        if not policy.get("structure_change_allowed"):
            self._raise_internal_structure_protected(
                _text(workflow.get("workflow_id")),
                action=action,
                message="An installed extension workflow is read-only; its structure cannot change.",
                policy=policy,
            )

    def assert_step_test_run_allowed(self, workflow_id: str, *, action: str) -> None:
        """Refuse running one author step of a protected workflow in isolation.

        This is its own check: being allowed to read the graph never authorizes
        a Step Test Run of it, on any adapter, even when the UI hides the button.
        """
        workflow = _call_db_get(self.db, "get_workflow", workflow_id)
        if workflow is None:
            return
        self.assert_step_test_run_allowed_for(workflow, action=action)

    def assert_step_test_run_allowed_for(self, workflow: dict[str, Any], *, action: str) -> None:
        policy = self._export_policy_from_workflow(workflow)
        if not policy.get("step_test_run_allowed"):
            self._raise_internal_structure_protected(
                _text(workflow.get("workflow_id")),
                action=action,
                message=(
                    "A step of a protected extension workflow cannot run on its own; "
                    "run the workflow, or test your own child workflow."
                ),
                policy=policy,
                code="workflow_step_test_run_protected",
            )

    def assert_clone_allowed(self, workflow_id: str) -> None:
        policy = self.export_policy(workflow_id)
        if not policy.get("clone_allowed"):
            raise WorkflowPortabilityError(
                "Protected extension workflows cannot be cloned.",
                code="workflow_clone_protected",
                status_code=403,
            )

        # A clone owns new declarations even when their hook-id strings match.
        workflow = self._authoritative_guard_definition(
            self._authoritative_guard_record("get_workflow", workflow_id)
        )
        # The source used for declaration authority must still allow cloning.
        if not self._export_policy_from_workflow(workflow).get("clone_allowed"):
            raise WorkflowPortabilityError(
                "Protected extension workflows cannot be cloned.",
                code="workflow_clone_protected",
                status_code=403,
            )
        self._assert_hook_declarations_allowed(workflow)

    @staticmethod
    def _authoring_guard_unavailable() -> WorkflowPortabilityError:
        return WorkflowPortabilityError(
            "Workflow authoring checks are unavailable. Try again.",
            code="workflow_authoring_guard_unavailable",
            status_code=503,
        )

    def _authoritative_guard_record(
        self, method_name: str, ref: str, **filters: Any
    ) -> dict[str, Any] | None:
        """A write guard must distinguish a failed read from a missing record."""
        try:
            method = getattr(self.db, method_name, None)
            if not callable(method):
                raise TypeError("Authoritative workflow lookup is unavailable")
            row = method(ref, **filters)
        except Exception as exc:
            raise self._authoring_guard_unavailable() from exc
        if row is None:
            return None
        if _is_mock(row) or not isinstance(row, dict):
            raise self._authoring_guard_unavailable()
        return dict(row)

    def _authoritative_guard_definition(self, workflow: dict[str, Any] | None) -> dict[str, Any]:
        if workflow is None or not isinstance(workflow.get("steps"), list):
            raise self._authoring_guard_unavailable()
        return workflow

    @staticmethod
    def _assert_hook_declarations_allowed(
        candidate: dict[str, Any], *, existing: dict[str, Any] | None = None
    ) -> None:
        issues = hook_declaration_authoring_issues(candidate, existing=existing)
        if issues:
            raise WorkflowPortabilityError(
                HOOK_DECLARATION_MODE_MESSAGE,
                code=HOOK_DECLARATION_MODE_CODE,
                status_code=403,
                errors=issues,
            )

    def public_create_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize untrusted admin creates so they cannot mint protected provenance."""
        sanitized = dict(payload)
        for field, default in TRUSTED_WORKFLOW_DEFAULTS.items():
            sanitized[field] = default
        if "settings" in sanitized:
            sanitized["settings"] = preserve_secret_references(sanitized.get("settings"), {})
        return sanitized

    def public_update_payload(
        self,
        existing: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize public full updates and block installed source overwrites."""
        policy = self._export_policy_from_workflow(existing)
        if not policy.get("structure_change_allowed"):
            raise WorkflowPortabilityError(
                "Installed extension workflows cannot be overwritten through the workflow editor.",
                code="workflow_update_protected",
                status_code=403,
            )
        sanitized = dict(payload)
        for field, default in TRUSTED_WORKFLOW_DEFAULTS.items():
            if field in existing:
                sanitized[field] = existing.get(field)
            else:
                sanitized[field] = default
        # Public metadata updates must not rewrite the stored definition. Omitting
        # steps and groups makes save_workflow take its metadata-only path; the
        # change service is the definition's only writer.
        sanitized.pop("steps", None)
        sanitized.pop("groups", None)
        sanitized.pop("for_each", None)
        # A configured secret's storage key never reaches the browser, so an
        # ordinary settings save cannot echo it back. Without this, editing any
        # unrelated field on the same form silently unconfigures the credential.
        if "settings" in sanitized:
            sanitized["settings"] = preserve_secret_references(
                sanitized.get("settings"), existing.get("settings")
            )
        return sanitized

    def public_patch_payload(
        self,
        existing: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate untrusted admin PATCH fields before persistence."""
        policy = self._export_policy_from_workflow(existing)
        attempted_trusted = TRUSTED_WORKFLOW_METADATA_FIELDS.intersection(updates)
        protected_fields = set()
        if not policy.get("structure_change_allowed"):
            protected_fields = (
                PROTECTED_WORKFLOW_PATCH_FIELDS
                | {"display_name", "description", "builder_metadata_jsonb"}
            ).intersection(updates)
        blocked_fields = sorted(attempted_trusted | protected_fields)
        if blocked_fields:
            raise WorkflowPortabilityError(
                "Workflow provenance, availability, and installed extension source metadata are not mutable through this API.",
                code="workflow_protected_metadata_immutable",
                status_code=403,
                errors=[
                    {
                        "workflow_id": _text(existing.get("workflow_id")),
                        "fields": blocked_fields,
                        "status": "blocked",
                    }
                ],
            )
        sanitized = dict(updates)
        if "settings" in sanitized:
            sanitized["settings"] = preserve_secret_references(
                sanitized.get("settings"), existing.get("settings")
            )
        return sanitized

    def _policy_for_history_entry(
        self,
        workflow_id: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        current = _call_db_get(self.db, "get_workflow", workflow_id)
        if current is not None:
            return self._export_policy_from_workflow(current)
        fallback = dict(snapshot)
        if workflow_id and not fallback.get("workflow_id"):
            fallback["workflow_id"] = workflow_id
        return self._export_policy_from_workflow(fallback)

    def redact_workflow_history_entry(
        self,
        entry: dict[str, Any],
        *,
        workflow_id: str = "",
    ) -> dict[str, Any]:
        row = dict(entry)
        entity_id = _text(workflow_id or row.get("entity_id"))
        snapshot = _as_dict(row.get("snapshot"))
        if not snapshot:
            return row
        if entity_id and not snapshot.get("workflow_id"):
            snapshot["workflow_id"] = entity_id
        policy = self._policy_for_history_entry(entity_id, snapshot)
        row["snapshot"] = self._redact_workflow_with_policy(snapshot, policy)
        return row

    def redact_workflow_history(
        self,
        entries: list[dict[str, Any]],
        *,
        workflow_id: str = "",
    ) -> list[dict[str, Any]]:
        return [
            self.redact_workflow_history_entry(entry, workflow_id=workflow_id)
            for entry in entries
            if isinstance(entry, dict)
        ]

    def redact_workflow_history_payload(self, payload: Any) -> Any:
        """Remove server-owned secret references from diff and restore responses."""
        redacted = strip_workflow_secret_authority(payload)
        if not isinstance(redacted, dict) or not isinstance(redacted.get("changes"), list):
            return redacted
        safe_changes = []
        for change in redacted["changes"]:
            if not isinstance(change, dict):
                safe_changes.append(change)
                continue
            path = _text(change.get("path") or change.get("field"))
            if path.endswith("default_secret_ref"):
                continue
            safe_changes.append(change)
        redacted["changes"] = safe_changes
        summary = redacted.get("summary")
        if isinstance(summary, dict) and redacted.get("comparison_kind") != "lifecycle":
            counts = {"added": 0, "removed": 0, "changed": 0}
            for change in safe_changes:
                kind = _text(change.get("change")) if isinstance(change, dict) else ""
                if kind in counts:
                    counts[kind] += 1
            redacted["summary"] = {**summary, "total": len(safe_changes), **counts}
        return redacted

    def _raise_internal_structure_protected(
        self,
        workflow_id: str,
        *,
        action: str,
        message: str,
        policy: dict[str, Any] | None = None,
        entry_id: str | None = None,
        code: str = "workflow_internal_structure_protected",
    ) -> None:
        error: dict[str, Any] = {
            "workflow_id": workflow_id,
            "action": action,
            "status": "protected_reference",
        }
        if entry_id is not None:
            error["entry_id"] = str(entry_id)
        if policy:
            error["source_install_id"] = policy.get("source_install_id")
            error["source_release_id"] = policy.get("source_release_id")
        raise WorkflowPortabilityError(
            message,
            code=code,
            status_code=403,
            errors=[error],
        )

    def _assert_live_workflow_internal_structure_allowed(
        self,
        workflow_id: str,
        *,
        action: str,
        message: str,
        source_write: bool = False,
    ) -> None:
        workflow = _call_db_get(self.db, "get_workflow", workflow_id)
        if workflow is None:
            return
        policy = self._export_policy_from_workflow(workflow)
        blocked = (
            not policy.get("structure_change_allowed") if source_write else policy.get("protected")
        )
        if blocked:
            self._raise_internal_structure_protected(
                workflow_id,
                action=action,
                message=message,
                policy=policy,
            )

    def _workflow_history_entry(
        self,
        workflow_id: str,
        entry_id: str | None,
    ) -> dict[str, Any] | None:
        if entry_id is None:
            return None
        method = getattr(self.db, "get_changelog_entry", None)
        if not callable(method):
            return None
        try:
            row = method(str(entry_id), entity_type="workflow", entity_id=workflow_id)
        except TypeError:
            try:
                row = method(str(entry_id))
            except Exception:
                return None
        except Exception:
            return None
        if _is_mock(row) or not row:
            return None
        return dict(row) if isinstance(row, dict) else None

    def _workflow_history_entries_for_diff(
        self,
        workflow_id: str,
        *,
        from_entry_id: str | None = None,
        to_entry_id: str | None = None,
    ) -> list[dict[str, Any]]:
        requested_ids = {
            str(entry_id).strip()
            for entry_id in (from_entry_id, to_entry_id)
            if entry_id is not None
        }
        requested_ids.discard("")
        entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for entry_id in sorted(requested_ids):
            entry = self._workflow_history_entry(workflow_id, entry_id)
            if not entry:
                continue
            entries.append(entry)
            seen_ids.add(str(entry.get("id") or entry_id).strip())
        if requested_ids and requested_ids.issubset(seen_ids):
            return entries

        method = getattr(self.db, "get_entity_history", None)
        if not callable(method):
            return entries
        try:
            rows = method(entity_type="workflow", entity_id=workflow_id, limit=200)
        except TypeError:
            try:
                rows = method("workflow", workflow_id, limit=200)
            except Exception:
                return entries
        except Exception:
            return entries
        if _is_mock(rows) or not isinstance(rows, list):
            return entries

        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id") or "").strip()
            if requested_ids:
                if row_id and row_id in requested_ids and row_id not in seen_ids:
                    entries.append(dict(row))
                    seen_ids.add(row_id)
                continue
            if len(entries) >= 2:
                break
            entries.append(dict(row))
        return entries

    def _assert_history_snapshots_allow_internal_structure(
        self,
        workflow_id: str,
        entries: list[dict[str, Any]],
        *,
        action: str,
        message: str,
        source_write: bool = False,
    ) -> None:
        for entry in entries:
            snapshot = _as_dict(entry.get("snapshot"))
            if not snapshot:
                continue
            if workflow_id and not snapshot.get("workflow_id"):
                snapshot["workflow_id"] = workflow_id
            policy = self._export_policy_from_workflow(snapshot)
            blocked = (
                not policy.get("structure_change_allowed")
                if source_write
                else policy.get("protected")
            )
            if not blocked:
                continue
            entry_id = str(entry.get("id") or "").strip() or None
            self._raise_internal_structure_protected(
                workflow_id,
                action=action,
                message=message,
                policy=policy,
                entry_id=entry_id,
            )

    def assert_history_diff_allowed(
        self,
        workflow_id: str,
        *,
        from_entry_id: str | None = None,
        to_entry_id: str | None = None,
    ) -> None:
        self._assert_live_workflow_internal_structure_allowed(
            workflow_id,
            action="workflow_history_diff",
            message="Protected extension workflow history diffs are not available through this API.",
        )
        entries = self._workflow_history_entries_for_diff(
            workflow_id,
            from_entry_id=from_entry_id,
            to_entry_id=to_entry_id,
        )
        self._assert_history_snapshots_allow_internal_structure(
            workflow_id,
            entries,
            action="workflow_history_diff",
            message="Protected extension workflow history diffs are not available through this API.",
        )

    def assert_restore_allowed(
        self,
        workflow_id: str,
        *,
        entry_id: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        current = self._authoritative_guard_record("get_workflow", workflow_id)
        if current is not None:
            policy = self._export_policy_from_workflow(current)
            if not policy.get("structure_change_allowed"):
                self._raise_internal_structure_protected(
                    workflow_id,
                    action="workflow_history_restore",
                    message="Installed extension workflow history restore is not available through this API.",
                    policy=policy,
                )
        if entry_id is None:
            raise self._authoring_guard_unavailable()
        entry = self._authoritative_guard_record(
            "get_changelog_entry", str(entry_id), entity_type="workflow", entity_id=workflow_id
        )
        if entry is None:
            raise self._authoring_guard_unavailable()
        entries = [entry]
        self._assert_history_snapshots_allow_internal_structure(
            workflow_id,
            entries,
            action="workflow_history_restore",
            message="Installed extension workflow history restore is not available through this API.",
            source_write=True,
        )
        if current is None:
            raise WorkflowPortabilityError(
                "A deleted workflow cannot be recreated from registry history.",
                code="workflow_deleted_restore_forbidden",
                status_code=409,
                errors=[
                    {
                        "workflow_id": workflow_id,
                        "entry_id": str(entry_id) if entry_id is not None else None,
                        "action": "workflow_history_restore",
                        "status": "hard_deleted",
                    }
                ],
            )

        # The retained-hook decision and the restore CAS must authorize the same
        # revision. A predicted newer revision must not reuse an older hook set.
        # Missing/invalid request revisions retain the adapters' existing 428/400
        # validation, after protected/deleted workflow policy has been applied.
        if expected_revision is not None and expected_revision > 0:
            current_revision = current.get("revision")
            if type(current_revision) is not int or current_revision < 1:
                raise self._authoring_guard_unavailable()
            if current_revision != expected_revision:
                from core.application.registry.db_store_workflows_mixin import (
                    WorkflowRevisionConflictError,
                )

                raise WorkflowRevisionConflictError(workflow_id, expected_revision)

        current = self._authoritative_guard_definition(current)
        snapshot = self._authoritative_guard_definition(_as_dict(entry.get("snapshot")))
        self._assert_hook_declarations_allowed(
            {**snapshot, "workflow_id": workflow_id}, existing=current
        )

    def assert_delete_allowed(self, workflow_id: str) -> None:
        self._assert_live_workflow_internal_structure_allowed(
            workflow_id,
            action="workflow_delete",
            message="Installed extension workflows cannot be deleted through this API.",
            source_write=True,
        )
