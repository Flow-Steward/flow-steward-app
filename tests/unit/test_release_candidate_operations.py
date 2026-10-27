from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.release.candidate_image_archive import write_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON = REPO_ROOT / "scripts" / "release" / "candidate_operation_common.sh"
VERIFY_PROVENANCE = REPO_ROOT / "scripts" / "release" / "verify_candidate_provenance.py"
ROLLBACK = REPO_ROOT / "scripts" / "release" / "candidate_rollback.sh"
REBUILD = REPO_ROOT / "scripts" / "release" / "candidate_rebuild.sh"
RESTART = REPO_ROOT / "scripts" / "release" / "candidate_restart.sh"
UPGRADE = REPO_ROOT / "scripts" / "release" / "candidate_upgrade.sh"


def _test_commit(tmp_path: Path) -> str:
    """A candidate commit unique to this test, even across xdist workers.

    The release directory lives under the shared workspace's .flow-steward, so a
    fixed commit let two tests on different workers write the same directory at
    once (ADR 0037). tmp_path is unique per worker and per test.
    """
    return hashlib.sha256(str(tmp_path).encode()).hexdigest()[:40]


def _write_candidate_tls_inputs(tmp_path: Path) -> tuple[Path, Path]:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("synthetic-candidate-cert\n", encoding="utf-8")
    key.write_text("synthetic-candidate-key\n", encoding="utf-8")
    return cert, key


def _write_candidate_state(release_dir: Path) -> Path:
    state_dir = release_dir / "candidate-state"
    state_dir.mkdir()
    (state_dir / "stack.toml").write_text('environment = "production"\n', encoding="utf-8")
    return state_dir


