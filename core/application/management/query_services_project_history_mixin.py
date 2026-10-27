# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .query_services_common import *  # NOSONAR


class ProjectQueryServiceHistoryMixin:
    def list_history(
        self,
        project_id: Any,
        *,
        limit: int = 50,
        scope: ScopeContext | None = None,
    ) -> ProjectSettingsHistoryListView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "list_project_settings_revisions"):
            raise ServiceUnavailableError(
                "Project settings history is not available in this schema"
            )

        revisions = (
            bl.list_project_settings_revisions(
                str(project_id), limit=max(1, min(int(limit or 50), 500))
            )
            or []
        )
        return ProjectSettingsHistoryListView(
            project_id=str(project_id),
            revisions=[dict(row) for row in revisions if isinstance(row, dict)],
        )

    def get_history_diff(
        self,
        project_id: Any,
        revision_id: Any,
        *,
        compare_to_revision_id: Any | None = None,
        scope: ScopeContext | None = None,
    ) -> ProjectSettingsHistoryDiffView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "get_project_settings_revision"):
            raise ServiceUnavailableError(
                "Project settings history is not available in this schema"
            )

        selected = bl.get_project_settings_revision(str(project_id), str(revision_id))
        if not isinstance(selected, dict):
            raise ServiceNotFoundError("Revision not found")
        selected_snapshot = self._to_json_obj(selected.get("snapshot_jsonb")) or {}

        compare_entry = None
        compare_snapshot: dict[str, Any] = {}
        compare_label = "current"
        if compare_to_revision_id:
            compare_entry = bl.get_project_settings_revision(
                str(project_id), str(compare_to_revision_id)
            )
            if not isinstance(compare_entry, dict):
                raise ServiceNotFoundError("Compare revision not found")
            compare_snapshot = self._to_json_obj(compare_entry.get("snapshot_jsonb")) or {}
            compare_label = f"revision:{str(compare_to_revision_id).strip()}"
        elif hasattr(bl, "get_project_settings_snapshot"):
            compare_snapshot = bl.get_project_settings_snapshot(str(project_id)) or {}

        diff = self._project_settings_diff(selected_snapshot, compare_snapshot)
        return ProjectSettingsHistoryDiffView(
            project_id=str(project_id),
            selected_revision={
                "id": str(selected.get("id") or "").strip(),
                "created_at": selected.get("created_at"),
                "author": selected.get("author"),
                "action": selected.get("action"),
                "source": selected.get("source"),
            },
            compare_to={
                "label": compare_label,
                "id": str(compare_entry.get("id") or "").strip()
                if isinstance(compare_entry, dict)
                else None,
                "created_at": compare_entry.get("created_at")
                if isinstance(compare_entry, dict)
                else None,
                "author": compare_entry.get("author")
                if isinstance(compare_entry, dict)
                else "system",
                "action": compare_entry.get("action")
                if isinstance(compare_entry, dict)
                else "current",
                "source": compare_entry.get("source")
                if isinstance(compare_entry, dict)
                else "runtime",
            },
            summary=diff.get("summary") or {"total": 0, "added": 0, "removed": 0, "changed": 0},
            changes=diff.get("changes") or [],
        )

    def get_history_page(
        self,
        project_id: Any,
        *,
        selected_revision_id: Any | None = None,
        scope: ScopeContext | None = None,
    ) -> ProjectSettingsHistoryPageView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        project = self._require_project_in_scope(bl, str(project_id), resolved)

        history_payload = self.list_history(str(project_id), limit=100, scope=scope)
        revisions = [
            dict(row) for row in (history_payload.revisions or []) if isinstance(row, dict)
        ]
        selected_revision = None
        if revisions:
            if selected_revision_id:
                selected_revision = next(
                    (
                        row
                        for row in revisions
                        if str(row.get("id") or "").strip() == str(selected_revision_id).strip()
                    ),
                    None,
                )
            if not selected_revision:
                selected_revision = revisions[0]

        compare_entry = {
            "label": "current",
            "created_at": datetime.now(UTC),
            "author": "system",
            "action": "current",
            "source": "runtime",
        }
        diff_payload: dict[str, Any] = {
            "changes": [],
            "summary": {"total": 0, "added": 0, "removed": 0, "changed": 0},
        }
        if isinstance(selected_revision, dict) and hasattr(bl, "get_project_settings_revision"):
            selected_entry = bl.get_project_settings_revision(
                str(project_id), str(selected_revision.get("id") or "").strip()
            )
            if isinstance(selected_entry, dict):
                selected_revision = selected_entry
                if hasattr(bl, "get_project_settings_snapshot"):
                    diff_view = self.get_history_diff(
                        str(project_id),
                        str(selected_revision.get("id") or "").strip(),
                        scope=scope,
                    )
                    compare_entry = dict(diff_view.compare_to or {})
                    diff_payload = {
                        "changes": list(diff_view.changes or []),
                        "summary": dict(diff_view.summary or {}),
                    }

        return ProjectSettingsHistoryPageView(
            project=project,
            revisions=revisions,
            selected_revision=selected_revision if isinstance(selected_revision, dict) else None,
            compare_entry=compare_entry,
            diff_payload=diff_payload,
        )


__all__ = [
    "ProjectQueryServiceHistoryMixin",
]
