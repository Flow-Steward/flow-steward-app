from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.application.extensions.extension_descriptor import (
    LLM_PROVIDER_CONTRACT_SCHEMA_VERSION,
)
from core.application.settings.model_catalog import PROVIDER_CATALOG


def _canonical_repo_root() -> Path:
    """The checkout that has Git metadata.

    This guard reads the Git index, and the core-only runner copies the tree
    into a workspace without `.git` on purpose. Pointed at that copy, `git`
    fails and the guard reported "run it inside a checkout" — which was both
    wrong and the opposite of actionable. The runner exports the real path.
    """
    supplied = os.environ.get("PYTEST_SOURCE_REPO_ROOT", "").strip()
    return Path(supplied).resolve() if supplied else Path(__file__).resolve().parents[2]


ROOT = _canonical_repo_root()
TEMPORARY_ORCHESTRATION_ARTIFACTS = {
    Path("docs/superpowers/specs/2026-08-29-bundled-cli-executor-purge-design.md"),
    Path("docs/superpowers/plans/2026-08-29-bundled-cli-executor-core-purge.md"),
}
_EXTERNAL_AGENT_RUNTIME = "co" + "dex"
ALLOWED_NON_PRODUCT_METADATA_LINES = {
    Path(".gitignore"): {
        f"# {_EXTERNAL_AGENT_RUNTIME.title()} container runtime, not product state".lower().encode(
            "utf-8"
        ),
        f".{_EXTERNAL_AGENT_RUNTIME}-docker/".encode(),
    }
}
ALLOWED_HISTORICAL_PROVENANCE_LINES = {
    # Source branch identities and agent tooling constraints in this completed
    # integration plan are provenance, not a shipped executor implementation.
    Path("docs/superpowers/plans/2026-09-27-compact-provider-runtime-main-integration.md"): {
        7,
        24,
        39,
        44,
        46,
        47,
        165,
    },
    Path("docs/superpowers/plans/2026-08-25-registry-tools-design-audit-fixes.md"): {
        5,
        6,
        7,
        261,
        387,
        388,
    },
    Path("docs/superpowers/plans/2026-08-26-tools-disclosure-key-value.md"): {
        5,
        52,
        53,
        141,
    },
    Path("docs/superpowers/plans/2026-08-27-tools-parameter-chip.md"): {5, 149},
    Path("docs/superpowers/plans/2026-08-30-generic-extension-owned-oauth.md"): {15},
    Path("docs/superpowers/specs/2026-08-24-registry-tools-detail-transfer-design.md"): {573},
    Path("docs/superpowers/specs/2026-08-26-registry-changelog-panel-transfer-design.md"): {
        5,
        33,
    },
    Path("docs/superpowers/specs/2026-08-27-registry-agents-detail-transfer-design.md"): {
        7,
        46,
    },
    Path("docs/superpowers/specs/2026-08-30-generic-extension-owned-oauth-design.md"): {
        482,
        485,
    },
    # These immutable design specs record the historical source branches used
    # for their already-completed workspace-transfer reviews.
    Path("docs/superpowers/specs/2026-09-04-registry-workspaces-detail-transfer-design.md"): {
        6,
        19,
    },
    Path("docs/superpowers/specs/2026-09-04-registry-workspaces-list-transfer-design.md"): {
        6,
    },
    Path("docs/superpowers/specs/2026-09-14-registry-data-connections-transfer-design.md"): {
        11,
        23,
    },
    Path("docs/superpowers/plans/2026-09-29-recover-unmerged-behavior.md"): {15},
    Path("docs/superpowers/plans/2026-09-29-source-release-transition.md"): {
        15,
        16,
        26,
        270,
        272,
    },
}
# External developer clients load these portable skills themselves; Flow Steward
# does not bundle their executors. Only the reviewed full lines below (including
# this plan's branch provenance) are allowed. A changed line, extra instruction,
# or runtime/module path still goes through the retired-surface scan.
_EXTERNAL_CODE_CLIENT = "Clau" + "de Code"
ALLOWED_TOOLKIT_DOCUMENTATION_LINES = {
    Path("docs/GUIDE.md"): {
        f"Follow the package README's supported {_EXTERNAL_AGENT_RUNTIME.title()} local marketplace, Cursor local".lower().encode(
            "utf-8"
        ),
    },
    Path("docs/agent-skills/flow-steward/README.md"): {
        line.lower().encode("utf-8")
        for line in (
            f"`plugin.json` targets Agent Plugins 1.0.0 for {_EXTERNAL_AGENT_RUNTIME.title()} and Cursor;",
            f"`.claude-plugin/plugin.json` provides {_EXTERNAL_CODE_CLIENT} metadata. Both discover the",
            f"### {_EXTERNAL_AGENT_RUNTIME.title()}",
            f"{_EXTERNAL_AGENT_RUNTIME} plugin marketplace add .",
            f"{_EXTERNAL_AGENT_RUNTIME} plugin list --marketplace flow-steward-local --available --json",
            f"{_EXTERNAL_AGENT_RUNTIME} plugin add flow-steward@flow-steward-local",
            f"marketplace. [{_EXTERNAL_AGENT_RUNTIME.title()} packaging documentation](https://developers.openai.com/plugins/build/plugins)",
            f"### {_EXTERNAL_CODE_CLIENT}",
            f"{_EXTERNAL_AGENT_RUNTIME.title()} `.agents/skills/` or `~/.agents/skills/`; Cursor `.agents/skills/`,",
            f"`$flow-steward-extension-authoring` in {_EXTERNAL_AGENT_RUNTIME.title()} and the corresponding `/` names",
            f"Native paths are documented for [{_EXTERNAL_AGENT_RUNTIME.title()}](https://developers.openai.com/{_EXTERNAL_AGENT_RUNTIME}/skills),",
            f"again. {_EXTERNAL_AGENT_RUNTIME.title()} loads a cached installed copy; confirm the refreshed install's bytes",
        )
    },
    Path("docs/extension-authoring-agent-playbook.md"): {
        f"native local {_EXTERNAL_AGENT_RUNTIME.title()} and {_EXTERNAL_CODE_CLIENT} skill directories. After an authorized".lower().encode(
            "utf-8"
        ),
    },
    Path("docs/superpowers/specs/2026-10-06-developer-toolkit-workflow-authoring-design.md"): {
        f"installation in {_EXTERNAL_AGENT_RUNTIME.title()} and {_EXTERNAL_CODE_CLIENT}. Validate loading in both clients using".lower().encode(
            "utf-8"
        ),
    },
}
SCANNED_TREES = (
    "assets",
    "core",
    "db",
    "docs",
    "examples",
    "extensions",
    "infra",
    "scripts",
    "tests",
)
EXCLUDED_PATH_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "node_modules",
}
ROOT_MANIFEST_NAMES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".secrets.baseline",
    "README.md",
    "pyproject.toml",
}
ROOT_MANIFEST_SUFFIXES = (".lock", ".txt", ".yaml", ".yml")


