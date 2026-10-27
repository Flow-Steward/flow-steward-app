from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.entrypoints.cli.ops import candidate_runtime_preflight
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from scripts.release.generate_candidate_provenance import _deployment_input_record
from scripts.release.prepare_candidate_runtime import prepare as prepare_candidate_runtime
from scripts.release.verify_candidate_provenance import verify as verify_candidate_provenance

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", ROOT)).resolve()
BUILD = ROOT / "scripts/release/build_candidate.sh"
PREPARE_RUNTIME = ROOT / "scripts/release/prepare_candidate_runtime.py"
PREPARE_SOURCE = ROOT / "scripts/release/prepare_candidate_source.py"
SYNC_SDK = ROOT / "scripts/release/sync_extension_sdk_wheel.py"
IMAGE_ARCHIVE = ROOT / "scripts/release/candidate_image_archive.py"
ENV_SOURCE = ROOT / "core/infrastructure/release_candidate/env_source.py"
CORE_INIT = ROOT / "core/__init__.py"
INFRASTRUCTURE_INIT = ROOT / "core/infrastructure/__init__.py"
RELEASE_CANDIDATE_INIT = ROOT / "core/infrastructure/release_candidate/__init__.py"
STACK = ROOT / "scripts/release/candidate_stack.sh"
PREPARE = ROOT / "scripts/release/candidate_prepare.sh"
PROD_COMPOSE = ROOT / "infra/docker/docker-compose.prod.yaml"
APP_DOCKERFILE = ROOT / "infra/docker/Dockerfile"
MCP_DOCKERFILE = ROOT / "infra/mcp/Dockerfile"
PROVENANCE_GENERATOR = ROOT / "scripts/release/generate_candidate_provenance.py"
PROVENANCE_VERIFIER = ROOT / "scripts/release/verify_candidate_provenance.py"


def _candidate_builder_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts/release"
    script_dir.mkdir(parents=True)
    builder = script_dir / "build_candidate.sh"
    shutil.copy2(BUILD, builder)
    shutil.copy2(PREPARE_RUNTIME, script_dir / "prepare_candidate_runtime.py")
    shutil.copy2(PREPARE_SOURCE, script_dir / "prepare_candidate_source.py")
    shutil.copy2(SYNC_SDK, script_dir / "sync_extension_sdk_wheel.py")
    shutil.copy2(IMAGE_ARCHIVE, script_dir / "candidate_image_archive.py")
    shutil.copy2(PROVENANCE_VERIFIER, script_dir / "verify_candidate_provenance.py")
    shutil.copytree(
        SOURCE_ROOT / "core/infrastructure/extension_sdk/public",
        repo / "core/infrastructure/extension_sdk/public",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    # Core ships no extensions. Local installed bundles are not release inputs
    # and must not become tracked fixture content in the core-only test workspace.
    (repo / "extensions").mkdir()
    env_source_dir = repo / "core/infrastructure/release_candidate"
    env_source_dir.mkdir(parents=True)
    shutil.copy2(CORE_INIT, repo / "core/__init__.py")
    shutil.copy2(INFRASTRUCTURE_INIT, repo / "core/infrastructure/__init__.py")
    shutil.copy2(RELEASE_CANDIDATE_INIT, env_source_dir / "__init__.py")
    shutil.copy2(ENV_SOURCE, env_source_dir / "env_source.py")
    copy_ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(
        ROOT / "core/application/release",
        repo / "core/application/release",
        dirs_exist_ok=True,
        ignore=copy_ignore,
    )
    shutil.copytree(
        ROOT / "core/infrastructure/release_candidate",
        repo / "core/infrastructure/release_candidate",
        dirs_exist_ok=True,
        ignore=copy_ignore,
    )
    for package in ("core/entrypoints/cli/ops/candidate_harness.py",):
        destination = repo / package
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / package, destination)
    shutil.copytree(
        ROOT / "scripts/smoke",
        repo / "scripts/smoke",
        ignore=copy_ignore,
    )
    for source, relative in (
        (ROOT / "infra/docker/Dockerfile", "infra/docker/Dockerfile"),
        (ROOT / "infra/mcp/Dockerfile", "infra/mcp/Dockerfile"),
        (
            ROOT / "infra/docker/docker-compose.prod.yaml",
            "infra/docker/docker-compose.prod.yaml",
        ),
        (
            ROOT / "infra/docker/postgres-extended/Dockerfile",
            "infra/docker/postgres-extended/Dockerfile",
        ),
        (ROOT / "requirements-runtime.lock", "requirements-runtime.lock"),
        (ROOT / "requirements-cli.lock", "requirements-cli.lock"),
        (
            ROOT / "assets/frontend/package-lock.json",
            "assets/frontend/package-lock.json",
        ),
    ):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Candidate Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_provenance = bin_dir / "fake-provenance"
    fake_provenance.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import platform
import sys
from pathlib import Path

args = sys.argv[1:]
values = {}
repeated = {}
for index, value in enumerate(args[:-1]):
    if value.startswith("--"):
        repeated.setdefault(value, []).append(args[index + 1])
        values[value] = args[index + 1]
manifest_path = Path(values["--source-manifest"])
manifest_bytes = manifest_path.read_bytes()
manifest = json.loads(manifest_bytes)
machine = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine().lower(), platform.machine().lower())
def pairs(option):
    return dict(item.split("=", 1) for item in repeated.get(option, []))
for source in pairs("--deployment-input-source").values():
    if not Path(source).is_file():
        raise SystemExit("deployment input source is unavailable")
