# mypy: ignore-errors
"""Workflow portability service mixin."""

from __future__ import annotations

from typing import Any

from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING
from core.application.workflows.workflow_portability_common import (
    _WORKFLOW_SAVE_FIELDS,
    _as_dict,
    _as_list,
    _invalid_dependency_ref,
    _missing_dependency_label,
    _normalize_participants,
    _redact_sensitive,
    _step_child_workflow_ref,
    _text,
    _workflow_for_each,
    _workflow_groups,
    _workflow_settings,
    _workflow_steps,
)


def _portable_extension_identity(row: dict[str, Any] | None, *, install_id: str) -> dict[str, Any]:
    """Describe the extension itself, keeping the install id only as a hint.

    AC17-R60: an `install_id` is unique to one installation on one machine — the supervisor
    searched a blocking one across five local installs and found it on none of them. The
    portable identity was already sitting in the same row the exporter can read, so this
    writes `extension_id` and the version and leaves `install_id` behind as a local hint,
    rather than introducing an identity scheme.
    """
    identity: dict[str, Any] = {}
    source = _as_dict(row)
    extension_id = _text(source.get("extension_id") or source.get("release_extension_id"))
    if extension_id:
        identity["extension_id"] = extension_id
    # The operator lookup returns the install's `manifest`; `descriptor_jsonb` is not
    # always there, and reading only it left the name as the extension id.
    descriptor = _as_dict(source.get("descriptor_jsonb")) or _as_dict(source.get("manifest"))
    version = _text(descriptor.get("version") or source.get("version"))
    if version:
        identity["version"] = version
    label = _text(descriptor.get("display_name") or descriptor.get("name") or extension_id)
    # Never leave the human-readable field holding the same opaque value as the identifier.
    if label and label != install_id:
        identity["display_name"] = label
    return identity


def _portable_extension_hint(item: dict[str, Any]) -> dict[str, Any]:
    """The `extension_id` and version an exported extension dependency carries."""
    hint: dict[str, Any] = {}
    extension_id = _text(item.get("extension_id"))
    if extension_id:
        hint["extension_id"] = extension_id
    version = _text(item.get("version"))
    if version:
        hint["version"] = version
    return hint


