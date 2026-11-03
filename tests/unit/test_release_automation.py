from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from core.application.release.images import all_release_roles
from core.application.tooling.release_automation import (
    preflight_registry,
    release_identity,
    require_complete_release,
    stable_version,
)

REVISION = "a" * 40
NAMESPACES = ("ghcr.io/flow-steward", "docker.io/flowsteward")


@pytest.mark.parametrize("tag", ["v1٢.2.3", "v1.2٣.3", "v1.2.3٤"])
def test_stable_version_rejects_unicode_digits(tag: str) -> None:
    with pytest.raises(ValueError, match="release tag"):
        stable_version(tag)


def test_release_quality_prepares_hosted_uids_and_hands_back_artifacts_after_cleanup() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    quality = workflow["jobs"]["quality"]
    steps = quality["steps"]
    prepare = next(
        (
            index
            for index, step in enumerate(steps)
            if step.get("uses") == "./.github/actions/prepare-quality"
        ),
        None,
    )
    assert prepare is not None, "Release quality must prepare hosted checkout/cache permissions"
    gate = next(
        index for index, step in enumerate(steps) if "lint.sh --full" in step.get("run", "")
    )
    commands = {step.get("run"): index for index, step in enumerate(steps) if "run" in step}
    adapter = "./core/entrypoints/cli/scripts/hosted-quality.sh "
    diagnostics = commands[adapter + "diagnostics"]
    cleanup = commands[adapter + "cleanup"]
    ownership = commands[adapter + "prepare-artifacts"]
    upload = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert prepare < gate < diagnostics < cleanup < ownership < upload
    for index in (diagnostics, cleanup, ownership):
        assert steps[index]["if"] == "always() && env.FS_QUALITY_CACHE_ROOT != ''"
    assert steps[upload]["if"] == "always()"
    assert "logs/quality/evidence" in steps[upload]["with"]["path"]
    postgres = next(
        step
        for step in steps
        if step.get("with", {}).get("file") == "infra/docker/postgres-extended/Dockerfile"
    )
    assert postgres["with"]["load"] is True
    assert postgres["with"]["push"] is False
    assert "scope=quality-postgres-amd64" in postgres["with"]["cache-from"]


def test_release_quality_keeps_resolved_tag_identity_for_dispatch_retries() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    quality = workflow["jobs"]["quality"]
    assert quality["needs"] == "resolve"
    assert "if" not in quality  # Archive reuse must not bypass the tag's gate.
    checkout = quality["steps"][0]
    assert checkout["with"]["ref"] == "${{ needs.resolve.outputs.revision }}"
    gate = next(step for step in quality["steps"] if "lint.sh --full" in step.get("run", ""))
    assert gate["env"]["RELEASE_REVISION"] == "${{ needs.resolve.outputs.revision }}"
    assert '--commit-sha "$RELEASE_REVISION"' in gate["run"]
    assert all("check-candidate" not in step.get("run", "") for step in quality["steps"])
    assert "needs.quality.result == 'success'" in workflow["jobs"]["publish"]["if"]


def test_release_defaults_to_existing_registries_without_extra_variables() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    publish = workflow["jobs"]["publish"]
    assert publish["env"]["FS_RELEASE_GHCR_NAMESPACE"] == (
        "${{ vars.RELEASE_GHCR_NAMESPACE || 'ghcr.io/flow-steward' }}"
    )
    assert publish["env"]["FS_RELEASE_DOCKERHUB_NAMESPACE"] == (
        "${{ vars.RELEASE_DOCKERHUB_NAMESPACE || 'docker.io/flowsteward' }}"
    )
    logins = {
        step["with"]["registry"]: step["with"]
        for step in publish["steps"]
        if step.get("uses", "").startswith("docker/login-action@")
    }
    assert logins["docker.io"]["username"] == ("${{ vars.DOCKERHUB_USERNAME || 'flowsteward' }}")
    assert logins["docker.io"]["password"] == "${{ secrets.DOCKERHUB_TOKEN }}"
    assert logins["ghcr.io"]["password"] == "${{ github.token }}"


