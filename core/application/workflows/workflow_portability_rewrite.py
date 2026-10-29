"""Workflow portability service mixin."""

from __future__ import annotations

from typing import Any

from core.application.workflows.extension_connection_validation import (
    named_extension_without_install,
)
from core.application.workflows.workflow_portability_common import (
    _WORKFLOW_SAVE_FIELDS,
    _as_dict,
    _as_list,
    _json_safe,
    _raise_if_raw_mcp_connector_bindings,
    _redact_sensitive,
    _resolution_target_ref,
    _sanitize_imported_trigger,
    _text,
)


class WorkflowPortabilityRewriteMixin:
    def _target_workflow_id(
        self,
        source_workflow_id: str,
        *,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        explicit_target: str = "",
    ) -> str:
        if _text(explicit_target):
            return _text(explicit_target)
        resolution = resolution_index.get(("workflow", source_workflow_id))
        if resolution and resolution.get("action") == "rename_workflow":
            return (
                _text(
                    resolution.get("target_workflow_id")
                    or resolution.get("target_ref")
                    or resolution.get("target_id")
                )
                or source_workflow_id
            )
        return source_workflow_id

    def _import_payload(
        self,
        workflow: dict[str, Any],
        target_workflow_id: str,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
        *,
        owner_account_id: str = "",
        owner_project_id: str = "",
    ) -> dict[str, Any]:
        payload = {
            key: _redact_sensitive(_json_safe(workflow.get(key)))
            for key in _WORKFLOW_SAVE_FIELDS
            if key in workflow
        }
        payload["workflow_id"] = target_workflow_id
        payload["enabled"] = False
        # Sanitize the imported trigger through the same policy authoring uses, so
        # forbidden routing/classification keys cannot bypass create/update
        # validation by entering through import (FS-DEEP-CAND-014).
        payload["trigger"] = _sanitize_imported_trigger(payload.get("trigger"))
        # Bind the persisted row to the caller's account/project rather than
        # letting save_workflow default account_id to NULL and scope_kind to
        # "system" (FS-DEEP-CAND-013). An import must never mint an unowned,
        # globally-visible workflow on behalf of an account-scoped caller.
        clean_account_id = _text(owner_account_id)
        clean_project_id = _text(owner_project_id)
        if clean_account_id:
            payload["account_id"] = clean_account_id
            payload["scope_kind"] = "account"
        if clean_project_id:
            payload["project_id"] = clean_project_id
        payload["source_install_id"] = None
        payload["source_release_id"] = None
        payload["source_projection_id"] = None
        payload["local_workflow_id"] = target_workflow_id
        payload["projection_mode"] = "manual"
        payload["availability_status"] = "available"
        payload["availability_reason_code"] = "none"
        payload["availability_reason_text"] = ""
        payload["new_runs_blocked"] = False
        payload["visible_in_catalog"] = True
        settings = _as_dict(payload.get("settings"))
        portability = _as_dict(settings.get("portability"))
        portability["import_state"] = "draft"
        settings["portability"] = portability
        payload["settings"] = settings
        payload["participants"] = self._rewrite_participants(
            _as_list(payload.get("participants")),
            resolution_index,
        )
        # The stored copy is the one in settings; a mapped or renamed agent must
        # reach it too, or the participant keeps naming the source agent.
        if isinstance(payload["settings"].get("participants"), list):
            payload["settings"]["participants"] = self._rewrite_participants(
                payload["settings"]["participants"], resolution_index
            )
        payload["steps"] = [
            self._rewrite_step(step, resolution_index)
            for step in _as_list(payload.get("steps"))
            if isinstance(step, dict)
        ]
        payload["groups"] = [
            dict(group) for group in _as_list(payload.get("groups")) if isinstance(group, dict)
        ]
        payload["for_each"] = [
            dict(item) for item in _as_list(payload.get("for_each")) if isinstance(item, dict)
        ]
        _raise_if_raw_mcp_connector_bindings(payload)
        return payload

    def _rewrite_participants(
        self,
        participants: list[Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rewritten: list[dict[str, Any]] = []
        for item in participants:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            agent_id = _text(row.get("agent_id") or row.get("agent"))
            mapped = self._mapped_ref("agent", agent_id, resolution_index)
            if mapped:
                row["agent_id"] = mapped
            rewritten.append(row)
        return rewritten

    def _rewrite_step(
        self,
        step: dict[str, Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        row = dict(step)
        for field, dependency_type in (
            ("agent_id", "agent"),
            ("connector_id", "tool"),
            ("agent", "agent"),
            ("skill_id", "skill"),
            ("skill", "skill"),
            ("workflow_id", "child_workflow"),
            ("child_workflow_id", "child_workflow"),
            ("block_key", "published_block"),
            ("connector_install_id", "extension"),
        ):
            current = _text(row.get(field))
            mapped = self._mapped_ref(dependency_type, current, resolution_index)
            if mapped:
                row[field] = mapped
        named_extension = named_extension_without_install(row)
        if named_extension is not None:
            # Discovery reported this step's extension by package id; the install it
            # was mapped to becomes the step's own, as the builder would have saved it.
            mapped_install = self._mapped_ref(
                "extension", named_extension.extension_id, resolution_index
            )
            if mapped_install:
                row["connector_install_id"] = mapped_install

        override = _as_dict(row.get("override"))
        if override:
            invoke = _as_dict(override.get("invoke_workflow"))
            workflow_id = _text(invoke.get("workflow_id"))
            mapped_workflow = self._mapped_ref("child_workflow", workflow_id, resolution_index)
            if mapped_workflow:
                invoke["workflow_id"] = mapped_workflow
                override["invoke_workflow"] = invoke

            connector_install_id = _text(override.get("connector_install_id"))
            mapped_install = self._mapped_ref("extension", connector_install_id, resolution_index)
            if mapped_install:
                override["connector_install_id"] = mapped_install

            override["add_tools"] = self._rewrite_tool_list(
                _as_list(override.get("add_tools")),
                resolution_index,
            )
            row["override"] = {
                key: value for key, value in override.items() if value not in (None, "", [])
            }

        binding = (
            row.get("connection_binding_jsonb")
            or row.get("connection_binding")
            or (override.get("connection_binding") if override else {})
        )
        rewritten_binding = self._rewrite_connection_binding(binding, resolution_index)
        if rewritten_binding:
            row["connection_binding_jsonb"] = rewritten_binding
        # The builder also keeps the binding inside the override. Runtime reads the
        # top-level copy first, but a stale override copy would travel in the next
        # export as the author's connection id.
        override = _as_dict(row.get("override"))
        if override:
            if override.get("connection_binding"):
                override["connection_binding"] = self._rewrite_connection_binding(
                    override["connection_binding"], resolution_index
                )
            connector = _as_dict(override.get("connector"))
            if connector.get("connection_binding"):
                connector["connection_binding"] = self._rewrite_connection_binding(
                    connector["connection_binding"], resolution_index
                )
                override["connector"] = connector
            row["override"] = override
        return row

    def _rewrite_tool_list(
        self,
        tools: list[Any],
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> list[Any]:
        rewritten: list[Any] = []
        for item in tools:
            if isinstance(item, dict):
                row = dict(item)
                tool_id = _text(row.get("tool_id") or row.get("id") or row.get("name"))
                mapped = self._mapped_ref("tool", tool_id, resolution_index)
                if mapped:
                    row["tool_id"] = mapped
                rewritten.append(row)
            else:
                mapped = self._mapped_ref("tool", _text(item), resolution_index)
                rewritten.append(mapped or item)
        return rewritten

    def _rewrite_connection_binding(
        self,
        value: Any,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> Any:
        if isinstance(value, dict):
            row: dict[str, Any] = {}
            for key, item in value.items():
                if key in {
                    "connection_id",
                    "project_connection_id",
                    "connection_key",
                    "binding_key",
                    "connection_ref",
                }:
                    mapped = self._mapped_ref("project_connection", _text(item), resolution_index)
                    row[key] = mapped or item
                else:
                    row[key] = self._rewrite_connection_binding(item, resolution_index)
            return row
        if isinstance(value, list):
            return [self._rewrite_connection_binding(item, resolution_index) for item in value]
        mapped = self._mapped_ref("project_connection", _text(value), resolution_index)
        return mapped or value

    def _mapped_ref(
        self,
        dependency_type: str,
        ref: str,
        resolution_index: dict[tuple[str, str], dict[str, Any]],
    ) -> str:
        normalized_ref = _text(ref)
        if not normalized_ref:
            return ""
        resolution = resolution_index.get((dependency_type, normalized_ref))
        if not resolution:
            return ""
        action = _text(resolution.get("action"))
        if action not in {
            "use_existing",
            "map_existing",
            "rename",
            "create_connection_placeholder",
            "install_extension_required",
        }:
            return ""
        return _resolution_target_ref(resolution) or normalized_ref
