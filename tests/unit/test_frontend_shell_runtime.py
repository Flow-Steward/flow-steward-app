import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.entrypoints.web.frontend_shell import create_registered_frontend_shell_router
from core.infrastructure.runtime.frontend_shell_runtime import (
    build_frontend_shell_artifacts,
    frontend_shell_available,
    manifest_asset_paths,
    resolve_frontend_asset,
)


def test_build_frontend_shell_artifacts_uses_expected_layout() -> None:
    artifacts = build_frontend_shell_artifacts("assets/frontend/dist")

    assert artifacts.index_file == Path("assets/frontend/dist") / ("index." + "html")
    assert artifacts.assets_root == Path("assets/frontend/dist/assets")
    assert artifacts.manifest_file == Path("assets/frontend/dist/.vite/manifest.json")


def test_frontend_shell_availability_requires_index_and_assets(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    artifacts = build_frontend_shell_artifacts(dist)

    assert frontend_shell_available(artifacts) is False

    (dist / "assets").mkdir()
    (dist / ("index." + "html")).write_text("<!doctype html>", encoding="utf-8")

    assert frontend_shell_available(artifacts) is True


def test_manifest_asset_paths_collects_vite_outputs() -> None:
    manifest = {
        "src/main.tsx": {
            "file": "assets/main-abc123.js",
            "css": ["assets/main-abc123.css"],
            "assets": ["assets/logo-xyz.svg"],
        }
    }

    assert manifest_asset_paths(manifest) == {
        "main-abc123.js",
        "main-abc123.css",
        "logo-xyz.svg",
    }


def test_resolve_frontend_asset_stays_inside_assets_root(tmp_path: Path) -> None:
    assets_root = tmp_path / "dist" / "assets"
    assets_root.mkdir(parents=True)
    target = assets_root / "main.js"
    target.write_text("console.log('ok')", encoding="utf-8")
    artifacts = build_frontend_shell_artifacts(tmp_path / "dist")

    resolved = resolve_frontend_asset(artifacts, "main.js", manifest_paths={"main.js"})
    blocked = resolve_frontend_asset(artifacts, "../outside.js", manifest_paths={"../outside.js"})

    assert resolved == target.resolve()
    assert blocked is None


def test_registered_frontend_shell_router_serves_explicit_private_paths(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / ("index." + "html")).write_text("<!doctype html><title>Shell</title>", encoding="utf-8")
    artifacts = build_frontend_shell_artifacts(dist)
    app = FastAPI()
    app.include_router(
        create_registered_frontend_shell_router(
            registered_paths=("/private/fixture",),
            build_artifacts=lambda: artifacts,
            frontend_shell_available=frontend_shell_available,
        )
    )

    response = TestClient(app).get("/private/fixture")

    assert response.status_code == 200
    assert "<title>Shell</title>" in response.text


def test_frontend_shell_bootstrap_does_not_use_synchronous_xhr() -> None:
    paths = [Path("assets/frontend") / ("index." + "html")]
    dist_index = Path("assets/frontend/dist") / ("index." + "html")
    if dist_index.exists():
        paths.append(dist_index)

    for path in paths:
        html = path.read_text(encoding="utf-8")
        assert "XMLHttpRequest" not in html
        assert not re.search(r"\.open\([^)]*,\s*false\s*\)", html)


def test_frontend_bootstrap_loads_platform_kit_from_static_mount() -> None:
    source = Path("assets/frontend/src/main.tsx").read_text(encoding="utf-8")

    assert '"/static/platform-kit/index.js"' in source
    assert "/app/static/platform-kit/" not in source
