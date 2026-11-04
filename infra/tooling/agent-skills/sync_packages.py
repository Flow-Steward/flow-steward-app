#!/usr/bin/env python3
"""Generate every agent skill package file that has a canonical source.

Each package directory under `docs/agent-skills/<package>/` must stay a
self-contained plugin root that a client can install on its own. The files a
person edits are elsewhere; this tool writes the rest from them:

- `docs/agent-skills/shared/skills/<name>/` is the one source of a skill that
  more than one package ships; each package carries a byte-identical copy.
- `docs/agent-skills/packages.json` is the one source of package metadata. The
  client manifests are rendered from it: the Agent Plugins `plugin.json`
  (OpenAI directory metadata under `extensions`), `.claude-plugin/plugin.json`,
  `.cursor-plugin/plugin.json` and the Claude marketplace file
  `docs/agent-skills/.claude-plugin/marketplace.json`.
- The package icon `assets/icon.svg` is the product favicon on a square canvas.

- `assets/frontend/src/pages/settings/account/agent-skill-packages.generated.json`
  is the Operator package as text for the web UI: the AI agents page builds a
  personal archive from it in the browser, with the installation's address and
  the user's token, which never travel to the server. It is written only into a
  checkout that has the web UI.

`gemini-extension.json` is rendered only into release archives
(`build_release.py`), because no gallery indexes this repository.

Run without arguments after editing a source; `--check` only reports drift and
exits 1 when a generated file differs from what its source renders.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = Path("docs/agent-skills")
SHARED_SKILLS = SKILLS_DIR / "shared" / "skills"
SOURCES_FILE = SKILLS_DIR / "packages.json"
PACKAGES = ("flow-steward-operator", "flow-steward-developer")
MARKETPLACE_FILE = SKILLS_DIR / ".claude-plugin" / "marketplace.json"
PACKAGE_ICON = Path("assets/icon.svg")
WEB_UI_PACKAGE_JSON = Path("assets/frontend/package.json")
WEB_BUNDLE_FILE = Path(
    "assets/frontend/src/pages/settings/account/agent-skill-packages.generated.json"
)
#: The packages the AI agents page builds personal archives from.
WEB_BUNDLE_PACKAGES = ("flow-steward-operator",)
#: Local leftovers that are never part of a package.
_NOT_PACKAGE_FILES = ("__pycache__", ".DS_Store")
AGENT_PLUGINS_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
OPENAI_EXTENSION = "com.openai"
_VIEWBOX = re.compile(rb'viewBox="0 0 ([0-9.]+) ([0-9.]+)"')


def _tree(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _json_bytes(data: object) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def load_sources(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    sources: dict[str, Any] = json.loads((repo_root / SOURCES_FILE).read_text(encoding="utf-8"))
    if tuple(sources["packages"]) != PACKAGES:
        raise ValueError(f"{SOURCES_FILE} must describe exactly {', '.join(PACKAGES)} in order")
    return sources


def shared_skill_names(repo_root: Path = REPO_ROOT) -> list[str]:
    source = repo_root / SHARED_SKILLS
    return sorted(path.name for path in source.iterdir() if path.is_dir())


def square_icon(svg: bytes) -> bytes:
    """The favicon centred on a square canvas, so catalogs can show it as an icon."""
    match = _VIEWBOX.search(svg)
    if match is None:
        raise ValueError('the icon source needs a viewBox="0 0 <width> <height>"')
    width, height = float(match.group(1)), float(match.group(2))
    side = max(width, height)

    def number(value: float) -> str:
        return f"{value + 0.0:g}"  # + 0.0 turns -0.0 into 0

    box = (
        f'viewBox="{number((width - side) / 2)} {number((height - side) / 2)} '
        f'{number(side)} {number(side)}"'
    )
    return svg[: match.start()] + box.encode("ascii") + svg[match.end() :]


def _common(sources: dict[str, Any], package: str) -> dict[str, Any]:
    spec = sources["packages"][package]
    return {
        "name": package,
        "version": spec["version"],
        "description": spec["description"],
        "author": sources["author"],
        "homepage": sources["homepage"],
        "repository": sources["repository"],
        "license": sources["license"],
        "keywords": spec["keywords"],
    }


def portable_manifest(sources: dict[str, Any], package: str) -> dict[str, Any]:
    """Agent Plugins 1.0.0 manifest; the OpenAI directory reads its `extensions` entry."""
    spec = sources["packages"][package]
    icon = f"./{PACKAGE_ICON.as_posix()}"
    return {
        "$schema": AGENT_PLUGINS_SCHEMA,
        **_common(sources, package),
        "extensions": {
            OPENAI_EXTENSION: {
                "interface": {
                    "displayName": spec["display_name"],
                    "shortDescription": spec["short_description"],
                    "longDescription": spec["long_description"],
                    "developerName": sources["author"]["name"],
                    "category": spec["category"],
                    "logo": icon,
                    "composerIcon": icon,
                }
            }
        },
    }


def _claude_mcp(connection: dict[str, Any]) -> dict[str, Any]:
    url_option = connection["url_option"]
    token_option = connection["token_option"]
    return {
        "userConfig": {
            url_option["key"]: {
                "type": "string",
                "title": url_option["title"],
                "description": url_option["description"],
                "required": True,
            },
            token_option["key"]: {
                "type": "string",
                "title": token_option["title"],
                "description": token_option["description"],
                "sensitive": True,
                "required": True,
            },
        },
        "mcpServers": {
            connection["server"]: {
                "type": "http",
                "url": "${user_config." + url_option["key"] + "}",
                "headers": {"Authorization": "Bearer ${user_config." + token_option["key"] + "}"},
            }
        },
    }


def claude_manifest(sources: dict[str, Any], package: str) -> dict[str, Any]:
    """`.claude-plugin/plugin.json`; Operator asks for its MCP address and token at install."""
    spec = sources["packages"][package]
    common = _common(sources, package)
    manifest: dict[str, Any] = {
        "name": common.pop("name"),
        "displayName": spec["display_name"],
        **common,
        "icon": f"./{PACKAGE_ICON.as_posix()}",
    }
    if "mcp_connection" in spec:
        manifest.update(_claude_mcp(spec["mcp_connection"]))
    return manifest


def cursor_manifest(sources: dict[str, Any], package: str) -> dict[str, Any]:
    """`.cursor-plugin/plugin.json`; skills only, the MCP server is connected by the user."""
    return {**_common(sources, package), "logo": PACKAGE_ICON.as_posix()}


def gemini_manifest(sources: dict[str, Any], package: str) -> dict[str, Any]:
    """`gemini-extension.json` for the release archive; skills only."""
    spec = sources["packages"][package]
    return {"name": package, "version": spec["version"], "description": spec["description"]}


def claude_marketplace(sources: dict[str, Any]) -> dict[str, Any]:
    marketplace = sources["marketplace"]
    return {
        "name": marketplace["name"],
        "owner": sources["author"],
        "description": marketplace["description"],
        "plugins": [
            {
                "name": package,
                "source": f"./{package}",
                "description": spec["description"],
                "category": spec["category"].lower(),
            }
            for package, spec in sources["packages"].items()
        ],
    }


def generated_files(repo_root: Path = REPO_ROOT) -> dict[str, bytes]:
    """Every rendered file, keyed by its repository-relative POSIX path."""
    sources = load_sources(repo_root)
    icon = square_icon((repo_root / sources["icon_source"]).read_bytes())
    files = {MARKETPLACE_FILE.as_posix(): _json_bytes(claude_marketplace(sources))}
    for package in PACKAGES:
        root = SKILLS_DIR / package
        files[(root / "plugin.json").as_posix()] = _json_bytes(portable_manifest(sources, package))
        files[(root / ".claude-plugin" / "plugin.json").as_posix()] = _json_bytes(
            claude_manifest(sources, package)
        )
        files[(root / ".cursor-plugin" / "plugin.json").as_posix()] = _json_bytes(
            cursor_manifest(sources, package)
        )
        files[(root / PACKAGE_ICON).as_posix()] = icon
    if (repo_root / WEB_UI_PACKAGE_JSON).is_file():
        files[WEB_BUNDLE_FILE.as_posix()] = web_bundle(repo_root, files)
    return files


def web_bundle(repo_root: Path, generated: dict[str, bytes]) -> bytes:
    """The web UI's copy of each bundled package, as its files will be after a sync."""
    packages: dict[str, dict[str, str]] = {}
    for package in WEB_BUNDLE_PACKAGES:
        root = SKILLS_DIR / package
        tree = {
            relative: content
            for relative, content in _tree(repo_root / root).items()
            if not any(part in _NOT_PACKAGE_FILES for part in relative.split("/"))
        }
        for name in shared_skill_names(repo_root):
            prefix = f"skills/{name}/"
            tree = {key: value for key, value in tree.items() if not key.startswith(prefix)}
            tree.update(
                {
                    prefix + key: value
                    for key, value in _tree(repo_root / SHARED_SKILLS / name).items()
                }
            )
        package_prefix = root.as_posix() + "/"
        for relative, content in generated.items():
            if relative.startswith(package_prefix):
                tree[relative.removeprefix(package_prefix)] = content
        packages[package] = {key: tree[key].decode("utf-8") for key in sorted(tree)}
    return _json_bytes(
        {
            "generated_by": "infra/tooling/agent-skills/sync_packages.py",
            "marketplace": generated[MARKETPLACE_FILE.as_posix()].decode("utf-8"),
            "packages": packages,
        }
    )


