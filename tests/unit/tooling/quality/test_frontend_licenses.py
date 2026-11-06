"""Frontend production licenses and the notices that ship with the report renderer."""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.quality import frontend_licenses as licenses
from core.application.tooling.quality.check_registry import load_check_registry

ROOT = Path(__file__).resolve().parents[4]
REGISTRY_PATH = ROOT / "infra/tooling/quality/check-registry.json"
LINT_SCRIPT = ROOT / "core/entrypoints/cli/scripts/lint.sh"


def _workspace(tmp_path: Path, packages: dict[str, dict], roots: list[str]) -> Path:
    frontend = tmp_path / "assets/frontend"
    (frontend / "public").mkdir(parents=True)
    (frontend / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": {}, **packages}}), encoding="utf-8"
    )
    (frontend / "third-party-notices.json").write_text(
        json.dumps({"roots": roots}), encoding="utf-8"
    )
    for path, entry in packages.items():
        directory = frontend / path
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "LICENSE").write_text(
            f"{entry.get('license')} text of {path}", encoding="utf-8"
        )
    return tmp_path


PACKAGES = {
    "node_modules/chart": {
        "version": "2.0.0",
        "license": "Apache-2.0",
        "dependencies": {"render": "2.0.0", "helper": "1.0.0"},
        "peerDependencies": {"react": ">=18"},
    },
    "node_modules/chart/node_modules/helper": {"version": "1.0.0", "license": "0BSD"},
    "node_modules/render": {
        "version": "2.0.0",
        "license": "BSD-3-Clause",
        "dependencies": {"helper": "1"},
    },
    "node_modules/render/node_modules/helper": {"version": "1.0.0", "license": "0BSD"},
    "node_modules/helper": {"version": "9.9.9", "license": "MIT"},
    "node_modules/react": {"version": "19.0.0", "license": "MIT"},
    "node_modules/test-runner": {"version": "1.0.0", "license": "GPL-3.0", "dev": True},
    "node_modules/shim": {"link": True},
}


def test_the_closure_follows_node_resolution_and_skips_peers(tmp_path: Path) -> None:
    root = _workspace(tmp_path, PACKAGES, ["chart"])
    closure = licenses.production_closure(licenses._load_lock(root), ["chart"])
    assert [(item.name, item.version) for item in closure] == [
        ("chart", "2.0.0"),
        ("helper", "1.0.0"),
        ("helper", "1.0.0"),
        ("render", "2.0.0"),
    ]
    assert "react" not in {item.name for item in closure}


def test_dev_packages_and_local_links_are_not_license_checked(tmp_path: Path) -> None:
    root = _workspace(tmp_path, PACKAGES, ["chart"])
    assert licenses.license_problems(licenses._load_lock(root)) == []


def test_a_production_package_outside_the_allowlist_fails(tmp_path: Path) -> None:
    packages = {**PACKAGES, "node_modules/copyleft": {"version": "1.0.0", "license": "GPL-3.0"}}
    root = _workspace(tmp_path, packages, ["chart"])
    problems = licenses.license_problems(licenses._load_lock(root))
    assert problems == ["copyleft@1.0.0: license GPL-3.0 is not on the allowlist"]


def test_generated_notices_cover_the_closure_once_per_version(tmp_path: Path) -> None:
    root = _workspace(tmp_path, PACKAGES, ["chart"])
    assert licenses.cmd_generate(root) == 0
    text = (root / licenses.NOTICES).read_text(encoding="utf-8")
    assert text.count("### helper 1.0.0 (0BSD)") == 1
    assert "### chart 2.0.0 (Apache-2.0)" in text
    assert "Apache-2.0 text of node_modules/chart" in text
    assert licenses.notice_problems(root) == []


def test_missing_and_stale_notices_are_reported(tmp_path: Path) -> None:
    root = _workspace(tmp_path, PACKAGES, ["chart"])
    licenses.cmd_generate(root)
    notices = root / licenses.NOTICES
    text = notices.read_text(encoding="utf-8").replace("### render 2.0.0", "### render 1.0.0")
    notices.write_text(text, encoding="utf-8")
    problems = licenses.notice_problems(root)
    assert any("no notice for render@2.0.0" in problem for problem in problems)
    assert any(
        "render@1.0.0 (BSD-3-Clause) is not in the locked closure" in problem
        for problem in problems
    )
    assert licenses.cmd_check(root) == 1


def test_the_repository_notices_are_complete() -> None:
    assert licenses.cmd_check(ROOT) == 0


def test_the_check_has_one_tier_two_owner_and_a_real_dispatch() -> None:
    registry = load_check_registry(REGISTRY_PATH)
    check = next(item for item in registry.checks if item.id == "frontend-licenses")
    assert check.owner_tier == 2
    assert check.command == (
        "bash",
        "core/entrypoints/cli/scripts/lint.sh",
        "--checks",
        "frontend-licenses",
    )
    script = LINT_SCRIPT.read_text(encoding="utf-8")
    assert "      frontend-licenses)\n        run_check_frontend_licenses" in script
    body = script.split("run_check_frontend_licenses() {", 1)[1].split("\n}\n", 1)[0]
    assert '"${PYTHON_BIN}" -m core.application.tooling.quality.frontend_licenses check' in body
