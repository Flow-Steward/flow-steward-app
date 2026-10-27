"""Browser and direct API requests use one bounded installer transport."""

import httpx
from starlette.responses import JSONResponse
from starlette.testclient import TestClient


def test_proxy_preserves_upload_and_auth_and_refreshes_after_success():
    from core.entrypoints.api.extension_installer_proxy import ExtensionInstallerProxy

    refreshed = []
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"extension": {"extension_id": "acme.test"}})

    async def app(scope, receive, send):
        await JSONResponse({"local": True})(scope, receive, send)

    proxy = ExtensionInstallerProxy(
        app,
        refresh=lambda: refreshed.append(True),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    response = TestClient(proxy).post(
        "/api/extensions/install",
        content=b"zip body",
        headers={
            "Authorization": "Bearer caller",
            "Content-Type": "multipart/form-data; boundary=test",
        },
    )
    assert response.status_code == 200
    assert refreshed == [True]
    assert len(seen) == 1
    assert seen[0].url.host == "fs-extension-installer"
    assert seen[0].content == b"zip body"
    assert seen[0].headers["authorization"] == "Bearer caller"


def test_proxy_uses_validated_compact_installer_origin(monkeypatch):
    from core.entrypoints.api import extension_installer_proxy as module

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setenv("FS_EXTENSION_INSTALLER_ORIGIN", "http://127.0.0.1:8101")
    proxy = module.ExtensionInstallerProxy(
        None,
        refresh=lambda: None,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )

    assert TestClient(proxy).post("/api/extensions/install", content=b"zip").status_code == 200
    assert str(seen[0].url).startswith("http://127.0.0.1:8101/")

    monkeypatch.setenv("FS_EXTENSION_INSTALLER_ORIGIN", "http://user:secret@127.0.0.1:8101")
    with __import__("pytest").raises(ValueError, match="credential-free"):
        TestClient(proxy).post("/api/extensions/install", content=b"zip")


def test_proxy_does_not_retry_or_refresh_rejected_upload():
    from core.entrypoints.api.extension_installer_proxy import ExtensionInstallerProxy

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(412, json={"error": "approval_required"})

    def forbidden():
        raise AssertionError("Rejected upload changed projections")

    proxy = ExtensionInstallerProxy(
        None,
        refresh=forbidden,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    response = TestClient(proxy).post("/api/extensions/install", content=b"zip body")
    assert response.status_code == 412
    assert response.json() == {"error": "approval_required"}
    assert len(seen) == 1


def test_lost_response_is_not_replayed_or_refreshed():
    from core.entrypoints.api.extension_installer_proxy import ExtensionInstallerProxy

    seen = []

    def upstream(request):
        seen.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    proxy = ExtensionInstallerProxy(
        None,
        refresh=lambda: seen.append("incorrect refresh"),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    response = TestClient(proxy).post("/api/extensions/install", content=b"zip")
    assert response.status_code == 502
    assert len(seen) == 1


def test_upload_limit_rejects_before_contacting_installer(monkeypatch):
    from core.entrypoints.api import extension_installer_proxy as module

    monkeypatch.setattr(module, "_MAX_UPLOAD_BYTES", 2)

    def forbidden():
        raise AssertionError("Oversized upload reached installer")

    response = TestClient(
        module.ExtensionInstallerProxy(None, refresh=forbidden, client_factory=forbidden)
    ).post("/api/extensions/install", content=b"zip")
    assert response.status_code == 413


def test_separate_worker_projections_observe_committed_release_changes():
    from types import SimpleNamespace

    from core.application.extensions.installer_projection_refresh import InstallerProjectionSync

    installs = [SimpleNamespace(install_id="one", release_id="r1", state="active")]
    store = SimpleNamespace(list_active_installs=lambda: installs)
    refreshed = [[], []]
    workers = [
        InstallerProjectionSync(
            get_install_store=lambda: store, refresh=lambda i=i: refreshed[i].append(True)
        )
        for i in range(2)
    ]
    for worker in workers:
        worker()
        worker()
    assert list(map(len, refreshed)) == [1, 1]
    installs[0].release_id = "r2"
    for worker in workers:
        worker()
    assert list(map(len, refreshed)) == [2, 2]
    installs.clear()
    for worker in workers:
        worker()
    assert list(map(len, refreshed)) == [3, 3]


def test_health_and_static_requests_do_not_query_install_store():
    from core.entrypoints.api.extension_installer_proxy import ExtensionInstallerProxy

    def unavailable():
        raise RuntimeError("Database unavailable")

    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    client = TestClient(ExtensionInstallerProxy(app, refresh=unavailable, synchronize=unavailable))
    for path in [
        "/health/live",
        "/api/health",
        "/assets/main.js",
        "/app/assets/main.js",
        "/api/auth/login",
        "/login",
        "/app/login",
    ]:
        assert client.get(path).status_code == 200


def test_frontend_documents_refresh_before_html_is_served():
    from core.entrypoints.api.extension_installer_proxy import ExtensionInstallerProxy

    events = []

    async def app(scope, receive, send):
        events.append("serve")
        await JSONResponse({"ok": True})(scope, receive, send)

    client = TestClient(
        ExtensionInstallerProxy(
            app, refresh=lambda: None, synchronize=lambda: events.append("refresh")
        )
    )
    for path in [
        "/registry/workflows",
        "/settings/account",
        "/extensions",
        "/app/registry/workflows",
    ]:
        events.clear()
        assert client.get(path).status_code == 200
        assert events == ["refresh", "serve"]
