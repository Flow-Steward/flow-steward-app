from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = ROOT / "install.sh"
INSTALLER_DOCKERFILE = ROOT / "infra/docker/installer/Dockerfile"


def test_missing_docker_points_to_official_installation_without_creating_state(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dirname = shutil.which("dirname")
    bash = shutil.which("bash")
    assert dirname is not None
    assert bash is not None
    (bin_dir / "dirname").symlink_to(dirname)
    _write_shim(bin_dir / "id", "#!/bin/sh\nprintf '1000\\n'\n")
    state_dir = tmp_path / "state"

    result = subprocess.run(
        [bash, str(INSTALL_SCRIPT)],
        env={"PATH": str(bin_dir), "FS_INSTALLER_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 127
    assert result.stdout == ""
    assert "Docker CLI is unavailable" in result.stderr
    assert "https://docs.docker.com/engine/install/" in result.stderr
    assert "https://docs.docker.com/desktop/" in result.stderr
    assert "./install.sh" in result.stderr
    assert not state_dir.exists()


def _write_shim(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_docker(tmp_path: Path, *, build_delay: float = 0.0) -> Path:
    log_path = tmp_path / "docker.log"
    _write_shim(
        tmp_path / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
{{ printf '%s' "${{1:-}}"; for arg in "${{@:2}}"; do printf '\t%s' "${{arg}}"; done; printf '\n'; }} >> "${{DOCKER_LOG}}"
docker_command="${{1:-}}"
if [[ "${{docker_command}}" == "--host" ]]; then
  docker_command="${{3:-}}"
fi
if [[ -n "${{DOCKER_ENV_LOG:-}}" ]]; then
  printf '%s\t%s\t%s\t%s\n' \
    "${{docker_command}}" \
    "${{DOCKER_CONFIG:-}}" \
    "${{BUILDX_CONFIG:-}}" \
    "${{BUILDX_NO_DEFAULT_ATTESTATIONS:-}}" \
    >> "${{DOCKER_ENV_LOG}}"
fi
if [[ "${{docker_command}}" == "run" && "${{FAKE_EXPECT_HELPER_MOUNT:-0}}" == "1" ]]; then
  helper_mount="${{FAKE_HELPER_CONFIG}}:${{FAKE_CONTAINER_DOCKER_CONFIG}}:ro"
  for docker_arg in "$@"; do
    if [[ "${{docker_arg}}" == "${{helper_mount}}" ]]; then
      if [[ ! -d "${{FAKE_HELPER_CONFIG}}" ]]; then
        echo "helper config mount source is missing: ${{FAKE_HELPER_CONFIG}}" >&2
        exit 65
      fi
      helper_mount_found=1
      break
    fi
  done
  if [[ "${{helper_mount_found:-0}}" != "1" ]]; then
    echo "expected isolated helper config mount: ${{helper_mount}}" >&2
    exit 65
  fi
fi
if [[ "${{docker_command}}" == "build" ]]; then
  sleep {build_delay}
  echo "build output retained"
fi
if [[ "${{1:-}}" == "context" && "${{2:-}}" == "inspect" ]]; then
  printf '%s\n' "${{FAKE_CONTEXT_ENDPOINT}}"
fi
if [[ "${{docker_command}}" == "image" && "$*" == *" inspect "* ]]; then
  if [[ -n "${{FAKE_EXISTING_IMAGE_ID:-}}" && "${{@: -1}}" == "${{FAKE_EXISTING_IMAGE_ID}}" ]]; then
    printf '[{{"Id":"%s"}}]\n' "${{FAKE_EXISTING_IMAGE_ID}}"
    exit 0
  fi
  exit 1
fi
if [[ -n "${{FAKE_CONTAINER_STATE:-}}" && "${{docker_command}}" == "container" ]]; then
  case " $* " in
    *" inspect "*) cat "${{FAKE_CONTAINER_STATE}}" 2>/dev/null || echo false; exit 0 ;;
    *" kill "*) echo false > "${{FAKE_CONTAINER_STATE}}"; exit 0 ;;
  esac
fi
if [[ "${{docker_command}}" == "run" && "${{FAKE_RUNTIME_CLIENT_DIES:-0}}" == "1" ]]; then
  previous=""
  for docker_arg in "$@"; do
    if [[ "${{previous}}" == "--cidfile" ]]; then
      printf '%064d\n' 0 > "${{docker_arg}}"
      break
    fi
    previous="${{docker_arg}}"
  done
  if [[ "${{FAKE_CONTAINER_STOPS_ITSELF:-0}}" == "1" ]]; then
    echo false > "${{FAKE_CONTAINER_STATE}}"
  else
    echo true > "${{FAKE_CONTAINER_STATE}}"
  fi
  echo "docker: Error response from daemon: container is not running" >&2
  exit 125
fi
if [[ "${{docker_command}}" == "run" && "${{FAKE_RUNTIME_WAIT:-0}}" == "1" ]]; then
  previous=""
  for docker_arg in "$@"; do
    if [[ "${{previous}}" == "--cidfile" ]]; then
      printf '%064d\n' 0 > "${{docker_arg}}"
      break
    fi
    previous="${{docker_arg}}"
  done
  trap 'exit 10' INT TERM
  while true; do sleep 1; done
fi
exit 0
""",
    )
    return log_path


def _run(
    tmp_path: Path,
    *args: str,
    delay: float = 0.0,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    docker_log = _fake_docker(tmp_path, build_delay=delay)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{tmp_path}{os.pathsep}{env.get('PATH', '')}",
            "DOCKER_LOG": str(docker_log),
            "FAKE_CONTEXT_ENDPOINT": f"unix://{tmp_path / 'docker.sock'}",
            "FS_INSTALLER_IMAGE": "flow-steward-installer:test",
            "FS_INSTALLER_STATE_DIR": str(tmp_path / "state"),
            "FS_INSTALLER_HEARTBEAT_SECONDS": "0.05",
            "PYTHON_BIN": "/definitely/not/python",
        }
    )
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    docker_lines = (
        docker_log.read_text(encoding="utf-8").splitlines() if docker_log.exists() else []
    )
    return completed, docker_lines


@dataclass
class _FakeInstallHost:
    environment: dict[str, str]
    docker_log: Path
    docker_environment_log: Path
    mktemp_log: Path
    bootstrap_config: Path
    helper_config: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), *args],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self._result = result
        return result

    @property
    def docker_run(self) -> str:
        lines = self.docker_log.read_text(encoding="utf-8").splitlines()
        return next(line for line in lines if line.startswith("run\t")).replace("\t", " ")

    def docker_environment(self, command: str) -> tuple[str, str]:
        lines = self.docker_environment_log.read_text(encoding="utf-8").splitlines()
        _command, docker_config, buildx_config, _attestations = next(
            line.split("\t") for line in lines if line.startswith(f"{command}\t")
        )
        return docker_config, buildx_config

    def docker_attestations_disabled(self, command: str) -> bool:
        lines = self.docker_environment_log.read_text(encoding="utf-8").splitlines()
        _command, _docker_config, _buildx_config, attestations = next(
            line.split("\t") for line in lines if line.startswith(f"{command}\t")
        )
        return attestations == "1"

    @property
    def mktemp_paths(self) -> list[str]:
        return self.mktemp_log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def fake_install_host(tmp_path: Path):
    def create(
        *,
        context_endpoint: str,
        socket_uid: int,
        socket_gid: int,
        docker_config: str,
        platform: str,
    ) -> _FakeInstallHost:
        docker_log = _fake_docker(tmp_path)
        docker_environment_log = tmp_path / "docker-environment.log"
        mktemp_log = tmp_path / "mktemp.log"
        bootstrap_config = tmp_path / "bootstrap-docker-config"
        helper_config = tmp_path / "docker-config"
        if helper_config.exists():
            pytest.fail(f"test helper config already exists: {helper_config}")
        docker_home = tmp_path / "home"
        config_path = docker_home / ".docker" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(docker_config, encoding="utf-8")
        host_buildx_config = tmp_path / "host-buildx"
        host_buildx_config.mkdir()
        if platform == "Darwin":
            stat_script = """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "-Lf" || "${2:-}" != "%g" || "${3:-}" != "${FAKE_SOCKET_PATH}" ]]; then
  echo "expected stat -Lf %g for ${FAKE_SOCKET_PATH}" >&2
  exit 64
fi
printf '%s\\n' "${FAKE_SOCKET_GID}"
"""
        else:
            stat_script = """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-Lf" && "${2:-}" == "%g" && "${3:-}" == "${FAKE_SOCKET_PATH}" ]]; then
  printf 'File: %s\\n%s\\n' "${FAKE_SOCKET_PATH}" "${FAKE_SOCKET_GID}"
  exit 0
fi
if [[ "${1:-}" != "-Lc" || "${2:-}" != "%g" || "${3:-}" != "${FAKE_SOCKET_PATH}" ]]; then
  echo "expected stat -Lc %g for ${FAKE_SOCKET_PATH}" >&2
  exit 64
fi
printf '%s\\n' "${FAKE_SOCKET_GID}"
"""
        _write_shim(
            tmp_path / "stat",
            stat_script,
        )
        _write_shim(
            tmp_path / "uname",
            f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "{platform}"
""",
        )
        _write_shim(
            tmp_path / "mktemp",
            """#!/usr/bin/env bash
set -euo pipefail
call_count=0
if [[ -f "${FAKE_MKTEMP_LOG}.count" ]]; then
  call_count="$(<"${FAKE_MKTEMP_LOG}.count")"
fi
call_count=$((call_count + 1))
printf '%s\\n' "${call_count}" > "${FAKE_MKTEMP_LOG}.count"
if [[ "${call_count}" -eq 1 ]]; then
  config_dir="${FAKE_BOOTSTRAP_CONFIG}"
else
  config_dir="${FAKE_HELPER_CONFIG}"
fi
mkdir -p "${config_dir}"
printf '%s\\n' "${config_dir}" >> "${FAKE_MKTEMP_LOG}"
printf '%s\\n' "${config_dir}"
""",
        )
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{tmp_path}{os.pathsep}{env.get('PATH', '')}",
                "DOCKER_LOG": str(docker_log),
                "DOCKER_ENV_LOG": str(docker_environment_log),
                "FAKE_CONTEXT_ENDPOINT": context_endpoint,
                "FAKE_SOCKET_PATH": context_endpoint.removeprefix("unix://"),
                "FAKE_SOCKET_GID": str(socket_gid),
                "FAKE_MKTEMP_LOG": str(mktemp_log),
                "FAKE_BOOTSTRAP_CONFIG": str(bootstrap_config),
                "FAKE_HELPER_CONFIG": str(helper_config),
                "FAKE_CONTAINER_DOCKER_CONFIG": "/tmp/flow-steward-docker-config",
                "FAKE_EXPECT_HELPER_MOUNT": "1"
                if '"credsStore"' in docker_config or '"credHelpers"' in docker_config
                else "0",
                "FS_INSTALLER_IMAGE": "flow-steward-installer:test",
                "FS_INSTALLER_STATE_DIR": str(tmp_path / "state"),
                "FS_INSTALLER_HEARTBEAT_SECONDS": "0.05",
                "HOME": str(docker_home),
                "DOCKER_CONFIG": str(config_path.parent),
                "BUILDX_CONFIG": str(host_buildx_config),
                "PYTHON_BIN": "/definitely/not/python",
            }
        )
        return _FakeInstallHost(
            environment=env,
            docker_log=docker_log,
            docker_environment_log=docker_environment_log,
            mktemp_log=mktemp_log,
            bootstrap_config=bootstrap_config,
            helper_config=helper_config,
        )

    return create


