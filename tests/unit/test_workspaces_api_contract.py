from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.entrypoints.admin import workspaces


def test_storage_unavailable_response_is_documented_for_workspace_routes(monkeypatch) -> None:
    monkeypatch.setattr(workspaces, "_get_registry", lambda: SimpleNamespace(db_store=None))
    app = FastAPI()
    app.include_router(workspaces.router)
    with TestClient(app) as client:
        response = client.get("/api/workspaces")
        schema = client.get("/openapi.json").json()
    assert response.status_code == 503
    assert response.json() == {"detail": "DB backend not configured"}
    operations = [
        operation
        for path, methods in schema["paths"].items()
        if path.startswith("/api/workspaces")
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert operations
    assert all(str(response.status_code) in operation["responses"] for operation in operations)