def drift(repo_root: Path = REPO_ROOT) -> list[str]:
    """Every generated file or skill copy that differs from its source, as readable lines."""
    problems: list[str] = []
    for name in shared_skill_names(repo_root):
        expected = _tree(repo_root / SHARED_SKILLS / name)
        for package in PACKAGES:
            target = SKILLS_DIR / package / "skills" / name
            actual = _tree(repo_root / target)
            for relative in sorted(expected.keys() | actual.keys()):
                if relative not in actual:
                    problems.append(f"{target.as_posix()}/{relative}: missing")
                elif relative not in expected:
                    problems.append(f"{target.as_posix()}/{relative}: not in the shared source")
                elif actual[relative] != expected[relative]:
                    problems.append(f"{target.as_posix()}/{relative}: differs from the source")
    for relative, content in generated_files(repo_root).items():
        path = repo_root / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
        elif path.read_bytes() != content:
            problems.append(f"{relative}: differs from what {SOURCES_FILE.as_posix()} renders")
    return problems


def sync(repo_root: Path = REPO_ROOT) -> list[str]:
    """Rewrite every drifted skill copy and generated file; return what was written."""
    written: list[str] = []
    for name in shared_skill_names(repo_root):
        source = repo_root / SHARED_SKILLS / name
        for package in PACKAGES:
            target = repo_root / SKILLS_DIR / package / "skills" / name
            if _tree(target) == _tree(source):
                continue
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            written.append(target.relative_to(repo_root).as_posix())
    for relative, content in generated_files(repo_root).items():
        path = repo_root / relative
        if path.is_file() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written.append(relative)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report drift, change nothing")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    if args.check:
        problems = drift(args.repo_root)
        for line in problems:
            print(line, file=sys.stderr)
        if problems:
            print("run infra/tooling/agent-skills/sync_packages.py", file=sys.stderr)
            return 1
        return 0
    for line in sync(args.repo_root):
        print(f"updated {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