release_inputs = {}
for name, source in pairs("--release-input").items():
    path = Path(source)
    release_inputs[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
payload = {
    "schema_version": "flow-steward-packaged-provenance-v2",
    "candidate_commit": values["--candidate-commit"],
    "source_sha256": manifest["source_sha256"],
    "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "git_lfs_objects": [
        {"path": item["path"], "oid": item["lfs_oid"], "size": item["lfs_size"]}
        for item in manifest.get("entries", []) if "lfs_oid" in item
    ],
    "release_inputs": release_inputs,
    "base_images": pairs("--base-image"),
    "infrastructure_images": pairs("--infrastructure-image"),
    "image_digests": pairs("--image-digest"),
    "build_arguments": pairs("--build-argument"),
    "architecture": f"linux/{machine}",
}
Path(values["--output"]).write_text(json.dumps(payload) + "\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    fake_provenance.chmod(0o755)
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "${1:-}" in\n'
        "  build)\n"
        '    context="${@: -1}"\n'
        '    printf "%s\\n" "$context" >>"$FAKE_DOCKER_CONTEXT_LOG"\n'
        '    if [ -n "${FORBIDDEN_BUILD_SIGNATURE:-}" ] && '
        'grep -R -F -l -- "$FORBIDDEN_BUILD_SIGNATURE" "$context" >/dev/null 2>&1; then\n'
        "      exit 23\n"
        "    fi\n"
        '    printf "build\\n" >>"$FAKE_DOCKER_BUILD_LOG"\n'
        "    exit 0\n"
        "    ;;\n"
        "  image)\n"
        '    if [ "${2:-}" = save ]; then\n'
        '      while [ "$#" -gt 0 ]; do\n'
        '        if [ "$1" = --output ]; then printf \'fixture-image-archive\\n\' >"$2"; exit 0; fi\n'
        "        shift\n"
        "      done\n"
        "      exit 21\n"
        "    fi\n"
        '    if [ "${2:-}" = inspect ] '
        '&& [[ " $* " = *"org.opencontainers.image.revision"* ]]; then\n'
        "      git rev-parse HEAD\n"
        "      exit 0\n"
        "    fi\n"
        "    printf 'sha256:%064d\\n' 1\n"
        "    exit 0\n"
        "    ;;\n"
        "  rm)\n"
        '    printf "%s\\n" "${@: -1}" >>"$FAKE_DOCKER_REMOVED_LOG"\n'
        "    ;;\n"
        "  run)\n"
        '    [ "${FAKE_DOCKER_FAIL_RUN:-0}" != 1 ] || exit 17\n'
        '    run_args=("$@")\n'
        "    run_user=\n"
        "    output=\n"
        '    while [ "$#" -gt 0 ]; do\n'
        '      if [ "$1" = --user ]; then run_user="$2"; shift 2\n'
        '      elif [ "$1" = --deployment-input-source ]; then\n'
        '        case "$2" in *="$EXPECTED_PROD_ENV") ;; *) exit 18 ;; esac\n'
        "        shift 2\n"
        '      elif [ "$1" = --output ]; then output="$2"; shift 2\n'
        "      else shift\n"
        "      fi\n"
        "    done\n"
        '    [ "$run_user" = "$EXPECTED_DOCKER_USER" ] || exit 20\n'
        '    "$FAKE_PROVENANCE_WRITER" "${run_args[@]}"\n'
        "    ;;\n"
        "  *) exit 19 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_python = bin_dir / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "${1:-}" == scripts/release/generate_candidate_provenance.py ]]; then\n'
        '  [[ "${FAKE_DOCKER_FAIL_RUN:-0}" != 1 ]] || exit 17\n'
        "  shift\n"
        '  exec "$FAKE_PROVENANCE_WRITER" "$@"\n'
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    prod_env = tmp_path / "candidate-production.env"
    prod_env.write_text(
        "SECURITY_PII_SALT=obsolete\n"
        "SECURITY_PII_SALT=fixture\n"
        "FS_AUTH_BOOTSTRAP_TOKEN=expired-fixture\n"  # pragma: allowlist secret
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT=\n"
        "FS_AUTH_BOOTSTRAP_STATE_FILE=/stale/bootstrap-token.json\n"
        "FS_AUTH_BOOTSTRAP_ENABLED=0\n"
        "FS_FILE_STORAGE=''\n"
        "ARTIFACT_S3_ENDPOINT_URL=''\n"
        "ARTIFACT_S3_ACCESS_KEY=''\n"
        'ARTIFACT_S3_SECRET_KEY=""\n',
        encoding="utf-8",
    )
    return repo, bin_dir, prod_env


def _run_candidate_builder(
    repo: Path,
    bin_dir: Path,
    prod_env: Path,
    *,
    fail_provenance: bool = False,
    forbidden_build_signature: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/release/build_candidate.sh"],
        cwd=repo,
        env={
            **os.environ,
            "EXPECTED_PROD_ENV": str(prod_env),
            "EXPECTED_DOCKER_USER": "0:0",
            "FAKE_DOCKER_REMOVED_LOG": str(prod_env.parent / "removed-containers.log"),
            "FAKE_DOCKER_BUILD_LOG": str(prod_env.parent / "docker-builds.log"),
            "FAKE_DOCKER_CONTEXT_LOG": str(prod_env.parent / "docker-contexts.log"),
            "FAKE_DOCKER_FAIL_RUN": "1" if fail_provenance else "0",
            "FAKE_PROVENANCE_WRITER": str(bin_dir / "fake-provenance"),
            "FORBIDDEN_BUILD_SIGNATURE": forbidden_build_signature,
            "FS_RELEASE_PROD_ENV": str(prod_env),
            "FS_RELEASE_TLS_CERT": str(prod_env),
            "FS_RELEASE_TLS_KEY": str(prod_env),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_candidate_builder_fixture_excludes_ambient_extension_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    shutil.copytree(
        SOURCE_ROOT / "core/infrastructure/extension_sdk/public",
        source_root / "core/infrastructure/extension_sdk/public",
    )
    ambient_root = source_root / "extensions/fixture-ambient"
    ambient_root.mkdir(parents=True)
    (ambient_root / "extension.yaml").write_text(
        "extension_id: fixture.ambient\n", encoding="utf-8"
    )
    (ambient_root / "requirements-dev.txt").write_text("", encoding="utf-8")
    (ambient_root / "dev-wheels").mkdir()
    (ambient_root / "dev-wheels/flowsteward_extension_sdk-ambient.whl").write_bytes(b"fixture")
    monkeypatch.setattr(sys.modules[__name__], "SOURCE_ROOT", source_root)

    repo, _bin_dir, _prod_env = _candidate_builder_fixture(tmp_path)

    assert (repo / "extensions").is_dir()
    assert not any((repo / "extensions").iterdir())
    assert (ambient_root / "extension.yaml").read_text(encoding="utf-8") == (
        "extension_id: fixture.ambient\n"
    )


def test_candidate_builder_excludes_untracked_files_from_every_image_context(
    tmp_path: Path,
) -> None:
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)
    sentinel = "untracked-candidate-secret-sentinel"
    (repo / ".env.bak-audit").write_text(sentinel, encoding="utf-8")
    audit_scratch = repo / "docs/audits/private-evidence.txt"
    audit_scratch.parent.mkdir(parents=True, exist_ok=True)
    audit_scratch.write_text(sentinel, encoding="utf-8")
    (repo / "untracked-sentinel.txt").write_text(sentinel, encoding="utf-8")
    assert subprocess.run(["git", "diff", "--quiet"], cwd=repo, check=False).returncode == 0
    assert (
        subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False).returncode
        == 0
    )

    result = _run_candidate_builder(
        repo,
        bin_dir,
        prod_env,
        forbidden_build_signature=sentinel,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    contexts = (tmp_path / "docker-contexts.log").read_text(encoding="utf-8").splitlines()
    # A census of the builds, not the invariant: the invariant is the two assertions
    # below -- every context is the scrubbed copy and they are all the same one. Six:
    # the release ships no extensions, so there is no initial-extensions build.
    assert len(contexts) == 6
    assert all(Path(context).resolve() != repo.resolve() for context in contexts)
    assert len(set(contexts)) == 1


def test_candidate_builder_uses_the_explicit_production_env_for_provenance(
    tmp_path: Path,
) -> None:
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)

    result = _run_candidate_builder(repo, bin_dir, prod_env)

    assert result.returncode == 0, result.stdout + result.stderr
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    release_dir = repo / ".flow-steward/release" / commit
    assert (release_dir / "candidate.env").is_file()
    assert (release_dir / "candidate-provenance.json").is_file()
    assert (release_dir / "candidate-snapshot.json").is_file()
    assert (release_dir / "candidate-finalized.json").is_file()
    finalized = json.loads((release_dir / "candidate-finalized.json").read_text(encoding="utf-8"))
    compose_project = f"fs-candidate-{commit[:12]}"
    assert finalized["compose_working_dir"] == str(repo / "infra/docker")
    assert finalized["resource_identities"] == {
        "containers": [
            f"{compose_project}-{service}-1"
            for service in sorted(
                (
                    "control-plane",
                    "fs-app",
                    "fs-cli",
                    "fs-mcp",
                    "fs-worker",
                    "mailpit",
                    "memory-postgres-extensions",
                    "pg-fs",
                    "rabbitmq-fs",
                    "seaweedfs",
                    "traefik",
                )
            )
        ],
        "images": [
            f"flow-steward-app:candidate-{commit[:12]}",
            f"flow-steward-cli:candidate-{commit[:12]}",
            f"flow-steward-control:candidate-{commit[:12]}",
            f"flow-steward-mcp:candidate-{commit[:12]}",
            f"flow-steward-postgres:candidate-{commit[:12]}",
            f"flow-steward-worker:candidate-{commit[:12]}",
        ],
        "networks": [
            f"{compose_project}_artifact-storage",
            f"{compose_project}_default",
        ],
        "volumes": [
            f"{compose_project}_fs-postgres",
            f"{compose_project}_fs-rabbitmq",
            f"{compose_project}_seaweedfs-data",
        ],
    }
    production_env = (release_dir / "production.env").read_text(encoding="utf-8")
    assert production_env.count("SECURITY_PII_SALT=") == 1
    assert "SECURITY_PII_SALT=fixture" in production_env
    assert "FS_AUTH_COOKIE_SECURE=1" in production_env
    assert production_env.count("FS_AUTH_BOOTSTRAP_TOKEN=") == 1
    assert "FS_AUTH_BOOTSTRAP_TOKEN=\n" in production_env
    assert "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT=\n" in production_env
    assert "FS_AUTH_BOOTSTRAP_STATE_FILE=\n" in production_env
    assert "FS_AUTH_BOOTSTRAP_ENABLED=0" in production_env
    assert "FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY=1" in production_env
    assert "FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY=1" in production_env
    assert "FS_MCP_AUTH_DISABLED=0" in production_env
    snapshot = json.loads((release_dir / "candidate-snapshot.json").read_text(encoding="utf-8"))
    app_port = snapshot["plain_inputs"]["APP_PORT"]
    assert f"FS_PUBLIC_BASE_URL=https://localhost:{app_port}" in production_env
    assert "FS_FILE_STORAGE=bundled" in production_env
    assert "ARTIFACT_STORAGE_BACKEND=s3" in production_env
    assert "ARTIFACT_S3_ENDPOINT_URL=http://seaweedfs:8333" in production_env
    assert (
        "ARTIFACT_S3_BUCKET=flow-steward-artifacts"  # pragma: allowlist secret - public synthetic bucket fixture
        in production_env
    )
    assert "ARTIFACT_S3_REGION=us-east-1" in production_env
    assert "ARTIFACT_S3_USE_SSL=false" in production_env
    artifact_access = next(
        line.partition("=")[2]
        for line in production_env.splitlines()
        if line.startswith("ARTIFACT_S3_ACCESS_KEY=")
    )
    artifact_secret = next(
        line.partition("=")[2]
        for line in production_env.splitlines()
        if line.startswith("ARTIFACT_S3_SECRET_KEY=")
    )
    assert artifact_access.strip("'\"")
    assert artifact_secret.strip("'\"")
    assert artifact_access != artifact_secret
    backup_encryption_key = next(
        line.partition("=")[2]
        for line in production_env.splitlines()
        if line.startswith("FS_BACKUP_ENCRYPTION_KEY=")
    )
    assert backup_encryption_key.strip("'\"")
    storage_identity = release_dir / "runtime" / "seaweedfs-s3.json"
    assert storage_identity.is_file()
    assert stat.S_IMODE(storage_identity.stat().st_mode) == 0o600
    identity = json.loads(storage_identity.read_text(encoding="utf-8"))["identities"][0]
    assert identity["name"] == "flow-steward-core"
    assert identity["credentials"] == [
        {
            "accessKey": artifact_access.strip("'\""),
            "secretKey": artifact_secret.strip("'\""),
        }
    ]
    # System email comes from a connected email extension, never from env.
    assert "SYSTEM_EMAIL" not in production_env
    assert "FS_ALLOW_PRIVATE_REMOTE_URLS=1" in production_env
    assert "FS_DEPLOYMENT_DOCTOR_BASE_URL=http://fs-app:8099" in production_env
    assert f"FS_BUILD_SHA={commit}" in production_env
    for env_name in ("FS_APP_IMAGE", "FS_WORKER_IMAGE", "FS_CONTROL_IMAGE"):
        assert f"{env_name}=sha256:{1:064d}" in production_env
        assert production_env.count(f"{env_name}=") == 1
    assert production_env.count("MCP_IMAGE=") == 1
    assert (  # pragma: allowlist secret
        "FS_CANDIDATE_STATE_DIR=/run/flow-steward-candidate-state"  # pragma: allowlist secret
        in production_env
    )
    assert "FS_CANDIDATE_PROD_ENV=/app/.env.prod" in production_env
    assert 'environment = "production"' in (
        release_dir / "candidate-state" / "stack.toml"
    ).read_text(encoding="utf-8")


def test_candidate_runtime_private_remote_override_requires_explicit_evidence_profile(
    tmp_path: Path,
) -> None:
    source_env = tmp_path / "source.env"
    source_env.write_text("SECURITY_PII_SALT=fixture\n", encoding="utf-8")
    output_env = tmp_path / "default" / "production.env"

    prepare_candidate_runtime(
        source_env=source_env,
        output_env=output_env,
        state_dir=tmp_path / "default" / "state",
        candidate_commit="a" * 40,
        public_port=18099,
    )

    assert "FS_ALLOW_PRIVATE_REMOTE_URLS=" not in output_env.read_text(encoding="utf-8")


def test_candidate_builder_starts_the_candidate_with_an_empty_extensions_directory(
    tmp_path: Path,
) -> None:
    """A release ships no extensions; the candidate starts like a fresh install."""
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)

    result = _run_candidate_builder(repo, bin_dir, prod_env)

    assert result.returncode == 0, result.stdout + result.stderr
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    release_dir = repo / ".flow-steward/release" / commit
    extensions_dir = release_dir / "candidate-extensions"
    assert extensions_dir.is_dir()
    assert not any(extensions_dir.iterdir())
    candidate_env = (release_dir / "candidate.env").read_text(encoding="utf-8")
    assert f"FS_EXTENSIONS_DIR={extensions_dir}\n" in candidate_env


