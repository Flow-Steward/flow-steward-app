from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.entrypoints.admin import workspaces as workspace_routes


class FakeWorkspaceDB:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {
            DEFAULT_WORKSPACE_ID: {
                "id": DEFAULT_WORKSPACE_ID,
                "tenant_id": DEFAULT_TENANT_ID,
                "account_id": DEFAULT_ACCOUNT_ID,
                "workspace_key": "default",
                "display_name": "Default Workspace",
                "description": "",
                "status": "active",
                "metadata_jsonb": {},
                "projects_count": 0,
                "jobs_count": 0,
            }
        }

    def list_workspaces(self, **kwargs):
        status = str(kwargs.get("status") or "").strip()
        rows = list(self.rows.values())
        if status:
            rows = [row for row in rows if row["status"] == status]
        return rows

    def get_workspace(self, workspace_id: str, **kwargs):
        row = self.rows.get(workspace_id)
        if row is not None and any(
            kwargs.get(key) is not None and row[key] != kwargs[key]
            for key in ("account_id", "tenant_id")
        ):
            return None
        return row

    def save_workspace(self, payload: dict, **kwargs):
        existing = next(
            (
                row
                for row in self.rows.values()
                if row["tenant_id"] == kwargs["tenant_id"]
                and row["account_id"] == kwargs["account_id"]
                and row["workspace_key"] == payload["workspace_key"]
            ),
            None,
        )
        if existing is not None and not payload.get("id"):
            existing.update(
                display_name=payload["display_name"],
                description=payload.get("description", ""),
                metadata_jsonb=payload.get("metadata_jsonb", {}),
            )
            return existing
        workspace_id = str(
            payload.get("id") or f"00000000-0000-0000-0000-0000000000{len(self.rows) + 3:02d}"
        )
        row = {
            "id": workspace_id,
            "tenant_id": kwargs["tenant_id"],
            "account_id": kwargs["account_id"],
            "workspace_key": payload["workspace_key"],
            "display_name": payload["display_name"],
            "description": payload.get("description", ""),
            "status": payload.get("status", "active"),
            "metadata_jsonb": payload.get("metadata_jsonb", {}),
            "projects_count": 0,
            "jobs_count": 0,
        }
        self.rows[workspace_id] = row
        return row

    def create_workspace(self, payload: dict, **kwargs):
        duplicate = any(
            row["tenant_id"] == kwargs["tenant_id"]
            and row["account_id"] == kwargs["account_id"]
            and row["workspace_key"] == payload["workspace_key"]
            for row in self.rows.values()
        )
        if duplicate:
            return None
        return self.save_workspace(payload, **kwargs)

    def set_workspace_archived(self, workspace_id: str, *, archived: bool, **kwargs):
        row = self.rows[workspace_id]
        row["status"] = "active"
        row["deleted_at"] = "2026-05-16T00:00:00Z" if archived else None
        return row

    def list_workspace_projects(self, workspace_id: str, **kwargs):
        return [
            {
                "id": "00000000-0000-0000-0000-000000000010",
                "name": "Demo",
                "workspace_id": workspace_id,
            }
        ]


