"""Restore-payload normalisation for registry revisions.

A changelog snapshot is a stored row; restoring it goes through the ordinary
save path of its entity type. ``RegistryDBStoreRestorePayloadsMixin`` turns a
snapshot into the payload that save path accepts: business id set, derived
columns dropped, nested skill and tool lists flattened.
"""

from __future__ import annotations

from core.application.registry.catalog_categories import known_catalog_category


class RegistryDBStoreRestorePayloadsMixin:
    @staticmethod
    def _normalize_agent_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["agent_id"] = entity_id

        skills = payload.get("skills")
        if not isinstance(skills, list):
            skills = payload.get("_skills")
        normalized_caps: list[str] = []
        cap_prompts: dict[str, str] = {}
        for item in skills or []:
            if isinstance(item, dict):
                skill_id = str(item.get("skill_id") or "").strip()
                skill_prompt = str(item.get("skill_prompt") or "")
            else:
                skill_id = str(item or "").strip()
                skill_prompt = ""
            if skill_id:
                normalized_caps.append(skill_id)
                if skill_prompt:
                    cap_prompts[skill_id] = skill_prompt
        payload["skills"] = normalized_caps

        tools = payload.get("tools")
        if not isinstance(tools, list):
            tools = payload.get("_tools")
        normalized_tools: list[str] = []
        for item in tools or []:
            if isinstance(item, dict):
                tool_id = str(item.get("tool_id") or "").strip()
            else:
                tool_id = str(item or "").strip()
            if tool_id:
                normalized_tools.append(tool_id)
        payload["tools"] = normalized_tools

        prompts = payload.get("prompts")
        if not isinstance(prompts, dict):
            prompts = {}
        if cap_prompts:
            existing = (
                prompts.get("skill_prompts")
                if isinstance(prompts.get("skill_prompts"), dict)
                else {}
            )
            prompts["skill_prompts"] = {**existing, **cap_prompts}
        payload["prompts"] = prompts

        for key in (
            "id",
            "created_at",
            "updated_at",
            "_skills",
            "_tools",
            "_inputs",
            "_outputs",
        ):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_tool_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["tool_id"] = entity_id
        for key in ("id", "created_at", "updated_at"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_skill_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["skill_id"] = entity_id
        if payload.get("expected_inputs") is None and isinstance(payload.get("_inputs"), list):
            payload["expected_inputs"] = payload.get("_inputs") or []
        if payload.get("expected_outputs") is None and isinstance(payload.get("_outputs"), list):
            payload["expected_outputs"] = payload.get("_outputs") or []
        for key in ("id", "created_at", "updated_at", "_inputs", "_outputs", "_tools"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_workflow_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["workflow_id"] = entity_id
        for key in ("id", "created_at", "updated_at"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _normalize_guardrail_restore_payload(entity_id: str, snapshot: dict) -> dict:
        payload = dict(snapshot)
        payload["id"] = str(entity_id or "").strip()
        if not payload["id"]:
            raise ValueError("guardrail id is required")
        if payload.get("agents") is None and isinstance(payload.get("_agents"), list):
            payload["agents"] = payload.get("_agents") or []
        for key in ("created_at", "updated_at", "_agents"):
            payload.pop(key, None)
        return payload

    def _normalize_restore_payload(self, entity_type: str, entity_id: str, snapshot: dict) -> dict:
        if entity_type in {"agent", "tool", "skill"}:
            # A revision saved before categories came from the shared list
            # restores under the default category rather than failing.
            snapshot = {**snapshot, "category": known_catalog_category(snapshot.get("category"))}
        if entity_type == "agent":
            return self._normalize_agent_restore_payload(entity_id, snapshot)
        if entity_type == "tool":
            return self._normalize_tool_restore_payload(entity_id, snapshot)
        if entity_type == "skill":
            return self._normalize_skill_restore_payload(entity_id, snapshot)
        if entity_type == "workflow":
            return self._normalize_workflow_restore_payload(entity_id, snapshot)
        if entity_type == "guardrail":
            return self._normalize_guardrail_restore_payload(entity_id, snapshot)
        raise ValueError(f"Unsupported entity type '{entity_type}'")
