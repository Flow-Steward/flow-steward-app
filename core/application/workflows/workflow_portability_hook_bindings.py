"""A project's hook bindings travel with the workflows they configure (spec §9.2, §13, §14).

A binding belongs to one project, so only a project-scoped export writes them:
for each workflow the bundle carries, the active binding of each of its hooks in
that project — hook id, owner, the contract version and semantic digest the
binding was saved against, its target and both mappings. Account and project
ids never travel, nor does anything the binding itself does not hold. The bound
child is an ordinary dependency: listed in ``dependencies.child_workflows`` and,
with ``include_children``, carried like an invoked child under its own export
policy (an extension's workflow stays a reference).

An extension owner is named by the bundle's reference entry: its install (the
extension dependency carries the portable ``extension_id``) and
``local_workflow_id``. With the binding's ``hook_id`` and contract version that
is the identity a binding moves by; a release UUID never is.

Import re-applies each binding in the import project through the binding
service's own save, inside the import transaction and after the workflows are
written, with owner and target pointed at wherever this import placed them.
What can be checked from the file is checked in preflight, before anything is
written; a binding the save still refuses rolls the whole import back. A
configured hook never arrives empty because its workflow id changed.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from core.application.workflows.workflow_catalog_identity import (
    parse_projected_identity,
    projected_workflow_id,
)
from core.application.workflows.workflow_portability_common import (
    HOOK_BINDINGS_KEY,
    WorkflowPortabilityError,
    _add_dependency,
    _as_dict,
    _as_list,
    _call_db_get,
    _dependency_key,
    _text,
)

#: The fields of one exported binding; any other field is refused, not ignored.
BINDING_FIELDS = (
    "hook_id",
    "owner_workflow_id",
    "hook_contract_version",
    "hook_semantic_digest",
    "target_workflow_id",
    "input_mapping",
    "output_mapping",
)
_EXTENSION_REF = "extension_workflow_ref"
DEPENDENCY_TYPE = "hook_binding"


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _entry_issues(entry: Any, path: str) -> list[dict[str, str]]:
    """Why one ``hook_bindings`` entry is not a binding this format can carry."""
    code = "workflow.hook.binding_entry_invalid"
    if not isinstance(entry, dict):
        return [_issue(code, "A hook binding must be an object", path)]
    issues: list[dict[str, str]] = []
    unknown = sorted(str(key) for key in set(entry) - set(BINDING_FIELDS))
    missing = [key for key in BINDING_FIELDS if key not in entry]
    if unknown:
        issues.append(_issue(code, f"Unknown hook binding fields: {', '.join(unknown)}", path))
    if missing:
        issues.append(_issue(code, f"Missing hook binding fields: {', '.join(missing)}", path))
    for key in ("hook_id", "owner_workflow_id", "target_workflow_id", "hook_semantic_digest"):
        if key in entry and not (isinstance(entry[key], str) and entry[key].strip()):
            issues.append(_issue(code, f"'{key}' must be a non-empty string", f"{path}.{key}"))
    version = entry.get("hook_contract_version")
    if "hook_contract_version" in entry and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        issues.append(
            _issue(
                code,
                "'hook_contract_version' must be a positive integer",
                f"{path}.hook_contract_version",
            )
        )
    for key in ("input_mapping", "output_mapping"):
        mapping = entry.get(key)
        if key in entry and not (
            isinstance(mapping, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
        ):
            issues.append(_issue(code, f"'{key}' must map names to names", f"{path}.{key}"))
    return issues


def bound_child_ids(bundle: dict[str, Any], carried_ids: Collection[str]) -> set[str]:
    """Carried workflows a bundled binding targets: children of the import, like invoked ones."""
    return {
        _text(entry.get("target_workflow_id"))
        for entry in _as_list(bundle.get(HOOK_BINDINGS_KEY))
        if isinstance(entry, dict) and _text(entry.get("target_workflow_id")) in carried_ids
    }


class HookBindingCollector:
    """The bindings of each exported workflow, read once, and the children they name."""

    def __init__(self, read: Callable[[str], list[dict[str, Any]]]) -> None:
        self._read = read
        self._seen: set[str] = set()
        self.entries: list[dict[str, Any]] = []

    def targets(self, workflow: dict[str, Any]) -> list[str]:
        workflow_id = _text(workflow.get("workflow_id"))
        if workflow_id and workflow_id not in self._seen:
            self._seen.add(workflow_id)
            self.entries.extend(self._read(workflow_id))
        return [
            entry["target_workflow_id"]
            for entry in self.entries
            if entry["owner_workflow_id"] == workflow_id
        ]


def _refused(
    row: dict[str, Any], owner_id: str, target_id: str, issues: list
) -> WorkflowPortabilityError:
    return WorkflowPortabilityError(
        f"Hook '{row['hook_id']}' of workflow '{owner_id}' could not be connected again in "
        "the import project; nothing was imported.",
        code="workflow_import_hook_binding_invalid",
        status_code=409,
        errors=[
            {
                "hook_id": row["hook_id"],
                "owner_workflow_id": owner_id,
                "target_workflow_id": target_id,
                "issues": list(issues),
            }
        ],
    )


class WorkflowPortabilityHookBindingsMixin:
    # Provided by WorkflowPortabilityService and its other mixins.
    db: Any
    account_id: str
    _mapped_ref: Callable[..., str]
    _target_workflow_id: Callable[..., str]
    _dependency_snapshot: Callable[[dict[str, Any]], dict[str, Any]]

    # -- export --------------------------------------------------------------

    def _hook_binding_service(self, *, account_id: str = "", actor: str = "") -> Any:
        from core.application.workflows.workflow_hook_binding_service import (
            WorkflowHookBindingService,
        )

        return WorkflowHookBindingService(
            self.db, account_id=account_id or self.account_id, actor=actor
        )

    def _hook_binding_collector(self, project_id: str) -> HookBindingCollector | None:
        """Without a project there is no binding to export (the admin YAML route)."""
        project_id = _text(project_id)
        if not project_id:
            return None
        return HookBindingCollector(
            lambda workflow_id: self._project_hook_bindings(workflow_id, project_id)
        )

    def _project_hook_bindings(self, workflow_id: str, project_id: str) -> list[dict[str, Any]]:
        """The active binding of each hook *workflow_id* declares, in *project_id*."""
        from core.application.workflows.workflow_hook_binding_service import HookBindingError

        try:
            described = self._hook_binding_service().describe(workflow_id, project_id)
        except HookBindingError as exc:
            raise WorkflowPortabilityError(
                str(exc), code="workflow_export_hook_bindings_unavailable", errors=exc.issues
            ) from exc
        entries: list[dict[str, Any]] = []
        for row in described:
            binding = row.get("binding")
            if not binding:
                continue
            entries.append(
                {
                    "hook_id": _text(row.get("hook_id")),
                    "owner_workflow_id": workflow_id,
                    # What the binding was saved against, so a stale one is refused
                    # on import instead of being re-validated against a new contract.
                    "hook_contract_version": int(binding["hook_contract_version"]),
                    "hook_semantic_digest": _text(binding["hook_semantic_digest"]),
                    "target_workflow_id": _text(binding["target_workflow_id"]),
                    "input_mapping": dict(binding.get("input_mapping") or {}),
                    "output_mapping": dict(binding.get("output_mapping") or {}),
                }
            )
        return entries

    def _hook_binding_dependencies(
        self, entries: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Each bound child as a child workflow dependency (and its extension, if any)."""
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in entries:
            target = entry["target_workflow_id"]
            source = f"hook_bindings.{entry['owner_workflow_id']}.{entry['hook_id']}"
            _add_dependency(rows, "child_workflow", target, source=source)
            install_id = parse_projected_identity(target).get("install_id")
            if install_id:
                _add_dependency(
                    rows,
                    "extension",
                    install_id,
                    source=f"{source}.target_workflow_id",
                    metadata={"dependency_kind": "child_workflow"},
                )
        sections: dict[str, list[dict[str, Any]]] = {"child_workflows": [], "extensions": []}
        section_of = {"child_workflow": "child_workflows", "extension": "extensions"}
        for item in sorted(rows.values(), key=lambda row: (row["dependency_type"], row["ref"])):
            sections[section_of[item["dependency_type"]]].append(self._dependency_snapshot(item))
        return sections

    # -- import preflight ----------------------------------------------------

    def _binding_placement(
        self,
        source_id: str,
        entry: dict[str, Any] | None,
        *,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        child_ids: set[str],
        root_id: str,
        explicit_target: str,
    ) -> tuple[str, dict[str, Any] | None, bool]:
        """``(id here, definition to check against, created by this import)``."""
        if entry is None:
            placed = self._mapped_ref("child_workflow", source_id, resolution_index) or source_id
            return placed, _call_db_get(self.db, "get_workflow", placed), False
        if _text(entry.get("portability_kind")) == _EXTENSION_REF:
            install_id = _text(entry.get("source_install_id"))
            local_id = _text(entry.get("local_workflow_id"))
            mapped = self._mapped_ref("extension", install_id, resolution_index) or install_id
            placed = projected_workflow_id(mapped, local_id) if mapped and local_id else source_id
            return placed, _call_db_get(self.db, "get_workflow", placed), False
        resolution = resolution_index.get(("workflow", source_id)) or {}
        if source_id in child_ids and _text(resolution.get("action")) == "use_existing":
            existing = (
                _text(
                    resolution.get("target_workflow_id")
                    or resolution.get("target_ref")
                    or resolution.get("target_id")
                )
                or source_id
            )
            return existing, _call_db_get(self.db, "get_workflow", existing), False
        placed = self._target_workflow_id(
            source_id,
            resolution_index=resolution_index,
            explicit_target=explicit_target if source_id == root_id else "",
        )
        return placed, entry, True

    def _hook_binding_plan(
        self,
        bundle: dict[str, Any],
        *,
        workflow_entries: list[dict[str, Any]],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        child_ids: set[str],
        root_id: str,
        explicit_target: str,
    ) -> list[dict[str, Any]]:
        """Where each bundled binding will be connected, or why it cannot be."""
        from core.application.workflows.workflow_hook_binding_service import (
            HookBindingError,
            find_hook,
            mapping_compatibility,
        )
        from core.application.workflows.workflow_hook_contract import hook_semantic_digest

        by_id = {
            _text(item.get("workflow_id")): item
            for item in workflow_entries
            if _text(item.get("workflow_id"))
        }

        def place(source_id: str) -> tuple[str, dict[str, Any] | None, bool]:
            return self._binding_placement(
                source_id,
                by_id.get(source_id),
                resolution_index=resolution_index,
                child_ids=child_ids,
                root_id=root_id,
                explicit_target=explicit_target,
            )

        plan: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, entry in enumerate(_as_list(bundle.get(HOOK_BINDINGS_KEY))):
            path = f"{HOOK_BINDINGS_KEY}[{index}]"
            issues = _entry_issues(entry, path)
            entry = entry if isinstance(entry, dict) else {}
            hook_id = _text(entry.get("hook_id"))
            owner_id = _text(entry.get("owner_workflow_id"))
            target_id = _text(entry.get("target_workflow_id"))
            row: dict[str, Any] = {
                "hook_id": hook_id,
                "owner_workflow_id": owner_id,
                "target_workflow_id": target_id,
                "imported_owner_workflow_id": "",
                "imported_target_workflow_id": "",
                "input_mapping": dict(_as_dict(entry.get("input_mapping"))),
                "output_mapping": dict(_as_dict(entry.get("output_mapping"))),
                "hook_contract_version": entry.get("hook_contract_version"),
                "hook_semantic_digest": _text(entry.get("hook_semantic_digest")),
                "path": path,
            }
            plan.append(row)
            if issues:
                row["issues"] = issues
                continue
            if owner_id not in by_id:
                issues.append(
                    _issue(
                        "workflow.hook.binding_owner_missing",
                        f"The binding names workflow '{owner_id}', which this file does not carry",
                        f"{path}.owner_workflow_id",
                    )
                )
            else:
                owner_here, owner, _created = place(owner_id)
                row["imported_owner_workflow_id"] = owner_here
                if (owner_here, hook_id) in seen:
                    issues.append(
                        _issue(
                            "workflow.hook.binding_duplicate",
                            f"Hook '{hook_id}' of '{owner_here}' is bound twice in this file",
                            path,
                        )
                    )
                seen.add((owner_here, hook_id))
                if owner is None:
                    issues.append(
                        _issue(
                            "workflow.hook.binding_owner_missing",
                            f"Workflow '{owner_here}', which declares hook '{hook_id}', "
                            "is not available here",
                            f"{path}.owner_workflow_id",
                        )
                    )
                else:
                    try:
                        _step, hook = find_hook(owner, hook_id)
                    except HookBindingError as exc:
                        issues.extend({**issue, "path": path} for issue in exc.issues)
                    else:
                        if (
                            hook.contract_version != row["hook_contract_version"]
                            or hook_semantic_digest(hook) != row["hook_semantic_digest"]
                        ):
                            issues.append(
                                _issue(
                                    "workflow.hook.contract_changed",
                                    f"Hook '{hook_id}' is at contract version "
                                    f"{hook.contract_version} here; the binding was saved "
                                    f"against version {row['hook_contract_version']} with "
                                    "another contract. Connect the hook again after import.",
                                    f"{path}.hook_contract_version",
                                )
                            )
            target_here, target, _created = place(target_id)
            row["imported_target_workflow_id"] = target_here
            if target is None:
                issues.append(
                    _issue(
                        "workflow.hook.binding_target_missing",
                        f"Workflow '{target_id}', which hook '{hook_id}' is connected to, is "
                        "neither in this file nor here. Export with children, or map it to an "
                        "existing workflow.",
                        f"{path}.target_workflow_id",
                    )
                )
            else:
                issues.extend(
                    _issue(
                        "workflow.hook.binding_mapping_incompatible",
                        f"Hook '{hook_id}' cannot be connected to '{target_here}': {reason}",
                        path,
                    )
                    for reason in mapping_compatibility(
                        row["input_mapping"], row["output_mapping"], target.get("settings")
                    )
                )
            row["issues"] = issues
        for row in plan:
            row["blocking"] = bool(row["issues"])
            row["status"] = "blocked" if row["blocking"] else "ready"
        return plan

    @staticmethod
    def _hook_binding_dependency_rows(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The plan as preflight dependency rows, so a blocker shows where every other does."""
        rows: list[dict[str, Any]] = []
        for row in plan:
            ref = f"{row['owner_workflow_id']}.{row['hook_id']}"
            rows.append(
                {
                    "dependency_key": _dependency_key(DEPENDENCY_TYPE, ref),
                    "dependency_type": DEPENDENCY_TYPE,
                    "ref": ref,
                    "label": row["hook_id"] or row["path"],
                    "status": row["status"],
                    "blocking": row["blocking"],
                    "required": True,
                    "sources": [row["path"]],
                    "resolution_actions": ["abort"] if row["blocking"] else [],
                    "resolved_by": None if row["blocking"] else {"action": "bundled"},
                    "message": "; ".join(issue["message"] for issue in row["issues"])
                    or "Connected again in the import project.",
                    "metadata": {
                        "issues": row["issues"],
                        "owner_workflow_id": row["imported_owner_workflow_id"],
                        "target_workflow_id": row["imported_target_workflow_id"],
                    },
                }
            )
        return rows

    # -- import apply --------------------------------------------------------

    def _apply_hook_bindings(
        self,
        plan: list[dict[str, Any]],
        *,
        placed_ids: dict[str, str],
        created_ids: set[str],
        account_id: str,
        project_id: str,
        author: str,
    ) -> list[dict[str, Any]]:
        """Connect each planned binding in *project_id*; the first refusal raises.

        Runs inside the import transaction, so a refusal also undoes the
        workflows this import wrote. A hook the project already connects to the
        same target with the same mappings is left as it is; one connected
        differently is never overwritten.
        """
        from core.application.workflows.workflow_hook_binding_repository import (
            HookBindingConflictError,
        )
        from core.application.workflows.workflow_hook_binding_service import (
            HookBindingError,
            HookBindingRequest,
        )

        service = self._hook_binding_service(account_id=account_id, actor=author)
        restored: list[dict[str, Any]] = []
        for row in plan:
            owner_id = placed_ids.get(row["owner_workflow_id"]) or row["imported_owner_workflow_id"]
            target_id = (
                placed_ids.get(row["target_workflow_id"]) or row["imported_target_workflow_id"]
            )
            try:
                current = next(
                    (
                        hook
                        for hook in service.describe(owner_id, project_id)
                        if hook.get("hook_id") == row["hook_id"]
                    ),
                    None,
                )
                if current is None:
                    raise HookBindingError(
                        [
                            _issue(
                                "workflow.hook.unknown",
                                f"Workflow '{owner_id}' declares no hook '{row['hook_id']}'",
                                row["path"],
                            )
                        ]
                    )
                existing = current.get("binding")
                if existing:
                    same = (
                        _text(existing.get("target_workflow_id")) == target_id
                        and dict(existing.get("input_mapping") or {}) == row["input_mapping"]
                        and dict(existing.get("output_mapping") or {}) == row["output_mapping"]
                    )
                    if not same:
                        raise HookBindingError(
                            [
                                _issue(
                                    "workflow.hook.binding_exists",
                                    f"Hook '{row['hook_id']}' of '{owner_id}' is already "
                                    f"connected to '{existing.get('target_workflow_id')}' in "
                                    "this project; disconnect it there, or import into "
                                    "another project",
                                    row["path"],
                                )
                            ]
                        )
                    restored.append(
                        {
                            "hook_id": row["hook_id"],
                            "owner_workflow_id": owner_id,
                            "target_workflow_id": target_id,
                            "binding_revision": int(current.get("binding_revision") or 0),
                            "status": "unchanged",
                        }
                    )
                    continue
                saved = service.save(
                    HookBindingRequest(
                        owner_workflow_id=owner_id,
                        project_id=project_id,
                        hook_id=row["hook_id"],
                        expected_revision=int(current.get("binding_revision") or 0),
                        target_workflow_id=target_id,
                        input_mapping=dict(row["input_mapping"]),
                        output_mapping=dict(row["output_mapping"]),
                        hook_contract_version=int(row["hook_contract_version"]),
                        hook_semantic_digest=row["hook_semantic_digest"],
                    ),
                    # A draft child is accepted only for an owner that is a draft of
                    # this same import; an existing owner never gets a disabled child.
                    draft_targets=created_ids if owner_id in created_ids else (),
                )
            except HookBindingError as exc:
                raise _refused(row, owner_id, target_id, exc.issues) from exc
            except HookBindingConflictError as exc:
                issue = _issue("workflow.hook.binding_revision_changed", str(exc), row["path"])
                raise _refused(row, owner_id, target_id, [issue]) from exc
            restored.append(
                {
                    "hook_id": row["hook_id"],
                    "owner_workflow_id": owner_id,
                    "target_workflow_id": target_id,
                    "binding_revision": int(saved.revision),
                    "status": "restored",
                }
            )
        return restored


__all__ = [
    "BINDING_FIELDS",
    "HookBindingCollector",
    "WorkflowPortabilityHookBindingsMixin",
    "bound_child_ids",
]