def evidence() -> dict:
    return {
        "version": "1.2.3",
        "revision": REVISION,
        "failures": [],
        "registries": [
            {
                "namespace": namespace,
                "status": "verified",
                "images": [
                    {
                        "role": role.role,
                        "reference": f"{namespace}/{role.repository}:1.2.3",
                        "index_digest": "sha256:" + "1" * 64,
                        "platforms": {
                            platform: {
                                "manifest_digest": "sha256:" + "2" * 64,
                                "config_digest": "sha256:" + "3" * 64,
                            }
                            for platform in ("linux/amd64", "linux/arm64")
                        },
                    }
                    for role in all_release_roles()
                ],
            }
            for namespace in NAMESPACES
        ],
    }


def test_complete_evidence_accepts_both_registries(tmp_path: Path) -> None:
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(evidence()), encoding="utf-8")
    require_complete_release(path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_registry",
        "partial_registry",
        "wrong_revision",
        "missing_role",
        "duplicate_role",
        "missing_platform",
        "wrong_child_digest",
        "wrong_reference",
        "invalid_digest",
        "failures",
    ],
)
def test_incomplete_or_mismatched_release_is_rejected(tmp_path: Path, mutation: str) -> None:
    value = copy.deepcopy(evidence())
    registry = value["registries"][1]
    image = registry["images"][0]
    if mutation == "missing_registry":
        value["registries"].pop()
    elif mutation == "partial_registry":
        registry["status"] = "incomplete"
    elif mutation == "wrong_revision":
        value["revision"] = "b" * 40
    elif mutation == "missing_role":
        registry["images"].pop()
    elif mutation == "duplicate_role":
        registry["images"][1] = copy.deepcopy(image)
    elif mutation == "missing_platform":
        image["platforms"].pop("linux/arm64")
    elif mutation == "wrong_child_digest":
        image["platforms"]["linux/arm64"]["manifest_digest"] = "sha256:" + "4" * 64
    elif mutation == "wrong_reference":
        image["reference"] = "docker.io/elsewhere/app:1.2.3"
    elif mutation == "invalid_digest":
        image["index_digest"] = "not-a-digest"
    elif mutation == "failures":
        value["failures"] = ["mirror incomplete"]
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        require_complete_release(path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES)


@pytest.mark.parametrize("tag", ["main", "v1.2", "v1.2.3-rc1", "v01.2.3", "v1.2.3\n", "--all"])
def test_release_tag_rejects_non_stable_semver(tag: str) -> None:
    with pytest.raises(ValueError, match="tag"):
        stable_version(tag)


def test_release_tag_extracts_exact_version() -> None:
    assert stable_version("v1.2.3") == "1.2.3"


