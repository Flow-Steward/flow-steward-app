from types import SimpleNamespace

from core.application.management.errors import ServiceForbiddenError
from core.entrypoints.admin import changelog


class _HistoryDB:
    def get_entity_history(self, **_kwargs):
        return [
            {
                "id": "2",
                "entity_type": "agent",
                "entity_id": "global-agent",
                "snapshot": {"agent_id": "global-agent", "display_name": "Current"},
            },
            {
                "id": "1",
                "entity_type": "agent",
                "entity_id": "global-agent",
                "snapshot": {"agent_id": "global-agent", "display_name": "Original"},
            },
        ]

    def diff_entity_history(self, **_kwargs):
        return {
            "changes": [
                {"path": "display_name", "from": "Original", "to": "Current"},
            ],
            "summary": {"total": 1, "changed": 1},
        }


def _admin_request():
    return SimpleNamespace(
        state=SimpleNamespace(
            auth_user={"id": "admin-1", "roles": ["admin"], "account_id": "restricted"},
        )
    )


def test_admin_global_changelog_history_and_diff_do_not_reapply_account_scope(monkeypatch) -> None:
    db = _HistoryDB()
    monkeypatch.setattr(changelog, "_get_db", lambda: db)
    monkeypatch.setattr(
        changelog,
        "require_account_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ServiceForbiddenError("Access denied")),
    )

    history = changelog.get_entity_history(
        _admin_request(),
        entity_type="agent",
        entity_id="global-agent",
        limit=50,
    )
    diff = changelog.get_entity_diff(
        _admin_request(),
        entity_type="agent",
        entity_id="global-agent",
        from_entry_id="1",
        to_entry_id="2",
    )

    assert [row["id"] for row in history] == ["2", "1"]
    assert diff["changes"] == [
        {"path": "display_name", "from": "Original", "to": "Current"},
    ]
