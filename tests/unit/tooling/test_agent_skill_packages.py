"""The Operator and Developer agent skill packages install on their own.

Each package directory is a complete plugin root: its client manifests, its
icon, its skills, and a byte-identical copy of every shared skill. Everything
that is not edited by hand is rendered from one source
(`docs/agent-skills/packages.json`, `docs/agent-skills/shared/skills/`, the
product favicon) by `infra/tooling/agent-skills/sync_packages.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = ROOT / "docs" / "agent-skills"
SYNC_SCRIPT = ROOT / "infra" / "tooling" / "agent-skills" / "sync_packages.py"
ICON_SOURCE = ROOT / "assets" / "frontend" / "public" / "favicon.svg"

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
_SVG_NAMESPACE = 'xmlns="http://www.w3.org/2000/svg"'


def _sync_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_packages", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _package_files(package: str) -> list[Path]:
    return sorted(path for path in (SKILLS_DIR / package).rglob("*") if path.is_file())


def _copy_sources(tmp_path: Path) -> Path:
    shutil.copytree(SKILLS_DIR, tmp_path / "docs" / "agent-skills")
    icon = tmp_path / ICON_SOURCE.relative_to(ROOT)
    icon.parent.mkdir(parents=True)
    shutil.copy2(ICON_SOURCE, icon)
    return tmp_path


def _json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_the_retired_combined_package_is_gone() -> None:
    assert not (SKILLS_DIR / "flow-steward").exists()
    assert sorted(path.name for path in SKILLS_DIR.iterdir() if path.is_dir()) == sorted(
        [*PACKAGE_SKILLS, "shared", "publishing", ".claude-plugin"]
    )


def test_every_shared_copy_matches_its_single_source() -> None:
    sync = _sync_module()

    assert set(sync.PACKAGES) == set(PACKAGE_SKILLS)
    assert set(sync.shared_skill_names(ROOT)) == SHARED
    assert sync.drift(ROOT) == []
    assert sync.main(["--check", "--repo-root", str(ROOT)]) == 0


def test_sync_restores_a_drifted_or_extra_copy(tmp_path: Path) -> None:
    sync = _sync_module()
    _copy_sources(tmp_path)
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


def test_sync_restores_a_hand_edited_or_missing_generated_file(tmp_path: Path) -> None:
    sync = _sync_module()
    _copy_sources(tmp_path)
    operator = tmp_path / "docs/agent-skills/flow-steward-operator"
    native = operator / ".claude-plugin" / "plugin.json"
    native.write_text(native.read_text(encoding="utf-8").replace("1.0", "9.9"), encoding="utf-8")
    (operator / ".cursor-plugin" / "plugin.json").unlink()

    assert sync.drift(tmp_path) == [
        "docs/agent-skills/flow-steward-operator/.claude-plugin/plugin.json: "
        "differs from what docs/agent-skills/packages.json renders",
        "docs/agent-skills/flow-steward-operator/.cursor-plugin/plugin.json: missing",
    ]
    assert sorted(sync.sync(tmp_path)) == [
        "docs/agent-skills/flow-steward-operator/.claude-plugin/plugin.json",
        "docs/agent-skills/flow-steward-operator/.cursor-plugin/plugin.json",
    ]
    assert sync.drift(tmp_path) == []


def test_a_metadata_change_in_the_source_reaches_every_manifest(tmp_path: Path) -> None:
    sync = _sync_module()
    _copy_sources(tmp_path)
    sources_path = tmp_path / "docs/agent-skills/packages.json"
    sources = _json(sources_path)
    sources["packages"]["flow-steward-developer"]["version"] = "1.0.1"
    sources_path.write_text(json.dumps(sources), encoding="utf-8")

    assert sorted(sync.sync(tmp_path)) == [
        f"docs/agent-skills/flow-steward-developer/{name}"
        for name in (".claude-plugin/plugin.json", ".cursor-plugin/plugin.json", "plugin.json")
    ]
    root = tmp_path / "docs/agent-skills/flow-steward-developer"
    for manifest in ("plugin.json", ".claude-plugin/plugin.json", ".cursor-plugin/plugin.json"):
        assert _json(root / manifest)["version"] == "1.0.1"


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_package_manifests_name_the_package_and_agree(package: str) -> None:
    root = SKILLS_DIR / package
    portable = _json(root / "plugin.json")
    native = _json(root / ".claude-plugin" / "plugin.json")
    cursor = _json(root / ".cursor-plugin" / "plugin.json")
    spec = _json(SKILLS_DIR / "packages.json")["packages"][package]

    assert re.fullmatch(r"\d+\.\d+\.\d+", spec["version"])
    shared = ("name", "version", "description", "author", "license", "keywords")
    for manifest in (portable, native, cursor):
        assert {key: manifest[key] for key in shared} == {key: portable[key] for key in shared}
    assert portable["name"] == package
    assert portable["version"] == spec["version"]
    # The Agent Plugins 1.0.0 schema allows no other top-level property.
    assert set(portable) <= {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
    assert portable["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert native["displayName"] == spec["display_name"]


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_openai_directory_fields_fit_the_listing_limits(package: str) -> None:
    root = SKILLS_DIR / package
    interface = _json(root / "plugin.json")["extensions"]["com.openai"]["interface"]

    assert 0 < len(interface["displayName"]) <= 30
    assert 0 < len(interface["shortDescription"]) <= 30
    assert 0 < len(interface["longDescription"]) <= 4000
    assert 0 < len(interface["developerName"]) <= 80
    assert interface["category"]
    for icon in (interface["logo"], interface["composerIcon"]):
        assert icon.startswith("./") and (root / icon).is_file()


@pytest.mark.parametrize("package", sorted(PACKAGE_SKILLS))
def test_icon_is_the_product_favicon_on_a_square_canvas(package: str) -> None:
    icon = (SKILLS_DIR / package / "assets" / "icon.svg").read_bytes()
    source = ICON_SOURCE.read_bytes()
    box = re.search(rb'viewBox="(\S+) (\S+) (\S+) (\S+)"', icon)

    assert box is not None and box.group(3) == box.group(4)
    assert re.sub(rb'viewBox="[^"]*"', b"", icon) == re.sub(rb'viewBox="[^"]*"', b"", source)
    assert (
        _json(SKILLS_DIR / package / ".cursor-plugin" / "plugin.json")["logo"] == "assets/icon.svg"
    )
    assert (
        _json(SKILLS_DIR / package / ".claude-plugin" / "plugin.json")["icon"]
        == "./assets/icon.svg"
    )


def test_only_operator_declares_an_mcp_server_and_only_for_claude() -> None:
    operator = SKILLS_DIR / "flow-steward-operator"
    native = _json(operator / ".claude-plugin" / "plugin.json")
    options = native["userConfig"]
    server = native["mcpServers"]["flow-steward"]

    assert set(options) == {"mcp_url", "mcp_token"}
    assert all(option["required"] is True for option in options.values())
    assert options["mcp_token"]["sensitive"] is True
    assert "sensitive" not in options["mcp_url"]
    assert server == {
        "type": "http",
        "url": "${user_config.mcp_url}",
        "headers": {"Authorization": "Bearer ${user_config.mcp_token}"},
    }
    developer = _json(SKILLS_DIR / "flow-steward-developer" / ".claude-plugin" / "plugin.json")
    assert not {"userConfig", "mcpServers"} & set(developer)
    for package in PACKAGE_SKILLS:
        for manifest in ("plugin.json", ".cursor-plugin/plugin.json"):
            assert not {"mcpServers", "variables", "userConfig"} & set(
                _json(SKILLS_DIR / package / manifest)
            )


def test_claude_marketplace_lists_both_packages_from_their_folders() -> None:
    marketplace = _json(SKILLS_DIR / ".claude-plugin" / "marketplace.json")

    assert marketplace["name"] == "flow-steward"
    assert [entry["name"] for entry in marketplace["plugins"]] == list(PACKAGE_SKILLS)
    for entry in marketplace["plugins"]:
        assert entry["source"] == f"./{entry['name']}"
        assert "version" not in entry
        assert (SKILLS_DIR / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


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
        text = path.read_text(encoding="utf-8").replace(_SVG_NAMESPACE, "")
        assert "http://" not in text, path
        assert not re.search(r"\blocalhost\b|\b\d{1,3}(?:\.\d{1,3}){3}\b", text), path
        assert not re.search(r"Bearer\s+[A-Za-z0-9._~+/-]{8,}", text), path
