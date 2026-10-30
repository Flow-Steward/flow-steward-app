# mypy: ignore-errors
"""Workflow portability service mixin."""

from __future__ import annotations

from typing import Any

from core.application.registry.projected_artifact_rows import (
    _durable_projection_install_id,
    projection_aware_catalog_row,
)
from core.application.workflows.capability_visibility import capability_visible_by_policy
from core.application.workflows.workflow_portability_common import (
    BLOCKING_STATUSES,
    DEPENDENCY_STATUSES,
    RESOLUTION_ACTIONS,
    WorkflowPortabilityError,
    _as_dict,
    _as_list,
    _call_db_get,
    _call_db_get_scoped,
    _callable_db_method,
    _dependency_key,
    _invalid_dependency_ref,
    _redact_sensitive,
    _text,
)

_REUSE_ACTIONS = frozenset({"use_existing", "map_existing"})


def _accepted_actions(offered: list[str]) -> set[str]:
    """The actions a row accepts: those it offers, with the two reuse spellings as one."""
    accepted = set(offered)
    if accepted & _REUSE_ACTIONS or "install_extension_required" in accepted:
        accepted |= _REUSE_ACTIONS
    return accepted


def _other_extension(source: dict[str, Any] | None, expected_extension: str) -> str:
    """The extension a chosen connection belongs to when it is not the step's one; else ''.

    Unknown on either side is no mismatch: a bundle from before steps named their
    extension, or a placeholder given only an install id, is judged as before.
    """
    if not source or not expected_extension:
        return ""
    actual = _text(
        source.get("extension_id")
        or source.get("connector_id")
        or source.get("target_extension_id")
    )
    return actual if actual and actual != expected_extension else ""