def test_candidate_builder_does_not_publish_partial_identity_when_provenance_fails(
    tmp_path: Path,
) -> None:
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)

    result = _run_candidate_builder(repo, bin_dir, prod_env, fail_provenance=True)

    assert result.returncode == 17
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    release_dir = repo / ".flow-steward/release" / commit
    assert not (release_dir / "candidate.env").exists()
    assert not (release_dir / "candidate-provenance.json").exists()
    assert not (release_dir / "candidate-snapshot.json").exists()
    assert not (release_dir / "candidate-finalized.json").exists()
    assert not (release_dir / "runtime").exists()


def test_candidate_builder_reuses_exact_finalized_candidate_without_rebuild(
    tmp_path: Path,
) -> None:
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)
    first = _run_candidate_builder(repo, bin_dir, prod_env)
    assert first.returncode == 0, first.stdout + first.stderr
    # What is being tested is that the count does not grow across the runs below --
    # the second call must reuse the finalized candidate instead of rebuilding. Six
    # images; the release ships no extensions to build.
    assert (tmp_path / "docker-builds.log").read_text(encoding="utf-8").splitlines() == [
        "build"
    ] * 6

    second = _run_candidate_builder(repo, bin_dir, prod_env)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "candidate_reused=true" in second.stdout
    assert (tmp_path / "docker-builds.log").read_text(encoding="utf-8").splitlines() == [
        "build"
    ] * 6

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    source_manifest = repo / ".flow-steward/release" / commit / "candidate-source.json"
    source_manifest.write_text("{}\n", encoding="utf-8")
    rejected = _run_candidate_builder(repo, bin_dir, prod_env)

    assert rejected.returncode == 2
    assert "source identity does not match" in rejected.stderr
    assert (tmp_path / "docker-builds.log").read_text(encoding="utf-8").splitlines() == [
        "build"
    ] * 6