def test_install_sh_invokes_only_private_installer_controller(tmp_path: Path) -> None:
    completed, docker_lines = _run(tmp_path, "--non-interactive", "--confirm")

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr.startswith("Flow Steward installer:")
    assert "build output retained" in completed.stderr
    run_line = next(line for line in docker_lines if line.startswith("run\t"))
    assert f"{ROOT}:{ROOT}" in run_line
    assert f"-w\t{ROOT}" in run_line
    state_dir = tmp_path / "state"
    assert f"-e\tFS_INSTALLER_STATE_DIR={state_dir}" in run_line
    assert f"-v\t{state_dir}:{state_dir}" in run_line
    assert "core.entrypoints.cli.installer" in run_line
    assert "--non-interactive\t--confirm" in run_line


def test_bootstrap_reuses_existing_immutable_installer_image_without_build(
    tmp_path: Path,
) -> None:
    image_id = f"sha256:{'a' * 64}"

    completed, docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        extra_env={
            "FS_INSTALLER_IMAGE": image_id,
            "FAKE_EXISTING_IMAGE_ID": image_id,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert any("image\tinspect" in line and image_id in line for line in docker_lines)
    assert not any("\tbuild\t" in f"\t{line}\t" for line in docker_lines)
    run_line = next(line for line in docker_lines if line.startswith("run\t"))
    assert image_id in run_line


def test_bootstrap_forwards_only_approved_non_secret_isolation_overrides(tmp_path: Path) -> None:
    approved = {
        "FS_COMPOSE_PROJECT_NAME": "fs-inst-bootstrap-test",
        "HTTP_PORT": "18080",
        "POSTGRES_HOST_PORT": "15432",
        "RABBITMQ_AMQP_HOST_PORT": "15672",
        "RABBITMQ_MANAGEMENT_HOST_PORT": "15673",
        "MAILPIT_SMTP_PORT": "11025",
        "MAILPIT_WEB_PORT": "18025",
        "MAILPIT_HTTP_PORT": "18026",
        "FRONTEND_HOST_PORT": "15173",
        "MCP_HOST_PORT": "18082",
        "SONAR_PORT": "19000",
        "SEAWEEDFS_S3_HOST_PORT": "18333",
        "SEAWEEDFS_MASTER_HOST_PORT": "19333",
        "FS_APP_IMAGE": "task-app:test",
        "FS_FRONTEND_IMAGE": "task-frontend:test",
        "MCP_IMAGE": "task-mcp:test",
        "FS_CLI_IMAGE": "task-cli:test",
        "POSTGRES_IMAGE": "task-postgres:test",
        "AWS_CA_BUNDLE": "/app/extensions/task-ca.crt",
    }
    forbidden = {
        "ARTIFACT_S3_ACCESS_KEY": "must-not-cross-bootstrap",
        "ARTIFACT_S3_SECRET_KEY": "must-not-cross-bootstrap",
        "FS_BUILD_SHA": "0" * 40,
        "FS_APP_IMAGE_DIGEST": f"sha256:{'a' * 64}",
        "MCP_IMAGE_DIGEST": f"sha256:{'b' * 64}",
        "FS_SECRET_KEY": "must-not-cross-bootstrap",
        # the source-project workspace mounts are gone; their roots are no override
        "FS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "FS_TEST_PROJECT_ROOT": str(tmp_path / "test-project"),
    }

    completed, docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        extra_env={**approved, **forbidden},
    )

    assert completed.returncode == 0, completed.stderr
    run_fields = next(line for line in docker_lines if line.startswith("run\t")).split("\t")
    forwarded = {
        run_fields[index + 1] for index, field in enumerate(run_fields[:-1]) if field == "-e"
    }
    assert set(approved).issubset(forwarded)
    assert set(forbidden).isdisjoint(forwarded)