def _write_candidate_provenance(release_dir: Path, commit: str) -> tuple[Path, Path]:
    source_manifest = release_dir / "candidate-source.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-candidate-source-v1",
                "candidate_commit": commit,
                "source_sha256": "a" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source_manifest_sha256 = hashlib.sha256(source_manifest.read_bytes()).hexdigest()
    input_path = REPO_ROOT / "scripts/release/candidate_restart.sh"
    machine = {"aarch64": "arm64", "x86_64": "amd64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    digest = f"sha256:{'b' * 64}"
    provenance = release_dir / "candidate-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-packaged-provenance-v2",
                "candidate_commit": commit,
                "source_manifest_sha256": source_manifest_sha256,
                "source_sha256": "a" * 64,
                "git_lfs_objects": [],
                "release_inputs": {
                    "scripts/release/candidate_restart.sh": {
                        "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                        "size": input_path.stat().st_size,
                    }
                },
                "base_images": {"python": digest},
                "infrastructure_images": {"postgres": digest},
                "image_digests": {"fs-app": digest},
                "build_arguments": {"BUILD_SHA": commit},
                "architecture": f"linux/{machine}",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return provenance, source_manifest


def _run_without_release_authorization(script: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (
        "FS_SMOKE_ISOLATED",
        "FS_SMOKE_CANDIDATE_COMMIT",
        "FS_SMOKE_COMPOSE_PROJECT",
        "FS_SMOKE_PG_CONTAINER",
    ):
        env.pop(name, None)
    return subprocess.run(
        ["bash", str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_candidate_operator_scripts_exist_and_are_executable() -> None:
    for script in (COMMON, ROLLBACK, REBUILD, RESTART, UPGRADE):
        assert script.is_file()
        assert os.access(script, os.X_OK)


def test_candidate_operations_verify_complete_provenance_before_compose_mutation() -> None:
    common = COMMON.read_text(encoding="utf-8")

    assert VERIFY_PROVENANCE.is_file()
    verify_call = "verify_candidate_provenance.py"
    assert verify_call in common
    assert common.index(verify_call) < common.index("candidate_stack config --quiet")
    assert "--verify-images" in common


def test_candidate_operator_scripts_fail_closed_without_isolation_authorization() -> None:
    for script in (ROLLBACK, REBUILD, RESTART, UPGRADE):
        result = _run_without_release_authorization(script)
        assert result.returncode == 2
        assert "FS_SMOKE_ISOLATED=1" in result.stderr


def test_candidate_rollback_is_project_bound_and_restores_a_custom_dump() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")

    assert "com.docker.compose.project" in common
    assert "FS_SMOKE_PG_CONTAINER" in source
    assert "pg_dump" in source
    assert "-Fc" in source
    assert "dropdb" in source
    assert "--if-exists" in source
    assert "--force" in source
    assert "createdb" in source
    assert "pg_restore" in source
    assert "--clean" not in source
    assert "pg_terminate_backend" in source
    assert "sha256" in source
    assert "ps --services --filter status=running" in common
    assert "restore_candidate_services" in source
    assert source.index("begin_candidate_maintenance") < source.index("pg_dump")
    assert "--drop-only" in source
    assert "rebuild_workflow_knowledge_graph" in source
    assert source.index("rebuild_workflow_knowledge_graph") < source.index("wait_candidate_ready")


def test_candidate_upgrade_uses_shared_writer_barrier() -> None:
    source = UPGRADE.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")

    assert "begin_candidate_maintenance" in source
    assert source.index("begin_candidate_maintenance") < source.index("database upgrade")
    assert "exec -T fs-cli python -m core.entrypoints.cli --non-interactive" in source
    assert "run --rm -T fs-cli" not in source
    assert "restore_candidate_services" in source
    for service in ("fs-app", "fs-worker", "control-plane", "fs-mcp", "fs-cli"):
        assert service in common


def test_stateful_candidate_operations_run_shared_preflight_before_mutation() -> None:
    common = COMMON.read_text(encoding="utf-8")
    restart = RESTART.read_text(encoding="utf-8")
    rollback = ROLLBACK.read_text(encoding="utf-8")

    assert "preflight_candidate_operation()" in common
    assert "candidate_stack config --quiet" in common
    assert "candidate-provenance.json" in common
    assert "df -Pk" in common
    assert ".ssl/cert.pem" in common
    assert ".ssl/key.pem" in common
    assert restart.index("preflight_candidate_operation") < restart.index(
        "begin_candidate_maintenance"
    )
    assert rollback.index("preflight_candidate_operation") < rollback.index("pg_dump")
    assert "wait_candidate_ready" in common
    assert rollback.index("restore_candidate_services") < rollback.index("wait_candidate_ready")


def test_candidate_rebuild_and_restart_are_commit_and_project_bound() -> None:
    rebuild = REBUILD.read_text(encoding="utf-8")
    restart = RESTART.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")

    assert "build_candidate.sh" not in rebuild
    assert "restore_candidate_images" in rebuild
    assert rebuild.index("restore_candidate_images") < rebuild.index(
        "preflight_candidate_operation"
    )
    assert "wait_candidate_ready" in rebuild
    assert "preflight_candidate_operation" in rebuild
    assert "FS_SMOKE_CANDIDATE_COMMIT" in common
    assert "--force-recreate" in rebuild
    assert "com.docker.compose.project" in common
    assert "FS_SMOKE_COMPOSE_PROJECT" in common
    assert 'git -C "$REPO_ROOT" symbolic-ref -q HEAD' in common
    assert "restart" in restart


def test_candidate_images_restore_twice_after_independent_deletion(tmp_path: Path) -> None:
    commit = _test_commit(tmp_path)
    release_dir = REPO_ROOT / ".flow-steward" / "release" / commit
    release_dir.mkdir(parents=True, exist_ok=True)
    archive = release_dir / "candidate-images.tar"
    archive.write_bytes(b"durable-candidate-image-archive")
    archive_images = {
        "fs-app": "sha256:" + "1" * 64,
        "fs-worker": "sha256:" + "2" * 64,
        "control-plane": "sha256:" + "3" * 64,
        "fs-cli": "sha256:" + "4" * 64,
        "fs-mcp": "sha256:" + "5" * 64,
        "postgres": "sha256:" + "6" * 64,
    }
    write_manifest(
        archive_path=archive,
        output_path=release_dir / "candidate-images.json",
        candidate_commit=commit,
        images=archive_images,
    )
    infrastructure = {
        "traefik": "example.invalid/traefik@sha256:" + "5" * 64,
        "rabbitmq": "example.invalid/rabbitmq@sha256:" + "6" * 64,
        "mailpit": "example.invalid/mailpit@sha256:" + "7" * 64,
        "seaweedfs": "example.invalid/seaweedfs@sha256:" + "9" * 64,
    }
    (release_dir / "candidate.env").write_text(
        "\n".join(
            (
                f"FS_APP_IMAGE={archive_images['fs-app']}",
                f"FS_WORKER_IMAGE={archive_images['fs-worker']}",
                f"FS_CONTROL_IMAGE={archive_images['control-plane']}",
                f"FS_CLI_IMAGE={archive_images['fs-cli']}",
                f"MCP_IMAGE={archive_images['fs-mcp']}",
                f"POSTGRES_IMAGE={archive_images['postgres']}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (release_dir / "candidate-provenance.json").write_text(
        json.dumps({"infrastructure_images": infrastructure}) + "\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    image_state = tmp_path / "images.txt"
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$*" >>"$FS_TEST_DOCKER_LOG"\n'
        'if [ "$1 $2" = "image inspect" ]; then grep -Fxq -- "$3" "$FS_TEST_IMAGE_STATE"; exit; fi\n'
        'if [ "$1 $2" = "image load" ]; then printf \'%s\\n\' "$FS_TEST_ARCHIVE_IMAGES" >>"$FS_TEST_IMAGE_STATE"; exit; fi\n'
        'if [ "$1" = "pull" ]; then printf \'%s\\n\' "$2" >>"$FS_TEST_IMAGE_STATE"; exit; fi\n'
        "exit 19\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "FS_TEST_ARCHIVE_IMAGES": "\n".join(archive_images.values()),
        "FS_TEST_DOCKER_LOG": str(docker_log),
        "FS_TEST_IMAGE_STATE": str(image_state),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
    }

    try:
        for _attempt in range(2):
            image_state.write_text("", encoding="utf-8")
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'source "{COMMON}"; CANDIDATE_COMMIT="{commit}"; restore_candidate_images',
                ],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            restored = set(image_state.read_text(encoding="utf-8").splitlines())
            assert restored == set(archive_images.values()) | set(infrastructure.values())
    finally:
        shutil.rmtree(release_dir, ignore_errors=True)

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("image load --input ") for call in calls) == 2


def test_candidate_restart_waits_for_the_full_stack_to_be_ready() -> None:
    restart = RESTART.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")

    assert "wait_candidate_ready" in restart
    assert "FS_SMOKE_BASE" in common
    assert '"$readiness_url"' in common
    assert "curl -ksSf" in common
    assert "RESTART_READY_ATTEMPTS" in common


@pytest.mark.parametrize(
    ("explicit_base", "expected_url"),
    (
        ("", "https://localhost:18443/health/ready"),
        ("https://explicit.example", "https://explicit.example/health/ready"),
    ),
)
def test_candidate_readiness_uses_candidate_port_unless_base_is_explicit(
    tmp_path: Path,
    explicit_base: str,
    expected_url: str,
) -> None:
    commit = _test_commit(tmp_path)
    release_dir = REPO_ROOT / ".flow-steward" / "release" / commit
    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / "candidate.env").write_text("APP_PORT=18443\n", encoding="utf-8")
    curl_log = tmp_path / "curl.log"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "${!#}" >"$FS_TEST_CURL_LOG"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "FS_SMOKE_CANDIDATE_COMMIT": commit,
        "FS_TEST_CURL_LOG": str(curl_log),
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
    }
    if explicit_base:
        env["FS_SMOKE_BASE"] = explicit_base
    else:
        env.pop("FS_SMOKE_BASE", None)
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{COMMON}"; CANDIDATE_COMMIT="$FS_SMOKE_CANDIDATE_COMMIT"; '
                "wait_candidate_ready",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        (release_dir / "candidate.env").unlink(missing_ok=True)
        release_dir.rmdir()

    assert result.returncode == 0, result.stdout + result.stderr
    assert curl_log.read_text(encoding="utf-8").strip() == expected_url


@pytest.mark.parametrize("tamper_source_manifest", (False, True))
def test_candidate_restart_verifies_source_identity_before_mutation(
    tmp_path: Path,
    tamper_source_manifest: bool,
) -> None:
    commit = _test_commit(tmp_path)
    project = f"fs-candidate-{commit[:12]}"
    candidate_env_source = tmp_path / "candidate.env"
    candidate_env_source.write_text("FS_BUILD_SHA=test\n", encoding="utf-8")
    candidate_env = REPO_ROOT / ".flow-steward" / f"restart-test-{_test_commit(tmp_path)[:12]}.env"
    candidate_env.symlink_to(candidate_env_source)
    production_env = tmp_path / "production.env"
    production_env.write_text("FS_SECRET_KEY=test\n", encoding="utf-8")
    tls_cert, tls_key = _write_candidate_tls_inputs(tmp_path)
    release_dir = REPO_ROOT / ".flow-steward" / "release" / commit
    release_dir.mkdir(parents=True, exist_ok=True)
    candidate_state = _write_candidate_state(release_dir)
    provenance, source_manifest = _write_candidate_provenance(release_dir, commit)
    if tamper_source_manifest:
        source_manifest.write_text("{}\n", encoding="utf-8")
    curl_log = tmp_path / "curl.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *symbolic-ref*) exit 1 ;; esac\n'
        f"printf '%s\\n' '{commit}'\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "${{1:-}} ${{2:-}}" = "image inspect" ]; then printf \'%s\\n\' "{commit}"; exit 0; fi\n'
        'if [ "${1:-}" = inspect ]; then printf \'%s\\n\' "$FS_TEST_PROJECT"; fi\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "${!#}" >>"$FS_TEST_CURL_LOG"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    try:
        result = subprocess.run(
            ["bash", str(RESTART)],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "FS_SMOKE_ISOLATED": "1",
                "FS_SMOKE_CANDIDATE_COMMIT": commit,
                "FS_SMOKE_COMPOSE_PROJECT": project,
                "FS_SMOKE_APP_CONTAINER": "candidate-app",
                "FS_SMOKE_BASE": "https://candidate.example",
                "FS_RELEASE_ENV_FILE": str(candidate_env),
                "FS_RELEASE_PROD_ENV": str(production_env),
                "FS_RELEASE_TLS_CERT": str(tls_cert),
                "FS_RELEASE_TLS_KEY": str(tls_key),
                "FS_TEST_PROJECT": project,
                "FS_TEST_CURL_LOG": str(curl_log),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        candidate_env.unlink(missing_ok=True)
        provenance.unlink(missing_ok=True)
        source_manifest.unlink(missing_ok=True)
        (candidate_state / "stack.toml").unlink(missing_ok=True)
        candidate_state.rmdir()
        release_dir.rmdir()

    if tamper_source_manifest:
        assert result.returncode == 2
        assert "candidate complete provenance" in result.stderr
        assert not curl_log.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_log.read_text(encoding="utf-8").splitlines() == [
            "https://candidate.example/health/ready"
        ]


def test_candidate_rollback_recreates_the_isolated_database_before_restore(
    tmp_path: Path,
) -> None:
    commit = _test_commit(tmp_path)
    project = f"fs-candidate-{commit[:12]}"
    release_dir = REPO_ROOT / ".flow-steward" / "release" / commit
    release_dir.mkdir(parents=True, exist_ok=True)
    candidate_state = _write_candidate_state(release_dir)
    candidate_env = release_dir / "candidate.env"
    candidate_env.write_text("FS_BUILD_SHA=test\nAPP_PORT=18443\n", encoding="utf-8")
    provenance, source_manifest = _write_candidate_provenance(release_dir, commit)
    production_env = tmp_path / "production.env"
    production_env.write_text("FS_SECRET_KEY=test\n", encoding="utf-8")
    tls_cert, tls_key = _write_candidate_tls_inputs(tmp_path)
    docker_log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *symbolic-ref*) exit 1 ;; esac\n'
        f"printf '%s\\n' '{commit}'\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >>"$FS_TEST_DOCKER_LOG"\n'
        f'if [ "${{1:-}} ${{2:-}}" = "image inspect" ]; then printf \'%s\\n\' "{commit}"; exit 0; fi\n'
        'if [ "${1:-}" = inspect ]; then printf \'%s\\n\' "$FS_TEST_PROJECT"; fi\n'
        "case \"$*\" in *pg_dump*) printf 'fake-dump' ;; *pg_restore*) cat >/dev/null ;; esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_shasum = bin_dir / "shasum"
    fake_shasum.write_text(
        "#!/usr/bin/env bash\nprintf '%064d  %s\\n' 0 \"${!#}\"\n",
        encoding="utf-8",
    )
    fake_shasum.chmod(0o755)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)

    try:
        result = subprocess.run(
            ["bash", str(ROLLBACK)],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "FS_SMOKE_ISOLATED": "1",
                "FS_SMOKE_CANDIDATE_COMMIT": commit,
                "FS_SMOKE_COMPOSE_PROJECT": project,
                "FS_SMOKE_PG_CONTAINER": "candidate-pg",
                "FS_RELEASE_ENV_FILE": str(candidate_env),
                "FS_RELEASE_PROD_ENV": str(production_env),
                "FS_RELEASE_TLS_CERT": str(tls_cert),
                "FS_RELEASE_TLS_KEY": str(tls_key),
                "FS_TEST_PROJECT": project,
                "FS_TEST_DOCKER_LOG": str(docker_log),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        candidate_env.unlink(missing_ok=True)
        provenance.unlink(missing_ok=True)
        source_manifest.unlink(missing_ok=True)
        (release_dir / "rollback-smoke.sha256").unlink(missing_ok=True)
        (candidate_state / "stack.toml").unlink(missing_ok=True)
        candidate_state.rmdir()
        release_dir.rmdir()

    assert result.returncode == 0, result.stdout + result.stderr
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    drop_index = next(i for i, call in enumerate(calls) if "dropdb --if-exists --force" in call)
    create_index = next(i for i, call in enumerate(calls) if "createdb" in call)
    restore_index = next(
        i for i, call in enumerate(calls) if "pg_restore" in call and "pg_restore -l" not in call
    )
    assert drop_index < create_index < restore_index
