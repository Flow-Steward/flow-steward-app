from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The isolated core-only workspace contains shipped sources without local installs.
SDK_CONTRACT = ROOT / "scripts/release/sync_extension_sdk_wheel.py"


def test_canonical_sdk_wheel_matches_every_shipped_extension() -> None:
    result = subprocess.run(
        [sys.executable, str(SDK_CONTRACT), "--root", str(ROOT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_shipped_extensions_do_not_import_private_sdk_symbols() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "extensions").glob("*/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not str(node.module or "").startswith("flowsteward_extension_sdk"):
                continue
            private = sorted(alias.name for alias in node.names if alias.name.startswith("_"))
            if private:
                violations.append(f"{path.relative_to(ROOT)}: {', '.join(private)}")

    assert violations == []


def test_shipped_extensions_do_not_import_monorepo_packages() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "extensions").glob("*/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(node.module or "").split(".", 1)[0] in {
                "core",
                "extensions",
            }:
                violations.append(f"{path.relative_to(ROOT)}: from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in {"core", "extensions"}:
                        violations.append(f"{path.relative_to(ROOT)}: import {alias.name}")

    assert violations == []


def test_sdk_contract_reports_source_expected_and_stale_wheel_identities(tmp_path: Path) -> None:
    shutil.copytree(
        ROOT / "core/infrastructure/extension_sdk/public",
        tmp_path / "core/infrastructure/extension_sdk/public",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    # Core vendors the wheel into no extension; a checkout that still does is kept in
    # step, so a synthetic consumer carries the wheel the script itself builds.
    built = subprocess.run(
        [
            sys.executable,
            str(SDK_CONTRACT),
            "--root",
            str(tmp_path),
            "--out",
            str(tmp_path / "dist"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "written" in built.stdout
    wheel = next((tmp_path / "dist").glob("flowsteward_extension_sdk-*.whl"))
    consumer = tmp_path / "extensions" / "acme_consumer"
    (consumer / "dev-wheels").mkdir(parents=True)
    (consumer / "extension.yaml").write_text("extension_id: acme.consumer\n", encoding="utf-8")
    (consumer / "requirements-dev.txt").write_text(f"./dev-wheels/{wheel.name}\n", encoding="utf-8")
    shutil.copy2(wheel, consumer / "dev-wheels" / wheel.name)

    stale_wheel = consumer / "dev-wheels" / wheel.name
    stale_wheel.write_bytes(stale_wheel.read_bytes() + b"stale")
    result = subprocess.run(
        [sys.executable, str(SDK_CONTRACT), "--root", str(tmp_path), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "stale SDK wheel" in result.stderr
    assert "source=" in result.stderr
    assert "expected=" in result.stderr
    assert "actual=" in result.stderr