class WorkflowPortabilityResolutionMixin:
    def _resolution_index(
        self,
        resolutions: list[dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        indexed: dict[tuple[str, str], dict[str, Any]] = {}
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                continue
            action = _text(resolution.get("action"))
            if action not in RESOLUTION_ACTIONS:
                continue
            dependency_key = _text(resolution.get("dependency_key"))
            dependency_type = _text(resolution.get("dependency_type") or resolution.get("type"))
            ref = _text(resolution.get("ref") or resolution.get("dependency_ref"))
            if dependency_key and ":" in dependency_key:
                dependency_type, ref = dependency_key.split(":", 1)
            if not dependency_type or not ref:
                continue
            indexed[(dependency_type, ref)] = {**resolution, "action": action}
        return indexed

    def _bundled_child_dependency(
        self,
        row: dict[str, Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        """A child workflow the bundle itself carries: created with it, never missing.

        Its reuse is decided on its `workflow:` row. A mapping on this row would make
        the rewrite point the caller at an id nothing checked, so it is a conflict.
        """
        dependency_type = _text(row.get("dependency_type"))
        ref = _text(row.get("ref"))
        resolution = resolution_index.get((dependency_type, ref))
        if resolution and _text(resolution.get("action")) != "abort":
            return {
                **self._classify_dependency(row, {}),
                "status": "conflict",
                "blocking": True,
                "resolution_actions": ["abort"],
                "message": "This workflow comes with the file; choose what to do on its own row.",
            }
        return {
            "dependency_key": _dependency_key(dependency_type, ref),
            "dependency_type": dependency_type,
            "ref": ref,
            "label": _text(row.get("label") or ref),
            "status": "resolved",
            "blocking": False,
            "required": bool(row.get("required", True)),
            "sources": _as_list(row.get("sources")),
            "resolution_actions": [],
            "resolved_by": {"action": "bundled"},
            "message": "Included in this template.",
            "metadata": _redact_sensitive({**_as_dict(row.get("metadata")), "bundled": True}),
        }

    def _classify_dependency(
        self,
        row: dict[str, Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        dependency_type = _text(row.get("dependency_type"))
        ref = _text(row.get("ref"))
        metadata = _as_dict(row.get("metadata"))
        resolution = resolution_index.get((dependency_type, ref))
        status: str
        resolution_actions: list[str]
        message = ""
        code = _text(metadata.get("code"))

        if code == "invalid_dependency_ref" or _invalid_dependency_ref(ref):
            status = "blocked"
            resolution_actions = ["abort"]
            message = (
                f"Bundle references a {dependency_type.replace('_', ' ')} dependency "
                "without a usable id. Fix the source YAML or exported workflow before import."
            )
        elif dependency_type == "workflow" and code == "workflow_id_conflict":
            status = "conflict"
            bundled_child = bool(metadata.get("bundled_child"))
            # A child the template carries can instead reuse the workflow already
            # here under that id: the calling workflow is pointed at it.
            resolution_actions = (
                ["rename_workflow", "use_existing", "abort"]
                if bundled_child
                else ["rename_workflow", "abort"]
            )
            action = _text(resolution.get("action")) if resolution else ""
            target = (
                _text(
                    resolution.get("target_workflow_id")
                    or resolution.get("target_ref")
                    or resolution.get("target_id")
                )
                if resolution
                else ""
            )
            if action == "rename_workflow":
                # The explicit root target wins during import. Classify the same
                # effective id here, even if a resolution names a different free id.
                effective_target = _text(metadata.get("target_workflow_id")) or target
                status = (
                    "resolved"
                    if effective_target and not self._workflow_identity_reserved(effective_target)
                    else "conflict"
                )
            elif action == "use_existing" and bundled_child:
                status = "resolved" if self._workflow_exists(target or ref) else "conflict"
            message = (
                "Workflow id is reserved, including IDs retained in Trash. "
                "Reuse an active existing workflow or choose a fresh id."
                if bundled_child
                else "Workflow id is reserved, including IDs retained in Trash. "
                "Choose a fresh target workflow id."
            )
        elif dependency_type == "workflow":
            status = "blocked"
            resolution_actions = ["abort"]
            if code == "workflow_id_missing":
                message = "Workflow definition is missing workflow_id."
            else:
                message = "Workflow definition is invalid."
        elif dependency_type == "extension_workflow_ref":
            status = "protected_reference"
            resolution_actions = ["install_extension_required", "abort"]
            message = (
                "Protected extension workflow exports only a reference. Install the "
                "extension to provision the workflow; this import path cannot recreate it."
            )
        elif dependency_type in {"agent", "skill", "tool", "child_workflow", "published_block"}:
            exists = self._dependency_exists(dependency_type, ref)
            status = "resolved" if exists else "requires_mapping"
            resolution_actions = ["use_existing", "abort"] if exists else ["map_existing", "abort"]
            if not bool(row.get("required", True)):
                resolution_actions.insert(1, "skip_optional")
            if resolution:
                action = _text(resolution.get("action"))
                if action in {"use_existing", "map_existing"}:
                    target = (
                        _text(
                            resolution.get("target_ref")
                            or resolution.get("target_id")
                            or resolution.get("target_workflow_id")
                        )
                        or ref
                    )
                    status = (
                        "resolved"
                        if self._dependency_exists(dependency_type, target)
                        else "conflict"
                    )
                elif action == "skip_optional" and not bool(row.get("required", True)):
                    status = "resolved"
            if status == "requires_mapping":
                message = f"{dependency_type.replace('_', ' ').title()} must be mapped to an existing registry row."
        elif dependency_type == "extension":
            status = "resolved" if self._install_exists(ref) else "requires_extension_install"
            resolution_actions = ["install_extension_required", "abort"]
            if resolution and resolution.get("action") in {
                "install_extension_required",
                "use_existing",
                "map_existing",
            }:
                target = _text(
                    resolution.get("target_ref")
                    or resolution.get("target_id")
                    or resolution.get("target_install_id")
                    or ref
                )
                exists = (
                    self._install_exists(target)
                    if resolution.get("action") == "install_extension_required"
                    else self._existing_install_in_account(target)
                )
                status = "resolved" if exists else "requires_extension_install"
            if status == "requires_extension_install":
                message = "Required extension install is missing."
                if _text(metadata.get("reason")) == "connector_install_id_missing":
                    message = (
                        f"A step calls extension '{ref}' without an install. Map it to this "
                        "account's install of that extension, or install it first."
                    )
        elif dependency_type == "project_connection":
            status = "requires_project_connection"
            resolution_actions = ["map_existing", "create_connection_placeholder", "abort"]
            expected_extension = _text(metadata.get("extension_id"))
            other_extension = ""
            if resolution:
                action = _text(resolution.get("action"))
                if action in {"map_existing", "use_existing"}:
                    target = _text(
                        resolution.get("target_ref")
                        or resolution.get("target_id")
                        or resolution.get("target_connection_id")
                    )
                    connection = self._project_connection_row(target) if target else None
                    other_extension = _other_extension(connection, expected_extension)
                    status = (
                        "resolved"
                        if connection is not None and not other_extension
                        else "requires_project_connection"
                    )
                elif action == "create_connection_placeholder":
                    other_extension = _other_extension(resolution, expected_extension)
                    status = (
                        "resolved"
                        if not other_extension
                        and self._can_create_connection_placeholder(resolution)
                        else "requires_project_connection"
                    )
            if other_extension:
                message = (
                    f"The step calls {expected_extension}, but that connection belongs to "
                    f"{other_extension}. Choose a {expected_extension} connection."
                )
            elif status == "requires_project_connection":
                message = (
                    "Project connection must be mapped to an existing connection or "
                    "resolved with enough project/extension metadata to create a placeholder."
                )
        else:
            status = "blocked"
            resolution_actions = ["abort"]
            message = "Unknown dependency type."

        action = _text(resolution.get("action")) if resolution else ""
        if action and action != "abort" and action not in _accepted_actions(resolution_actions):
            # The rewrite applies any mapping action it is given, so one this row does not
            # offer (rename on a child workflow, say) would point the workflow at an id
            # nothing here checked.
            status = "conflict"
            message = "That choice is not available for this dependency."
        if status not in DEPENDENCY_STATUSES:
            status = "blocked"
        blocking = status in BLOCKING_STATUSES
        return {
            "dependency_key": _dependency_key(dependency_type, ref),
            "dependency_type": dependency_type,
            "ref": ref,
            "label": _text(row.get("label") or ref),
            "status": status,
            "blocking": blocking,
            "required": bool(row.get("required", True)),
            "sources": _as_list(row.get("sources")),
            "resolution_actions": resolution_actions,
            "resolved_by": _as_dict(resolution) if resolution and not blocking else None,
            "message": message,
            "metadata": _redact_sensitive(metadata),
        }

    def _dependency_exists(self, dependency_type: str, ref: str) -> bool:
        if dependency_type in {"agent", "skill"}:
            return self._existing_row_for_dependency(dependency_type, ref) is not None
        if dependency_type == "tool":
            return _call_db_get(self.db, "get_tool", ref) is not None
        if dependency_type == "child_workflow":
            return self._workflow_exists(ref)
        if dependency_type == "published_block":
            return _call_db_get(self.db, "get_published_block_contract", ref) is not None
        return False

    def _existing_row_for_dependency(self, dependency_type: str, ref: str) -> dict[str, Any] | None:
        if dependency_type in {"agent", "skill"}:
            manual_row = _call_db_get(self.db, f"get_{dependency_type}", ref)
            account_id = _text(getattr(self, "account_id", ""))
            if not account_id:
                # Native existence is legacy portability behavior, not permission.
                # A bridge needs actual account/active-install authority to resolve.
                return None if _durable_projection_install_id(manual_row or {}) else manual_row
            row = projection_aware_catalog_row(
                self.db,
                dependency_type,
                f"{dependency_type}_id",
                ref,
                manual_row,
                account_id=account_id,
            )
            if (
                row
                and _durable_projection_install_id(row)
                and (
                    _text(row.get("account_id")) != account_id
                    or not capability_visible_by_policy(ref, capability=row)
                )
            ):
                return None
            return row
        if dependency_type == "tool":
            return _call_db_get(self.db, "get_tool", ref)
        if dependency_type == "child_workflow":
            return _call_db_get(self.db, "get_workflow", ref)
        if dependency_type == "published_block":
            return _call_db_get(self.db, "get_published_block_contract", ref)
        if dependency_type == "extension":
            return self._extension_install_row(ref)
        return None

    def _extension_install_row(self, install_id: str) -> dict[str, Any] | None:
        """Resolve an install to the identity an export can carry to another machine.

        AC17-R60: this returned None, so an extension dependency was never enriched and the
        human-readable field fell back to the raw ref -- an `install_id` that is unique to one
        installation on one machine. The importing operator saw a UUID as the title, the same
        UUID as the subtitle, and an empty target list.

        Deliberately the operator lookup rather than the executable one: an export must
        describe a dependency that exists, and a fresh installation arrives with every
        extension disabled (AC17-R66), so an active-only lookup would erase exactly the
        dependencies a new install is most likely to have.
        """
        store = getattr(self, "extension_install_store", None)
        account_id = str(getattr(self, "account_id", "") or "").strip()
        if store is None or not account_id:
            return None
        return _call_db_get_scoped(
            store, "get_extension_install_for_operator", install_id, account_id=account_id
        )

    def _workflow_exists(self, workflow_id: str) -> bool:
        return _call_db_get(self.db, "get_workflow", workflow_id) is not None

    def _workflow_identity_reserved(self, workflow_id: str) -> bool:
        return _call_db_get(self.db, "get_workflow", workflow_id, include_trashed=True) is not None

    def _install_exists(self, install_id: str) -> bool:
        # AC17-R67. Ask the store that owns installs first, scoped to this account. Falling
        # back to self.db keeps every caller that passes a store with these methods working;
        # it is not the path the admin routes take, because RegistryDBStore has none of them.
        store = getattr(self, "extension_install_store", None)
        if store is not None:
            account_id = str(getattr(self, "account_id", "") or "").strip()
            if not account_id:
                # Asking with no account disables the lookup's own account filter, so a
                # preflight would resolve installs owned by other accounts. Refusing to
                # answer is the safe direction: the dependency stays blocking, exactly as
                # it did before this path existed.
                return False
            if _call_db_get_scoped(
                store, "get_extension_by_install_id", install_id, account_id=account_id
            ):
                return True
        for method_name in ("get_extension_by_install_id", "get_extension_install", "get_install"):
            row = _call_db_get(self.db, method_name, install_id)
            if row is not None:
                return True
        return False

    def _existing_install_in_account(self, install_id: str) -> bool:
        """Mapping may reuse only an installed target verified for this account."""
        account_id = str(getattr(self, "account_id", "") or "").strip()
        if not account_id:
            return False
        store = getattr(self, "extension_install_store", None) or self.db
        row = _call_db_get_scoped(
            store, "get_extension_by_install_id", install_id, account_id=account_id
        )
        if row is None:
            return False
        row_account = _text(row.get("account_id"))
        if row_account and row_account != account_id and row.get("visibility") != "global":
            return False
        state = _text(row.get("state"))
        return row.get("enabled") is not False and (not state or state == "active")

    def _project_connection_exists(self, connection_ref: str) -> bool:
        return self._project_connection_row(connection_ref) is not None

    def _project_connection_row(self, connection_ref: str) -> dict[str, Any] | None:
        method = getattr(self.project_connection_store, "get_project_connection", None)
        if callable(method):
            for kwargs in ({"connection_id": connection_ref}, {}):
                try:
                    row = method(connection_ref) if not kwargs else method(**kwargs)
                except Exception:
                    row = None
                if isinstance(row, dict) and row:
                    return row
        return None

    def _can_create_connection_placeholder(self, resolution: dict[str, Any]) -> bool:
        if not _text(resolution.get("project_id")):
            return False
        extension_id = _text(
            resolution.get("extension_id")
            or resolution.get("connector_id")
            or resolution.get("target_extension_id")
        )
        extension_install_id = _text(
            resolution.get("extension_install_id") or resolution.get("target_extension_install_id")
        )
        if not extension_id and not extension_install_id:
            return False
        return any(
            _callable_db_method(self.project_connection_store, method_name) is not None
            for method_name in (
                "create_project_connection_placeholder",
                "create_connection_placeholder",
                "upsert_project_connection",
            )
        )

    def _ensure_resolution_side_effects(
        self,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        updated = {key: dict(value) for key, value in resolution_index.items()}
        for (dependency_type, ref), resolution in list(updated.items()):
            action = _text(resolution.get("action"))
            if dependency_type == "extension" and action == "install_extension_required":
                target = (
                    _text(
                        resolution.get("target_ref")
                        or resolution.get("target_id")
                        or resolution.get("target_install_id")
                    )
                    or ref
                )
                if not self._install_exists(target):
                    raise WorkflowPortabilityError(
                        "Required extension install is missing.",
                        code="workflow_import_extension_missing",
                        errors=[
                            {
                                "dependency_type": "extension",
                                "ref": ref,
                                "target_ref": target,
                                "status": "requires_extension_install",
                            }
                        ],
                    )
            if (
                dependency_type == "project_connection"
                and action == "create_connection_placeholder"
            ):
                target_ref = self._create_connection_placeholder(ref, resolution)
                resolution["target_ref"] = target_ref
                resolution["target_connection_id"] = target_ref
                updated[(dependency_type, ref)] = resolution
        return updated

    def _create_connection_placeholder(
        self,
        ref: str,
        resolution: dict[str, Any],
    ) -> str:
        existing_target = _text(
            resolution.get("target_ref")
            or resolution.get("target_id")
            or resolution.get("target_connection_id")
        )
        if existing_target and self._project_connection_exists(existing_target):
            return existing_target
        if self._project_connection_exists(ref):
            return ref
        if not self._can_create_connection_placeholder(resolution):
            raise WorkflowPortabilityError(
                "Project connection placeholder resolution is incomplete.",
                code="workflow_import_project_connection_unresolved",
                errors=[
                    {
                        "dependency_type": "project_connection",
                        "ref": ref,
                        "status": "requires_project_connection",
                    }
                ],
            )

        project_id = _text(resolution.get("project_id"))
        if not project_id:
            raise WorkflowPortabilityError(
                "Project connection placeholder requires a project_id.",
                code="workflow_import_project_connection_unresolved",
                errors=[
                    {
                        "dependency_type": "project_connection",
                        "ref": ref,
                        "status": "requires_project_connection",
                    }
                ],
            )
        extension_id = _text(
            resolution.get("extension_id")
            or resolution.get("connector_id")
            or resolution.get("target_extension_id")
        )
        extension_install_id = _text(
            resolution.get("extension_install_id") or resolution.get("target_extension_install_id")
        )

        for method_name in (
            "create_project_connection_placeholder",
            "create_connection_placeholder",
        ):
            method = _callable_db_method(self.project_connection_store, method_name)
            if method is None:
                continue
            row = method(
                connection_ref=ref,
                project_id=project_id,
                extension_id=extension_id,
                extension_install_id=extension_install_id,
            )
            if isinstance(row, dict) and _text(row.get("connection_id")):
                return _text(row.get("connection_id"))

        upsert = _callable_db_method(self.project_connection_store, "upsert_project_connection")
        if upsert is not None:
            row = upsert(
                project_id=project_id,
                extension_id=extension_id,
                connection_kind=_text(resolution.get("connection_kind"))
                or ("extension_runtime" if extension_install_id else "extension"),
                extension_install_id=extension_install_id,
                runtime_mode=_text(resolution.get("runtime_mode")),
                enabled=False,
                health_status="missing_configuration",
                runtime_status="requires_configuration",
                last_error="Created as workflow import placeholder; configure before enabling.",
                connection_config={
                    "portability_placeholder": True,
                    "source_connection_ref": ref,
                },
                secret_refs=[],
                connection_routes=[],
                ingress_config={},
                workflow_mappings={},
            )
            if isinstance(row, dict) and _text(row.get("connection_id")):
                return _text(row.get("connection_id"))

        raise WorkflowPortabilityError(
            "Project connection placeholder could not be created.",
            code="workflow_import_project_connection_unresolved",
            errors=[
                {
                    "dependency_type": "project_connection",
                    "ref": ref,
                    "status": "requires_project_connection",
                }
            ],
        )
