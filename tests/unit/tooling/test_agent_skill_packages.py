"""The Operator and Developer agent skill packages install on their own.

Each package directory is a complete plugin root: its manifests, its skills,
and a byte-identical copy of every shared skill, kept from one source in
`docs/agent-skills/shared/skills/` by `infra/tooling/agent-skills/sync_shared_skills.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = ROOT / "docs" / "agent-skills"
SYNC_SCRIPT = ROOT / "infra" / "tooling" / "agent-skills" / "sync_shared_skills.py"

SHARED = {"flow-steward-report-issue"}
PACKAGE_SKILLS = {
    "flow-steward-operator": {
        "flow-steward-workflow-authoring",
        "flow-steward-publish-workflow",
    }
    | SHARED,
    "flow-steward-developer": {
        "flow-steward-extension-authoring",
        "flow-steward-publish-extension",
        "flow-steward-contribute-code",
    }
    | SHARED,
}
ALL_SKILLS = set().union(*PACKAGE_SKILLS.values())
_FRONTMATTER_NAME = re.compile(r"\A---\nname: ([a-z0-9-]+)\ndescription: .+?\n---\n", re.S)
_MARKDOWN_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def _sync_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_shared_skills", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package_files(package: str) -> list[Path]:
    return sorted(path for path in (SKILLS_DIR / package).rglob("*") if path.is_file())


def test_the_retired_combined_package_is_gone() -> None:
    assert not (SKILLS_DIR / "flow-steward").exists()
    assert sorted(path.name for path in SKILLS_DIR.iterdir() if path.is_dir()) == sorted(
        [*PACKAGE_SKILLS, "shared"]
    )


def test_every_shared_copy_matches_its_single_source() -> None:
    sync = _sync_module()

    assert set(sync.PACKAGES) == set(PACKAGE_SKILLS)
    assert set(sync.shared_skill_names(ROOT)) == SHARED
    assert sync.drift(ROOT) == []


def test_sync_restores_a_drifted_or_extra_copy(tmp_path: Path) -> None:
    sync = _sync_module()
    shutil.copytree(SKILLS_DIR, tmp_path / "docs" / "agent-skills")
    operator_copy = (
        tmp_path / "docs/agent-skills/flow-steward-operator/skills/flow-steward-report-issue"
    )
    (operator_copy / "SKILL.md").write_text("edited copy\n", encoding="utf-8")
    (operator_copy / "stray.md").write_text("only here\n", encoding="utf-8")
    shutil.rmtree(tmp_path / "docs/agent-skills/flow-steward-developer/skills" / next(iter(SHARED)))

    assert len(sync.drift(tmp_path)) == 3
    assert sync.main(["--check", "--repo-root", str(tmp_path)]) == 1

    assert sorted(sync.sync(tmp_path)) == [
        "docs/agent-skills/flow-steward-developer/skills/flow-steward-report-issue",
        "docs/agent-skills/flow-steward-operator/skills/flow-steward-report-issue",
    ]
    assert sync.drift(tmp_path) == []
    assert sync.sync(tmp_path) == []


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_package_manifests_name_the_package_and_agree(package: str) -> None:
    root = SKILLS_DIR / package
    portable = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    native = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert portable["name"] == package
    assert {key: value for key, value in portable.items() if key != "$schema"} == native
    assert set(native) == {"name", "version", "description", "author"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", native["version"])


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_package_ships_exactly_its_skills_under_their_own_names(package: str) -> None:
    skills = SKILLS_DIR / package / "skills"

    assert {path.name for path in skills.iterdir()} == PACKAGE_SKILLS[package]
    for name in PACKAGE_SKILLS[package]:
        match = _FRONTMATTER_NAME.match((skills / name / "SKILL.md").read_text(encoding="utf-8"))
        assert match, f"{package}/{name}: SKILL.md needs name and description front matter"
        assert match.group(1) == name


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_package_needs_no_skill_from_the_other_package(package: str) -> None:
    foreign = ALL_SKILLS - PACKAGE_SKILLS[package]
    for path in _package_files(package):
        text = path.read_text(encoding="utf-8")
        named = {name for name in foreign if name in text}
        assert not named, f"{path.relative_to(ROOT)} names {sorted(named)}"


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_relative_links_stay_inside_the_package(package: str) -> None:
    root = (SKILLS_DIR / package).resolve()
    for path in _package_files(package):
        if path.suffix != ".md":
            continue
        for target in _MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("mailto:"):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.is_relative_to(root), f"{path.relative_to(ROOT)} -> {target}"
            assert resolved.exists(), f"{path.relative_to(ROOT)} -> {target}"


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_package_carries_no_endpoint_token_or_server_configuration(package: str) -> None:
    names = {path.name for path in _package_files(package)}
    assert not names & {".mcp.json", "mcp.json", ".env"}
    for path in _package_files(package):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, path
        assert not re.search(r"\blocalhost\b|\b\d{1,3}(?:\.\d{1,3}){3}\b", text), path
        assert not re.search(r"Bearer\s+[A-Za-z0-9._~+/-]{8,}", text), path