def _client(db: FakeWorkspaceDB, monkeypatch, *, raise_server_exceptions=True) -> TestClient:
    monkeypatch.setattr(workspace_routes, "_get_db", lambda: db)
    app = FastAPI()
    app.include_router(workspace_routes.router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


@pytest.mark.parametrize("hidden", [False, True])
def test_workspace_update_missing_or_foreign_resource_is_404_without_upsert(monkeypatch, hidden):
    db = FakeWorkspaceDB()
    workspace_id = "00000000-0000-0000-0000-000000000099"
    if hidden:
        db.rows[workspace_id] = {
            **db.rows[DEFAULT_WORKSPACE_ID],
            "id": workspace_id,
            "account_id": "foreign-account",
        }
    before = {key: dict(value) for key, value in db.rows.items()}
    client = _client(db, monkeypatch, raise_server_exceptions=False)
    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 404
    response = client.put(f"/api/workspaces/{workspace_id}", json={"display_name": "No upsert"})
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Workspace not found"}
    assert db.rows == before


def test_retired_workspace_archive_route_cannot_bypass_trash(monkeypatch) -> None:
    db = FakeWorkspaceDB()
    client = _client(db, monkeypatch)
    workspace_id = client.post(
        "/api/workspaces",
        json={"workspace_key": "release", "display_name": "Release"},
    ).json()["id"]
    before = dict(db.rows[workspace_id])

    response = client.patch(f"/api/workspaces/{workspace_id}/archive")

    assert response.status_code == 404
    assert db.rows[workspace_id] == before
    assert "/api/workspaces/{workspace_id}/archive" not in client.app.openapi()["paths"]


def test_workspace_registry_crud_and_projects(monkeypatch) -> None:
    db = FakeWorkspaceDB()
    client = _client(db, monkeypatch)

    create_response = client.post(
        "/api/workspaces",
        json={
            "workspace_key": "release",
            "display_name": "Release",
            "description": "Release workspace",
        },
    )
    assert create_response.status_code == 200
    workspace_id = create_response.json()["id"]

    list_response = client.get("/api/workspaces")
    assert list_response.status_code == 200
    assert {row["workspace_key"] for row in list_response.json()["workspaces"]} == {
        "default",
        "release",
    }

    update_response = client.put(
        f"/api/workspaces/{workspace_id}",
        json={
            "workspace_key": "release",
            "display_name": "Release Workspace",
            "description": "Updated",
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["display_name"] == "Release Workspace"

    db.rows[workspace_id]["deleted_at"] = "2026-05-16T00:00:00Z"
    restore_response = client.patch(f"/api/workspaces/{workspace_id}/restore")
    assert restore_response.status_code == 200
    assert restore_response.json()["status"] == "active"
    assert restore_response.json()["deleted_at"] is None

    projects_response = client.get(f"/api/workspaces/{workspace_id}/projects")
    assert projects_response.status_code == 200
    assert projects_response.json()["projects"][0]["workspace_id"] == workspace_id


def test_workspace_display_name_update_preserves_omitted_stable_key(monkeypatch) -> None:
    db = FakeWorkspaceDB()
    client = _client(db, monkeypatch)
    workspace_id = client.post(
        "/api/workspaces",
        json={"workspace_key": "release", "display_name": "Release"},
    ).json()["id"]

    response = client.put(
        f"/api/workspaces/{workspace_id}",
        json={"display_name": "Release Renamed", "description": "Updated"},
    )

    assert response.status_code == 200
    assert response.json()["workspace_key"] == "release"
    assert response.json()["display_name"] == "Release Renamed"


def test_workspace_key_change_requires_preview_and_exact_confirmation(monkeypatch) -> None:
    db = FakeWorkspaceDB()
    client = _client(db, monkeypatch)
    workspace_id = client.post(
        "/api/workspaces",
        json={"workspace_key": "release", "display_name": "Release"},
    ).json()["id"]

    ordinary_update = client.put(
        f"/api/workspaces/{workspace_id}",
        json={"workspace_key": "renamed", "display_name": "Release"},
    )
    assert ordinary_update.status_code == 400
    assert "key-change" in ordinary_update.json()["detail"]

    preview = client.post(
        f"/api/workspaces/{workspace_id}/key-change/preview",
        json={"workspace_key": "renamed"},
    )
    assert preview.status_code == 200
    assert preview.json()["conflict"] is False
    assert preview.json()["impact"]["projects_count"] == 0
    assert preview.json()["confirmation_workspace_id"] == workspace_id

    unconfirmed = client.post(
        f"/api/workspaces/{workspace_id}/key-change",
        json={"workspace_key": "renamed", "confirm_workspace_id": "wrong"},
    )
    assert unconfirmed.status_code == 400

    changed = client.post(
        f"/api/workspaces/{workspace_id}/key-change",
        json={"workspace_key": "renamed", "confirm_workspace_id": workspace_id},
    )
    assert changed.status_code == 200
    assert changed.json()["workspace_key"] == "renamed"


def test_create_workspace_rejects_duplicate_key_without_overwriting_original(monkeypatch) -> None:
    db = FakeWorkspaceDB()
    client = _client(db, monkeypatch)

    original = client.post(
        "/api/workspaces",
        json={"display_name": "Release", "description": "Original description"},
    )
    assert original.status_code == 200
    original_id = original.json()["id"]

    duplicate = client.post(
        "/api/workspaces",
        json={"display_name": "Release", "description": "Replacement description"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "workspace_key 'release' already exists"
    assert db.rows[original_id]["description"] == "Original description"