def test_identity_requires_matching_version_and_main_ancestry(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    git("add", ".")
    git("commit", "-m", "initial")
    git("tag", "v1.2.3")
    revision = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", revision)
    assert release_identity(tmp_path, "v1.2.3") == ("1.2.3", revision)
    (tmp_path / "VERSION").write_text("2.0.0\n")
    with pytest.raises(ValueError):
        release_identity(tmp_path, "v1.2.3")


def write_native_records(directory: Path) -> None:
    for architecture in ("amd64", "arm64"):
        images = []
        for role in all_release_roles():
            name = f"{role.role}-{architecture}.oci.tar"
            (directory / name).write_bytes(b"fixture-archive")
            images.append(
                {
                    "role": role.role,
                    "repository": role.repository,
                    "platform": f"linux/{architecture}",
                    "artifact": name,
                    "sha256": hashlib.sha256(b"fixture-archive").hexdigest(),
                    "manifest_digest": "sha256:" + "2" * 64,
                }
            )
        (directory / f"build-{architecture}.json").write_text(
            json.dumps(
                {
                    "version": "1.2.3",
                    "revision": REVISION,
                    "platform": f"linux/{architecture}",
                    "images": images,
                }
            )
        )


def test_existing_version_cannot_be_replaced_with_different_artifacts(tmp_path: Path) -> None:
    write_native_records(tmp_path)
    with pytest.raises(ValueError, match="immutable"):
        preflight_registry(
            tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=lambda ref: '{"schemaVersion":2}'
        )


def test_artifact_checksum_is_verified_before_any_registry_access(tmp_path: Path) -> None:
    write_native_records(tmp_path)
    (tmp_path / "compact-runtime-arm64.oci.tar").write_bytes(b"corrupted")
    calls = []
    with pytest.raises(ValueError, match="checksum"):
        preflight_registry(
            tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=lambda ref: calls.append(ref)
        )
    assert calls == []


def test_empty_registry_and_identical_children_allow_idempotent_publication(tmp_path: Path) -> None:
    write_native_records(tmp_path)
    preflight_registry(tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=lambda ref: None)


@pytest.mark.parametrize(
    "event,branch,accepted",
    [
        ("push", "v1.2.3", True),
        ("workflow_dispatch", "main", True),
        ("workflow_dispatch", "untrusted-branch", False),
    ],
)
def test_archive_run_provenance_executes_workflow_guard(
    event: str, branch: str, accepted: bool
) -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    script = next(
        step["with"]["script"]
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("name") == "Validate retained archive run provenance"
    )
    harness = """
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const github = {rest: {actions: {
  getWorkflowRun: async () => ({data: input.run}),
  getWorkflow: async () => ({data: {id: 7}})
}}};
const context = {repo: {owner: 'Flow-Steward', repo: 'flow-steward-app'}};
const processStub = {env: {ARTIFACT_RUN_ID: '123', FS_RELEASE_REVISION: input.revision}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github', 'context', 'process', input.script)(github, context, processStub)
 .then(() => process.exit(0), error => {console.error(error.message); process.exit(1)});
"""
    result = subprocess.run(
        ["node", "-e", harness],
        input=json.dumps(
            {
                "script": script,
                "revision": REVISION,
                "run": {
                    "workflow_id": 7,
                    "head_sha": REVISION,
                    "head_branch": branch,
                    "event": event,
                },
            }
        ),
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) is accepted, result.stderr


def test_release_v099_focused_exception_is_narrow_and_records_failed_and_impacted_tests() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["quality"]["steps"]
    full = next(step for step in steps if "lint.sh --full" in step.get("run", ""))
    focused = next(
        step for step in steps if step.get("name") == "Focused acceptance (full suite not rerun)"
    )
    assert full["if"].startswith("needs.resolve.outputs.tag != 'v0.9.9' && (")
    assert focused["if"] == "needs.resolve.outputs.tag == 'v0.9.9'"
    assert focused["env"]["RELEASE_REVISION"] == "${{ needs.resolve.outputs.revision }}"
    command = focused["run"]
    assert "git rev-parse HEAD" in command
    assert "git merge-base --is-ancestor c876d3efb66cadba95c99e6677cba20e45a8d514" in command
    assert "set -euo pipefail" in command
    assert "--checks targeted-tests" in command
    assert "--checks frontend-targeted-tests" in command
    assert "41591208411" in command
    for target in (
        "tests/unit/test_install_script.py",
        "tests/unit/installer/test_installer_process.py",
        "tests/unit/lifecycle/test_docker_host_state.py",
        "tests/unit/test_retired_agent_type_migration.py",
        "tests/unit/test_released_migration_immutability.py",
        "tests/integration/test_inbound_oauth_migration_postgres.py",
        "src/pages/registry/data-connections/connection-preservation.test.tsx",
        "src/pages/registry/mcp/index.test.tsx",
        "src/pages/registry/mcp/mcp-json-import.test.ts",
        "src/pages/registry/mcp/mcp-json-import-field.test.tsx",
        "src/pages/workflows/detail/index.change-set-scope.test.tsx",
        "src/pages/workflows/detail/index.hook-frame.test.tsx",
        "src/pages/workflows/detail/index.transform-draft.test.tsx",
        "src/pages/workflows/detail/index.unsaved-forms.test.tsx",
        "src/pages/workflows/builder/canvas-edge-label-placement.test.ts",
        "src/pages/workflows/builder/canvas-slot-labels.test.tsx",
        "src/pages/workflows/builder/canvas-viewport.test.ts",
    ):
        assert target in command
    publish = workflow["jobs"]["publish"]
    assert "needs.quality.result == 'success'" in publish["if"]
    release = next(
        step
        for step in publish["steps"]
        if step.get("name", "").startswith("Publish stable GitHub Release")
    )
    assert "Focused acceptance (full suite not rerun)" in release["with"]["script"]
    assert "updateRelease" in release["with"]["script"]
