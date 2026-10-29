# mypy: ignore-errors
"""Workflow portability service mixin."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import yaml

from core.application.workflows.extension_connection_validation import (
    named_extension_without_install,
)
from core.application.workflows.workflow_catalog_identity import parse_projected_identity
from core.application.workflows.workflow_portability_artifacts import (
    DEFINITIONS_KEY,
    definitions_have_entries,
)
from core.application.workflows.workflow_portability_bundle_children import workflow_child_refs
from core.application.workflows.workflow_portability_common import (
    BUNDLE_KIND,
    HOOK_BINDINGS_KEY,
    SCHEMA_VERSION,
    WorkflowPortabilityError,
    _add_dependency,
    _agent_tool_refs,
    _as_dict,
    _as_list,
    _call_db_get,
    _call_db_list,
    _connection_refs,
    _dependency_option,
    _invalid_dependency_ref,
    _is_mock,
    _json_safe,
    _list_runtime_dependencies,
    _normalize_participants,
    _redact_sensitive,
    _sorted_options,
    _step_child_workflow_ref,
    _text,
    _tool_refs_from_override,
    _workflow_settings,
    _workflow_steps,
)
from core.application.workflows.workflow_provider_trigger_repository import (
    list_workflow_trigger_kinds,
)
from core.infrastructure.config.version import get_app_version


class WorkflowPortabilityExportMixin:
    def export_yaml(
        self,
        workflow_id: str,
        *,
        include_children: bool = False,
        project_id: str = "",
        reference_only: bool = False,
    ) -> str:
        """The workflow (and, with *include_children*, what it calls) as one bundle.

        A hook binding belongs to one project, so only an export for *project_id*
        carries the bindings of the exported workflows' hooks there, and follows a
        bound child like an invoked one. Without a project — the admin YAML route,
        which has none — the bundle carries no binding: there is no project whose
        configuration it could honestly describe.

        The bundle also carries the definitions of the agents, skills, manual data
        contracts and guardrails its workflows use, unless *reference_only* asks
        for the earlier file that names them only.
        """
        workflow = _call_db_get(self.db, "get_workflow", workflow_id)
        if workflow is None:
            raise WorkflowPortabilityError(
                f"Workflow '{workflow_id}' not found",
                code="workflow_not_found",
                status_code=404,
            )
        bindings = self._hook_binding_collector(project_id)
        bound = bindings.targets if bindings is not None else None
        workflows, dependencies = self._export_entry(workflow_id, workflow)
        full_sources = [workflow] if self._full_export(workflow_id) else []
        if bound is not None:
            bound(workflow)
        child_ids: list[str] = []
        if include_children:
            # One file is then a complete template: every workflow the root invokes,
            # and every workflow those invoke, travels with it.
            for child_id, child in self._child_workflows(workflow, bound=bound):
                child_entries, child_dependencies = self._export_entry(child_id, child)
                workflows.extend(child_entries)
                dependencies = _merge_dependency_sections(dependencies, child_dependencies)
                child_ids.append(child_id)
                if self._full_export(child_id):
                    full_sources.append(child)
        definitions = (
            None
            if reference_only
            else self._export_definitions(
                full_sources, account_id=_text(workflow.get("account_id"))
            )
        )
        binding_entries = bindings.entries if bindings is not None else []
        if binding_entries:
            dependencies = _merge_dependency_sections(
                dependencies, self._hook_binding_dependencies(binding_entries)
            )

        bundle = {
            "schema_version": SCHEMA_VERSION,
            "kind": BUNDLE_KIND,
            "metadata": {
                "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "exported_by": "admin",
                "source_workflow_id": _text(workflow.get("workflow_id") or workflow_id),
                "portability_kind": "workflow_bundle",
                # Informational: import ignores both. A catalog reads them to say how
                # the workflow starts and which Flow Steward it was exported from; the
                # version is the product release catalogs check compatibility against.
                "flow_steward_version": get_app_version(),
                "triggers": self._trigger_kinds(workflow_id),
                **({"child_workflow_ids": child_ids} if child_ids else {}),
            },
            "workflows": workflows,
            "dependencies": dependencies,
            **({HOOK_BINDINGS_KEY: binding_entries} if binding_entries else {}),
            **(
                {DEFINITIONS_KEY: definitions}
                if definitions is not None and definitions_have_entries(definitions)
                else {}
            ),
            "import_hints": {
                "default_enabled": False,
                "import_state": "draft",
                "conflict_policy": "rename_workflow",
            },
        }
        return yaml.safe_dump(
            _json_safe(_redact_sensitive(bundle)),
            sort_keys=False,
            allow_unicode=True,
        )

    def _full_export(self, workflow_id: str) -> bool:
        policy = self.export_policy(workflow_id)
        return not (policy.get("protected") and not policy.get("full_export_allowed"))

    def _export_entry(
        self, workflow_id: str, workflow: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """One workflow's bundle entry and dependency section, honouring export policy."""
        if not self._full_export(workflow_id):
            return [self._extension_workflow_ref(workflow)], self._reference_dependencies(workflow)
        return [self._workflow_definition(workflow)], self.export_dependency_section(workflow)

    def _child_workflows(
        self,
        root: dict[str, Any],
        *,
        bound: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Every workflow the root invokes, transitively, from the root's own account.

        Breadth first, each once; a cycle back to an included workflow stops there. A
        child that does not exist, or belongs to another account, is left out and
        stays a dependency the importer has to provide. *bound* names the children
        a workflow's hooks are connected to in the exported project; they are
        followed like invoked ones.
        """

        def calls(workflow: dict[str, Any]) -> list[str]:
            return [*workflow_child_refs(workflow), *(bound(workflow) if bound else [])]

        root_id = _text(root.get("workflow_id"))
        root_account = _text(root.get("account_id"))
        seen = {root_id}
        queue = calls(root)
        found: list[tuple[str, dict[str, Any]]] = []
        while queue:
            child_id = queue.pop(0)
            if not child_id or child_id in seen:
                continue
            seen.add(child_id)
            child = _call_db_get(self.db, "get_workflow", child_id)
            if not isinstance(child, dict):
                continue
            if _text(child.get("account_id")) != root_account:
                continue
            found.append((child_id, child))
            queue.extend(calls(child))
        return found

    def _trigger_kinds(self, workflow_id: str) -> list[dict[str, Any]]:
        """Schedule, webhook and provider-trigger kinds, read from the store that owns them."""
        store = getattr(self, "extension_install_store", None)
        if store is None or _is_mock(store) or getattr(store, "conn", None) is None:
            return []
        return list_workflow_trigger_kinds(
            store,
            workflow_id=workflow_id,
            account_id=str(getattr(self, "account_id", "") or ""),
        )

    def discover_dependencies(
        self, workflow: dict[str, Any], *, include_stored: bool = True
    ) -> list[dict[str, Any]]:
        """Dependencies of one workflow; ``include_stored`` adds its stored runtime rows.

        An import passes False: the file is judged by its own contents, and the rows
        stored here under the same workflow id belong to a different record.
        """
        dependencies: dict[tuple[str, str], dict[str, Any]] = {}
        workflow_id = _text(workflow.get("workflow_id"))
        settings = _workflow_settings(workflow)

        source_install_id = _text(workflow.get("source_install_id"))
        if source_install_id:
            _add_dependency(
                dependencies,
                "extension",
                source_install_id,
                label=source_install_id,
                source="workflow.source_install_id",
                metadata={"dependency_kind": "source_install"},
            )

        participants = _normalize_participants(workflow)
        participant_agents = {
            item["participant_key"]: item["agent_id"]
            for item in participants
            if item.get("participant_key") and item.get("agent_id")
        }
        for item in participants:
            _add_dependency(
                dependencies,
                "agent",
                item["agent_id"],
                source=f"participants.{item['participant_key']}",
            )

        for step in _workflow_steps(workflow):
            step_id = _text(step.get("step_id") or step.get("id"))
            source = step_id or "workflow.step"
            participant_key = _text(step.get("participant_key"))
            agent_id = _text(step.get("agent_id") or step.get("agent"))
            if not agent_id and participant_key:
                agent_id = participant_agents.get(participant_key, "")
            if agent_id:
                _add_dependency(dependencies, "agent", agent_id, source=source)

            skill_id = _text(step.get("skill_id") or step.get("skill"))
            if skill_id:
                _add_dependency(dependencies, "skill", skill_id, source=source)

            override = _as_dict(step.get("override"))
            for tool_id in _tool_refs_from_override(override):
                _add_dependency(dependencies, "tool", tool_id, source=f"{source}.override")

            block_key = _text(step.get("block_key"))
            if block_key:
                _add_dependency(dependencies, "published_block", block_key, source=source)
                projected = parse_projected_identity(block_key)
                if projected.get("install_id"):
                    _add_dependency(
                        dependencies,
                        "extension",
                        projected["install_id"],
                        source=f"{source}.block_key",
                        metadata={"dependency_kind": "published_block"},
                    )

            child_workflow_id = _step_child_workflow_ref(step)
            if child_workflow_id:
                _add_dependency(
                    dependencies,
                    "child_workflow",
                    child_workflow_id,
                    source=source,
                    required=not bool(step.get("optional", False)),
                )
                projected = parse_projected_identity(child_workflow_id)
                if projected.get("install_id"):
                    _add_dependency(
                        dependencies,
                        "extension",
                        projected["install_id"],
                        source=f"{source}.workflow_id",
                        metadata={"dependency_kind": "child_workflow"},
                    )

            connector_install_id = _text(
                step.get("connector_install_id") or override.get("connector_install_id")
            )
            if connector_install_id:
                _add_dependency(
                    dependencies,
                    "extension",
                    connector_install_id,
                    source=f"{source}.connector_install_id",
                    metadata={"dependency_kind": "extension_install"},
                )
            named_extension = named_extension_without_install(step)
            if named_extension is not None and (
                named_extension.connector_form
                or _call_db_get(self.db, "get_tool", _text(step.get("connector_id"))) is None
            ):
                # The step names its extension by package id and no install. Report it
                # like any extension, so import maps it to this account's install and
                # the rewrite fills connector_install_id, instead of a draft that
                # imports cleanly and then fails every run.
                _add_dependency(
                    dependencies,
                    "extension",
                    named_extension.extension_id,
                    source=f"{source}.connector_id",
                    metadata={
                        "dependency_kind": "extension_install",
                        "extension_id": named_extension.extension_id,
                        "reason": "connector_install_id_missing",
                    },
                )

            connection_binding = (
                step.get("connection_binding_jsonb")
                or step.get("connection_binding")
                or override.get("connection_binding")
                or {}
            )
            for connection_ref in _connection_refs(connection_binding):
                _add_dependency(
                    dependencies,
                    "project_connection",
                    connection_ref,
                    source=f"{source}.connection_binding",
                )

        runtime_deps = _as_list(settings.get("workflow_runtime_dependencies"))
        if workflow_id and include_stored:
            runtime_deps.extend(_list_runtime_dependencies(self.db, workflow_id))
        for dep in runtime_deps:
            if not isinstance(dep, dict):
                continue
            dep_kind = _text(dep.get("dependency_kind") or dep.get("type"))
            dep_ref = _text(dep.get("dependency_ref") or dep.get("ref"))
            source = _text(dep.get("source") or "workflow_runtime_dependencies")
            if _invalid_dependency_ref(dep_ref):
                continue
            if dep_kind == "child_workflow":
                _add_dependency(dependencies, "child_workflow", dep_ref, source=source)
            elif dep_kind == "published_block":
                block_key = _text(dep.get("block_key") or dep_ref)
                _add_dependency(dependencies, "published_block", block_key, source=source)
                projected = parse_projected_identity(block_key)
                if projected.get("install_id"):
                    _add_dependency(
                        dependencies, "extension", projected["install_id"], source=source
                    )
            elif dep_kind in {"extension_install", "source_install"}:
                install_id = _text(dep.get("install_id") or dep_ref)
                _add_dependency(dependencies, "extension", install_id, source=source)
            elif dep_kind == "project_connection":
                _add_dependency(dependencies, "project_connection", dep_ref, source=source)

        for agent_id in [
            row["ref"] for row in dependencies.values() if row["dependency_type"] == "agent"
        ]:
            for tool_id in _agent_tool_refs(self.db, agent_id):
                _add_dependency(
                    dependencies,
                    "tool",
                    tool_id,
                    source=f"agent_manifest.{agent_id}",
                )

        return sorted(
            dependencies.values(),
            key=lambda row: (row["dependency_type"], row["ref"]),
        )

    def export_dependency_section(
        self, workflow: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        section_map = {
            "agent": "agents",
            "skill": "skills",
            "tool": "tools",
            "child_workflow": "child_workflows",
            "published_block": "published_blocks",
            "extension": "extensions",
            "project_connection": "project_connections",
        }
        dependencies = {section: [] for section in section_map.values()}
        for item in self.discover_dependencies(workflow):
            section_name = section_map.get(item["dependency_type"])
            if not section_name:
                continue
            dependencies[section_name].append(self._dependency_snapshot(item))
        return dependencies

    def import_dependency_options(
        self,
        *,
        project_id: Any | None = None,
        account_id: Any | None = None,
        project_connection_rows: list[dict[str, Any]] | None = None,
        extension_install_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return typed registry candidates for the import wizard resolution step.

        When ``account_id`` is supplied, registry candidates are confined to that
        account (plus platform-global rows) so the wizard cannot surface another
        account's agents/tools/skills/workflows/blocks/extensions.
        """
        scoped_account_id = str(account_id or "").strip() or None

        def _row_in_account_scope(row: Any) -> bool:
            if not scoped_account_id:
                return True
            row_account = str((row or {}).get("account_id") or "").strip()
            return not row_account or row_account == scoped_account_id

        def _scoped_summary(summary_name: str, fallback_name: str, **fallback_kwargs: Any):
            rows = _call_db_list(
                self.db, summary_name, enabled_only=False, account_id=scoped_account_id
            )
            # Only fall back to the unscoped list when NO account scope is enforced;
            # otherwise an account with zero rows would leak the global list.
            if not rows and scoped_account_id is None:
                rows = _call_db_list(self.db, fallback_name, enabled_only=False, **fallback_kwargs)
            # Defense-in-depth: _call_db_list retries without kwargs on TypeError, which
            # could silently drop the account_id filter on an adapter that lacks the
            # kwarg. Always post-filter by account so scoping can never be lost.
            return [row for row in rows if _row_in_account_scope(row)]

        agent_rows = _scoped_summary("list_agents_summary", "list_agents")
        skill_rows = _scoped_summary("list_skills_summary", "list_skills")
        tool_rows = _scoped_summary("list_tools_summary", "list_tools")
        workflow_rows = _scoped_summary(
            "list_workflows_summary", "list_workflows", include_definitions=False
        )
        block_rows = [
            row
            for row in _call_db_list(
                self.db,
                "list_published_block_contracts",
                enabled_only=False,
                ensure_projection=False,
            )
            if _row_in_account_scope(row)
        ]
        # The admin routes hand in install rows from the store that owns installs, the
        # way they hand in project connections: the registry store passed as `db` has
        # no `list_extensions`, so listing from it alone always offered no extension.
        if extension_install_rows is not None:
            extension_rows = [dict(row) for row in extension_install_rows if isinstance(row, dict)]
        else:
            extension_rows = [
                row
                for row in _call_db_list(self.db, "list_extensions", enabled_only=False)
                if _row_in_account_scope(row)
            ]
        connection_rows = [
            dict(row) for row in (project_connection_rows or []) if isinstance(row, dict)
        ]
        normalized_project_id = str(project_id or "").strip()
        if project_connection_rows is None and normalized_project_id:
            connection_rows = _call_db_list(
                self.project_connection_store,
                "list_project_connections",
                normalized_project_id,
                enabled_only=False,
            )

        options = {
            "agent": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=("agent_id", "id"),
                            description_keys=("skill_names", "model"),
                        )
                        for row in agent_rows
                    )
                    if option is not None
                ]
            ),
            "skill": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=("skill_id", "id"),
                            description_keys=("description_preview", "description", "category"),
                        )
                        for row in skill_rows
                    )
                    if option is not None
                ]
            ),
            "tool": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=("tool_id", "id"),
                            description_keys=("description_preview", "description", "category"),
                        )
                        for row in tool_rows
                    )
                    if option is not None
                ]
            ),
            "child_workflow": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=("workflow_id", "id"),
                            description_keys=("description_preview", "description", "version"),
                        )
                        for row in workflow_rows
                    )
                    if option is not None
                ]
            ),
            "published_block": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=("block_key", "key", "id"),
                            label_keys=("display_name", "title", "block_key"),
                            description_keys=("description_preview", "description", "block_type"),
                        )
                        for row in block_rows
                    )
                    if option is not None
                ]
            ),
            "extension": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=(
                                "install_id",
                                "source_install_id",
                                "extension_install_id",
                                "id",
                                "extension_id",
                            ),
                            label_keys=("display_name", "name", "extension_id", "install_id"),
                            description_keys=("description_preview", "description", "kind"),
                        )
                        for row in extension_rows
                    )
                    if option is not None
                ]
            ),
            "project_connection": _sorted_options(
                [
                    option
                    for option in (
                        _dependency_option(
                            row,
                            value_keys=(
                                "connection_id",
                                "project_connection_id",
                                "connection_key",
                                "connection_ref",
                                "id",
                            ),
                            label_keys=("display_name", "name", "connection_id"),
                            description_keys=(
                                "description_preview",
                                "description",
                                "connection_kind",
                            ),
                        )
                        for row in connection_rows
                    )
                    if option is not None
                ]
            ),
        }
        return {
            "kind": "workflow_import_dependency_options",
            "options": options,
        }


def _merge_dependency_sections(
    base: dict[str, list[dict[str, Any]]], extra: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Both sections' rows, each distinct row once, in first-seen order."""
    merged: dict[str, list[dict[str, Any]]] = {key: list(rows) for key, rows in base.items()}
    for section, rows in extra.items():
        target = merged.setdefault(section, [])
        seen = {json.dumps(row, sort_keys=True, default=str) for row in target}
        for row in rows:
            key = json.dumps(row, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                target.append(row)
    return merged