def test_install_sh_rejects_removed_public_subcommands_before_docker(tmp_path: Path) -> None:
    completed, docker_lines = _run(tmp_path, "configure")

    assert completed.returncode == 2
    assert "no longer accepts subcommands" in completed.stderr
    assert docker_lines == []


def test_install_sh_rejects_root_caller_before_docker(tmp_path: Path) -> None:
    _write_shim(
        tmp_path / "id",
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == '-u' ]]; then echo 0; exit 0; fi\n"
        'exec /usr/bin/id "$@"\n',
    )

    completed, docker_lines = _run(tmp_path, "--non-interactive", "--confirm")

    assert completed.returncode == 4
    assert "must not be run as root" in completed.stderr
    assert docker_lines == []


def test_install_sh_precreates_owner_only_lifecycle_operations_before_docker() -> None:
    source = INSTALL_SCRIPT.read_text(encoding="utf-8")

    state_declaration = 'LIFECYCLE_STATE_DIR="${ROOT_DIR}/.flow-steward"'
    declaration = 'LIFECYCLE_OPERATIONS_DIR="${LIFECYCLE_STATE_DIR}/operations"'
    creation = 'mkdir -p "${LIFECYCLE_OPERATIONS_DIR}"'
    permissions = 'chmod 700 "${LIFECYCLE_STATE_DIR}" "${LIFECYCLE_OPERATIONS_DIR}"'
    assert state_declaration in source
    assert declaration in source
    assert creation in source
    assert permissions in source
    assert source.index(creation) < source.index("if ! host_docker info")