def test_candidate_scripts_use_durable_repo_owned_inputs() -> None:
    build = BUILD.read_text(encoding="utf-8")
    stack = STACK.read_text(encoding="utf-8")

    assert "git diff --quiet" in build
    assert "BUILD_SHA" in build
    assert "FS_BUILD_REVISION" in build
    assert ".flow-steward/release" in build
    assert "/private/tmp" not in build
    assert "/private/tmp/*" in stack
    assert "candidate env must be durable" in stack
    assert "docker-compose.prod.yaml" in stack
    assert "--env-file" in stack
    assert "FS_RELEASE_COMPOSE_PROJECT" in stack
    assert 'FS_INSTALLER_STATE_DIR="$RELEASE_DIR"' in stack
    assert "git rev-parse --git-common-dir" in build
    assert "python3 scripts/release/generate_candidate_provenance.py" in build


def test_candidate_postgres_image_is_revision_labeled_for_exact_cleanup() -> None:
    build = BUILD.read_text(encoding="utf-8")
    postgres_build = build.split('docker build --build-arg "AGE_REF=PG17/v1.6.0-rc0"', 1)[1].split(
        "\n\nAPP_DIGEST=", 1
    )[0]

    assert '--label "org.opencontainers.image.revision=$COMMIT"' in postgres_build


def test_production_compose_allows_candidate_to_disable_bootstrap_token_state() -> None:
    compose = PROD_COMPOSE.read_text(encoding="utf-8")

    assert 'FS_AUTH_BOOTSTRAP_STATE_FILE: "${FS_AUTH_BOOTSTRAP_STATE_FILE:-}"' in compose


def test_candidate_provenance_runs_in_the_installer_checkout_tooling() -> None:
    """The installer owns Git and YAML; product/retained CLI stay slim."""
    build = BUILD.read_text(encoding="utf-8")

    assert "python3 scripts/release/generate_candidate_provenance.py" in build
    assert '"$APP_IMAGE" scripts/release/generate_candidate_provenance.py' not in build
    installer = (ROOT / "infra/docker/installer/Dockerfile").read_text(encoding="utf-8")
    assert "PyYAML==6.0.3" in installer
    for name in (
        "SECURITY_PII_SALT",
        "FS_METRICS_TOKEN",
        "FS_WORKER_MCP_BEARER_TOKEN",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASS",
        "TLS_CERT",
        "TLS_KEY",
    ):
        assert f'--deployment-input-source "{name}=' in build


def test_candidate_rejects_sdk_wheel_drift_before_any_docker_build() -> None:
    build = BUILD.read_text(encoding="utf-8")

    sdk_check = 'python3 "$SOURCE_ROOT/scripts/release/sync_extension_sdk_wheel.py"'
    assert sdk_check in build
    assert build.index(sdk_check) < build.index("docker build --target app-runtime")
    assert '--root "$SOURCE_ROOT" --check' in build
    assert '--sdk-attestation "$SOURCE_ROOT/core/infrastructure/extension_sdk/public/' in build