class WorkflowPortabilityDefinitionsMixin:
    def _workflow_definition(self, workflow: dict[str, Any]) -> dict[str, Any]:
        settings = _workflow_settings(workflow)
        # The graph is service-owned: it travels as its own definition field and
        # comes back in through the change service, never as a settings key.
        graph = settings.get(EXECUTION_GRAPH_SETTING)
        settings = _redact_sensitive(
            {key: value for key, value in settings.items() if key != EXECUTION_GRAPH_SETTING}
        )
        settings.pop("canvas_annotations_revision", None)
        # Files remain in this installation's artifact plane; a portable note
        # carries its text and layout, never an unusable local artifact authority.
        if isinstance(settings.get("canvas_annotations"), list):
            settings["canvas_annotations"] = [
                {key: value for key, value in note.items() if key != "image"}
                for note in settings["canvas_annotations"]
                if isinstance(note, dict)
            ]
        definition = {
            "workflow_id": _text(workflow.get("workflow_id")),
            "version": _text(workflow.get("version") or "1.0.0"),
            "display_name": _text(workflow.get("display_name") or workflow.get("workflow_id")),
            "description": _text(workflow.get("description")),
            "trigger": _redact_sensitive(_as_dict(workflow.get("trigger"))),
            "settings": settings,
            "participants": _normalize_participants(workflow),
            "tags": [str(item) for item in _as_list(workflow.get("tags"))],
            "steps": [self._step_definition(step) for step in _workflow_steps(workflow)],
            "groups": [self._group_definition(group) for group in _workflow_groups(workflow)],
            "for_each": [self._for_each_definition(item) for item in _workflow_for_each(workflow)],
            "execution_graph": _as_dict(graph) or None,
        }
        return {key: definition[key] for key in _WORKFLOW_SAVE_FIELDS}

    def _group_definition(self, group: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "group_id": _text(group.get("group_id")),
            "display_name": _text(group.get("display_name") or group.get("group_id")),
            "description": _text(group.get("description")),
            "body": _as_dict(group.get("body")),
            "error_policy": _as_dict(group.get("error_policy")) or None,
            "boundary_contract": _redact_sensitive(_as_dict(group.get("boundary_contract")))
            or None,
        }
        return {key: value for key, value in payload.items() if value not in (None, "")}

    def _for_each_definition(self, item: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "for_each_id": _text(item.get("for_each_id")),
            "group_id": _text(item.get("group_id")) or None,
            "display_name": _text(item.get("display_name") or item.get("for_each_id")),
            "description": _text(item.get("description")),
            "over": _text(item.get("over")),
            "item_name": _text(item.get("item_name")),
            "index_name": _text(item.get("index_name")) or None,
            "body": _as_dict(item.get("body")),
            "error_policy": _as_dict(item.get("error_policy")) or None,
            "boundary_contract": _redact_sensitive(_as_dict(item.get("boundary_contract"))) or None,
        }
        # group_id and index_name are kept as explicit nulls: null is meaningful.
        return {
            key: value
            for key, value in payload.items()
            if key in {"group_id", "index_name"} or value not in (None, "")
        }

    def _step_definition(self, step: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "step_id",
            "step_kind",
            "group_id",
            "for_each_id",
            "lane_id",
            "block_key",
            "display_name",
            "participant_key",
            "agent_id",
            "skill_id",
            "connector_id",
            "connector_install_id",
            "workflow_id",
            "input_mapping_jsonb",
            "output_mapping_jsonb",
            "connection_binding_jsonb",
            "error_policy",
            "boundary_contract",
            "optional",
            "disabled",
            "context_inject",
            "override",
        )
        payload: dict[str, Any] = {}
        for key in keys:
            value = step.get(key)
            if value in (None, "", [], {}):
                continue
            if key == "disabled" and value is not True:
                # Only a disabled step says so; an enabled one is the default.
                continue
            if key in {"connector_id", "connector_install_id"} and _invalid_dependency_ref(value):
                continue
            if key == "workflow_id" and _invalid_dependency_ref(value):
                child_workflow_ref = _step_child_workflow_ref(step)
                if not child_workflow_ref or not _invalid_dependency_ref(child_workflow_ref):
                    continue
            payload[key] = _redact_sensitive(value, key=key)
        if "step_kind" not in payload:
            payload["step_kind"] = "agent"
        return payload

    def _extension_workflow_ref(self, workflow: dict[str, Any]) -> dict[str, Any]:
        ref = {
            "portability_kind": "extension_workflow_ref",
            "source_install_id": _text(workflow.get("source_install_id")),
            "source_release_id": _text(workflow.get("source_release_id")),
            "local_workflow_id": _text(workflow.get("local_workflow_id")),
            "workflow_id": _text(workflow.get("workflow_id")),
            "runtime_mode": _text(workflow.get("runtime_mode")),
            "requires_extension": True,
        }
        return {key: value for key, value in ref.items() if value not in ("", None)}

    def _reference_dependencies(self, workflow: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        dependencies = {
            section: []
            for section in (
                "agents",
                "skills",
                "tools",
                "child_workflows",
                "published_blocks",
                "extensions",
                "project_connections",
            )
        }
        source_install_id = _text(workflow.get("source_install_id"))
        if source_install_id:
            # The portable extension identity beside the local install id, so an
            # import elsewhere can find the install (and a hook binding its owner).
            row = self._existing_row_for_dependency("extension", source_install_id)
            dependencies["extensions"].append(
                {
                    "install_id": source_install_id,
                    "requires_extension": True,
                    "source": "workflow.source_install_id",
                    **_portable_extension_identity(row, install_id=source_install_id),
                }
            )
        return dependencies

    def _dependency_snapshot(self, item: dict[str, Any]) -> dict[str, Any]:
        dependency_type = item["dependency_type"]
        ref = item["ref"]
        key_by_type = {
            "agent": "agent_id",
            "skill": "skill_id",
            "tool": "tool_id",
            "child_workflow": "workflow_id",
            "published_block": "block_key",
            "extension": "install_id",
            "project_connection": "connection_ref",
        }
        payload: dict[str, Any] = {
            key_by_type.get(dependency_type, "ref"): ref,
            "display_name": item.get("label") or ref,
            "required": bool(item.get("required", True)),
            "sources": list(item.get("sources") or []),
        }
        metadata = _as_dict(item.get("metadata"))
        if metadata:
            payload["metadata"] = _redact_sensitive(metadata)
        row = self._existing_row_for_dependency(dependency_type, ref)
        if row:
            for key in ("display_name", "description", "version", "enabled", "status"):
                if key in row and row[key] not in (None, "", [], {}):
                    payload[key] = _redact_sensitive(row[key], key=key)
        if dependency_type == "extension":
            payload.update(_portable_extension_identity(row, install_id=ref))
        return payload

    def _bundle_dependencies(self, bundle: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for workflow in _as_list(bundle.get("workflows")):
            if (
                isinstance(workflow, dict)
                and _text(workflow.get("portability_kind")) != "extension_workflow_ref"
            ):
                rows.extend(self.discover_dependencies(workflow))

        raw_dependencies = bundle.get("dependencies")
        if isinstance(raw_dependencies, list):
            for item in raw_dependencies:
                if isinstance(item, dict):
                    rows.append(self._dependency_from_bundle_item(item, "dependencies"))
            return rows
        if not isinstance(raw_dependencies, dict):
            return rows
        section_map = {
            "agents": ("agent", "agent_id"),
            "skills": ("skill", "skill_id"),
            "tools": ("tool", "tool_id"),
            "child_workflows": ("child_workflow", "workflow_id"),
            "published_blocks": ("published_block", "block_key"),
            "extensions": ("extension", "install_id"),
            "project_connections": ("project_connection", "connection_ref"),
        }
        for section, (dependency_type, ref_key) in section_map.items():
            for item in _as_list(raw_dependencies.get(section)):
                if not isinstance(item, dict):
                    continue
                ref = _text(
                    item.get(ref_key)
                    or item.get("ref")
                    or item.get("id")
                    or item.get("dependency_ref")
                    or item.get("connection_id")
                )
                if not ref:
                    continue
                metadata = _as_dict(item.get("metadata"))
                if dependency_type == "extension":
                    # AC17-R60 exports the portable identity beside the install id;
                    # keep it, or an import elsewhere is left with an unknown UUID.
                    metadata.update(_portable_extension_hint(item))
                label = _text(item.get("display_name") or ref)
                if _invalid_dependency_ref(ref):
                    metadata.setdefault("code", "invalid_dependency_ref")
                    metadata.setdefault("raw_ref", ref)
                    label = _missing_dependency_label(dependency_type)
                rows.append(
                    {
                        "dependency_type": dependency_type,
                        "ref": ref,
                        "label": label,
                        "sources": _as_list(item.get("sources")) or [f"dependencies.{section}"],
                        "required": bool(item.get("required", True)),
                        "metadata": _redact_sensitive(metadata),
                    }
                )
        return rows

    def _dependency_from_bundle_item(
        self,
        item: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        dependency_type = _text(item.get("dependency_type") or item.get("type"))
        ref = _text(item.get("ref") or item.get("dependency_ref") or item.get("id"))
        metadata = _as_dict(item.get("metadata"))
        label = _text(item.get("label") or item.get("display_name") or ref)
        if _invalid_dependency_ref(ref):
            metadata.setdefault("code", "invalid_dependency_ref")
            metadata.setdefault("raw_ref", ref)
            label = _missing_dependency_label(dependency_type)
        return {
            "dependency_type": dependency_type,
            "ref": ref,
            "label": label,
            "sources": _as_list(item.get("sources")) or [source],
            "required": bool(item.get("required", True)),
            "metadata": _redact_sensitive(metadata),
        }

    def _dedupe_dependency_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            dependency_type = _text(row.get("dependency_type"))
            ref = _text(row.get("ref"))
            code = _text(_as_dict(row.get("metadata")).get("code"))
            if not dependency_type or not ref:
                continue
            key = (dependency_type, ref, code)
            current = deduped.setdefault(
                key,
                {
                    "dependency_type": dependency_type,
                    "ref": ref,
                    "label": _text(row.get("label") or ref),
                    "sources": [],
                    "required": bool(row.get("required", True)),
                    "metadata": {},
                },
            )
            for source in _as_list(row.get("sources")):
                if _text(source) and source not in current["sources"]:
                    current["sources"].append(source)
            current["required"] = bool(current.get("required", True) and row.get("required", True))
            current["metadata"].update(_as_dict(row.get("metadata")))
        return list(deduped.values())
