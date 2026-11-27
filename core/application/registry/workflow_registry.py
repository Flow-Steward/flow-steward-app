"""
WorkflowRegistry — manages workflow manifests loaded from PostgreSQL.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.projected_artifact_rows import projection_aware_catalog_rows
from core.application.registry.workflow_account import (
    bound_workflow_account,
    workflow_account_scope,
)
from core.application.registry.workflow_manifest import WorkflowManifest

if TYPE_CHECKING:
    from core.application.registry.db_store import RegistryDBStore

logger = logging.getLogger(__name__)


def _coerce_project_name(project_config: dict | None) -> str:
    if not isinstance(project_config, dict):
        return ""
    return str(project_config.get("project_name") or project_config.get("name") or "").strip()


def _default_account_workflows(db_store: RegistryDBStore) -> list[dict]:
    with workflow_account_scope(DEFAULT_ACCOUNT_ID):
        return db_store.list_workflows()


def _secondary_account() -> str | None:
    """The bound account when it is not the default account, else ``None``.

    The cached manifests are the default account's. A request or job of another
    account must never be answered from them (ADR 0020, C4c): its workflow with the
    same id is a different workflow, and the default account's is not its own.
    """
    bound = str(bound_workflow_account() or "").strip()
    return bound if bound and bound != DEFAULT_ACCOUNT_ID else None


def _manifest(row: object) -> WorkflowManifest | None:
    if not isinstance(row, dict):
        return None
    try:
        return WorkflowManifest.from_db_row(row)
    except Exception:
        logger.exception("Failed to load workflow from DB row %s", row.get("workflow_id"))
        return None


class WorkflowRegistry:
    def __init__(self, db_store: RegistryDBStore):
        self._db_store = db_store
        self._workflows: dict[str, WorkflowManifest] = {}
        self._project_workflow_links_by_name: dict[str, list[dict]] = {}
        self._project_workflow_mapping_available = True
        self._load_from_db()

    def _load_from_db(self) -> None:
        self._workflows.clear()
        self._project_workflow_links_by_name.clear()
        self._project_workflow_mapping_available = True
        try:
            # The in-memory registry is shared by every request and job of the
            # process, so it holds the default account's workflows only, whatever
            # account a reload happens to run under (ADR 0020, C4b). Other accounts'
            # workflows are read from the store with their account.
            rows = projection_aware_catalog_rows(
                self._db_store,
                "workflow",
                "workflow_id",
                _default_account_workflows(self._db_store),
            )
            for row in rows:
                try:
                    manifest = WorkflowManifest.from_db_row(row)
                    self._workflows[manifest.workflow_id] = manifest
                except Exception as e:
                    logger.exception(
                        "Failed to load workflow from DB row %s: %s", row.get("workflow_id"), e
                    )

            try:
                for row in self._db_store.list_project_workflows(enabled_only=True):
                    if str(row.get("account_id") or DEFAULT_ACCOUNT_ID) != DEFAULT_ACCOUNT_ID:
                        continue
                    project_name = str(row.get("project_name") or "").strip().lower()
                    workflow_id = str(row.get("workflow_id") or "").strip()
                    if not project_name or not workflow_id:
                        continue
                    self._project_workflow_links_by_name.setdefault(project_name, []).append(
                        dict(row)
                    )
            except Exception as e:
                self._project_workflow_mapping_available = False
                logger.exception("Failed to load project_workflows mapping: %s", e)
            logger.debug("Loaded %d workflows from DB", len(self._workflows))
        except Exception as e:
            logger.exception("Failed to load workflows from DB: %s", e)

    def _account_manifests(self, account_id: str) -> list[WorkflowManifest]:
        """Another account's workflows, read from the store with that account."""
        with workflow_account_scope(account_id):
            rows = projection_aware_catalog_rows(
                self._db_store,
                "workflow",
                "workflow_id",
                self._db_store.list_workflows(),
                account_id=account_id,
            )
        return [manifest for row in rows if (manifest := _manifest(row)) is not None]

    def get_workflow(self, workflow_id: str) -> WorkflowManifest | None:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return None
        account_id = _secondary_account()
        if account_id is not None:
            from core.application.registry.workflow_lookup import get_workflow_row

            return _manifest(get_workflow_row(self._db_store, workflow_id, account_id=account_id))
        direct = self._workflows.get(workflow_id)
        if direct:
            return direct
        return None

    def list_all(self) -> list[WorkflowManifest]:
        account_id = _secondary_account()
        if account_id is not None:
            return self._account_manifests(account_id)
        return list(self._workflows.values())

    def list_enabled(self) -> list[WorkflowManifest]:
        return [w for w in self.list_all() if w.enabled]

    @staticmethod
    def _is_workflow_visible_to_project(
        workflow: WorkflowManifest, project_config: dict | None
    ) -> bool:
        visibility = (workflow.settings or {}).get("project_visibility")
        if visibility is None:
            return True
        if isinstance(visibility, str):
            visibility_norm = visibility.strip().lower()
            return visibility_norm in {"", "all", "global", "public"}
        if not isinstance(visibility, dict):
            return True

        project_name = _coerce_project_name(project_config)
        allow = visibility.get("allow")
        if isinstance(allow, list):
            allow_norm = {str(x).strip() for x in allow if str(x).strip()}
            if allow_norm and project_name and project_name not in allow_norm:
                return False
            if allow_norm and not project_name:
                return False
        deny = visibility.get("deny")
        if isinstance(deny, list):
            deny_norm = {str(x).strip() for x in deny if str(x).strip()}
            if project_name and project_name in deny_norm:
                return False
        return True

    def _linked_workflow_ids_for_project(self, project_config: dict | None) -> set[str]:
        project_cfg = project_config if isinstance(project_config, dict) else {}
        linked_ids: set[str] = set()

        project_links = project_cfg.get("project_workflows")
        if isinstance(project_links, list):
            for item in project_links:
                if not isinstance(item, dict):
                    continue
                if not bool(item.get("enabled", True)):
                    continue
                if item.get("deleted_at"):
                    continue
                workflow_id = str(item.get("workflow_id") or "").strip()
                if workflow_id:
                    linked_ids.add(workflow_id)

        if linked_ids:
            return linked_ids

        project_name = _coerce_project_name(project_cfg).lower()
        if not project_name or _secondary_account() is not None:
            # The cached project links are the default account's.
            return set()
        rows = self._project_workflow_links_by_name.get(project_name) or []
        for row in rows:
            workflow_id = str(row.get("workflow_id") or "").strip()
            if workflow_id:
                linked_ids.add(workflow_id)
        return linked_ids

    def reload(self) -> None:
        logger.info("Reloading workflow registry from DB")
        self._load_from_db()
        logger.info("Workflow registry reloaded: %d workflows", len(self._workflows))
