from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.application.release.images import all_release_roles
from core.application.tooling.release_automation import (
    RegistryUnavailable,
    preflight_registry,
    release_identity,
    require_complete_release,
    stable_version,
)

REVISION = "a" * 40
NAMESPACES = ("ghcr.io/flow-steward", "docker.io/flowsteward")


def _publication_artifacts():
    return (
        *(role for role in all_release_roles() if role.role != "installer"),
        SimpleNamespace(role="installer", repository="flow-steward-installer"),
    )


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
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Focused acceptance (full suite not rerun)"
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
    assert steps[prepare]["if"] == steps[gate]["if"] == "needs.resolve.outputs.tag == 'v0.9.9'"
    blocked = next(
        step
        for step in steps
        if step.get("name", "").startswith("Require existing complete exact-SHA")
    )
    assert "exit 1" in blocked["run"]
    assert not any("lint.sh --full" in step.get("run", "") for step in steps)
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
    lookup = next(step for step in quality["steps"] if step.get("id") == "previous-quality")
    assert lookup["env"]["RELEASE_REVISION"] == "${{ needs.resolve.outputs.revision }}"
    assert "head_sha: process.env.RELEASE_REVISION" in lookup["with"]["script"]
    assert "choose(run, jobs, process.env.RELEASE_REVISION" in lookup["with"]["script"]
    blocked = next(
        step
        for step in quality["steps"]
        if step.get("name", "").startswith("Require existing complete exact-SHA")
    )
    assert "inputs.require_full_quality == true" in blocked["if"]
    assert "steps.previous-quality.outcome != 'success'" in blocked["if"]
    assert "steps.previous-quality.outputs.reuse != 'true'" in blocked["if"]
    assert "exit 1" in blocked["run"]
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
                    for role in _publication_artifacts()
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
        for role in _publication_artifacts():
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
    full = next(
        step
        for step in steps
        if step.get("name", "").startswith("Require existing complete exact-SHA")
    )
    focused = next(
        step for step in steps if step.get("name") == "Focused acceptance (full suite not rerun)"
    )
    # v0.9.10 later took its own explicit exception, recorded in its own step.
    assert full["if"].startswith(
        "inputs.require_full_quality == true && needs.resolve.outputs.tag != 'v0.9.9' && needs.resolve.outputs.tag != 'v0.9.10' && ("
    )
    assert not any("lint.sh --full" in step.get("run", "") for step in steps)
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


def test_one_complete_registry_accepts_unavailable_mirror_without_mixing(tmp_path: Path) -> None:
    value = evidence()
    value["registries"][0] = {"namespace": NAMESPACES[0], "status": "incomplete", "images": []}
    value["failures"] = [f"{NAMESPACES[0]}: registry unavailable"]
    path = tmp_path / "verification.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    require_complete_release(path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES)


def test_all_unavailable_registries_fail_closed(tmp_path: Path) -> None:
    value = evidence()
    for registry in value["registries"]:
        registry["status"] = "incomplete"
        registry["images"] = []
    value["failures"] = ["both unavailable"]
    path = tmp_path / "verification.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        require_complete_release(path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES)


def test_invalid_registry_cannot_be_downgraded_to_transport_failure(tmp_path: Path) -> None:
    value = evidence()
    value["registries"][0]["status"] = "invalid"
    value["failures"] = ["image identity mismatch"]
    path = tmp_path / "verification.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        require_complete_release(path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES)


def test_preflight_skips_unavailable_registry_without_authorizing_its_writes(
    tmp_path: Path,
) -> None:
    write_native_records(tmp_path)

    def inspect(reference: str) -> None:
        if reference.startswith(NAMESPACES[0]):
            raise RegistryUnavailable("registry denied")
        return

    assert preflight_registry(tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=inspect) == (
        NAMESPACES[1],
    )


def test_release_workflow_uses_only_preflight_approved_registries() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "preflight")
    assert "--preflight-output" in preflight["run"]
    copy = next(
        step
        for step in steps
        if step.get("name") == "Copy to available registries and verify complete release sets"
    )
    assert 'if [[ "$GHCR_READY" == true ]]' in copy["run"]
    assert 'if [[ "$DOCKERHUB_READY" == true ]]' in copy["run"]
    assert 'official_release.py" verify --root "$FS_RELEASE_SOURCE"' in copy["run"]
    assert '--artifact-dir "$FS_RELEASE_ARTIFACT_DIR"' in copy["run"]
    release = next(
        step for step in steps if step.get("name", "").startswith("Publish stable GitHub Release")
    )
    assert "Registry availability:" in release["with"]["script"]
    assert "status === 'verified'" in release["with"]["script"]
    assert "status === 'incomplete'" in release["with"]["script"]
    assert "target_commitish" not in release["with"]["script"]


def test_preflight_rejects_all_unavailable_registries(tmp_path: Path) -> None:
    write_native_records(tmp_path)

    def unavailable(_reference: str) -> None:
        raise RegistryUnavailable("offline")

    with pytest.raises(ValueError, match="no registry"):
        preflight_registry(tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=unavailable)


