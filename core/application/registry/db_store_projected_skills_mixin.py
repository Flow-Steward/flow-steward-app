"""Persistence for declarative identity anchors of projected skills."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from core.application.registry.db_store_common import json


def _projected_skill_contract_rows(value: Any, *, default_required: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "type": str(item.get("type") or "text").strip() or "text",
                "description": str(item.get("description") or ""),
                "required": bool(item.get("required", default_required)),
            }
        )
    return rows


class RegistryDBStoreProjectedSkillsMixin:
    _cursor: Callable[[], AbstractContextManager[Any]]
    _transaction: Callable[[], AbstractContextManager[Any]]
    _build_contract_schema: Callable[[list[dict]], dict]
    materialize_agent_contract: Callable[[str], None]

    def ensure_projected_skill_anchor(self, data: dict, *, account_id: str) -> None:
        """Persist identity and declared I/O only for an active installed skill."""
        skill_id = str(data.get("skill_id") or "").strip()
        install_id = str(data.get("source_install_id") or "").strip()
        normalized_account_id = str(account_id or "").strip()
        release_id = str(data.get("source_release_id") or "").strip()
        if (
            not skill_id.startswith("extension.")
            or not install_id
            or not normalized_account_id
            or not release_id
        ):
            raise ValueError("An active projected skill identity is required")

        inputs = _projected_skill_contract_rows(data.get("expected_inputs"), default_required=False)
        outputs = _projected_skill_contract_rows(
            data.get("expected_outputs"), default_required=True
        )
        with self._transaction() as cur:
            cur.execute(
                """SELECT release_id FROM extension_installs
                   WHERE install_id = %s AND account_id = %s AND state = 'active'
                   FOR SHARE""",
                (install_id, normalized_account_id),
            )
            install = cur.fetchone()
            if not install or str(install.get("release_id") or "") != release_id:
                raise ValueError("The projected skill install is no longer active")

            cur.execute(
                """INSERT INTO registry_skills
                   (skill_id, version, display_name, description, enabled, category,
                    instructions, prompt_template, examples, tags, input_schema, output_schema,
                    account_id, project_id, scope_kind, source_install_id,
                    availability_status, availability_reason_code, availability_reason_text,
                    new_runs_blocked, visible_in_catalog)
                   VALUES (%s, %s, %s, %s, false, %s, %s, '', %s, %s, %s, %s,
                           %s, NULL, 'account', %s, 'available', 'none', '', false, false)
                   ON CONFLICT (skill_id) DO NOTHING""",
                (
                    skill_id,
                    str(data.get("version") or "1.0.0"),
                    str(data.get("display_name") or skill_id),
                    str(data.get("description") or ""),
                    str(data.get("category") or ""),
                    [],
                    "[]",
                    [],
                    json.dumps(self._build_contract_schema(inputs)),
                    json.dumps(self._build_contract_schema(outputs)),
                    normalized_account_id,
                    install_id,
                ),
            )
            cur.execute(
                """SELECT account_id::text AS account_id, project_id, scope_kind,
                          source_install_id
                   FROM registry_skills WHERE skill_id = %s FOR UPDATE""",
                (skill_id,),
            )
            existing = cur.fetchone()
            if (
                not existing
                or str(existing.get("account_id") or "") != normalized_account_id
                or existing.get("project_id") is not None
                or str(existing.get("scope_kind") or "") != "account"
                or str(existing.get("source_install_id") or "") != install_id
            ):
                raise ValueError("The projected skill identity is owned by another registry row")

            # Executable content is deliberately absent from the durable anchor.
            cur.execute(
                """UPDATE registry_skills
                   SET version = %s, display_name = %s, description = %s, enabled = false,
                       category = %s, instructions = %s, prompt_template = '', examples = %s,
                       tags = %s, input_schema = %s, output_schema = %s,
                       availability_status = 'available', availability_reason_code = 'none',
                       availability_reason_text = '', new_runs_blocked = false,
                       visible_in_catalog = false
                   WHERE skill_id = %s AND account_id = %s AND source_install_id = %s""",
                (
                    str(data.get("version") or "1.0.0"),
                    str(data.get("display_name") or skill_id),
                    str(data.get("description") or ""),
                    str(data.get("category") or ""),
                    [],
                    "[]",
                    [],
                    json.dumps(self._build_contract_schema(inputs)),
                    json.dumps(self._build_contract_schema(outputs)),
                    skill_id,
                    normalized_account_id,
                    install_id,
                ),
            )
            cur.execute("DELETE FROM registry_skill_inputs WHERE skill_id = %s", (skill_id,))
            for sort_order, item in enumerate(inputs):
                cur.execute(
                    """INSERT INTO registry_skill_inputs
                       (skill_id, name, type, description, required, sort_order, contract_id)
                       VALUES (%s, %s, %s, %s, %s, %s, NULL)""",
                    (
                        skill_id,
                        item["name"],
                        item["type"],
                        item["description"],
                        item["required"],
                        sort_order,
                    ),
                )
            cur.execute("DELETE FROM registry_skill_outputs WHERE skill_id = %s", (skill_id,))
            for sort_order, item in enumerate(outputs):
                cur.execute(
                    """INSERT INTO registry_skill_outputs
                       (skill_id, name, type, description, sort_order, required, contract_id)
                       VALUES (%s, %s, %s, %s, %s, %s, NULL)""",
                    (
                        skill_id,
                        item["name"],
                        item["type"],
                        item["description"],
                        sort_order,
                        item["required"],
                    ),
                )

    def remove_projected_skill_anchor(
        self, skill_id: str, *, account_id: str, install_id: str
    ) -> bool:
        """Remove an unused anchor or clear contracts on a still-referenced one."""
        with self._transaction() as cur:
            cur.execute(
                "SELECT install_id FROM extension_installs WHERE install_id = %s AND account_id = %s FOR UPDATE",
                (install_id, account_id),
            )
            cur.fetchone()
            cur.execute(
                """SELECT account_id::text AS account_id, source_install_id
                   FROM registry_skills WHERE skill_id = %s FOR UPDATE""",
                (skill_id,),
            )
            row = cur.fetchone()
            if not row:
                return True
            if (
                str(row.get("account_id") or "") != str(account_id or "").strip()
                or str(row.get("source_install_id") or "") != str(install_id or "").strip()
            ):
                raise ValueError("The projected skill identity is owned by another registry row")
            cur.execute(
                "SELECT 1 FROM registry_agent_skills WHERE skill_id = %s LIMIT 1", (skill_id,)
            )
            if cur.fetchone():
                cur.execute("DELETE FROM registry_skill_inputs WHERE skill_id = %s", (skill_id,))
                cur.execute("DELETE FROM registry_skill_outputs WHERE skill_id = %s", (skill_id,))
                cur.execute(
                    """UPDATE registry_skills SET input_schema = '{}'::jsonb,
                              output_schema = '{}'::jsonb, enabled = false,
                              visible_in_catalog = false
                       WHERE skill_id = %s AND account_id = %s AND source_install_id = %s""",
                    (skill_id, account_id, install_id),
                )
                self.refresh_projected_skill_dependents({skill_id}, account_id=account_id)
                return False
            cur.execute(
                "DELETE FROM registry_skills WHERE skill_id = %s AND account_id = %s AND source_install_id = %s",
                (skill_id, account_id, install_id),
            )
            return cur.rowcount == 1

    def delete_projected_skill_anchors(
        self, *, account_id: str, install_id: str, keep_skill_ids: set[str] | None = None
    ) -> list[str]:
        """Delete obsolete unused anchors, retaining referenced or current ones."""
        keep = set(keep_skill_ids or set())
        with self._cursor() as cur:
            cur.execute(
                """SELECT skill_id FROM registry_skills
                   WHERE account_id = %s AND source_install_id = %s
                     AND skill_id LIKE %s ORDER BY skill_id""",
                (account_id, install_id, "extension.%"),
            )
            skill_ids = [str(row.get("skill_id") or "") for row in cur.fetchall()]
        retained: list[str] = []
        for skill_id in skill_ids:
            if skill_id not in keep and not self.remove_projected_skill_anchor(
                skill_id, account_id=account_id, install_id=install_id
            ):
                retained.append(skill_id)
        return retained

    def refresh_projected_skill_dependents(self, skill_ids: set[str], *, account_id: str) -> None:
        """Recompute authored agents' inherited I/O after declaration refresh."""
        normalized_ids = sorted({str(value).strip() for value in skill_ids if str(value).strip()})
        if not normalized_ids:
            return
        with self._cursor() as cur:
            cur.execute(
                """SELECT DISTINCT a.agent_id
                   FROM registry_agent_skills s
                   JOIN registry_agents a ON a.agent_id = s.agent_id
                   WHERE s.skill_id = ANY(%s) AND a.account_id = %s
                     AND a.project_id IS NULL AND COALESCE(a.source_install_id, '') = ''
                   ORDER BY a.agent_id""",
                (normalized_ids, account_id),
            )
            agent_ids = [str(row.get("agent_id") or "") for row in cur.fetchall()]
        for agent_id in agent_ids:
            self.materialize_agent_contract(agent_id)

    def deactivate_projected_skill_anchors(self, *, account_id: str, install_id: str) -> None:
        """Keep referenced identities but remove declarations when projections deactivate."""
        with self._transaction() as cur:
            cur.execute(
                """SELECT install_id FROM extension_installs
                   WHERE install_id = %s AND account_id = %s FOR UPDATE""",
                (install_id, account_id),
            )
            cur.fetchone()
            cur.execute(
                """SELECT skill_id FROM registry_skills
                   WHERE account_id = %s AND source_install_id = %s
                     AND skill_id LIKE %s ORDER BY skill_id""",
                (account_id, install_id, "extension.%"),
            )
            skill_ids = {str(row.get("skill_id") or "") for row in cur.fetchall()}
            if not skill_ids:
                return
            cur.execute(
                "DELETE FROM registry_skill_inputs WHERE skill_id = ANY(%s)", (list(skill_ids),)
            )
            cur.execute(
                "DELETE FROM registry_skill_outputs WHERE skill_id = ANY(%s)", (list(skill_ids),)
            )
            cur.execute(
                """UPDATE registry_skills SET input_schema = '{}'::jsonb,
                          output_schema = '{}'::jsonb, enabled = false,
                          visible_in_catalog = false
                   WHERE skill_id = ANY(%s) AND account_id = %s AND source_install_id = %s""",
                (list(skill_ids), account_id, install_id),
            )
        self.refresh_projected_skill_dependents(skill_ids, account_id=account_id)
