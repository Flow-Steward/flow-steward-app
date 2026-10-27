"""Bundling a stdlib-only bundle stays offline and ships no wheels.

The subject is the platform's bundler, so the bundle is built here: pointing at a
shipped one made this a statement about that extension's dependency closure, which
is the extension's business and changes without notice.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml

from core.application.tooling.extensions import bundle_extension as bundle_module
from core.infrastructure.extension_runtime.bundled_dependencies import (
    materialize_bundled_extension_dependencies,
)
from core.infrastructure.extension_runtime.extension_runtime import (
    run_extension_health,
    scan_extensions,
)
from tests.support.extension_icon import write_extension_icon

EXTENSION_ID = "acme.stdlib-only"
BUNDLE_DIR_NAME = "acme_stdlib_only"


def _write_stdlib_only_bundle(extensions_root: Path) -> Path:
    bundle = extensions_root / BUNDLE_DIR_NAME
    (bundle / "ui").mkdir(parents=True, exist_ok=True)
    (bundle / "extension.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": EXTENSION_ID,
                "kind": "tool_provider",
                "version": "1.0.0",
                "display_name": "Acme Stdlib Only",
                "description": "Test-only bundle with an empty dependency closure.",
                "vendor": {"name": "Acme Test"},
                "features": ["tool"],
                "required_scopes": ["extension:invoke"],
                "entrypoint": {"command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"]},
                "ui_manifest": "ui/ui_manifest.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n", encoding="utf-8")
    (bundle / "README.md").write_text("# Acme Stdlib Only\n", encoding="utf-8")
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    write_extension_icon(bundle)
    return bundle


def test_clean_bundle_validates_empty_stdlib_dependency_closure_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extensions_root = tmp_path / "extensions"
    _write_stdlib_only_bundle(extensions_root)
    monkeypatch.setenv("FS_EXTENSION_DEPS_FETCH", "off")
    monkeypatch.setattr(
        bundle_module,
        "download_wheels_for_requirements",
        lambda **_kwargs: pytest.fail("empty stdlib closure must not download wheels"),
    )

    result = bundle_module.bundle_extension(
        bundle_module.ExtensionBundleRequest(
            extension_id=EXTENSION_ID,
            root=extensions_root,
            out_dir=tmp_path / "dist",
            with_wheels=True,
            platforms="manylinux2014_x86_64,manylinux2014_aarch64",
        )
    )

    assert result.status == "bundled", result.output
    archive_path = next((tmp_path / "dist").glob("*.zip"))
    with zipfile.ZipFile(archive_path) as archive:
        assert f"{BUNDLE_DIR_NAME}/README.md" in archive.namelist()
        archive.extractall(tmp_path / "unpacked")

    unpacked_root = tmp_path / "unpacked"
    evidence = materialize_bundled_extension_dependencies(unpacked_root)
    scanned = scan_extensions(unpacked_root)

    assert evidence == []
    assert len(scanned) == 1
    assert scanned[0]["compiled_manifest"]["python_requirements"] == []
    assert not (unpacked_root / BUNDLE_DIR_NAME / "wheels").exists()
    assert run_extension_health(scanned[0])["status"] == "healthy"