def test_deployment_input_provenance_requires_real_presence_without_reading_secret_value(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text(
        "FS_METRICS_TOKEN=secret-value\n",  # pragma: allowlist secret - synthetic fixture
        encoding="utf-8",
    )

    assert _deployment_input_record("FS_METRICS_TOKEN", str(env_file)) == {
        "present": True,
    }
    with pytest.raises(SystemExit, match=r"SECURITY_PII_SALT.*not present"):
        _deployment_input_record("SECURITY_PII_SALT", str(env_file))
    assert "secret-value" not in repr(_deployment_input_record("FS_METRICS_TOKEN", str(env_file)))


def test_candidate_app_image_uses_the_production_runtime_target() -> None:
    build = BUILD.read_text(encoding="utf-8")

    assert 'docker build --target app-runtime --build-arg "BUILD_SHA=$COMMIT"' in build
    assert 'docker build --target worker-runtime --build-arg "BUILD_SHA=$COMMIT"' in build
    assert 'docker build --target control-runtime --build-arg "BUILD_SHA=$COMMIT"' in build
    assert 'docker build --target runtime --build-arg "FS_BUILD_REVISION=$COMMIT"' in build


def test_candidate_builds_and_records_explicit_app_family_role_images() -> None:
    build = BUILD.read_text(encoding="utf-8")
    stack = STACK.read_text(encoding="utf-8")

    for target, env_name in (
        ("app-runtime", "FS_APP_IMAGE"),
        ("worker-runtime", "FS_WORKER_IMAGE"),
        ("control-runtime", "FS_CONTROL_IMAGE"),
    ):
        assert f"--target {target}" in build
        assert env_name in build
        assert env_name in stack
    legacy_name = "FS_" + "IMAGE"
    assert f"printf '{legacy_name}=%s" not in build
    assert f":{legacy_name}" not in stack


def _stage_body(source: str, stage: str, *, next_stage: str | None) -> str:
    marker = f" AS {stage}"
    start = next(
        source.index(line)
        for line in source.splitlines(keepends=True)
        if line.startswith("FROM ") and line.rstrip().endswith(marker)
    )
    if next_stage is None:
        return source[start:]
    next_marker = f" AS {next_stage}"
    end = next(
        offset
        for offset, line in _line_offsets(source)
        if offset > start and line.startswith("FROM ") and line.rstrip().endswith(next_marker)
    )
    return source[start:end]


def _line_offsets(source: str):
    offset = 0
    for line in source.splitlines(keepends=True):
        yield offset, line
        offset += len(line)


def test_candidate_images_cache_locked_dependencies_before_revision_metadata() -> None:
    app_source = APP_DOCKERFILE.read_text(encoding="utf-8")
    mcp_source = MCP_DOCKERFILE.read_text(encoding="utf-8")
    cli = _stage_body(app_source, "cli", next_stage="runtime-dependencies")
    runtime_dependencies = _stage_body(
        app_source,
        "runtime-dependencies",
        next_stage="control-dependencies",
    )
    runtime = _stage_body(app_source, "app-runtime", next_stage="worker-runtime")
    mcp_runtime = _stage_body(mcp_source, "runtime", next_stage="local-runtime")

    assert "COPY requirements-runtime.lock ." in runtime_dependencies
    assert "pip install --require-hashes -r requirements-runtime.lock" in runtime_dependencies
    assert "target=/root/.cache/pip,sharing=locked" in runtime_dependencies
    assert "--no-cache-dir" not in runtime_dependencies
    assert "ARG BUILD_SHA" not in runtime_dependencies
    assert "ARG BUILD_SHA" in runtime
    assert cli.index("requirements-cli.lock") < cli.index("ARG FS_BUILD_REVISION")

    # AC17-R144: runtime-stage copies normalise file modes (test_image_file_modes.py).
    assert "COPY --chmod=u=rwX,go=rX requirements-mcp.lock ." in mcp_runtime
    assert "pip install --no-cache-dir --require-hashes -r requirements-mcp.lock" in mcp_runtime
    assert mcp_runtime.index("requirements-mcp.lock") < mcp_runtime.index("ARG FS_BUILD_REVISION")


def test_frontend_candidate_stage_caches_npm_dependencies_before_source_copy() -> None:
    source = APP_DOCKERFILE.read_text(encoding="utf-8")
    dependencies = _stage_body(source, "frontend-dependencies", next_stage="frontend-build")
    build = _stage_body(source, "frontend-build", next_stage="frontend-development")

    manifests = (
        "COPY assets/frontend/package.json assets/frontend/package-lock.json assets/frontend/"
    )
    frontend_source = "COPY assets/frontend/ assets/frontend/"
    external_styles = "COPY assets/platform-components.css assets/platform-components.css"
    assert manifests in dependencies
    assert dependencies.index(manifests) < dependencies.index("npm ci")
    assert frontend_source in build
    assert external_styles in build
    assert (
        build.index(frontend_source) < build.index(external_styles) < build.index("npm run build")
    )
    assert "COPY . ." not in dependencies + build


def test_runtime_candidate_marks_cached_frontend_dist_fresh_after_source_copy() -> None:
    source = APP_DOCKERFILE.read_text(encoding="utf-8")
    runtime = _stage_body(source, "app-runtime", next_stage="worker-runtime")

    source_copy = "COPY --chown=fs:fs --from=source-slices /out/app/ /app/"
    dist_copy = (
        "COPY --chmod=u=rwX,go=rX --chown=fs:fs --from=frontend-build "
        "/app/assets/frontend/dist assets/frontend/dist"
    )
    freshness_marker = "RUN touch assets/frontend/dist/.vite/manifest.json"
    assert source_copy in runtime
    assert dist_copy in runtime
    assert freshness_marker in runtime
    assert runtime.index(source_copy) < runtime.index(dist_copy) < runtime.index(freshness_marker)


def test_recorded_candidate_identity_cannot_be_overwritten_and_runtime_is_digest_pinned() -> None:
    build = BUILD.read_text(encoding="utf-8")

    refusal = "candidate identity is already recorded"
    assert refusal in build
    assert build.index(refusal) < build.index("docker build --target app-runtime")
    assert "printf 'FS_APP_IMAGE=%s\\n' \"$APP_DIGEST\"" in build
    assert "printf 'FS_WORKER_IMAGE=%s\\n' \"$WORKER_DIGEST\"" in build
    assert "printf 'FS_CONTROL_IMAGE=%s\\n' \"$CONTROL_DIGEST\"" in build
    assert "printf 'FS_CLI_IMAGE=%s\\n' \"$CLI_DIGEST\"" in build
    assert "printf 'MCP_IMAGE=%s\\n' \"$MCP_DIGEST\"" in build


def test_production_candidate_compose_uses_required_provenance_inputs() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["fs-app"]["image"].startswith("${FS_APP_IMAGE:?")
    assert services["fs-worker"]["image"].startswith("${FS_WORKER_IMAGE:?")
    assert services["control-plane"]["image"].startswith("${FS_CONTROL_IMAGE:?")
    assert services["fs-cli"]["image"].startswith("${FS_CLI_IMAGE:?")
    assert services["fs-mcp"]["image"].startswith("${MCP_IMAGE:?")


def test_release_base_and_infrastructure_images_are_digest_pinned() -> None:
    for dockerfile in (APP_DOCKERFILE, MCP_DOCKERFILE):
        first_from = next(
            line
            for line in dockerfile.read_text(encoding="utf-8").splitlines()
            if line.startswith("FROM ")
        )
        assert "@sha256:" in first_from

    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    assert "mailpit" not in services
    for service_name in ("traefik", "rabbitmq-fs", "seaweedfs"):
        assert "@sha256:" in services[service_name]["image"]
    assert services["pg-fs"]["image"].startswith("${POSTGRES_IMAGE:?")
    assert services["memory-postgres-extensions"]["image"].startswith("${POSTGRES_IMAGE:?")


def test_candidate_provenance_declares_and_verifies_complete_release_input_graph() -> None:
    generator = PROVENANCE_GENERATOR.read_text(encoding="utf-8")
    verifier = PROVENANCE_VERIFIER.read_text(encoding="utf-8")
    build = BUILD.read_text(encoding="utf-8")

    for option in (
        "--release-input",
        "--base-image",
        "--infrastructure-image",
        "--build-argument",
        "--architecture",
    ):
        assert option in generator
        assert option in build
    for field in (
        "release_inputs",
        "git_lfs_objects",
        "base_images",
        "infrastructure_images",
        "build_arguments",
        "architecture",
    ):
        assert field in generator
        assert field in verifier


def test_complete_provenance_refuses_changed_or_missing_release_input(tmp_path: Path) -> None:
    release_input = tmp_path / "requirements.lock"
    release_input.write_text("locked\n", encoding="utf-8")
    commit = "a" * 40
    manifest = tmp_path / "candidate-source.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-source-v1",
                "candidate_commit": commit,
                "source_sha256": "b" * 64,
                "entries": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    digest = f"sha256:{'c' * 64}"
    payload = {
        "schema_version": "flow-steward-packaged-provenance-v2",
        "candidate_commit": commit,
        "source_sha256": "b" * 64,
        "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "git_lfs_objects": [],
        "release_inputs": {
            "requirements.lock": {
                "sha256": hashlib.sha256(release_input.read_bytes()).hexdigest(),
                "size": release_input.stat().st_size,
            }
        },
        "base_images": {"python": digest},
        "infrastructure_images": {"postgres": digest},
        "image_digests": {"fs-app": digest},
        "build_arguments": {"BUILD_SHA": commit},
        "architecture": "linux/arm64"
        if os.uname().machine in {"arm64", "aarch64"}
        else "linux/amd64",
    }
    provenance = tmp_path / "candidate-provenance.json"
    provenance.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    verify_candidate_provenance(
        root=tmp_path,
        provenance_path=provenance,
        source_manifest_path=manifest,
        commit=commit,
        verify_images=False,
    )
    release_input.write_text("drifted\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="release input differs"):
        verify_candidate_provenance(
            root=tmp_path,
            provenance_path=provenance,
            source_manifest_path=manifest,
            commit=commit,
            verify_images=False,
        )
    release_input.unlink()
    with pytest.raises(SystemExit, match="release input differs"):
        verify_candidate_provenance(
            root=tmp_path,
            provenance_path=provenance,
            source_manifest_path=manifest,
            commit=commit,
            verify_images=False,
        )


def test_complete_provenance_refuses_candidate_image_from_another_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_input = tmp_path / "requirements.lock"
    release_input.write_text("locked\n", encoding="utf-8")
    commit = "a" * 40
    manifest = tmp_path / "candidate-source.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-source-v1",
                "candidate_commit": commit,
                "source_sha256": "b" * 64,
                "entries": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    digest = f"sha256:{'c' * 64}"
    provenance = tmp_path / "candidate-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-packaged-provenance-v2",
                "candidate_commit": commit,
                "source_sha256": "b" * 64,
                "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "git_lfs_objects": [],
                "release_inputs": {
                    "requirements.lock": {
                        "sha256": hashlib.sha256(release_input.read_bytes()).hexdigest(),
                        "size": release_input.stat().st_size,
                    }
                },
                "base_images": {"python": digest},
                "infrastructure_images": {"postgres": digest},
                "image_digests": {"fs-app": digest},
                "build_arguments": {"BUILD_SHA": commit},
                "architecture": "linux/arm64"
                if os.uname().machine in {"arm64", "aarch64"}
                else "linux/amd64",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if "--format" in command:
            return SimpleNamespace(returncode=0, stdout=f"{'d' * 40}\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("scripts.release.verify_candidate_provenance.subprocess.run", fake_run)

    with pytest.raises(
        SystemExit, match=r"candidate image revision mismatch: image_digests\.fs-app"
    ):
        verify_candidate_provenance(
            root=tmp_path,
            provenance_path=provenance,
            source_manifest_path=manifest,
            commit=commit,
            verify_images=True,
        )


def test_production_candidate_excludes_development_mailpit_service() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))

    assert "mailpit" not in compose["services"]


def test_candidate_cli_keeps_product_database_inputs_in_runtime_services() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    for service_name in ("fs-app", "fs-worker", "control-plane"):
        assert services[service_name]["environment"]["FS_DB_DSN"] == "${FS_DB_DSN}"

    assert "FS_DB_DSN" not in services["fs-mcp"]["environment"]
    assert "FS_DB_DSN" not in services["fs-cli"]["environment"]
    assert "LANGGRAPH_POSTGRES_URI" not in services["fs-cli"]["environment"]
    assert services["fs-cli"]["environment"]["FS_RUNTIME_ENVIRONMENT"] == "production"
    assert services["fs-cli"]["environment"]["FS_LOAD_REPO_DOTENV"] == "0"
    assert services["fs-cli"]["environment"]["FS_CONFIGURATION_STATE_ROOT"] == (
        "/run/flow-steward-candidate-state"
    )
    assert (
        "${FS_CANDIDATE_STATE_DIR:?candidate state directory is required}:"
        "/run/flow-steward-candidate-state:rw" in services["fs-cli"]["volumes"]
    )
    assert (
        "${FS_CANDIDATE_PROD_ENV:?candidate production env is required}:/app/.env.prod:rw"
        in services["fs-cli"]["volumes"]
    )


def test_configuration_store_can_read_release_state_without_changing_project_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    state_root = tmp_path / "candidate-state"
    project_root.mkdir()
    state_root.mkdir()
    (state_root / "stack.toml").write_text(
        'environment = "production"\nfile_storage = "bundled"\n',
        encoding="utf-8",
    )

    store = LifecycleConfigurationStore(project_root, state_root=state_root)

    assert store.project_root == project_root.resolve()
    assert store.state_root == state_root.resolve()
    assert store.read_active().environment == "production"


def test_candidate_env_file_is_not_exported_into_role_processes() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    for service_name in ("fs-app", "fs-worker", "control-plane"):
        assert "env_file" not in services[service_name]
        assert "FS_ENV_FILE" not in services[service_name]["environment"]

    assert "env_file" not in services["fs-mcp"]
    assert "env_file" not in services["fs-cli"]
    assert (
        "${FS_CANDIDATE_PROD_ENV:?candidate production env is required}:/app/.env.prod:rw"
    ) in services["fs-cli"]["volumes"]


def test_production_mcp_service_cannot_inherit_the_dev_auth_bypass() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))

    assert compose["services"]["fs-mcp"]["environment"]["FS_MCP_AUTH_DISABLED"] == "0"


