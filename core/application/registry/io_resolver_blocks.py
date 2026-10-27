# ruff: noqa: RUF100
# mypy: ignore-errors
from core.application.workflows.execution_graph_queries import DefinitionGraph

from .io_resolver_common import *  # noqa: F403,F401  # NOSONAR


class IOResolverBlockContractMixin:
    """Published block contract and workflow step lookup helpers."""

    def _resolve_primary_skill_id(self, agent_id: str, skill_id: str = "") -> str:
        """Return explicit skill_id or the first skill assigned to the agent."""
        explicit = str(skill_id or "").strip()
        if explicit:
            return explicit
        if not str(agent_id or "").strip():
            return ""
        for cap in self._get_agent_skills(agent_id) or []:
            cap_id = str(cap.get("skill_id") or "").strip()
            if cap_id:
                return cap_id
        return ""

    @staticmethod
    def _parse_agent_skill_ref(source_ref: str) -> tuple[str, str]:
        raw = str(source_ref or "").strip()
        if not raw:
            return "", ""
        if "::" in raw:
            agent_id, skill_id = raw.split("::", 1)
            return str(agent_id or "").strip(), str(skill_id or "").strip()
        return "", ""

    @staticmethod
    def _extract_step_override(step: dict) -> dict:
        override = (step or {}).get("override")
        if isinstance(override, dict):
            return dict(override)
        if isinstance(override, str):
            try:
                import json as _json

                parsed = _json.loads(override)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    def _resolve_step_block_key(self, step: dict | None) -> str:
        override = self._extract_step_override(step or {})
        for key in ("published_block_key", "block_key"):
            value = str(override.get(key) or "").strip()
            if value:
                return value
        return ""

    def _list_published_block_contracts_safe(self, *, enabled_only: bool = False) -> list[dict]:
        if self._db_has_active_transaction():
            return []
        list_fn = getattr(self.db, "list_published_block_contracts", None)
        if not callable(list_fn):
            return []
        try:
            rows = list_fn(enabled_only=enabled_only)
        except Exception:
            clear_failed_tx = getattr(self.db, "_clear_failed_transaction", None)
            if callable(clear_failed_tx):
                clear_failed_tx()
            return []
        if not isinstance(rows, list):
            return []
        return [dict(r) for r in rows if isinstance(r, dict)]

    def _get_published_block_contract_safe(self, block_key: str) -> dict | None:
        key = str(block_key or "").strip()
        if not key:
            return None
        if self._db_has_active_transaction():
            return None
        get_fn = getattr(self.db, "get_published_block_contract", None)
        if not callable(get_fn):
            return None
        try:
            row = get_fn(key)
        except Exception:
            clear_failed_tx = getattr(self.db, "_clear_failed_transaction", None)
            if callable(clear_failed_tx):
                clear_failed_tx()
            return None
        return dict(row) if isinstance(row, dict) else None

    def _db_has_active_transaction(self) -> bool:
        """Trust only explicit boolean transaction state from DB adapters.

        Tests frequently use ``MagicMock`` DB doubles; their default callable
        attributes evaluate truthy, which would incorrectly suppress contract
        resolution. Production adapters return a concrete boolean.
        """
        has_active_tx = getattr(self.db, "_has_active_transaction", None)
        if not callable(has_active_tx):
            return False
        try:
            state = has_active_tx()
        except Exception:
            return False
        return state if isinstance(state, bool) else False

    def _resolve_published_block_contract(
        self,
        *,
        agent_id: str = "",
        skill_id: str = "",
        block_key: str = "",
        account_id: str | None = None,
    ) -> dict | None:
        from core.application.registry.published_block_selection import applicable_agent_block

        return applicable_agent_block(
            agent_id=str(agent_id or "").strip(),
            skill_id=str(skill_id or "").strip(),
            block_key=str(block_key or "").strip(),
            account_id=account_id,
            list_blocks=lambda: self._list_published_block_contracts_safe(enabled_only=True),
            get_block=self._get_published_block_contract_safe,
        )

    @staticmethod
    def _normalize_published_block_input_item(raw: dict, *, block_key: str) -> dict | None:
        name = str((raw or {}).get("field_key") or "").strip()
        if not name:
            return None
        description = str((raw or {}).get("notes") or "").strip()
        if not description:
            description = str((raw or {}).get("field_description") or "").strip()
        if not description:
            description = str((raw or {}).get("field_display_name") or "").strip()
        return {
            "name": name,
            "type": str((raw or {}).get("value_type") or "text").strip() or "text",
            "description": description,
            "required": bool((raw or {}).get("required", False)),
            "source": "published_block",
            "source_id": str(block_key or "").strip(),
        }

    @staticmethod
    def _normalize_published_block_output_item(raw: dict, *, block_key: str) -> dict | None:
        name = str((raw or {}).get("field_key") or "").strip()
        if not name:
            return None
        from core.application.registry.output_contract_fields import published_output_description

        description = published_output_description(raw)
        return {
            "name": name,
            "type": str((raw or {}).get("value_type") or "text").strip() or "text",
            "description": description,
            "required": bool((raw or {}).get("required_by_default", False)),
            "exposed_by_default": bool((raw or {}).get("exposed_by_default", True)),
            "source": "published_block",
            "source_id": str(block_key or "").strip(),
        }

    def _get_workflow_step(self, workflow_id: str, step_id: str) -> tuple[dict, dict]:
        """Return (workflow, step) where step may be empty if not found.

        ``step_id`` is unique per workflow; the row ``id`` is accepted too so
        callers holding the stored row reference resolve the same step.
        """
        workflow = self._get_workflow(workflow_id) or {}
        normalized_step_id = str(step_id or "").strip()
        if not normalized_step_id:
            return workflow, {}
        steps = workflow.get("steps")
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            if normalized_step_id in {
                str(step.get("step_id") or "").strip(),
                str(step.get("id") or "").strip(),
            }:
                return workflow, step
        return workflow, {}

    @staticmethod
    def _definition_graph(workflow: dict) -> DefinitionGraph:
        return DefinitionGraph.from_workflow(workflow if isinstance(workflow, dict) else {})


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
