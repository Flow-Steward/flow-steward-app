from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from core.application.tooling.ops.generate_env_catalog import render_env_catalog_markdown

ROOT = Path(__file__).resolve().parents[2]
ENV_ARTIFACT_LINT_TIMEOUT_SECONDS = 120


def test_repository_exposes_env_artifact_sync_command() -> None:
    assert importlib.util.find_spec("core.application.tooling.ops.sync_env_artifacts") is not None


def _artifact_fixture_repo(tmp_path: Path) -> Path:
    manifest_source = ROOT / "core/infrastructure/config/flow_steward_env_manifest.json"
    manifest_target = tmp_path / manifest_source.relative_to(ROOT)
    manifest_target.parent.mkdir(parents=True)
    manifest_target.write_text(manifest_source.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        "FS_STACK_ENVIRONMENT=production\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    source = tmp_path / "core/example.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('import os\nVALUE = os.getenv("FS_STACK_ENVIRONMENT")\n', encoding="utf-8")
    docs = tmp_path / "docs/configuration"
    docs.mkdir(parents=True)
    (docs / "flow-steward-env-audit.json").write_text("[]\n", encoding="utf-8")
    (docs / "flow-steward-env-catalog.md").write_text("# stale\n", encoding="utf-8")
    return tmp_path


def test_check_reports_both_stale_artifacts_without_rewriting_them(tmp_path: Path) -> None:
    repo = _artifact_fixture_repo(tmp_path)
    module = __import__(
        "core.application.tooling.ops.sync_env_artifacts", fromlist=["synchronize_env_artifacts"]
    )

    with pytest.raises(module.EnvArtifactsOutOfDate) as raised:
        module.synchronize_env_artifacts(repo, check=True)

    actual_paths = {path.name for path in raised.value.paths}
    expected_paths = {
        "flow-steward-env-audit.json",
        "flow-steward-env-catalog.md",
        # The Host settings page, rendered from the manifest and the config set fields.
        "host-settings.md",
    }
    assert actual_paths == expected_paths
    assert (repo / "docs/configuration/flow-steward-env-audit.json").read_text() == "[]\n"
    assert (repo / "docs/configuration/flow-steward-env-catalog.md").read_text() == "# stale\n"


def test_sync_replaces_both_artifacts_from_one_fresh_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _artifact_fixture_repo(tmp_path)
    module = __import__(
        "core.application.tooling.ops.sync_env_artifacts", fromlist=["synchronize_env_artifacts"]
    )
    real_builder = module.build_env_audit
    calls = 0

    def counted_builder(*, repo_root: Path) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return real_builder(repo_root=repo_root)

    monkeypatch.setattr(module, "build_env_audit", counted_builder)

    changed = module.synchronize_env_artifacts(repo, check=False)

    audit_path = repo / "docs/configuration/flow-steward-env-audit.json"
    catalog_path = repo / "docs/configuration/flow-steward-env-catalog.md"
    rows = json.loads(audit_path.read_text(encoding="utf-8"))
    stack_row = next(row for row in rows if row["variable_name"] == "FS_STACK_ENVIRONMENT")
    assert calls == 1
    assert changed == (audit_path, catalog_path, repo / "docs/configuration/host-settings.md")
    assert "core/example.py" in stack_row["code_read_locations"]
    assert "Raw env-like names discovered by audit:" in catalog_path.read_text(encoding="utf-8")
    assert "FS_STACK_ENVIRONMENT" in catalog_path.read_text(encoding="utf-8")


def test_line_only_source_shift_does_not_stale_tracked_artifacts(tmp_path: Path) -> None:
    repo = _artifact_fixture_repo(tmp_path)
    module = __import__(
        "core.application.tooling.ops.sync_env_artifacts", fromlist=["synchronize_env_artifacts"]
    )
    module.synchronize_env_artifacts(repo, check=False)
    audit_path = repo / "docs/configuration/flow-steward-env-audit.json"
    catalog_path = repo / "docs/configuration/flow-steward-env-catalog.md"
    expected = (audit_path.read_text(encoding="utf-8"), catalog_path.read_text(encoding="utf-8"))

    source = repo / "core/example.py"
    source.write_text("\n" + source.read_text(encoding="utf-8"), encoding="utf-8")

    assert module.synchronize_env_artifacts(repo, check=True) == ()
    assert (
        audit_path.read_text(encoding="utf-8"),
        catalog_path.read_text(encoding="utf-8"),
    ) == expected


def test_env_artifact_fingerprint_covers_every_scanned_source() -> None:
    registry = json.loads(
        (ROOT / "infra/tooling/quality/check-registry.json").read_text(encoding="utf-8")
    )
    check = next(item for item in registry["checks"] if item["id"] == "env-artifacts")

    assert {
        ".env.example*",
        "core/**/*.py",
        "examples/**/*.py",
        "tests/**/*.py",
        "core/**/*.sh",
        "infra/**/*.sh",
        "infra/**/*.yaml",
        "infra/**/*.yml",
        "docs/configuration/flow-steward-env-audit.json",
        "docs/configuration/flow-steward-env-catalog.md",
    } <= set(check["inputs"])


def test_pre_commit_runs_sync_before_fast_gate() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = config["repos"][0]["hooks"]
    ids = [hook["id"] for hook in hooks]
    sync = next(hook for hook in hooks if hook["id"] == "fs-env-artifacts-sync")

    assert ids.index("fs-env-artifacts-sync") < ids.index("fs-quality-fast")
    assert sync["entry"] == "core/entrypoints/cli/scripts/sync_env_artifacts.sh"


def test_sync_wrapper_updates_both_outputs_and_requires_manual_restaging(tmp_path: Path) -> None:
    repo = _artifact_fixture_repo(tmp_path)
    wrapper = ROOT / "core/entrypoints/cli/scripts/sync_env_artifacts.sh"
    assert wrapper.is_file()

    completed = subprocess.run(
        ["bash", str(wrapper), "--repo-root", str(repo)],
        cwd=ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "manually stage" in completed.stderr
    assert json.loads(
        (repo / "docs/configuration/flow-steward-env-audit.json").read_text(encoding="utf-8")
    )
    assert "FS_STACK_ENVIRONMENT" in (
        repo / "docs/configuration/flow-steward-env-catalog.md"
    ).read_text(encoding="utf-8")


def test_lint_exposes_fast_env_artifact_check(tmp_path: Path) -> None:
    tracked_manifest = tmp_path / "tracked-files.txt"
    tracked_manifest.write_text(".pre-commit-config.yaml\n", encoding="utf-8")
    completed = subprocess.run(
        [
            "bash",
            str(ROOT / "core/entrypoints/cli/scripts/lint.sh"),
            "--inside",
            "--checks",
            "env-artifacts",
        ],
        cwd=ROOT,
        env={**os.environ, "FS_BOUNDARY_TRACKED_FILES_MANIFEST": str(tracked_manifest)},
        capture_output=True,
        text=True,
        timeout=ENV_ARTIFACT_LINT_TIMEOUT_SECONDS,
        check=False,
    )

    assert completed.returncode in {0, 1}
    assert "Environment artifact check" in completed.stdout


def test_catalog_renderer_uses_supplied_fresh_audit_snapshot() -> None:
    snapshot = [
        {
            "variable_name": "FS_STACK_ENVIRONMENT",
            "canonical_name": "FS_STACK_ENVIRONMENT",
            "retired_alias_names": [],
            "documented_sources": [".env.example:1"],
            "code_read_locations": ["core/example.py:1"],
            "owning_subsystem": "runtime",
            "required": True,
            "secret": False,
            "environments": ["production"],
            "conditional_groups": ["stack_identity"],
            "default_example_value": "production",
            "notes": "Fresh snapshot sentinel.",
        }
    ]

    rendered = render_env_catalog_markdown(ROOT, audit_rows=snapshot)

    assert "Raw env-like names discovered by audit: `1`" in rendered
    assert "Fresh snapshot sentinel." in rendered