def test_quiet_bootstrap_emits_configured_heartbeat(tmp_path: Path) -> None:
    completed, _docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        delay=0.15,
    )

    assert completed.returncode == 0
    assert "still preparing" in completed.stderr


def test_sigint_stops_attached_installer_runtime_with_contract_exit(tmp_path: Path) -> None:
    docker_log = _fake_docker(tmp_path)
    state_dir = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_LOG": str(docker_log),
        "FAKE_CONTEXT_ENDPOINT": "unix:///var/run/docker.sock",
        "FAKE_RUNTIME_WAIT": "1",
        "FS_INSTALLER_IMAGE": "flow-steward-installer:test",
        "FS_INSTALLER_STATE_DIR": str(state_dir),
        "FS_INSTALLER_HEARTBEAT_SECONDS": "0.05",
    }
    process = subprocess.Popen(
        ["bash", str(INSTALL_SCRIPT), "--non-interactive", "--confirm"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    cidfile = state_dir / "installer-runtime.cid"
    deadline = time.monotonic() + 10
    while not cidfile.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cidfile.is_file()

    started = time.monotonic()
    os.killpg(process.pid, signal.SIGINT)
    process.communicate(timeout=15)

    assert process.returncode == 10
    assert time.monotonic() - started < 15
    assert not cidfile.exists()
    assert any(
        "container\tkill\t--signal\tINT" in line
        for line in docker_log.read_text(encoding="utf-8").splitlines()
    )


def test_a_hangup_stops_the_installer_runtime_instead_of_orphaning_it(tmp_path: Path) -> None:
    """P3 from the auditor: interactive installer containers left behind by dropped SSH
    sessions were still waiting for input 22 hours later."""
    docker_log = _fake_docker(tmp_path)
    state_dir = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_LOG": str(docker_log),
        "FAKE_CONTEXT_ENDPOINT": "unix:///var/run/docker.sock",
        "FAKE_RUNTIME_WAIT": "1",
        "FS_INSTALLER_IMAGE": "flow-steward-installer:test",
        "FS_INSTALLER_STATE_DIR": str(state_dir),
        "FS_INSTALLER_HEARTBEAT_SECONDS": "0.05",
    }
    process = subprocess.Popen(
        ["bash", str(INSTALL_SCRIPT), "--non-interactive", "--confirm"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    cidfile = state_dir / "installer-runtime.cid"
    deadline = time.monotonic() + 10
    while not cidfile.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cidfile.is_file()

    os.killpg(process.pid, signal.SIGHUP)
    process.communicate(timeout=15)

    assert process.returncode == 10
    assert any(
        "container\tkill\t--signal\tTERM" in line
        for line in docker_log.read_text(encoding="utf-8").splitlines()
    )


def test_install_sh_records_itself_from_its_start_to_its_end(tmp_path: Path) -> None:
    """AC17-R162 (L1): ./flow-steward refuses what would change the installation for the
    whole run -- the record exists before the installer container, and not after."""
    docker_log = _fake_docker(tmp_path)
    state_dir = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_LOG": str(docker_log),
        "FAKE_CONTEXT_ENDPOINT": "unix:///var/run/docker.sock",
        "FAKE_RUNTIME_WAIT": "1",
        "FS_INSTALLER_IMAGE": "flow-steward-installer:test",
        "FS_INSTALLER_STATE_DIR": str(state_dir),
        "FS_INSTALLER_HEARTBEAT_SECONDS": "0.05",
    }
    process = subprocess.Popen(
        ["bash", str(INSTALL_SCRIPT), "--non-interactive", "--confirm"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    record = state_dir / "installer-host.pid"
    deadline = time.monotonic() + 10
    while not record.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert record.read_text(encoding="utf-8").strip() == str(process.pid)
    assert record.stat().st_mode & 0o777 == 0o600

    os.killpg(process.pid, signal.SIGINT)
    process.communicate(timeout=15)

    assert not record.exists()


def test_a_live_runs_record_is_not_taken_over(tmp_path: Path) -> None:
    """A second ./install.sh leaves the first one's record; the first's lock refuses it."""
    other = tmp_path / "install.sh"
    other.write_text("sleep 60\n", encoding="utf-8")
    first = subprocess.Popen(["bash", str(other)])
    try:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        record = state_dir / "installer-host.pid"
        record.write_text(f"{first.pid}\n", encoding="utf-8")

        _run(tmp_path, "--non-interactive", "--confirm")

        assert record.read_text(encoding="utf-8").strip() == str(first.pid)
    finally:
        first.kill()
        first.wait()


@pytest.mark.parametrize("still_running", [True, False], ids=["orphaned", "finished"])
def test_a_client_that_exits_on_its_own_does_not_orphan_the_installer_runtime(
    tmp_path: Path, still_running: bool
) -> None:
    """AC17-R162: after a failed rollback the client exited 125 in 8 s, and its installer
    container ran on for ~3.5 minutes holding the lock -- a rerun was refused."""
    state = tmp_path / "container-state"
    completed, docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        extra_env={
            "FAKE_RUNTIME_CLIENT_DIES": "1",
            "FAKE_CONTAINER_STATE": str(state),
            "FAKE_CONTAINER_STOPS_ITSELF": "0" if still_running else "1",
        },
    )

    kills = [line for line in docker_lines if "\tkill\t" in line]
    if still_running:
        assert kills, docker_lines
        assert "kept running; stopping it" in completed.stderr
        assert completed.returncode == 10
        assert state.read_text(encoding="utf-8").strip() == "false"
    else:  # the green direction: nothing left behind, nothing stopped, the code kept
        assert kills == []
        assert "kept running" not in completed.stderr
        assert completed.returncode == 125
    assert not (tmp_path / "state" / "installer-runtime.cid").exists()


def test_host_python_fallback_is_explicit_and_uses_private_controller(tmp_path: Path) -> None:
    python_log = tmp_path / "python.log"
    python_shim = tmp_path / "python-shim"
    _write_shim(
        python_shim,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${PYTHON_LOG}"
if [[ "${1:-}" == "-" ]]; then while IFS= read -r _line; do :; done; fi
exit 0
""",
    )
    _write_shim(tmp_path / "docker", "#!/usr/bin/env bash\nexit 99\n")
    env = {
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FS_INSTALLER_USE_HOST_PYTHON": "1",
        "PYTHON_BIN": str(python_shim),
        "PYTHON_LOG": str(python_log),
    }

    completed = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--non-interactive", "--confirm"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert python_log.read_text(encoding="utf-8").splitlines() == [
        "-",
        "-m core.entrypoints.cli.installer --non-interactive --confirm",
    ]
    assert "host.docker.internal" not in python_log.read_text(encoding="utf-8")


def test_installer_image_contains_private_controller_runtime_dependencies() -> None:
    text = INSTALLER_DOCKERFILE.read_text(encoding="utf-8")

    for requirement in (
        "boto3==1.38.0",
        "click==8.4.2",
        "cryptography==50.0.0",
        "psycopg[binary]==3.3.3",
        "textual==8.2.8",
        "rich==15.0.0",
        "pydantic==2.12.5",
    ):
        assert requirement in text
    assert "python3 -m venv" in text


def test_installer_image_records_the_exact_build_revision() -> None:
    text = INSTALLER_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG FS_BUILD_REVISION=unknown" in text
    assert 'LABEL org.opencontainers.image.revision="${FS_BUILD_REVISION}"' in text


def test_bootstrap_uses_resolved_desktop_socket_and_writable_buildx(
    fake_install_host,
    tmp_path: Path,
) -> None:
    desktop_socket = tmp_path / "docker.sock"
    host = fake_install_host(
        context_endpoint=f"unix://{desktop_socket}",
        socket_uid=501,
        socket_gid=20,
        docker_config='{"credsStore":"osxkeychain"}',
        platform="Darwin",
    )

    result = host.run("--help")

    assert result.returncode == 0
    assert f"-v {desktop_socket}:/var/run/docker.sock" in host.docker_run
    assert "--group-add 20" in host.docker_run
    assert "--group-add 0" in host.docker_run
    assert "-e FS_DOCKER_GID=0" in host.docker_run
    assert "-e DOCKER_CONTEXT=default" in host.docker_run
    assert "-e BUILDX_CONFIG=/tmp/buildx" in host.docker_run
    assert f"-v {host.helper_config}:/tmp/flow-steward-docker-config:ro" in host.docker_run
    assert host.mktemp_paths == [str(host.bootstrap_config), str(host.helper_config)]
    assert not host.bootstrap_config.exists()
    assert not host.helper_config.exists()


def test_bootstrap_mounts_docker_config_for_non_root_user(
    fake_install_host, tmp_path: Path
) -> None:
    desktop_socket = tmp_path / "docker.sock"
    host = fake_install_host(
        context_endpoint=f"unix://{desktop_socket}",
        socket_uid=501,
        socket_gid=20,
        docker_config='{"credsStore":"osxkeychain"}',
        platform="Darwin",
    )

    result = host.run("--help")

    assert result.returncode == 0
    assert f"-v {host.helper_config}:/tmp/flow-steward-docker-config:ro" in host.docker_run
    assert "-e DOCKER_CONFIG=/tmp/flow-steward-docker-config" in host.docker_run
    assert "/root/.docker" not in host.docker_run


def test_bootstrap_isolates_host_build_client_state(fake_install_host, tmp_path: Path) -> None:
    desktop_socket = tmp_path / "docker.sock"
    host = fake_install_host(
        context_endpoint=f"unix://{desktop_socket}",
        socket_uid=501,
        socket_gid=20,
        docker_config='{"credsStore":"osxkeychain"}',
        platform="Darwin",
    )

    result = host.run("--help")

    assert result.returncode == 0
    expected = (str(host.bootstrap_config), f"{host.bootstrap_config}/buildx")
    assert host.docker_environment("info") == expected
    assert host.docker_environment("build") == expected
    assert not host.docker_attestations_disabled("info")
    assert host.docker_attestations_disabled("build")
    assert not host.bootstrap_config.exists()


def test_bootstrap_keeps_native_linux_socket_group(fake_install_host) -> None:
    host = fake_install_host(
        context_endpoint="unix:///var/run/docker.sock",
        socket_uid=0,
        socket_gid=998,
        docker_config="{}",
        platform="Linux",
    )

    result = host.run("--help")

    assert result.returncode == 0
    assert "--group-add 998" in host.docker_run
    assert "--group-add 0" not in host.docker_run
    assert "-e FS_DOCKER_GID=998" in host.docker_run


@pytest.mark.parametrize("platform", ("Darwin", "Linux"))
def test_bootstrap_routes_only_installer_probes_to_docker_host(
    fake_install_host,
    platform: str,
) -> None:
    host = fake_install_host(
        context_endpoint="unix:///var/run/docker.sock",
        socket_uid=0,
        socket_gid=998,
        docker_config="{}",
        platform=platform,
    )

    result = host.run("--help")

    assert result.returncode == 0
    assert "--add-host host.docker.internal:host-gateway" in host.docker_run
    assert "-e FS_INSTALLER_USE_HOST_PYTHON=0" in host.docker_run
    assert "--network host" not in host.docker_run


def test_launcher_mounts_absolute_operator_input_files_read_only(tmp_path: Path) -> None:
    credentials = tmp_path / "s3.json"
    certificate = tmp_path / "site.crt"
    private_key = tmp_path / "site.key"
    for path in (credentials, certificate, private_key):
        path.write_text("input", encoding="utf-8")

    completed, docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        "--tls-mode",
        "provided",
        "--s3-credentials-path",
        str(credentials),
        f"--certificate-path={certificate}",
        "--private-key-path",
        str(private_key),
    )

    assert completed.returncode == 0, completed.stderr
    run_line = next(line for line in docker_lines if line.startswith("run\t"))
    for path in (credentials, certificate, private_key):
        assert f"{path}:{path}:ro" in run_line


def test_launcher_rejects_generated_tls_output_outside_repository(tmp_path: Path) -> None:
    output = tmp_path / "new-site.crt"

    completed, docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        "--tls-mode",
        "local",
        "--certificate-path",
        str(output),
    )

    assert completed.returncode == 3
    assert "must be inside" in completed.stderr
    assert docker_lines == []


def test_launcher_rejects_relative_path_that_escapes_repository(tmp_path: Path) -> None:
    completed, docker_lines = _run(
        tmp_path,
        "--non-interactive",
        "--confirm",
        "--s3-credentials-path",
        "../secret.json",
    )

    assert completed.returncode == 3
    assert "must stay inside" in completed.stderr
    assert docker_lines == []


def test_the_installer_image_carries_every_tool_its_build_script_needs() -> None:
    """AC17-R116: the build finally ran and died on `[Errno 2] No such file: 'bash'`.

    The installer image is Alpine-based and the release build script declares
    `#!/usr/bin/env bash` and uses constructs POSIX sh does not have, so pointing it at
    sh would have failed differently and more quietly. Only bash was missing --
    measured against the base image rather than assumed, which is why git is not added
    here even though the script's first act is a git call.

    Pinned because this is the second surface in the same chain: the build not running
    hid it, and fixing that revealed it. A third addition to the script's tool needs
    should fail here rather than on an operator's install.
    """
    dockerfile = INSTALLER_DOCKERFILE.read_text(encoding="utf-8")
    apk_line = next(
        (line for line in dockerfile.splitlines() if line.startswith("RUN apk add")), ""
    )
    assert apk_line, "no apk install line found; this test would be vacuous"

    script = (ROOT / "scripts/release/build_candidate.sh").read_text(encoding="utf-8")
    assert script.startswith("#!/usr/bin/env bash"), (
        "the script no longer asks for bash; the image should stop carrying it"
    )

    assert " bash " in apk_line, (
        "the installer runs a bash script and its image does not install bash"
    )


def test_the_installer_image_carries_git_lfs_exactly_when_the_repository_uses_lfs() -> None:
    """AC17-R116, the sixth obstacle: `git-lfs filter-process: git-lfs: not found`.

    Derived from the fact that causes it rather than from a list of tools. The build
    script's first act is a git call over the mounted repository; if .gitattributes
    routes anything through the lfs filter, git runs a filter the image must carry,
    and registered, or git silently keeps pointer files. Core ships no extensions and
    routes nothing through the filter now, so the image carries no git-lfs; the moment
    a pattern returns, this asks for the package and its registration again.
    """
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    lfs_paths = [
        line.split()[0]
        for line in attributes.splitlines()
        if "filter=lfs" in line and not line.lstrip().startswith("#")
    ]
    dockerfile = INSTALLER_DOCKERFILE.read_text(encoding="utf-8")
    apk_line = next(
        (line for line in dockerfile.splitlines() if line.startswith("RUN apk add")), ""
    )
    assert apk_line, "the installer image installs its packages with one apk add line"

    if not lfs_paths:
        assert " git-lfs " not in f"{apk_line} ", "nothing uses the lfs filter; drop git-lfs"
        assert "git lfs install" not in dockerfile
        return

    assert " git-lfs " in f"{apk_line} ", (
        f"the repository routes {len(lfs_paths)} path patterns through the lfs filter "
        "and the installer image, which runs git over that repository, has no git-lfs"
    )
    assert "git lfs install --system" in dockerfile, (
        "the lfs filter must be registered for every user in the image, not left to a "
        "package side effect; without it git silently keeps pointer files"
    )


# The one thing here that cannot be derived: an import name is not a distribution name.
# An import missing from this table fails the guard with an instruction rather than
# passing quietly, which is the behaviour we want the next new dependency to get.
_DISTRIBUTION_FOR_IMPORT = {"yaml": "pyyaml"}


def _logical_commands(script: str) -> list[str]:
    """Join backslash continuations so a call and its image argument stay together."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [line.strip() for line in joined.splitlines() if line.strip()]


def _third_party_imports(relative: str) -> set[str]:
    import ast
    import sys

    names: set[str] = set()
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            candidates = [node.module.split(".")[0]]
        else:
            continue
        names.update(
            name
            for name in candidates
            if name not in sys.stdlib_module_names and name not in {"core", "scripts"}
        )
    return names


def _requirements_of_build_target(dockerfile: str, target: str) -> str:
    """Return the requirements file the named stage installs, following its ancestry.

    A stage usually inherits its dependencies: `FROM runtime-dependencies AS
    app-runtime` installs nothing itself. Walking the FROM chain is what makes the
    answer the real one rather than "this stage has no requirements".
    """
    stages = {}
    for stage in re.split(r"(?m)^FROM\s+", dockerfile)[1:]:
        header = stage.splitlines()[0].strip()
        named = re.search(r"\bAS\s+([A-Za-z0-9_-]+)\s*$", header)
        if named:
            stages[named.group(1)] = (header.split()[0], stage)

    seen = set()
    current = target
    while current in stages and current not in seen:
        seen.add(current)
        parent, body = stages[current]
        found = re.findall(r"-r\s+(requirements[A-Za-z0-9._-]*\.(?:lock|txt))", body)
        if found:
            return found[0]
        current = parent
    raise AssertionError(f"no stage from '{target}' up its FROM chain installs a requirements file")


def test_every_build_script_has_its_imports_in_the_image_that_actually_runs_it() -> None:
    """AC17-R116, seventh obstacle, and the correction of my own first answer to it.

    `ModuleNotFoundError: No module named 'yaml'` was real, and adding pyyaml to the
    installer image changed nothing: five of the six script invocations in
    build_candidate.sh run `python3` inside the installer container, and the sixth --
    the only script with a third-party import -- runs `docker run --entrypoint python
    "$CLI_IMAGE"`. The question was never what a script imports, it was what it
    imports in the image that runs it, and a guard phrased the first way would have
    pinned the wrong answer as firmly as the right one.

    So the whole chain is followed instead: the call form says which image runs the
    script, `docker build --target ... --tag "$IMAGE"` says which Dockerfile stage
    that image is, and the stage says which requirements file it installs. An
    unrecognised call form fails rather than being assumed to run locally.
    """
    script = (ROOT / "scripts/release/build_candidate.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    commands = _logical_commands(script)

    built_by_variable = {}
    for command in commands:
        target = re.search(r"docker build .*?--target\s+([A-Za-z0-9_-]+)", command)
        tag = re.search(r'--tag\s+"\$([A-Za-z0-9_]+)"', command)
        if target and tag:
            built_by_variable[tag.group(1)] = target.group(1)

    checked = 0
    for command in commands:
        called = [
            f"scripts/release/{name}"
            for name in re.findall(r"scripts/release/([A-Za-z0-9_]+\.py)", command)
        ]
        if not called:
            continue
        image_variable = None
        if "docker run" in command and "--entrypoint python" in command:
            match = re.search(r'"\$([A-Za-z0-9_]+_IMAGE)"', command)
            assert match, f"cannot tell which image runs this call: {command[:120]}"
            image_variable = match.group(1)
        else:
            assert "python3 " in command, (
                f"unrecognised call form; the guard cannot say where this runs: {command[:120]}"
            )

        for relative in called:
            checked += 1
            imports = _third_party_imports(relative)
            if not imports:
                continue
            distributions = set()
            for module in sorted(imports):
                distribution = _DISTRIBUTION_FOR_IMPORT.get(module)
                assert distribution, (
                    f"{relative} imports '{module}', which this guard cannot map to a "
                    "distribution; add it to _DISTRIBUTION_FOR_IMPORT"
                )
                distributions.add(distribution)

            if image_variable is None:
                installed = INSTALLER_DOCKERFILE.read_text(encoding="utf-8")
                where = "the installer image"
            else:
                target = built_by_variable.get(image_variable)
                assert target, f"${image_variable} is run but never built in this script"
                requirements = _requirements_of_build_target(dockerfile, target)
                installed = (ROOT / requirements).read_text(encoding="utf-8")
                where = f"{requirements} (Dockerfile stage '{target}', ${image_variable})"
            for distribution in sorted(distributions):
                assert re.search(rf"(?mi)^\s*['\"]?{re.escape(distribution)}(\[|==)", installed), (
                    f"{relative} runs in {where} and imports a module from "
                    f"{distribution}, which is not installed there"
                )

    assert checked >= 5, "too few script calls found; this guard would be vacuous"


def _shells_out_to_git(relative: str) -> bool:
    """True when the script runs the git program itself."""
    import ast

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.List | ast.Tuple) or not first.elts:
            continue
        head = first.elts[0]
        if isinstance(head, ast.Constant) and head.value == "git":
            return True
    return False


def _array_literals(name: str, script_text: str) -> list[str]:
    """Every `name=(...)` / `name+=(...)` body, found by balancing parentheses.

    A regex that ended the body at the next line starting with `)` silently ran past a
    single-line `name+=(...)` and swallowed the block after it -- which made the guard
    below pass with its subject removed. Found from the green direction, as usual.
    """
    import re as _re

    bodies: list[str] = []
    for match in _re.finditer(rf"(?m)^\s*{_re.escape(name)}(?:\+)?=\(", script_text):
        depth = 0
        index = match.end() - 1
        while index < len(script_text):
            character = script_text[index]
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    bodies.append(script_text[match.end() : index])
                    break
            index += 1
    return bodies


def _expanded_environment_names(command: str, script_text: str) -> set[str]:
    """Names a docker run supplies, following `"${array[@]}"` into its literals."""
    import re as _re

    pattern = r"(?:--env|-e)\s+\"?([A-Z][A-Z0-9_]*)="
    names = set(_re.findall(pattern, command))
    for array in _re.findall(r'"\$\{([A-Za-z_][A-Za-z0-9_]*)\[@\]\}"', command):
        for body in _array_literals(array, script_text):
            names.update(_re.findall(pattern, body))
    return names


def test_a_nested_run_that_uses_git_is_given_gits_configuration() -> None:
    """AC17-R116, eighth obstacle: `fatal: detected dubious ownership in repository`.

    Second case in a row of the same shape: a container started by a container starts
    with an empty environment, so what install.sh arranges for its own container --
    first an interpreter with dependencies, now git's safe.directory -- reaches the
    nested run only if the nested run is given it. This one runs --user 0:0 against a
    tree owned by the invoking user, so even root is refused and the script's first
    git call ends the build.

    Derived: a nested run whose script shells out to git must be handed git's
    configuration. Which scripts those are is read from their own source, and the
    environment a run supplies is resolved through `"${array[@]}"` into the array
    literals, because that is how this script builds its arguments.
    """
    script = (ROOT / "scripts/release/build_candidate.sh").read_text(encoding="utf-8")
    commands = _logical_commands(script)

    checked = 0
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    for command in commands:
        called = [
            f"scripts/release/{name}"
            for name in re.findall(r"scripts/release/([A-Za-z0-9_]+\.py)", command)
        ]
        if not any(_shells_out_to_git(relative) for relative in called):
            continue
        checked += 1
        if "docker run" not in command:
            # Provenance now runs in the caller's installer container, which
            # owns Git and PyYAML, rather than in the runtime CLI image.
            assert "python3 " in command, command
            assert "GIT_CONFIG_COUNT=" in installer
            continue
        supplied = _expanded_environment_names(command, script)
        assert "GIT_CONFIG_COUNT" in supplied, (
            f"{called} runs git inside a nested container and is given no git "
            "configuration; a container started by a container inherits nothing"
        )

    assert checked >= 1, (
        "expected a build script that calls git; the inventory changed and this "
        "guard must be re-read rather than trusted"
    )


@pytest.mark.parametrize(
    "relative",
    ["install.sh", "scripts/release/build_candidate.sh"],
)
def test_every_declared_git_config_count_matches_the_pairs_beside_it(relative: str) -> None:
    """An off-by-one here is silent, which is why it is worth a test.

    Measured: with GIT_CONFIG_COUNT=1 and two pairs declared, git reads only the first
    and fails with exactly the same dubious-ownership message as with no configuration
    at all. Nothing warns that the second pair was ignored.
    """
    text = (ROOT / relative).read_text(encoding="utf-8")
    declarations = list(re.finditer(r"GIT_CONFIG_COUNT=(\d+)", text))
    if not declarations:
        assert relative != "install.sh", "the installer must configure Git for its mount"
        nested_git_calls = [
            command
            for command in _logical_commands(text)
            if "docker run" in command
            and any(
                _shells_out_to_git(f"scripts/release/{name}")
                for name in re.findall(r"scripts/release/([A-Za-z0-9_]+\.py)", command)
            )
        ]
        assert not nested_git_calls, "nested Git calls require their own configuration"

    for declaration in declarations:
        count = int(declaration.group(1))
        # The pairs belong to the same parenthesised block as the count.
        block_start = text.rfind("=(", 0, declaration.start())
        block_end = text.find("\n)", declaration.end())
        block = text[block_start : block_end if block_end != -1 else len(text)]
        for index in range(count):
            assert f"GIT_CONFIG_KEY_{index}=" in block, (
                f"{relative} declares GIT_CONFIG_COUNT={count} with no KEY_{index}"
            )
            assert f"GIT_CONFIG_VALUE_{index}=" in block, (
                f"{relative} declares GIT_CONFIG_COUNT={count} with no VALUE_{index}"
            )
        assert f"GIT_CONFIG_KEY_{count}=" not in block, (
            f"{relative} declares GIT_CONFIG_COUNT={count} and a KEY_{count} beside it, "
            "which git silently ignores"
        )


def _shell_assignments(script_text: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r'(?m)^([A-Z][A-Z0-9_]*)="(.*)"\s*$', script_text)
    }


def _rooted_at_repository(variable: str, assignments: dict[str, str]) -> bool:
    """Resolve a path variable through its definitions until REPO_ROOT appears.

    REPO_ROOT is the stopping point rather than another substitution: it is the
    directory install.sh mounts into its own container, so "under REPO_ROOT" is
    exactly "a path the host and both containers agree on".
    """
    value = assignments.get(variable, "")
    for _ in range(8):
        if "$REPO_ROOT" in value or "${REPO_ROOT}" in value:
            return True
        referenced = [
            name
            for name in re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", value)
            if name in assignments and name != "REPO_ROOT"
        ]
        if not referenced:
            return False
        for name in referenced:
            value = re.sub(rf"\$\{{{name}\}}|\${name}\b", assignments[name], value)
    return False


def test_every_temporary_path_this_script_creates_lives_under_the_mounted_repository() -> None:
    """AC17-R116, ninth obstacle: FileNotFoundError on a file that was never missing.

    Third and last case of one shape. A container cannot share its own ephemeral
    filesystem with a container it starts: Docker resolves `--volume` against the
    host, so a path created in the installer container's /tmp does not exist there and
    the nested run is handed a silently created empty directory. The build then failed
    on a file that is tracked, present in `git archive HEAD`, and present on disk.

    install.sh mounts the repository root and nothing else, so a path this script
    creates exists for the host only if it is created under REPO_ROOT. That is the
    rule checked here, and it is checked over every mktemp rather than only over the
    paths handed across today -- a path that is safe to create is safe to pass, and
    the next one to be passed will already be in the right place.

    Paths the script *discovers* rather than creates are deliberately outside this:
    GIT_COMMON_DIR comes from git and may legitimately sit elsewhere, which is why the
    script mounts it explicitly when it does. A discovered path already exists on the
    host; a created one is the only kind that can fail to.
    """
    script = (ROOT / "scripts/release/build_candidate.sh").read_text(encoding="utf-8")
    assignments = _shell_assignments(script)

    created = {name: value for name, value in assignments.items() if "mktemp" in value}
    assert len(created) >= 4, "too few temporary paths found; this guard would be vacuous"

    for name in sorted(created):
        assert _rooted_at_repository(name, assignments), (
            f"${name} is created with mktemp outside $REPO_ROOT, the only directory "
            "install.sh mounts into the container this runs in; a path created there "
            "does not exist for the host, so handing it to a nested run yields an "
            "empty directory created on the spot"
        )