def _retired_fragments() -> tuple[bytes, ...]:
    first = "co" + "dex"
    second = "clau" + "de"
    preview_tool = "preview" + "_start"
    preview_module = "platform" + "_preview.py"
    return tuple(
        token.lower().encode("utf-8")
        for token in (
            first,
            f"{second} cli",
            f"{second} code",
            f"{second}_cli",
            f"{second}.md",
            f"{second}_home",
            f"@anthropic-ai/{second}-code",
            preview_tool,
            preview_module,
            f".{second}/launch.json",
        )
    )


def _binary_retired_markers() -> tuple[bytes, ...]:
    first = "co" + "dex"
    second = "clau" + "de"
    return tuple(
        marker.lower().encode("utf-8")
        for marker in (
            f"{first}_cli",
            f"{second}_cli",
            f"@openai/{first}",
            f"@anthropic-ai/{second}-code",
            f".{first}/auth.json",
            f".{second}/auth.json",
            f"{first}_home",
            f"{second}_code_oauth_token",
            f"{first} exec",
            f"{second} --print",
            f"{second} -p",
            f"agents.{first}_cli_agent",
            f"agents.{second}_cli_agent",
        )
    )


def _is_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:8192]


def _git_tracked_files() -> tuple[list[Path] | None, str]:
    """Return every file in the Git index, plus why it could not be read.

    The shipped tree is what Git tracks. Walking the filesystem instead sweeps in
    whatever a developer's or a runner's local state happens to leave under a
    scanned directory — project index files, scratch output — and makes a release
    gate depend on local project names.

    The reason is returned rather than dropped. Reporting only "run it inside a
    checkout" sent an investigation after the wrong thing once already: the
    tests were running in a checkout, and Git's own stderr — the one place the
    actual cause was written down — had been thrown away.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git could not be run: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        return None, f"git ls-files exited {completed.returncode}: {detail}"
    return [
        Path(entry) for entry in completed.stdout.decode("utf-8").split("\0") if entry.strip()
    ], ""


def _is_scanned_product_path(relative: Path) -> bool:
    if EXCLUDED_PATH_PARTS.intersection(relative.parts):
        return False
    if len(relative.parts) > 1:
        return relative.parts[0] in SCANNED_TREES
    name = relative.name
    return (
        name in ROOT_MANIFEST_NAMES
        or name.startswith(".env.example.")
        or name.endswith(ROOT_MANIFEST_SUFFIXES)
    )


def _tracked_product_paths() -> list[Path]:
    tracked, failure = _git_tracked_files()
    assert tracked is not None, (
        "the shipped-tree guard needs the Git index to tell product files from "
        f"local runtime state, and could not read it — {failure}"
    )
    paths = {
        relative
        for relative in tracked
        if _is_scanned_product_path(relative) and (ROOT / relative).is_file()
    }
    return sorted(path for path in paths if path not in TEMPORARY_ORCHESTRATION_ARTIFACTS)


def _surface_offenders() -> dict[str, list[int]]:
    tokens = _retired_fragments()
    offenders: dict[str, list[int]] = {}
    for relative in _tracked_product_paths():
        path_key = relative.as_posix()
        path_bytes = path_key.lower().encode("utf-8")
        path_hits = [0] if any(token in path_bytes for token in tokens) else []
        payload = (ROOT / relative).read_bytes().lower()
        content_tokens = _binary_retired_markers() if _is_binary(payload) else tokens
        allowed_lines = ALLOWED_NON_PRODUCT_METADATA_LINES.get(relative, set()) | (
            ALLOWED_TOOLKIT_DOCUMENTATION_LINES.get(relative, set())
        )
        allowed_line_numbers = ALLOWED_HISTORICAL_PROVENANCE_LINES.get(relative, set())
        line_hits = [
            line_number
            for line_number, line in enumerate(payload.splitlines(), start=1)
            if line_number not in allowed_line_numbers
            and line.strip() not in allowed_lines
            and any(token in line for token in content_tokens)
        ]
        hits = sorted(set(path_hits + line_hits))
        if hits:
            offenders[path_key] = hits
    return offenders


def test_shipped_tree_has_no_retired_executor_surface() -> None:
    offenders = _surface_offenders()
    detail = "\n".join(
        f"{path}:{','.join(str(line) for line in lines)}"
        for path, lines in sorted(offenders.items())
    )

    assert not offenders, f"retired product surface remains:\n{detail}"


def test_surviving_provider_and_protocol_contracts_remain_present() -> None:
    assert {"openai", "anthropic"} <= set(PROVIDER_CATALOG)
    assert (ROOT / "core" / "entrypoints" / "mcp" / "server.py").is_file()
    assert LLM_PROVIDER_CONTRACT_SCHEMA_VERSION == "llm_provider_extension_v1"


def test_guard_enumerates_the_git_index_not_the_working_tree() -> None:
    """OPS-006: a release gate must not read local runtime state.

    Project index files under `core/application/projects/` are git-ignored and
    carry whatever a developer happened to name a local project. Scanning the
    filesystem turned those names into gate failures on a tree that is clean.
    """
    tracked, failure = _git_tracked_files()
    assert tracked is not None, f"the Git index could not be read — {failure}"
    tracked_set = set(tracked)

    scanned = _tracked_product_paths()

    assert scanned, "the guard must still have product files to scan"
    untracked = sorted(path.as_posix() for path in scanned if path not in tracked_set)
    assert not untracked, (
        f"the guard scanned files Git does not track: {untracked}. Those are local "
        "runtime state, not shipped product."
    )


def test_guard_still_scans_the_product_trees_it_is_meant_to_cover() -> None:
    scanned = {path.as_posix() for path in _tracked_product_paths()}

    assert "core/application/orchestration/state_paths.py" in scanned
    assert "pyproject.toml" in scanned
    covered_trees = {path.split("/", 1)[0] for path in scanned if "/" in path}
    assert set(SCANNED_TREES).issubset(covered_trees | {"examples"})


def test_ignored_runtime_state_under_a_scanned_tree_is_not_product(tmp_path) -> None:
    """The exact shape that failed the full gate."""
    runtime_state = Path("core/application/projects/Some Local Project/.index-state.json")

    assert _is_scanned_product_path(runtime_state), (
        "the path itself looks like product; only the Git index can tell the "
        "difference, which is why the guard must consult it"
    )
    assert runtime_state not in set(_git_tracked_files()[0] or [])
    assert runtime_state not in set(_tracked_product_paths())


_EXTERNAL_CLIENT_DOCUMENTATION_EXAMPLES = (
    (
        Path("docs/GUIDE.md"),
        f"Follow the package README's supported {_EXTERNAL_AGENT_RUNTIME.title()} local marketplace, Cursor local",
    ),
    (
        Path("docs/agent-skills/flow-steward/README.md"),
        f"{_EXTERNAL_AGENT_RUNTIME} plugin marketplace add .",
    ),
)


@pytest.mark.parametrize(("relative", "client_line"), _EXTERNAL_CLIENT_DOCUMENTATION_EXAMPLES)
def test_native_external_client_documentation_is_not_a_bundled_executor(
    tmp_path, monkeypatch, relative, client_line
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(client_line + "\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_tracked_product_paths", lambda: [relative])

    assert _surface_offenders() == {}


@pytest.mark.parametrize(("relative", "client_line"), _EXTERNAL_CLIENT_DOCUMENTATION_EXAMPLES)
def test_external_client_documentation_allowance_rejects_executor_instructions(
    tmp_path, monkeypatch, relative, client_line
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        client_line
        + "\n"
        + client_line
        + f"; run {_EXTERNAL_AGENT_RUNTIME} exec inside the platform executor\n"
        + f"Run {_EXTERNAL_AGENT_RUNTIME} exec to execute platform jobs.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_tracked_product_paths", lambda: [relative])

    assert _surface_offenders() == {relative.as_posix(): [2, 3]}


@pytest.mark.parametrize(
    "client_line", [line for _, line in _EXTERNAL_CLIENT_DOCUMENTATION_EXAMPLES]
)
def test_external_client_documentation_allowance_is_bound_to_the_reviewed_path(
    tmp_path, monkeypatch, client_line
) -> None:
    relative = Path("docs/other-client-guide.md")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(client_line + "\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_tracked_product_paths", lambda: [relative])

    assert _surface_offenders() == {relative.as_posix(): [1]}


def test_external_client_documentation_allowance_does_not_allow_executor_modules(
    tmp_path, monkeypatch
) -> None:
    relative = Path(f"core/application/orchestration/agents/{_EXTERNAL_AGENT_RUNTIME}_cli_agent.py")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("class BundledExecutor: pass\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_tracked_product_paths", lambda: [relative])

    assert _surface_offenders() == {relative.as_posix(): [0]}
