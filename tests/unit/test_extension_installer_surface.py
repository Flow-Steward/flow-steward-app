"""The writable installer exposes only the existing authenticated lifecycle API."""

import re
from pathlib import Path

import pytest
import yaml
from starlette.responses import JSONResponse
from starlette.testclient import TestClient


@pytest.mark.parametrize(
    "method,path,allowed",
    [
        ("GET", "/api/extensions/capabilities/archive-lifecycle", True),
        ("POST", "/api/extensions/install", True),
        ("GET", "/api/extensions/acme.test/uninstall-plan", True),
        ("POST", "/api/extensions/acme.test/uninstall", True),
        ("POST", "/api/extensions/scan", True),
        ("GET", "/api/extensions/scan", False),
        ("GET", "/api/extensions", False),
        ("POST", "/api/extensions/acme.test/action/run", False),
        ("POST", "/api/auth/login", False),
        ("POST", "/api/extensions/capabilities/archive-lifecycle", False),
        ("GET", "/api/extensions/install", False),
        ("GET", "/docs", False),
    ],
)
def test_installer_passes_only_lifecycle_requests_to_normal_auth_stack(method, path, allowed):
    from core.entrypoints.api.extension_installer import InstallerSurface

    async def authenticated_app(scope, receive, send):
        assert (b"authorization", b"Bearer caller-token") in scope["headers"]
        await JSONResponse({"normal_auth_stack": True}, status_code=401)(scope, receive, send)

    client = TestClient(InstallerSurface(authenticated_app))
    response = client.request(method, path, headers={"Authorization": "Bearer caller-token"})
    assert response.status_code == (401 if allowed else 404)


#: `${VAR:-default}` carries its own colon, so the variable has to be masked
#: before a short-form bind can be split on ":".
_COMPOSE_VARIABLE = re.compile(r"\$\{[^}]*\}")


def _bind_target(volume: str) -> str:
    """The container-side path of a `source:target[:mode]` short-form bind."""
    parts = _COMPOSE_VARIABLE.sub("VARIABLE", str(volume)).split(":")
    return parts[1] if len(parts) > 1 else ""


@pytest.mark.parametrize("filename", ["docker-compose.yaml", "docker-compose.prod.yaml"])
def test_web_installer_is_running_without_writable_app_or_docker_socket(filename):
    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / "infra/docker" / filename).read_text())["services"]
    installer = services["fs-extension-installer"]
    assert not installer.get("profiles")
    assert not installer.get("ports")
    assert installer["restart"] == "always"
    assert any(v.endswith(":/app/extensions:rw") for v in installer["volumes"])
    assert not any("docker.sock" in v for v in installer["volumes"])
    assert any(v.endswith(":/app/extensions:ro") for v in services["fs-app"]["volumes"])
    assert "core.entrypoints.api.extension_installer:create_app" in installer["command"]


@pytest.mark.parametrize("filename", ["docker-compose.yaml", "docker-compose.prod.yaml"])
def test_installer_never_binds_over_the_application_root(filename):
    """A bind at /app costs the installer its writable extensions mount.

    With `../../:/app:ro` in place, the observed Docker Desktop/virtiofs
    runtime mounted only /app, read-only: `docker inspect` still listed the
    nested `/app/extensions:rw` bind, but /proc/mounts inside the live
    container did not contain it at all, so the extensions tree was simply part
    of the read-only parent and the capability check read ST_RDONLY. The
    installer runs the code baked into its image, exactly as production does.
    """
    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / "infra/docker" / filename).read_text())["services"]
    volumes = services["fs-extension-installer"]["volumes"]

    assert not any(_bind_target(volume) == "/app" for volume in volumes), (
        f"{filename} binds over /app; the nested extensions mount cannot be written"
    )
    extensions_mounts = [v for v in volumes if _bind_target(v) == "/app/extensions"]
    assert [v.endswith(":rw") for v in extensions_mounts] == [True]


@pytest.mark.parametrize("filename", ["docker-compose.yaml", "docker-compose.prod.yaml"])
def test_only_the_installer_may_write_the_extensions_tree(filename):
    """Every other runtime declares a read-only view of the same directory.

    This pins compose intent, not a runtime guarantee. The same Docker
    Desktop/virtiofs behaviour this file documents drops these nested mounts
    too: fs-app and fs-worker bind the repository at /app read-write, and
    /proc/mounts inside them has no /app/extensions line at all, so the tree is
    writable there in the dev stack whatever `:ro` says here. No user-facing
    path is opened by that — the archive routes are proxied to the installer —
    but worker-side isolation of the extensions tree is its own question, not
    something this assertion establishes.
    """
    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / "infra/docker" / filename).read_text())["services"]

    for service_name in ("fs-app", "fs-worker"):
        mounts = [
            volume
            for volume in services[service_name].get("volumes") or []
            if _bind_target(volume) == "/app/extensions"
        ]
        assert mounts, f"{filename}:{service_name} has no extensions mount"
        assert all(volume.endswith(":ro") for volume in mounts), (
            f"{filename}:{service_name} may not write the extensions tree"
        )


def test_main_api_owns_proxy_for_all_ingress_modes():
    root = Path(__file__).resolve().parents[2]
    for filename in ["docker-compose.yaml", "docker-compose.prod.yaml"]:
        services = yaml.safe_load((root / "infra/docker" / filename).read_text())["services"]
        assert services["fs-app"]["environment"]["FS_EXTENSION_INSTALLER_PROXY_ENABLED"] == "1"
        assert (
            "FS_EXTENSION_INSTALLER_PROXY_ENABLED"
            not in services["fs-extension-installer"]["environment"]
        )
        assert "FS_AUTH_COOKIE_SECURE" in services["fs-extension-installer"]["environment"]
        assert "FS_AUTO_INDEX_ENABLED" not in services["fs-extension-installer"]["environment"]
