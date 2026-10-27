from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_extension_runtime_has_one_canonical_public_module() -> None:
    runtime_package = ROOT / "core/infrastructure/extension_runtime"
    runtime_module = runtime_package / "extension_runtime.py"

    assert runtime_module.is_file()
    assert not (runtime_module.parent / "extension_runtime_exports.py").exists()
    assert "Compatibility facade" not in runtime_module.read_text(encoding="utf-8")
    assert "__dict__.update" not in runtime_module.read_text(encoding="utf-8")

    for path in runtime_package.glob("extension_runtime*.py"):
        assert "_extension_runtime_facade_global" not in path.read_text(encoding="utf-8"), path


def test_extension_runtime_entrypoints_work_when_imported_directly() -> None:
    script = """
from core.infrastructure.extension_runtime import extension_runtime_entrypoints as entrypoints

entrypoints.run_extension_invocation = lambda *_args, **_kwargs: {
    "status": "succeeded",
    "detail": "",
    "response": {"ok": True},
}
extension = {
    "extension_id": "direct-import",
    "runtime": {"contract_version": "extension_host_v2"},
    "action_definitions": [
        {
            "action_id": "sync",
            "handler": {"mode": "extension"},
        }
    ],
}
result = entrypoints.run_extension_action(extension, action_id="sync")
assert result["status"] == "succeeded"
assert result["operation_id"] == "sync"
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_http_runtime_has_no_global_state_singleton() -> None:
    runtime_dir = ROOT / "core/infrastructure/runtime"

    assert not (runtime_dir / "http_runtime_state.py").exists()
    for path in ROOT.glob("core/**/*.py"):
        assert "http_runtime_state" not in path.read_text(encoding="utf-8"), path


def test_production_workflow_modules_have_no_legacy_monkeypatch_lookup() -> None:
    for path in ROOT.glob("core/entrypoints/admin/workflows*.py"):
        assert "_workflow_facade_global" not in path.read_text(encoding="utf-8"), path


def test_pytest_runtime_uses_explicit_app_dependencies_without_facade_sync() -> None:
    test_surface = (ROOT / "tests/api_runtime_test_surface.py").read_text(encoding="utf-8")
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")

    assert "_sync_split_module_globals" not in test_surface
    assert "_HttpAppFacadeModule" not in test_surface
    assert "_initialize_api_runtime_test_surface" not in conftest