def test_production_app_and_worker_mount_no_source_project_workspace() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))

    for service_name in ("fs-app", "fs-worker"):
        volumes = [str(volume) for volume in compose["services"][service_name]["volumes"]]

        assert volumes  # non-vacuity
        assert not any("FS_TEST_PROJECT_ROOT" in volume for volume in volumes)
        assert not any("FS_PROJECTS_ROOT" in volume for volume in volumes)
        assert not any(":/project/" in volume or ":/projects" in volume for volume in volumes)


def test_candidate_stack_exports_selected_production_env_to_services(tmp_path: Path) -> None:
    candidate_env = tmp_path / "candidate.env"
    candidate_env.write_text("FS_BUILD_SHA=candidate\n", encoding="utf-8")
    durable_dir = ROOT / ".flow-steward"
    durable_dir.mkdir(exist_ok=True)
    durable_candidate_env = durable_dir / f"candidate-stack-test-{tmp_path.name}.env"
    durable_candidate_env.symlink_to(candidate_env)
    production_env = tmp_path / "production.env"
    production_env.write_text("FS_AUTH_COOKIE_SECURE=1\n", encoding="utf-8")
    production_env_arg = os.path.relpath(production_env, ROOT)
    candidate_state = tmp_path / "candidate-state"
    candidate_state.mkdir()
    (candidate_state / "stack.toml").write_text('environment = "production"\n', encoding="utf-8")
    (candidate_state / "compose.env").write_text(
        "FS_COMPOSE_PROFILES=storage-seaweedfs\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'FS_ENV_FILE=%s\\n' \"${FS_ENV_FILE:-}\"\n"
        "printf 'args=%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    try:
        result = subprocess.run(
            ["bash", str(STACK), "config"],
            cwd=ROOT,
            env={
                **os.environ,
                "FS_RELEASE_CANDIDATE_COMMIT": "candidate",
                "FS_RELEASE_ENV_FILE": str(durable_candidate_env),
                "FS_RELEASE_PROD_ENV": production_env_arg,
                "FS_CANDIDATE_STATE_DIR": str(candidate_state),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        durable_candidate_env.unlink(missing_ok=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"FS_ENV_FILE={production_env.resolve()}" in result.stdout
    assert "--profile storage-seaweedfs" in result.stdout


def test_candidate_stack_accepts_an_empty_compose_profile_set(tmp_path: Path) -> None:
    candidate_env = tmp_path / "candidate.env"
    candidate_env.write_text("FS_BUILD_SHA=candidate\n", encoding="utf-8")
    durable_dir = ROOT / ".flow-steward"
    durable_dir.mkdir(exist_ok=True)
    durable_candidate_env = durable_dir / f"candidate-empty-profile-{tmp_path.name}.env"
    durable_candidate_env.symlink_to(candidate_env)
    production_env = tmp_path / "production.env"
    production_env.write_text("FS_AUTH_COOKIE_SECURE=1\n", encoding="utf-8")
    candidate_state = tmp_path / "candidate-state"
    candidate_state.mkdir()
    (candidate_state / "stack.toml").write_text('environment = "production"\n', encoding="utf-8")
    (candidate_state / "compose.env").write_text("FS_COMPOSE_PROFILES=\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nprintf 'args=%s\\n' \"$*\"\n", encoding="utf-8")
    fake_docker.chmod(0o755)

    try:
        result = subprocess.run(
            ["bash", str(STACK), "config"],
            cwd=ROOT,
            env={
                **os.environ,
                "FS_RELEASE_CANDIDATE_COMMIT": "candidate",
                "FS_RELEASE_ENV_FILE": str(durable_candidate_env),
                "FS_RELEASE_PROD_ENV": str(production_env),
                "FS_CANDIDATE_STATE_DIR": str(candidate_state),
                "BASH_COMPAT": "3.2",
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        durable_candidate_env.unlink(missing_ok=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--profile" not in result.stdout


def test_candidate_stack_does_not_allow_inherited_shell_secrets_to_override_env_files() -> None:
    stack = STACK.read_text(encoding="utf-8")

    for name in (
        "SECURITY_PII_SALT",
        "FS_METRICS_TOKEN",
        "FS_WORKER_MCP_BEARER_TOKEN",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASS",
    ):
        assert f"-u {name}" in stack


def test_candidate_stack_up_fails_before_compose_mutation_when_bootstrap_is_incomplete(
    tmp_path: Path,
) -> None:
    candidate_env_source = tmp_path / "candidate.env"
    candidate_env_source.write_text("FS_BUILD_SHA=candidate\n", encoding="utf-8")
    durable_candidate_env = ROOT / ".flow-steward" / f"preflight-test-{tmp_path.name}.env"
    durable_candidate_env.symlink_to(candidate_env_source)
    production_env = tmp_path / "production.env"
    production_env.write_text("SECURITY_PII_SALT=configured\n", encoding="utf-8")
    candidate_state = tmp_path / "candidate-state"
    candidate_state.mkdir()
    (candidate_state / "stack.toml").write_text('environment = "production"\n', encoding="utf-8")
    docker_log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$FS_TEST_DOCKER_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    try:
        result = subprocess.run(
            ["bash", str(STACK), "up", "-d", "--no-build"],
            cwd=ROOT,
            env={
                **os.environ,
                "FS_RELEASE_CANDIDATE_COMMIT": "candidate",
                "FS_RELEASE_ENV_FILE": str(durable_candidate_env),
                "FS_RELEASE_PROD_ENV": str(production_env),
                "FS_CANDIDATE_STATE_DIR": str(candidate_state),
                "FS_TEST_DOCKER_LOG": str(docker_log),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        durable_candidate_env.unlink(missing_ok=True)

    assert result.returncode == 2
    assert "candidate bring-up preflight failed" in result.stderr
    assert "candidate_prepare.sh database" in result.stderr
    assert "candidate_prepare.sh worker-identity" in result.stderr
    assert "candidate_stack.sh up -d --no-build" in result.stderr
    assert not docker_log.exists()


def test_candidate_stack_accepts_the_canonical_compose_working_directory(
    tmp_path: Path,
) -> None:
    commit = f"candidate-owner-{tmp_path.name}"
    release_dir = ROOT / ".flow-steward" / "release" / commit
    release_dir.mkdir(parents=True)
    digest = f"sha256:{'a' * 64}"
    extensions_dir = tmp_path / "extensions"
    extensions_dir.mkdir()
    candidate_env = release_dir / "candidate.env"
    candidate_env.write_text(
        "\n".join(
            (
                f"FS_BUILD_SHA={commit}",
                f"FS_APP_IMAGE={digest}",
                f"FS_WORKER_IMAGE={digest}",
                f"FS_CONTROL_IMAGE={digest}",
                f"FS_CLI_IMAGE={digest}",
                f"MCP_IMAGE={digest}",
                f"FS_EXTENSIONS_DIR={extensions_dir}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (release_dir / "candidate-provenance.json").write_text(
        json.dumps(
            {
                "candidate_commit": commit,
                "image_digests": {
                    "fs-app": digest,
                    "fs-worker": digest,
                    "control-plane": digest,
                    "fs-cli": digest,
                    "fs-mcp": digest,
                },
            }
        ),
        encoding="utf-8",
    )
    production_env = tmp_path / "production.env"
    production_env.write_text(
        "\n".join(
            f"{name}=configured"
            for name in (
                "SECURITY_PII_SALT",
                "FS_METRICS_TOKEN",
                "FS_WORKER_MCP_BEARER_TOKEN",
                "FS_DB_DSN",
                "LANGGRAPH_POSTGRES_URI",
                "POSTGRES_DB",
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "RABBITMQ_USER",
                "RABBITMQ_PASS",
                "RABBITMQ_URL",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    candidate_state = tmp_path / "candidate-state"
    candidate_state.mkdir()
    (candidate_state / "stack.toml").write_text('environment = "production"\n', encoding="utf-8")
    docker_log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$*" >>"$FS_TEST_DOCKER_LOG"\n'
        'case "${1:-}" in\n'
        "  image) exit 0 ;;\n"
        "  ps) printf 'candidate-container\\n' ;;\n"
        "  inspect)\n"
        '    case "$*" in\n'
        "      *project.working_dir*) printf '%s\\n' \"$EXPECTED_OWNER_ROOT\" ;;\n"
        "      *project.config_files*) printf '%s\\n' \"$EXPECTED_CONFIG_FILE\" ;;\n"
        "      *) exit 31 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  compose)\n"
        '    case "$*" in\n'
        "      *' ps --status running -q pg-fs'*) printf 'candidate-pg\\n' ;;\n"
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 32 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    try:
        result = subprocess.run(
            ["bash", str(STACK), "up", "-d", "--no-build"],
            cwd=ROOT,
            env={
                **os.environ,
                "EXPECTED_CONFIG_FILE": str(PROD_COMPOSE),
                "EXPECTED_OWNER_ROOT": str(PROD_COMPOSE.parent),
                "FS_CANDIDATE_STATE_DIR": str(candidate_state),
                "FS_RELEASE_CANDIDATE_COMMIT": commit,
                "FS_RELEASE_ENV_FILE": str(candidate_env),
                "FS_RELEASE_PROD_ENV": str(production_env),
                "FS_RELEASE_TLS_CERT": str(production_env),
                "FS_RELEASE_TLS_KEY": str(production_env),
                "FS_TEST_DOCKER_LOG": str(docker_log),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(release_dir, ignore_errors=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert " up -d --no-build" in docker_log.read_text(encoding="utf-8")


def test_candidate_prepare_keeps_upgrade_and_worker_identity_explicit_and_ordered() -> None:
    source = PREPARE.read_text(encoding="utf-8")

    assert PREPARE.is_file()
    assert os.access(PREPARE, os.X_OK)
    assert "database upgrade --author release-candidate" in source
    assert "core.entrypoints.cli.ops.worker_identity_bootstrap" in source
    assert source.index("database upgrade --author release-candidate") < source.index(
        "core.entrypoints.cli.ops.worker_identity_bootstrap"
    )
    bootstrap_start = source.index("prepare_worker_identity()")
    bootstrap_command = source[bootstrap_start : source.index('case "$MODE"', bootstrap_start)]
    assert "--entrypoint python fs-app" in bootstrap_command
    assert '--volume "$CANDIDATE_PROD_ENV:/tmp/flow-steward-installer.env:rw"' in bootstrap_command
    assert "--env-file /tmp/flow-steward-installer.env" in bootstrap_command
    assert (
        "--entrypoint python fs-cli"
        not in bootstrap_command.split(
            "core.entrypoints.cli.ops.worker_identity_bootstrap", maxsplit=1
        )[0]
    )
    assert "fs-worker" not in source
    assert "control-plane" not in source


def test_candidate_runtime_preflight_is_read_only_and_accepts_current_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    class IdentityStore:
        def __init__(self) -> None:
            self.conn = Connection()

        def resolve_machine_token(self, token: str) -> dict[str, object]:
            assert token == "protected-worker-token"  # pragma: allowlist secret
            return {
                "account_id": "account-1",
                "service_account_key": "flow-steward-worker",  # pragma: allowlist secret - account key name
                "scopes": ["projects.read"],
                "allowed_project_ids": [],
            }

    connection = Connection()
    identity_store = IdentityStore()
    monkeypatch.setattr(
        candidate_runtime_preflight,
        "_runtime_env",
        {
            "FS_DB_DSN": "postgresql://protected-dsn",  # pragma: allowlist secret
            "FS_WORKER_MCP_BEARER_TOKEN": "protected-worker-token",  # pragma: allowlist secret
            "FS_DEFAULT_ACCOUNT_ID": "account-1",
        },
    )
    monkeypatch.setattr(candidate_runtime_preflight, "connect_database", lambda _dsn: connection)
    monkeypatch.setattr(
        candidate_runtime_preflight,
        "inspect_migrations",
        lambda _connection, *, root: {"pending": [], "drifted": []},
    )
    monkeypatch.setattr(
        candidate_runtime_preflight,
        "create_identity_store",
        lambda _dsn: identity_store,
    )

    assert candidate_runtime_preflight.main() == 0
    assert connection.closed is True
    assert identity_store.conn.closed is True
    output = capsys.readouterr()
    assert "protected-dsn" not in output.out + output.err
    assert "protected-worker-token" not in output.out + output.err


def test_candidate_runtime_preflight_reads_compact_worker_identity_from_data_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "worker-mcp-token"
    token_path.write_text("compact-worker-token\n", encoding="utf-8")
    monkeypatch.setattr(candidate_runtime_preflight, "_runtime_env", {})
    monkeypatch.setattr(
        candidate_runtime_preflight,
        "_COMPACT_WORKER_TOKEN_PATH",
        token_path,
    )

    assert candidate_runtime_preflight._worker_token() == "compact-worker-token"


def test_candidate_runtime_preflight_reports_pending_migrations_without_identity_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Connection:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        candidate_runtime_preflight,
        "_runtime_env",
        {
            "FS_DB_DSN": "postgresql://protected-dsn",  # pragma: allowlist secret
            "FS_WORKER_MCP_BEARER_TOKEN": "protected-worker-token",  # pragma: allowlist secret
            "FS_DEFAULT_ACCOUNT_ID": "account-1",
        },
    )
    monkeypatch.setattr(candidate_runtime_preflight, "connect_database", lambda _dsn: Connection())
    monkeypatch.setattr(
        candidate_runtime_preflight,
        "inspect_migrations",
        lambda _connection, *, root: {
            "pending": ["20990101000000_pending_probe.sql"],
            "drifted": [],
        },
    )
    monkeypatch.setattr(
        candidate_runtime_preflight,
        "create_identity_store",
        lambda _dsn: pytest.fail("identity lookup must not run before migration readiness"),
    )

    assert candidate_runtime_preflight.main() == 2
    output = capsys.readouterr()
    assert "20990101000000_pending_probe.sql" in output.err
    assert "protected-dsn" not in output.err
    assert "protected-worker-token" not in output.err


def test_a_build_behind_an_ingress_that_owns_tls_does_not_demand_a_certificate_file(
    tmp_path: Path,
) -> None:
    """AC17-R143: "deployment input TLS_CERT source is not readable: .../.ssl/cert.pem".

    Behind an existing Traefik the certificate belongs to that Traefik and no file
    exists. The fixture provenance writer rejects missing deployment input files,
    so naming a nonexistent certificate still fails whenever the build asks for it.
    """
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)
    absent = str(tmp_path / "no-such" / "cert.pem")

    result = _run_candidate_builder(
        repo,
        bin_dir,
        prod_env,
        extra_env={
            "FS_RELEASE_TLS_INPUTS": "ingress",
            "FS_RELEASE_TLS_CERT": absent,
            "FS_RELEASE_TLS_KEY": absent,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_build_that_holds_its_certificate_still_requires_it(tmp_path: Path) -> None:
    """The green direction of the change above is this red one: the default is unchanged."""
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)
    absent = str(tmp_path / "no-such" / "cert.pem")

    result = _run_candidate_builder(
        repo,
        bin_dir,
        prod_env,
        extra_env={"FS_RELEASE_TLS_CERT": absent, "FS_RELEASE_TLS_KEY": absent},
    )

    assert result.returncode != 0


def test_an_unknown_tls_input_mode_is_refused_by_name(tmp_path: Path) -> None:
    repo, bin_dir, prod_env = _candidate_builder_fixture(tmp_path)

    result = _run_candidate_builder(
        repo, bin_dir, prod_env, extra_env={"FS_RELEASE_TLS_INPUTS": "external"}
    )

    assert result.returncode == 2
    assert 'FS_RELEASE_TLS_INPUTS must be "local" or "ingress"' in result.stderr