def test_preflight_does_not_downgrade_mismatched_registry_to_unavailable(tmp_path: Path) -> None:
    write_native_records(tmp_path)

    def inspect(reference: str) -> str | None:
        if reference.startswith(NAMESPACES[0]):
            raise RegistryUnavailable("offline")
        return '{"schemaVersion":2}'

    with pytest.raises(ValueError, match="immutable"):
        preflight_registry(tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=inspect)


def test_complete_registry_must_match_native_archives(tmp_path: Path) -> None:
    write_native_records(tmp_path)
    value = evidence()
    value["registries"][0] = {"namespace": NAMESPACES[0], "status": "incomplete", "images": []}
    value["failures"] = [f"{NAMESPACES[0]}: unavailable"]
    value["registries"][1]["images"][0]["platforms"]["linux/amd64"]["manifest_digest"] = (
        "sha256:" + "4" * 64
    )
    path = tmp_path / "verification.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="native release artifacts"):
        require_complete_release(
            path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES, artifact_dir=tmp_path
        )


def test_publication_uses_workflow_pinned_tooling_and_separate_tagged_source() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    source = steps[0]["with"]
    tooling = next(
        step["with"]
        for step in steps
        if step.get("name") == "Check out the workflow's immutable publication tooling"
    )
    assert source["ref"] == "${{ needs.resolve.outputs.revision }}"
    assert source["path"] == "source"
    assert tooling["ref"] == "${{ github.sha }}"
    assert tooling["path"] == "tooling"
    preflight = next(step for step in steps if step.get("id") == "preflight")
    assert (
        "$FS_RELEASE_TOOLING/core/entrypoints/cli/scripts/release-automation.py" in preflight["run"]
    )
    assert '--root "$FS_RELEASE_SOURCE"' in preflight["run"]


def test_release_adapter_identity_uses_explicit_source_root(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    git("add", ".")
    git("commit", "-m", "source")
    git("tag", "v1.2.3")
    revision = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", revision)
    script = (
        Path(__file__).resolve().parents[2] / "core/entrypoints/cli/scripts/release-automation.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "identity", "--tag", "v1.2.3", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"revision={revision}" in result.stdout


@pytest.mark.parametrize("kind", ["direct", "annotated", "different", "missing"])
def test_release_tag_guard_checks_remote_ref_before_create_or_update(kind: str) -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    release = next(
        step
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("name", "").startswith("Publish stable GitHub Release")
    )
    script = release["with"]["script"]
    guard = script.split("async function assertTagMatchesRevision() {", 1)[1].split(
        "\nlet release;", 1
    )[0]
    assert script.count("await assertTagMatchesRevision();") == 3
    assert "await assertTagMatchesRevision();\nawait github.rest.repos.uploadReleaseAsset" in script
    node = f"""const kind = {json.dumps(kind)};
const expected = "a".repeat(40);
process.env.FS_RELEASE_REVISION = expected;
const tag = "v1.2.3";
const context = {{repo: {{owner: "o", repo: "r"}}}};
const github = {{rest: {{git: {{
  getRef: async () => {{ if (kind === "missing") throw new Error("missing"); return {{data: {{object: kind === "annotated" ? {{type: "tag", sha: "tag-object"}} : {{type: "commit", sha: kind === "different" ? "b".repeat(40) : expected}}}}}}; }},
  getTag: async () => ({{data: {{object: {{type: "commit", sha: expected}}}}}}),
}}}}}};
async function assertTagMatchesRevision() {{{guard}
assertTagMatchesRevision().then(() => process.exit(0)).catch(() => process.exit(1));"""
    result = subprocess.run(["node", "-e", node], capture_output=True, text=True)
    assert (result.returncode == 0) == (kind in {"direct", "annotated"}), result.stderr


@pytest.mark.parametrize("existing", [None, "registry-only", "human-edited"])
def test_publication_uses_product_notes_and_keeps_registry_results_as_footer(
    existing: str | None,
) -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    release = next(
        step
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("name", "").startswith("Publish stable GitHub Release")
    )
    notes = "## Flow Steward 1.2.3\n\n### Fixes\n\n- Preserve the customer's report after reopening a completed job."
    edited = notes + "\n\nAdditional upgrade guidance written by the publisher."
    wanted = edited if existing == "human-edited" else notes
    node = f"""
const scenario = {json.dumps(existing)};
const notes = {json.dumps(notes)};
const wanted = {json.dumps(wanted)};
const notice = 'Registry availability: verified complete AMD64 and ARM64 images: ghcr.io/flow-steward. See attached digest evidence.';
const fs = {{readFileSync: () => JSON.stringify({{registries: [{{status:'verified', namespace:'ghcr.io/flow-steward'}}]}}), writeFileSync: () => {{}}}};
const fakeRequire = name => {{
  if (name === 'node:fs' || name === 'fs') return fs;
  if (name === 'node:child_process') return {{execFileSync: () => JSON.stringify({{body:wanted}})}};
  throw new Error('Unexpected module: ' + name);
}};
process.env.RELEASE_TAG='v1.2.3';
process.env.FS_RELEASE_REVISION='a'.repeat(40);
process.env.FS_RELEASE_TOOLING='/tooling';
process.env.FS_RELEASE_SOURCE='/source';
process.env.FS_RELEASE_PREVIOUS_TAG='v1.2.2';
process.env.FS_RELEASE_NOTES_SHA256='b'.repeat(64);
process.env.RUNNER_TEMP='/temporary';
let published;
const github = {{rest: {{
  git: {{getRef: async () => ({{data: {{object: {{type:'commit', sha:'a'.repeat(40)}}}}}})}},
  repos: {{
    getReleaseByTag: async () => {{if (scenario === null) {{const e=new Error('missing');e.status=404;throw e;}}
      return {{data: {{id:1, draft:false, prerelease:false, body: scenario === 'human-edited' ? wanted + '\\n\\nRegistry availability: old mirror.' : 'Registry availability: old mirror.'}}}};}},
    createRelease: async args => {{published=args.body;return {{data: {{id:1,draft:false,prerelease:false,body:args.body}}}};}},
    updateRelease: async args => {{published=args.body;return {{data: {{id:1,draft:false,prerelease:false,body:args.body}}}};}},
    uploadReleaseAsset: async () => ({{}}),
  }}
}}}};
const context = {{repo: {{owner:'Flow-Steward',repo:'flow-steward-app'}},runId:7}};
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
new AsyncFunction('require','github','context',{json.dumps(release["with"]["script"])})(fakeRequire,github,context)
 .then(() => {{if (published !== wanted + '\\n\\n' + notice) throw new Error('Incorrect published product notes: ' + published);}})
 .catch(error => {{console.error(error.message);process.exitCode=1;}});
"""
    result = subprocess.run(["node", "-e", node], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_release_resolves_product_notes_before_native_builds() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    resolve = workflow["jobs"]["resolve"]
    notes = next((step for step in resolve["steps"] if step.get("id") == "release-notes"), None)
    assert notes is not None, "Missing product notes must stop the resolve job before native builds"
    assert "listReleases" in notes["with"]["script"]
    assert "release-automation.py" in notes["with"]["script"]
    assert "notes_sha256" in resolve["outputs"]
    assert "resolve" in workflow["jobs"]["native"]["needs"]


def test_complete_release_requires_installer_evidence(tmp_path: Path) -> None:
    value = evidence()
    for registry in value["registries"]:
        registry["images"] = [image for image in registry["images"] if image["role"] != "installer"]
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="complete verified role set"):
        require_complete_release(path, version="1.2.3", revision=REVISION, namespaces=NAMESPACES)


def test_preflight_requires_installer_native_metadata_before_registry_access(
    tmp_path: Path,
) -> None:
    write_native_records(tmp_path)
    for architecture in ("amd64", "arm64"):
        path = tmp_path / f"build-{architecture}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["images"] = [image for image in record["images"] if image["role"] != "installer"]
        path.write_text(json.dumps(record), encoding="utf-8")
    inspected = []
    with pytest.raises(ValueError, match="wrong role set"):
        preflight_registry(
            tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=lambda ref: inspected.append(ref)
        )
    assert not inspected


def test_preflight_checks_installer_native_and_index_tags(tmp_path: Path) -> None:
    write_native_records(tmp_path)
    inspected = []
    preflight_registry(
        tmp_path, "1.2.3", REVISION, NAMESPACES, inspect=lambda ref: inspected.append(ref)
    )
    assert {ref for ref in inspected if "/flow-steward-installer:" in ref} == {
        "ghcr.io/flow-steward/flow-steward-installer:1.2.3-amd64",
        "ghcr.io/flow-steward/flow-steward-installer:1.2.3-arm64",
        "ghcr.io/flow-steward/flow-steward-installer:1.2.3",
        "docker.io/flowsteward/flow-steward-installer:1.2.3-amd64",
        "docker.io/flowsteward/flow-steward-installer:1.2.3-arm64",
        "docker.io/flowsteward/flow-steward-installer:1.2.3",
    }


def test_stable_release_waits_for_anonymous_installer_pulls_and_retains_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-images.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    names = [step.get("name", "") for step in steps]
    anonymous = next(
        (i for i, step in enumerate(steps) if "anonymous-installer" in step.get("run", "")), None
    )
    assert anonymous is not None, "Stable publication needs actual anonymous installer pulls"
    assert names.index("Copy to available registries and verify complete release sets") < anonymous
    assert anonymous < names.index(
        "Publish stable GitHub Release after a complete registry verifies"
    )
    upload = next(
        step for step in steps if step.get("name") == "Retain registry verification evidence"
    )
    assert upload["if"] == "always()"
    assert "anonymous-installer" in upload["with"]["path"]


def test_readme_install_command_matches_candidate_version() -> None:
    root = Path(__file__).resolve().parents[2]
    version = (root / "VERSION").read_text().strip()
    assert (
        f"git clone --branch v{version} https://github.com/Flow-Steward/flow-steward-app.git"
        in (root / "README.md").read_text()
    )
