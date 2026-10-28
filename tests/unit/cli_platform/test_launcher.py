"""Fake-Docker contract tests for the root Flow Steward CLI launcher."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from core.application.installer import InstallConfig
from core.infrastructure.installer import gateway as installer_gateway_module
from core.infrastructure.installer import gateway_cli_runtime
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore

ROOT = Path(__file__).parents[3]
LAUNCHER = ROOT / "core/entrypoints/cli/scripts/flow_steward_launcher.sh"
RUNTIME_IDENTITY = ROOT / "core/infrastructure/cli_runtime/runtime_identity.sh"
ROOT_WRAPPER = ROOT / "flow-steward"
# The earlier doctor is the configuration section of `./flow-steward doctor` now: the
# launcher runs it as an inner run marked so, and passes its report to the installation
# checks in ./install.sh's management runtime (the doctor task).
_CONFIGURATION_SECTION = {"FS_DOCTOR_SECTION": "configuration"}


def _write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    # The audit Compose project is infrastructure, not this fake installation's
    # identity. Individual cases supply their deployment identity explicitly.
    monkeypatch.delenv("FS_COMPOSE_PROJECT_NAME", raising=False)
    repo = tmp_path / "repo"
    scripts_dir = repo / "core/entrypoints/cli/scripts"
    runtime_dir = repo / "core/infrastructure/cli_runtime"
    docker_dir = repo / "infra/docker"
    scripts_dir.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)
    docker_dir.mkdir(parents=True)
    (repo / "VERSION").write_text((ROOT / "VERSION").read_text(encoding="utf-8"), encoding="utf-8")
    shutil.copy2(ROOT_WRAPPER, repo / "flow-steward")
    shutil.copy2(LAUNCHER, scripts_dir / "flow_steward_launcher.sh")
    shutil.copy2(RUNTIME_IDENTITY, runtime_dir / "runtime_identity.sh")
    (docker_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (docker_dir / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (docker_dir / "docker-compose.prod.yaml").write_text("services: {}\n", encoding="utf-8")
    (docker_dir / "scalable").mkdir()
    for overlay in ("cli-host-directories.yaml", "cli-host-directories-artifacts.yaml"):
        shutil.copy2(ROOT / "infra/docker/scalable" / overlay, docker_dir / "scalable" / overlay)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "docker.log"
    _write_executable(
        bin_dir / "git",
        """\
        #!/bin/sh
        if [ "${1:-}" = "-C" ]; then
          shift 2
        fi
        if [ "${1:-}" = "rev-parse" ] && [ "${2:-}" = "HEAD" ]; then
          printf '%s\\n' "${FAKE_GIT_REVISION:-test-revision}"
          exit 0
        fi
        if [ "${1:-}" = "diff" ] && [ "${2:-}" = "--name-only" ]; then
          if [ "${FAKE_GIT_DIFF_FAIL:-0}" = "1" ]; then
            exit 2
          fi
          if [ -n "${FAKE_GIT_CHANGED_PATHS:-}" ]; then
            printf '%s\\n' "$FAKE_GIT_CHANGED_PATHS"
          fi
          exit 0
        fi
        printf 'unexpected git command: %s\\n' "$*" >&2
        exit 2
        """,
    )
    _write_executable(
        bin_dir / "docker",
        """\
        #!/bin/sh
        set -eu
        : "${FAKE_DOCKER_LOG:?}"
        : "${FAKE_DOCKER_STATE:?}"
        # The launcher runs three `docker` preflight calls concurrently, so several copies
        # of this fake append to one log at once. Writing a line at a time reopened the log
        # per line and let those copies interleave, which can split a record another test
        # matches as a contiguous block. One grouped redirection writes each invocation's
        # record once instead.
        {
          printf 'CMD=%s\\n' "$*"
          if [ "${1:-}" = "compose" ]; then
            if [ "${NO_COLOR+x}" ]; then
              printf 'ENV_NO_COLOR_STATE=set\\n'
              printf 'ENV_NO_COLOR_VALUE=%s\\n' "$NO_COLOR"
            else
              printf 'ENV_NO_COLOR_STATE=unset\\n'
            fi
            printf 'ENV_TERM=%s\\n' "${TERM-}"
            printf 'ENV_FS_DOCKER_GID=%s\\n' "${FS_DOCKER_GID-}"
            printf 'ENV_FS_STACK_ENVIRONMENT=%s\\n' "${FS_STACK_ENVIRONMENT-}"
            printf 'ENV_FS_CLI_IMAGE=%s\\n' "${FS_CLI_IMAGE-}"
            printf 'ENV_FS_CLI_RUNTIME_REVISION=%s\\n' "${FS_CLI_RUNTIME_REVISION-}"
            printf 'ENV_FS_EXTENSIONS_DIR=%s\\n' "${FS_EXTENSIONS_DIR-}"
            printf 'ENV_FS_BACKUPS_DIR=%s\\n' "${FS_BACKUPS_DIR-}"
            printf 'ENV_FS_LOGS_DIR=%s\\n' "${FS_LOGS_DIR-}"
          fi
          for arg in "$@"; do
            printf 'ARG=%s\\n' "$arg"
          done
        } >> "$FAKE_DOCKER_LOG"
        case "${1:-}" in
          context)
            if [ "${FAKE_DOCKER_MODE:-}" = "context_error" ]; then
              printf 'context unavailable\\n' >&2
              exit 9
            fi
            printf '%s\\n' "${FAKE_CONTEXT_HOST:-unix:///var/run/docker.sock}"
            exit 0
            ;;
          info)
            if [ "${FAKE_DOCKER_MODE:-}" = "daemon_down" ]; then
              printf 'daemon unavailable\\n' >&2
              exit 1
            fi
            case "$*" in
              *SecurityOptions*)
                if [ "${FAKE_ROOTLESS:-0}" = "1" ]; then
                  printf '%s\\n' 'name=rootless'
                else
                  printf '%s\\n' 'name=seccomp,profile=default'
                fi
                ;;
              *)
                printf '%s\\n' 'Client: Docker'
                ;;
            esac
            exit 0
            ;;
          compose)
            if [ "${2:-}" = "version" ]; then
              if [ "${FAKE_DOCKER_MODE:-}" = "compose_missing" ]; then
                printf 'compose missing\\n' >&2
                exit 1
              fi
              if [ "${FAKE_DOCKER_MODE:-}" = "compose_v1" ]; then
                printf '%s\\n' '1.29.2'
                exit 0
              fi
              if [ "${FAKE_DOCKER_MODE:-}" = "compose_current" ]; then
                printf '%s\\n' '5.2.0'
                exit 0
              fi
              printf '%s\\n' 'v2.99.0'
              exit 0
            fi
            compose_action=
            for arg in "$@"; do
              case "$arg" in
                ps|start|up|rm|run|exec)
                  compose_action=$arg
                  break
                  ;;
              esac
            done
            if [ "$compose_action" = "exec" ]; then
              case " $* " in
                *" core.entrypoints.cli.lifecycle_lock_probe "*)
                  printf 'ACTION=lifecycle-probe\\n' >> "$FAKE_DOCKER_LOG"
                  if [ -n "${FAKE_LIFECYCLE_OPERATION:-}" ]; then
                    printf '%s\\n' "$FAKE_LIFECYCLE_OPERATION"
                    exit 0
                  fi
                  exit 3
                  ;;
              esac
            fi
            printf 'ACTION=%s\\n' "$compose_action" >> "$FAKE_DOCKER_LOG"
            case "$compose_action" in
              ps)
                if [ "${FAKE_CONTAINER_LOOKUP_FAIL:-0}" = "1" ]; then
                  printf '%s\\n' 'container lookup failed' >&2
                  exit 51
                fi
                if [ -f "$FAKE_DOCKER_STATE" ] || [ "${FAKE_CONTAINER_PRESENT:-1}" = "1" ]; then
                  printf '%s\\n' "${FAKE_CONTAINER_ID:-fakeclicontainerid}"
                fi
                exit 0
                ;;
              up)
                if [ "${FAKE_REQUIRE_TRANSFER_ROOT:-0}" = "1" ] && [ ! -d "${FS_BACKUPS_DIR:-}" ]; then
                  printf '%s\\n' 'selected Clone transfer directory is missing' >&2
                  exit 43
                fi
                count=0
                if [ -f "$FAKE_DOCKER_STATE.up-count" ]; then
                  IFS= read -r count < "$FAKE_DOCKER_STATE.up-count"
                fi
                count=$((count + 1))
                printf '%s\\n' "$count" > "$FAKE_DOCKER_STATE.up-count"
                if [ "${FAKE_BOOTSTRAP_MODE:-}" = "fail" ] || {
                  [ "${FAKE_BOOTSTRAP_MODE:-}" = "retry" ] && [ "$count" -eq 1 ]
                }; then
                  printf '%s\\n' "${FAKE_BOOTSTRAP_OUTPUT:-fake fs-cli start failure}"
                  exit "${FAKE_BOOTSTRAP_EXIT:-42}"
                fi
                if [ "${FAKE_BOOTSTRAP_SLEEP:-0}" != "0" ]; then
                  sleep "$FAKE_BOOTSTRAP_SLEEP"
                fi
                printf '%s\\n' 'repaired' > "$FAKE_DOCKER_STATE"
                printf '%s\\n' 'fake fs-cli started'
                exit 0
                ;;
              start)
                printf '%s\n' 'repaired' > "$FAKE_DOCKER_STATE"
                printf '%s\n' 'fake fs-cli started'
                exit 0
                ;;
              rm)
                rm -f "$FAKE_DOCKER_STATE"
                printf '%s\\n' 'fake invalid fs-cli removed'
                exit 0
                ;;
              exec)
                if [ -d "$FS_DOCKER_HOST_PROJECT_ROOT/.flow-steward/runtime/cli-runtime.lock" ]; then
                  printf 'EXEC_LOCK_PRESENT=1\\n' >> "$FAKE_DOCKER_LOG"
                else
                  printf 'EXEC_LOCK_PRESENT=0\\n' >> "$FAKE_DOCKER_LOG"
                fi
                if [ -n "${FAKE_EXEC_BARRIER_DIR:-}" ]; then
                  mkdir -p "$FAKE_EXEC_BARRIER_DIR"
                  mkdir "$FAKE_EXEC_BARRIER_DIR/exec-$$"
                  barrier_waits=0
                  while :; do
                    set -- "$FAKE_EXEC_BARRIER_DIR"/exec-*
                    if [ -e "$1" ] && [ "$#" -ge 2 ]; then
                      break
                    fi
                    barrier_waits=$((barrier_waits + 1))
                    [ "$barrier_waits" -lt 100 ] || exit 65
                    sleep 0.05
                  done
                fi
                if [ "${FAKE_SIGNAL_CHILD:-0}" = "1" ]; then
                  trap 'exit 130' INT
                  : > "${FAKE_SIGNAL_READY:?}"
                  while :; do
                    sleep 1
                  done
                fi
                if [ "${FAKE_ECHO_STDIN:-0}" = "1" ]; then
                  cat
                elif [ "${FAKE_CAPTURE_STDIN:-0}" = "1" ]; then
                  stdin_payload=$(cat)
                  printf 'STDIN=%s\\n' "$stdin_payload" >> "$FAKE_DOCKER_LOG"
                fi
                if [ "${FAKE_COMPOSE_STDOUT:-}" ]; then
                  printf '%s\\n' "$FAKE_COMPOSE_STDOUT"
                fi
                if [ "${FAKE_COMPOSE_STDERR:-}" ]; then
                  printf '%s\\n' "$FAKE_COMPOSE_STDERR" >&2
                fi
                exit "${FAKE_COMPOSE_EXIT:-0}"
                ;;
              run)
                if [ "${FAKE_ECHO_STDIN:-0}" = "1" ]; then
                  cat
                elif [ "${FAKE_CAPTURE_STDIN:-0}" = "1" ]; then
                  stdin_payload=$(cat)
                  printf 'STDIN=%s\n' "$stdin_payload" >> "$FAKE_DOCKER_LOG"
                fi
                if [ "${FAKE_COMPOSE_STDOUT:-}" ]; then
                  printf '%s\n' "$FAKE_COMPOSE_STDOUT"
                fi
                if [ "${FAKE_COMPOSE_STDERR:-}" ]; then
                  printf '%s\n' "$FAKE_COMPOSE_STDERR" >&2
                fi
                exit "${FAKE_COMPOSE_EXIT:-0}"
                ;;
            esac
            printf 'unexpected compose command: %s\\n' "$*" >&2
            exit 64
            ;;
          image)
            if [ "${2:-}" = "inspect" ]; then
              case "$*" in
                *'{{.Id}}|'*org.opencontainers.image.revision*)
                  if [ "${FAKE_IMAGE_ID_FAIL:-0}" = "1" ]; then
                    printf '%s\n' 'image metadata unavailable' >&2
                    exit 52
                  fi
                  if [ -f "$FAKE_DOCKER_STATE.image-built" ]; then
                    printf '%s|%s\n' \
                      "${FAKE_IMAGE_ID:-sha256:expected-cli-image}" \
                      "${FAKE_GIT_REVISION:-test-revision}"
                    exit 0
                  fi
                  if [ "${FAKE_IMAGE_REVISION+x}" ]; then
                    printf '%s|%s\n' \
                      "${FAKE_IMAGE_ID:-sha256:expected-cli-image}" \
                      "$FAKE_IMAGE_REVISION"
                    exit 0
                  fi
                  exit 1
                  ;;
                *org.opencontainers.image.revision*)
                  if [ -f "$FAKE_DOCKER_STATE.image-built" ]; then
                    printf '%s\\n' "${FAKE_GIT_REVISION:-test-revision}"
                    exit 0
                  fi
                  if [ "${FAKE_IMAGE_REVISION+x}" ]; then
                    printf '%s\\n' "$FAKE_IMAGE_REVISION"
                    exit 0
                  fi
                  exit 1
                  ;;
                *'{{.Id}}'*)
                  if [ "${FAKE_IMAGE_ID_FAIL:-0}" = "1" ]; then
                    printf '%s\\n' 'image id unavailable' >&2
                    exit 52
                  fi
                  printf '%s\\n' "${FAKE_IMAGE_ID:-sha256:expected-cli-image}"
                  exit 0
                  ;;
              esac
              exit 64
            fi
            ;;
          container)
            if [ "${2:-}" = "inspect" ] && [ -n "${FAKE_INSTALLER_CID:-}" ] &&
              [ "${3:-}" = "$FAKE_INSTALLER_CID" ]; then
              printf 'ACTION=installer-inspect\n' >> "$FAKE_DOCKER_LOG"
              printf '%s\n' "${FAKE_INSTALLER_RUNNING:-false}"
              exit 0
            fi
            if [ "${2:-}" = "inspect" ]; then
              if [ "${FAKE_CONTAINER_PRESENT:-1}" = "0" ] && [ ! -f "$FAKE_DOCKER_STATE" ]; then
                printf '%s\n' 'container inspect failed' >&2
                exit 53
              fi
              source_prefix=${FAKE_DOCKER_SOURCE_PREFIX:-}
              if [ -z "$source_prefix" ] && [ "$(uname -s)" = "Darwin" ]; then
                source_prefix=/host_mnt
              fi
              if [ "${FAKE_CONTAINER_INSPECT_FAIL:-0}" = "1" ] && [ ! -f "$FAKE_DOCKER_STATE" ]; then
                printf '%s\\n' 'container inspect failed' >&2
                exit 53
              fi
              if [ -f "$FAKE_DOCKER_STATE" ]; then
                running=true
                image_id=${FAKE_IMAGE_ID:-sha256:expected-cli-image}
                project=${FS_COMPOSE_PROJECT_NAME:-flow-steward}
                service=fs-cli
                runtime_label=true
                contract_label=1
                revision_label=${FS_CLI_RUNTIME_REVISION:-unconfigured}
                configured_user=${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000}
                privileged=false
                group_add=${FS_DOCKER_GID:-0},
                app_source=$source_prefix$FS_DOCKER_HOST_PROJECT_ROOT
                host_source=$source_prefix$FS_DOCKER_HOST_PROJECT_ROOT
                host_destination=$FS_DOCKER_HOST_PROJECT_ROOT
                host_rw=true
                socket_source=/var/run/docker.sock
                socket_destination=/var/run/docker.sock
                socket_rw=true
                runtime_source=$source_prefix$FS_INSTALLER_STATE_DIR/runtime
                candidate_state_source=$source_prefix$FS_DOCKER_HOST_PROJECT_ROOT/.flow-steward
                candidate_env_source=$source_prefix$FS_ENV_FILE
                extra_socket_mount=0
              else
                running=${FAKE_CONTAINER_RUNNING:-true}
                image_id=${FAKE_CONTAINER_IMAGE_ID:-${FAKE_IMAGE_ID:-sha256:expected-cli-image}}
                project=${FAKE_CONTAINER_PROJECT:-${FS_COMPOSE_PROJECT_NAME:-flow-steward}}
                service=${FAKE_CONTAINER_SERVICE:-fs-cli}
                runtime_label=${FAKE_CONTAINER_RUNTIME_LABEL:-true}
                contract_label=${FAKE_CONTAINER_CONTRACT_LABEL:-1}
                revision_label=${FAKE_CONTAINER_RUNTIME_REVISION:-${FS_CLI_RUNTIME_REVISION:-unconfigured}}
                configured_user=${FAKE_CONTAINER_USER:-${FS_HOST_UID:-1000}:${FS_HOST_GID:-1000}}
                privileged=${FAKE_CONTAINER_PRIVILEGED:-false}
                group_add=${FAKE_CONTAINER_GROUP_ADD:-${FS_DOCKER_GID:-0},}
                app_source=${FAKE_CONTAINER_APP_SOURCE:-$source_prefix$FS_DOCKER_HOST_PROJECT_ROOT}
                host_source=${FAKE_CONTAINER_HOST_SOURCE:-$source_prefix$FS_DOCKER_HOST_PROJECT_ROOT}
                host_destination=${FAKE_CONTAINER_HOST_DESTINATION:-$FS_DOCKER_HOST_PROJECT_ROOT}
                host_rw=${FAKE_CONTAINER_HOST_RW:-true}
                socket_source=${FAKE_CONTAINER_SOCKET_SOURCE:-/var/run/docker.sock}
                socket_destination=${FAKE_CONTAINER_SOCKET_DESTINATION:-/var/run/docker.sock}
                socket_rw=${FAKE_CONTAINER_SOCKET_RW:-true}
                runtime_source=${FAKE_CONTAINER_RUNTIME_SOURCE:-$source_prefix$FS_INSTALLER_STATE_DIR/runtime}
                candidate_state_source=${FAKE_CONTAINER_CANDIDATE_STATE_SOURCE:-$source_prefix$FS_DOCKER_HOST_PROJECT_ROOT/.flow-steward}
                candidate_env_source=${FAKE_CONTAINER_CANDIDATE_ENV_SOURCE:-$source_prefix$FS_ENV_FILE}
                extra_socket_mount=${FAKE_CONTAINER_EXTRA_SOCKET_MOUNT:-0}
              fi
              printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \
                "$running" "$image_id" "$project" "$service" \
                "$runtime_label" "$contract_label" "$revision_label" "$configured_user" \
                "$privileged" "$group_add"
              if [ ! -f "$FAKE_DOCKER_STATE" ] && [ "${FAKE_CONTAINER_APP_MOUNT:-0}" = "1" ]; then
                printf 'bind\\t%s\\t/app\\ttrue\\n' "$app_source"
              fi
              printf 'bind\\t%s\\t%s\\t%s\\n' "$host_source" "$host_destination" "$host_rw"
              printf 'bind\\t%s\\t%s\\t%s\\n' "$socket_source" "$socket_destination" "$socket_rw"
              printf 'bind\\t%s\\t/run/flow-steward-runtime\\ttrue\\n' "$runtime_source"
              if [ "$FS_STACK_ENVIRONMENT" != "production" ]; then
                if [ "${FAKE_CONTAINER_SELECTED_STATE_MOUNTS:-}" = "correct" ] ||
                  [ -z "${FAKE_CONTAINER_SELECTED_STATE_MOUNTS:-}" ] ||
                  [ -f "$FAKE_DOCKER_STATE" ]; then
                printf 'bind\\t%s\\t/run/flow-steward-install-state\\ttrue\\n' \
                  "$source_prefix$FS_INSTALLER_STATE_DIR"
                printf 'bind\\t%s\\t%s\\ttrue\\n' \
                  "$source_prefix$FS_INSTALLER_STATE_DIR" "$FS_INSTALLER_STATE_DIR"
                elif [ "${FAKE_CONTAINER_SELECTED_STATE_MOUNTS:-}" = "wrong-alias" ]; then
                printf 'bind\\t%s\\t/run/flow-steward-install-state\\ttrue\\n' \
                  "/wrong/installer-state"
                printf 'bind\\t%s\\t%s\\ttrue\\n' \
                  "$source_prefix$FS_INSTALLER_STATE_DIR" "$FS_INSTALLER_STATE_DIR"
                elif [ "${FAKE_CONTAINER_SELECTED_STATE_MOUNTS:-}" = "wrong-identity" ]; then
                printf 'bind\\t%s\\t/run/flow-steward-install-state\\ttrue\\n' \
                  "$source_prefix$FS_INSTALLER_STATE_DIR"
                printf 'bind\\t%s\\t%s\\ttrue\\n' \
                  "/wrong/installer-state" "$FS_INSTALLER_STATE_DIR"
                fi
              fi
              if [ "$FS_STACK_ENVIRONMENT" = "production" ]; then
                printf 'bind\\t%s\\t/run/flow-steward-candidate-state\\ttrue\\n' "$candidate_state_source"
                printf 'bind\\t%s\\t/app/.env.prod\\ttrue\\n' "$candidate_env_source"
              fi
              case " ${FS_COMPOSE_FILES:-} " in
                *"/scalable/cli-host-directories.yaml "*)
                  if [ -f "$FAKE_DOCKER_STATE" ] || [ "${FAKE_CONTAINER_CLONE_MOUNTS:-1}" = "1" ]; then
                    printf 'bind\\t%s\\t/app/extensions\\ttrue\\n' "$source_prefix${FS_EXTENSIONS_DIR}"
                    printf 'bind\\t%s\\t/app/backups\\t%s\\n' \
                      "${FAKE_CONTAINER_TRANSFER_SOURCE:-$source_prefix${FS_BACKUPS_DIR}}" \
                      "${FAKE_CONTAINER_TRANSFER_RW:-true}"
                    case " ${FS_COMPOSE_FILES:-} " in
                      *"/scalable/cli-host-directories-artifacts.yaml "*)
                        printf 'bind\\t%s\\t/app/logs/artifacts\\ttrue\\n' "$source_prefix${FS_LOGS_DIR}/artifacts"
                        ;;
                    esac
                  fi
                  ;;
              esac
              if [ "$extra_socket_mount" = "1" ]; then
                printf 'bind\\t%s\\t/alternate/docker.sock\\ttrue\\n' "/var/run/docker.sock"
              fi
              exit 0
            fi
            ;;
          build)
            if [ "${FAKE_BUILD_SLEEP:-0}" != "0" ]; then
              sleep "$FAKE_BUILD_SLEEP"
            fi
            if [ -n "${FAKE_BUILD_RELEASE_FILE:-}" ]; then
              while [ ! -f "$FAKE_BUILD_RELEASE_FILE" ]; do
                sleep 0.1
              done
            fi
            printf '%s\\n' "${FAKE_BUILD_OUTPUT:-fake build output}"
            if [ "${FAKE_DOCKER_MODE:-}" = "build_fail" ]; then
              printf '%s\\n' 'safe final build line'
              exit 17
            fi
            : > "$FAKE_DOCKER_STATE.image-built"
            exit 0
            ;;
        esac
        printf 'unexpected docker command: %s\\n' "$*" >&2
        exit 64
        """,
    )
    return repo, log_path


def _launcher_env(
    fake_repo: tuple[Path, Path],
    env: dict[str, str | None] | None = None,
) -> dict[str, str]:
    _repo, log_path = fake_repo
    command_env = os.environ.copy()
    command_env.update(
        {
            "PATH": f"{log_path.parent / 'bin'}:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(log_path),
            "FAKE_DOCKER_STATE": str(log_path.parent / "docker.state"),
            "FAKE_GIT_REVISION": "test-revision",
            "FAKE_IMAGE_ID": "sha256:expected-cli-image",
        }
    )
    if env:
        for key, value in env.items():
            if value is None:
                command_env.pop(key, None)
            else:
                command_env[key] = value
    return command_env


def _run_launcher(
    fake_repo: tuple[Path, Path],
    *args: str,
    env: dict[str, str | None] | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    repo, _log_path = fake_repo
    return subprocess.run(
        [str(repo / "flow-steward"), *args],
        cwd=cwd or repo,
        env=_launcher_env(fake_repo, env),
        text=True,
        capture_output=True,
        input=input_text,
        timeout=timeout,
        check=False,
    )


def _log(fake_repo: tuple[Path, Path]) -> str:
    return fake_repo[1].read_text(encoding="utf-8") if fake_repo[1].exists() else ""


def _bootstrap_log(repo: Path) -> Path:
    logs = sorted((repo / ".flow-steward/logs").glob("cli-bootstrap-*.log"))
    assert logs
    return logs[-1]


def _write_selected_cli_image(
    fake_repo: tuple[Path, Path],
    *,
    environment: str,
    value: str | None,
) -> None:
    repo, _log_path = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir(exist_ok=True)
    if environment == "development":
        env_file = ".env"
        compose_file = "infra/docker/docker-compose.yaml"
        stack_environment = "local_development"
    else:
        env_file = ".env.prod"
        compose_file = "infra/docker/docker-compose.prod.yaml"
        stack_environment = "production"
    env_lines = [] if value is None else [f"FS_CLI_IMAGE={value}"]
    if environment == "production":
        immutable = f"registry.example/product@sha256:{'e' * 64}"
        env_lines.extend(
            (
                f"FS_APP_IMAGE={immutable}",
                f"FS_WORKER_IMAGE={immutable}",
                f"FS_CONTROL_IMAGE={immutable}",
                f"MCP_IMAGE={immutable}",
                f"FS_CANDIDATE_STATE_DIR={state_dir}",
                f"FS_CANDIDATE_PROD_ENV={repo / env_file}",
                "FS_INSTALL_MODE=official",
            )
        )
    env_text = "\n".join(env_lines) + ("\n" if env_lines else "")
    (repo / env_file).write_text(env_text, encoding="utf-8")
    (state_dir / "compose.env").write_text(
        "\n".join(
            (
                f"FS_ENV_FILE={env_file}",
                f"FS_COMPOSE_FILES={compose_file}",
                f"FS_STACK_ENVIRONMENT={stack_environment}",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _write_compact_installation(fake_repo: tuple[Path, Path], *, image: str) -> None:
    repo, _log_path = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir(exist_ok=True)
    environment_path = repo / ".env.prod"
    environment_path.write_text(
        "\n".join(
            (
                f"FS_COMPACT_IMAGE={image}",
                f"FS_CLI_IMAGE={image}",
                f"FS_CANDIDATE_STATE_DIR={state_dir}",
                f"FS_CANDIDATE_PROD_ENV={environment_path}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "compose.env").write_text(
        "\n".join(
            (
                "FS_DEPLOYMENT_PROFILE=compact",
                "FS_ENV_FILE=.env.prod",
                "FS_COMPOSE_FILES=infra/docker/docker-compose.compact.yaml",
                "FS_COMPOSE_PROFILES=cli",
                "FS_STACK_ENVIRONMENT=production",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _runtime_log(repo: Path) -> Path:
    logs = sorted((repo / ".flow-steward/logs").glob("cli-runtime-*.log"))
    assert logs
    return logs[-1]


def _count_action(fake_repo: tuple[Path, Path], action: str) -> int:
    return _log(fake_repo).splitlines().count(f"ACTION={action}")


def _arg_sequence(*args: str) -> str:
    return "\n".join(f"ARG={arg}" for arg in args)


def _write_fake_host_diagnostics_python(fake_repo: tuple[Path, Path], payload: str) -> None:
    _repo, log_path = fake_repo
    _write_executable(
        log_path.parent / "bin/python3",
        f"""\
        #!/bin/sh
        printf '%s\\n' '{payload}'
        """,
    )


def test_root_launcher_files_exist_are_executable_and_do_not_reference_old_parser() -> None:
    assert ROOT_WRAPPER.exists()
    assert LAUNCHER.exists()
    assert os.access(ROOT_WRAPPER, os.X_OK)
    assert os.access(LAUNCHER, os.X_OK)

    source = ROOT_WRAPPER.read_text(encoding="utf-8") + LAUNCHER.read_text(encoding="utf-8")
    for forbidden in (
        "core.entrypoints.cli.main",
        "manage.py",
        "python -m core.entrypoints.cli ",
    ):
        assert forbidden not in source


def test_first_status_line_is_emitted_immediately(fake_repo: tuple[Path, Path]) -> None:
    repo, log_path = fake_repo
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{log_path.parent / 'bin'}:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(log_path),
            "FAKE_DOCKER_STATE": str(log_path.parent / "docker.state"),
            "FAKE_BUILD_SLEEP": "6",
            "FAKE_GIT_REVISION": "test-revision",
            "FAKE_IMAGE_REVISION": "old-revision",
            "FAKE_IMAGE_ID": "sha256:expected-cli-image",
        }
    )
    env.pop("NO_COLOR", None)
    process = subprocess.Popen(
        [str(repo / "core/entrypoints/cli/scripts/flow_steward_launcher.sh"), "--help"],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        ready = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not ready:
            ready, _, _ = select.select([process.stderr.fileno()], [], [], 0.05)
        assert ready, "launcher did not emit a status line before build work started"
        assert os.read(process.stderr.fileno(), 4096).decode().splitlines()[0] == (
            "Flow Steward CLI: preparing management runtime..."
        )
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_missing_docker_cli_reports_remediation_and_exit_127(fake_repo: tuple[Path, Path]) -> None:
    repo, _log_path = fake_repo
    no_docker_bin = repo.parent / "no-docker-bin"
    no_docker_bin.mkdir()
    for tool in ("dirname", "sed", "cut"):
        tool_path = shutil.which(tool)
        assert tool_path is not None
        (no_docker_bin / tool).symlink_to(tool_path)
    result = subprocess.run(
        [str(repo / "core/entrypoints/cli/scripts/flow_steward_launcher.sh"), "--help"],
        cwd=repo,
        env={"PATH": str(no_docker_bin)},
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 127
    assert result.stdout == ""
    assert "Docker CLI was not found" in result.stderr
    assert "Install Docker Desktop or Docker Engine" in result.stderr
    assert "https://docs.docker.com/engine/install/" in result.stderr
    assert "https://docs.docker.com/desktop/" in result.stderr


def test_root_management_command_is_rejected_before_docker(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, log_path = fake_repo
    _write_executable(
        log_path.parent / "bin/id",
        """\
        #!/bin/sh
        case "${1:-}" in
          -u|-g) printf '0\\n'; exit 0 ;;
        esac
        exec /usr/bin/id "$@"
        """,
    )

    result = _run_launcher(fake_repo, "status")

    assert result.returncode == 4
    assert "must not be run as root" in result.stderr
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""
    assert not (repo / ".flow-steward").exists()


def test_root_json_doctor_returns_precondition_without_docker(
    fake_repo: tuple[Path, Path],
) -> None:
    _repo, log_path = fake_repo
    _write_executable(
        log_path.parent / "bin/id",
        """\
        #!/bin/sh
        case "${1:-}" in
          -u|-g) printf '0\\n'; exit 0 ;;
        esac
        exec /usr/bin/id "$@"
        """,
    )

    result = _run_launcher(fake_repo, "--json", "doctor")

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["code"] == "precondition_failed"
    assert payload["error"]["details"]["findings"][0]["check_id"] == "deployment_owner"
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_json_doctor_reports_missing_docker_without_starting_runtime(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    no_docker_bin = repo.parent / "doctor-no-docker-bin"
    no_docker_bin.mkdir()
    for tool in ("dirname", "sed", "cut"):
        tool_path = shutil.which(tool)
        assert tool_path is not None
        (no_docker_bin / tool).symlink_to(tool_path)

    result = subprocess.run(
        [str(repo / "flow-steward"), "--json", "doctor"],
        cwd=repo,
        env={"PATH": str(no_docker_bin), "SENSITIVE_TOKEN": "never-render-this"},
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    payload = json.loads(result.stdout)
    finding = payload["error"]["details"]["findings"][0]
    assert result.returncode == 4
    assert payload["schema"] == "flow-steward.cli.v1"
    assert payload["command"] == "doctor"
    assert payload["error"]["code"] == "precondition_failed"
    assert finding["check_id"] == "docker_cli"
    assert finding["status"] == "failed"
    assert "Docker Desktop or Docker Engine" in finding["remediation"]
    assert "https://docs.docker.com/engine/install/" in finding["remediation"]
    assert "https://docs.docker.com/desktop/" in finding["remediation"]
    assert "never-render-this" not in result.stdout + result.stderr
    assert "preparing management runtime" in result.stderr


@pytest.mark.parametrize(
    ("mode", "check_id", "expected"),
    [
        ("daemon_down", "docker_daemon", "Start Docker Desktop or Docker Engine"),
        ("compose_missing", "docker_compose", "Install the Docker Compose v2 plugin"),
        ("compose_v1", "docker_compose", "Install the Docker Compose v2 plugin"),
    ],
)
def test_plain_doctor_reports_docker_precondition_without_building(
    fake_repo: tuple[Path, Path], mode: str, check_id: str, expected: str
) -> None:
    result = _run_launcher(fake_repo, "doctor", "--plain", env={"FAKE_DOCKER_MODE": mode})

    assert result.returncode == 4
    assert f"failed: {check_id}" in result.stdout
    assert expected in result.stdout
    assert "CMD=build" not in _log(fake_repo)


def test_doctor_accepts_current_compose_plugin_major(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={"FAKE_DOCKER_MODE": "compose_current"},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["details"]["findings"][0]["check_id"] == "doctor_runtime"


def test_reachable_doctor_delegates_to_canonical_python_probe(
    fake_repo: tuple[Path, Path],
) -> None:
    canonical_payload = '{"schema":"flow-steward.cli.v1","command":"doctor","result":{}}'
    _write_fake_host_diagnostics_python(
        fake_repo,
        '[{"id":"host_platform","category":"core","title":"Host platform",'
        '"status":"pass","detail":"Detected Darwin on arm64.",'
        '"remediation":"No action required."}]',
    )

    result = _run_launcher(
        fake_repo,
        "--json",
        "doctor",
        env={
            **_CONFIGURATION_SECTION,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_STDOUT": canonical_payload,
        },
    )

    assert result.returncode == 0
    assert result.stdout.strip() == canonical_payload
    log = _log(fake_repo)
    assert "CMD=image inspect" in log
    assert "CMD=container inspect" in log
    assert sum(line.startswith("CMD=info") for line in log.splitlines()) == 1
    assert _count_action(fake_repo, "exec") == 1
    assert _count_action(fake_repo, "start") == 0
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "run") == 0
    assert "CMD=build" not in log
    assert (
        _arg_sequence(
            "fs-cli",
            "python",
            "-m",
            "core.entrypoints.cli.lifecycle_click_app",
            "--json",
            "doctor",
        )
        in log
    )


def _fake_install_sh(repo: Path) -> Path:
    """./install.sh as the doctor meets it: records its arguments and the doctor's inputs,
    one per line (plain sh: the test's python3 on PATH is a fake)."""
    record = repo.parent / "install-sh.record"
    _write_executable(
        repo / "install.sh",
        f"""\
        #!/bin/sh
        {{
          printf 'module=%s\\n' "${{FS_MANAGEMENT_MODULE:-}}"
          printf 'section=%s\\n' "${{FS_DOCTOR_SECTION:-}}"
          printf 'report=%s\\n' "${{FS_DOCTOR_CONFIGURATION_REPORT:-}}"
          printf 'live=%s\\n' "${{FS_DOCTOR_LIVE_INSTALLER_PID:-}}"
          for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
        }} > {str(record)!r}
        """,
    )
    return record


def _handed(record: Path) -> dict[str, object]:
    lines = record.read_text(encoding="utf-8").splitlines()
    fields = dict(line.split("=", 1) for line in lines if not line.startswith("arg="))
    return {**fields, "args": [line[4:] for line in lines if line.startswith("arg=")]}


def test_doctor_literal_help_identifier_does_not_bypass_the_live_installer_lock(
    fake_repo: tuple[Path, Path], running_install_sh: int
) -> None:
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    _write_fake_host_diagnostics_python(fake_repo, "[]")
    result = _run_launcher(
        fake_repo,
        "doctor",
        "--fix",
        "--",
        "--help",
        env={
            "FS_DOCTOR_SECTION": None,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_STDOUT": "{}",
        },
    )

    assert result.returncode == 6, result.stderr
    assert f"./install.sh is running (process {running_install_sh})" in result.stderr
    assert not record.exists()
    assert _log(fake_repo) == ""


@pytest.mark.parametrize("profile", ["scalable", "compact"])
@pytest.mark.parametrize(
    "arguments",
    [
        ("doctor", "--help"),
        ("doctor", "-h"),
        ("--json", "doctor", "--help"),
        ("--tui", "doctor", "--help"),
        ("doctor", "--fix", "stack_stopped", "--confirm", "--help"),
    ],
)
def test_doctor_help_is_metadata_only_without_installation_or_docker_actions(
    fake_repo: tuple[Path, Path], profile: str, arguments: tuple[str, ...]
) -> None:
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    if profile == "compact":
        _write_compact_installation(fake_repo, image="registry.example/compact:installed")
    _write_fake_host_diagnostics_python(fake_repo, "[]")
    state = repo / ".flow-steward"
    state_before = (
        {
            str(path.relative_to(state)): path.read_bytes()
            for path in state.rglob("*")
            if path.is_file()
        }
        if state.exists()
        else {}
    )

    result = _run_launcher(
        fake_repo,
        *arguments,
        env={
            "FS_DOCTOR_SECTION": None,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_STDOUT": "{}",
        },
    )

    assert not record.exists(), "metadata help invoked install.sh"
    assert _log(fake_repo) == "", "metadata help invoked Docker diagnostics or runtime actions"
    assert result.returncode == 0, result.stderr
    assert "usage: flow-steward doctor" in result.stdout.lower()
    for option in (
        "--fix",
        "--confirm",
        "--json",
        "--plain",
        "--tui",
        "--no-color",
        "--verbose",
        "--non-interactive",
        "--help",
    ):
        assert option in result.stdout
    state_after = (
        {
            str(path.relative_to(state)): path.read_bytes()
            for path in state.rglob("*")
            if path.is_file()
        }
        if state.exists()
        else {}
    )
    assert state_after == state_before


def test_doctor_help_after_argument_terminator_remains_a_fix_identifier(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    _write_fake_host_diagnostics_python(fake_repo, "[]")

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--fix",
        "--",
        "--help",
        env={
            "FS_DOCTOR_SECTION": None,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_STDOUT": "{}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _handed(record)["args"] == ["--fix", "--", "--help"]
    assert "usage: flow-steward doctor" not in result.stdout.lower()


def test_doctor_launcher_help_covers_the_management_controller_options(
    fake_repo: tuple[Path, Path],
) -> None:
    from core.entrypoints.cli.doctor import _parser

    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    _write_fake_host_diagnostics_python(fake_repo, "[]")
    result = _run_launcher(
        fake_repo,
        "doctor",
        "--help",
        env={
            "FS_DOCTOR_SECTION": None,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_STDOUT": "{}",
        },
    )

    assert not record.exists()
    assert result.returncode == 0, result.stderr
    for action in _parser()._actions:
        for option in action.option_strings:
            assert option in result.stdout
    assert "ids" in result.stdout


def test_doctor_runs_the_configuration_section_then_the_installation_checks(
    fake_repo: tuple[Path, Path],
) -> None:
    """The doctor task: one doctor for both profiles, in ./install.sh's management
    runtime; the earlier checks arrive as its configuration section, run with --json."""
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    canonical_payload = '{"schema":"flow-steward.cli.v1","command":"doctor","result":{}}'
    _write_fake_host_diagnostics_python(fake_repo, "[]")

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--plain",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_COMPOSE_STDOUT": canonical_payload},
    )

    assert result.returncode == 0, result.stderr
    handed = _handed(record)
    assert handed["args"] == ["--plain"]  # ./install.sh takes no subcommand
    assert handed["module"] == "doctor"
    assert handed["report"] == canonical_payload
    assert handed["section"] == ""
    assert _arg_sequence(
        "fs-cli", "python", "-m", "core.entrypoints.cli.lifecycle_click_app", "doctor", "--json"
    ) in _log(fake_repo)
    # A plain doctor writes nothing to the installation: the section's transient files
    # were in a directory of its own, and it is gone.
    runtime_dir = repo / ".flow-steward/runtime"
    assert not any(runtime_dir.glob("cli-runtime-*.capture"))
    assert not any(runtime_dir.glob("docker-preflight-*"))


def test_doctor_fix_hands_its_ids_to_the_installation_checks_without_a_section(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    _write_fake_host_diagnostics_python(fake_repo, "[]")

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--fix",
        "stack_stopped",
        "--confirm",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_COMPOSE_STDOUT": "{}"},
    )

    assert result.returncode == 0, result.stderr
    handed = _handed(record)
    assert handed["args"] == ["--fix", "stack_stopped", "--confirm"]
    assert handed["module"] == "doctor"
    assert handed["report"] == ""  # it repairs the installation, not its configuration
    assert handed["live"] == "none"


def test_doctor_fix_runs_while_only_an_orphaned_installer_container_is_left(
    fake_repo: tuple[Path, Path],
) -> None:
    """orphan_installer: an installer container still runs, its ./install.sh is gone.
    The container half of "is ./install.sh running" refused every other command --
    rightly -- and would have refused the one repair that stops it."""
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    state = repo / ".flow-steward"
    state.mkdir(exist_ok=True)
    (state / "installer-runtime.cid").write_text("a1b2c3d4e5f6a7b8c9d0\n", encoding="utf-8")
    _write_fake_host_diagnostics_python(fake_repo, "[]")

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--fix",
        "orphan_installer",
        "--confirm",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_STDOUT": "{}",
            "FAKE_INSTALLER_CID": "a1b2c3d4e5f6a7b8c9d0",
            "FAKE_INSTALLER_RUNNING": "true",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _handed(record)["args"] == ["--fix", "orphan_installer", "--confirm"]


def test_doctor_fix_is_refused_while_install_sh_lives(
    fake_repo: tuple[Path, Path], running_install_sh: int
) -> None:
    """The green side of the one above: a live ./install.sh is never repaired under."""
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)

    result = _run_launcher(fake_repo, "doctor", "--fix", "--confirm")

    assert result.returncode == 6
    assert f"./install.sh is running (process {running_install_sh})" in result.stderr
    assert not record.exists()


def test_a_plain_doctor_hears_which_install_sh_lives(
    fake_repo: tuple[Path, Path], running_install_sh: int
) -> None:
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)
    _write_fake_host_diagnostics_python(fake_repo, "[]")

    result = _run_launcher(
        fake_repo,
        "doctor",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_COMPOSE_STDOUT": "{}"},
    )

    assert result.returncode == 0, result.stderr
    assert _handed(record)["live"] == str(running_install_sh)


def test_doctor_blocked_before_its_checks_answers_as_before_and_runs_nothing_else(
    fake_repo: tuple[Path, Path],
) -> None:
    """Without Docker the installation checks cannot run either: the same answer as the
    earlier doctor, and ./install.sh is not started (the green side of the fallback is
    the test above)."""
    repo, _log_path = fake_repo
    record = _fake_install_sh(repo)

    result = _run_launcher(fake_repo, "--json", "doctor", env={"FAKE_DOCKER_MODE": "daemon_down"})

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["code"] == "precondition_failed"
    assert payload["error"]["details"]["findings"][0]["check_id"] == "docker_daemon"
    assert not record.exists()


def test_compact_doctor_uses_one_shot_compact_cli_without_persistent_runtime(
    fake_repo: tuple[Path, Path],
) -> None:
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)
    canonical_payload = '{"schema":"flow-steward.cli.v1","command":"doctor","result":{}}'

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={
            **_CONFIGURATION_SECTION,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": compact_image,
            "FAKE_COMPOSE_STDOUT": canonical_payload,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == canonical_payload
    log = _log(fake_repo)
    assert f"CMD=image inspect {compact_image} " in log
    assert f"ENV_FS_CLI_IMAGE={compact_image}" in log
    assert _count_action(fake_repo, "run") == 1
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "start") == 0
    assert _count_action(fake_repo, "up") == 0
    assert "CMD=container inspect" not in log
    assert "CMD=build" not in log
    assert _arg_sequence("fs-cli", "doctor", "--json") in log


def test_compact_configuration_doctor_forwards_section_to_one_shot_container(
    fake_repo: tuple[Path, Path],
) -> None:
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={
            **_CONFIGURATION_SECTION,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": compact_image,
        },
    )

    assert result.returncode == 0, result.stderr
    assert _arg_sequence("-e", "FS_DOCTOR_SECTION=configuration", "fs-cli", "doctor", "--json") in (
        _log(fake_repo)
    )


@pytest.mark.parametrize("args", ((), ("--help",), ("-h",)))
def test_compact_root_help_lists_supported_commands_without_starting_runtime(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(fake_repo, *args)

    assert result.returncode == 0, result.stderr
    assert "Usage: flow-steward [OPTIONS] COMMAND" in result.stdout
    for command in (
        "status",
        "doctor",
        "maintenance status|enable|disable",
        "database status|upgrade",
        "backup create|list|verify",
    ):
        assert command in result.stdout
    assert _log(fake_repo) == ""


def test_compact_clone_create_reaches_platform_dispatcher_with_secret_stdin_flag(
    fake_repo: tuple[Path, Path],
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        "clone",
        "create",
        "--output-directory",
        "/private/backups",
        "--passphrase-stdin",
        "--confirm",
        "--non-interactive",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 0, result.stderr
    assert _arg_sequence(
        "fs-cli",
        "clone-create",
        "--output",
        "/private/backups",
        "--confirm",
        "--passphrase-stdin",
        "--non-interactive",
    ) in _log(fake_repo)


def test_compact_clone_restore_forwards_archive_and_target_bound_confirmation(
    fake_repo: tuple[Path, Path],
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    token = "target|compact|clone.example.test|" + "a" * 64

    result = _run_launcher(
        fake_repo,
        "clone",
        "restore",
        "/private/source.fsclone",
        "--passphrase-stdin",
        "--confirm",
        token,
        "--non-interactive",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 0, result.stderr
    assert _arg_sequence(
        "fs-cli",
        "clone-restore",
        "--archive",
        "/private/source.fsclone",
        "--confirmation",
        token,
        "--passphrase-stdin",
        "--non-interactive",
    ) in _log(fake_repo)


def test_compact_clone_restore_allows_interactive_confirmation_after_resume(
    fake_repo: tuple[Path, Path],
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        "clone",
        "restore",
        "/private/source.fsclone",
        "--operation-id",
        "clone-restore-resume",
        "--passphrase-stdin",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 0, result.stderr
    assert _arg_sequence(
        "fs-cli",
        "clone-restore",
        "--archive",
        "/private/source.fsclone",
        "--operation-id",
        "clone-restore-resume",
        "--passphrase-stdin",
    ) in _log(fake_repo)


def test_compact_doctor_fails_closed_when_exact_compact_image_is_missing(
    fake_repo: tuple[Path, Path],
) -> None:
    _write_compact_installation(fake_repo, image="")

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["details"]["findings"][0]["check_id"] == "doctor_runtime"
    assert "Compact image" in payload["error"]["details"]["findings"][0]["detail"]
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 0
    assert "CMD=build" not in log


def test_compact_command_runs_when_the_deployment_checkout_revision_differs(
    fake_repo: tuple[Path, Path],
) -> None:
    """AC17-R36: a pinned Compact image is authoritative, the deployment directory is not.

    A provider deployment is usually not a Git checkout at all, so its revision cannot
    decide whether the installed image is current. Before this, every flow-steward
    command died in the launcher with exit 4 before any Python ran.
    """
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(
        fake_repo,
        "status",
        "--json",
        env={
            "FAKE_GIT_REVISION": "current-revision",
            "FAKE_IMAGE_REVISION": "stale-revision",
            "FAKE_IMAGE_ID": compact_image,
            "FAKE_COMPOSE_STDOUT": '{"schema":"flow-steward.cli.v1","command":"status"}',
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    # the invariant this case has always protected stays: no build, no retag
    assert "CMD=build" not in log
    assert "CMD=image tag" not in log
    # and the independently pinned Compact management image is what the runtime is given.
    assert f"ENV_FS_CLI_IMAGE={compact_image}" in log
    assert f"FS_CLI_IMAGE={compact_image}" in (fake_repo[0] / ".env.prod").read_text(
        encoding="utf-8"
    )


def test_compact_uses_the_canonical_cli_role_pin(
    fake_repo: tuple[Path, Path],
) -> None:
    """The management/CLI role is canonical and independent of the runtime profile."""
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)
    env_file = fake_repo[0] / ".env.prod"
    cli_image = f"sha256:{'d' * 64}"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + f"FS_CLI_IMAGE={cli_image}\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        fake_repo,
        "status",
        "--json",
        env={
            "FAKE_GIT_REVISION": "current-revision",
            "FAKE_IMAGE_REVISION": "previous-revision",
            "FAKE_IMAGE_ID": cli_image,
            "FAKE_COMPOSE_STDOUT": '{"schema":"flow-steward.cli.v1","command":"status"}',
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"ENV_FS_CLI_IMAGE={cli_image}" in _log(fake_repo)


def test_compact_command_fails_closed_when_the_pinned_image_does_not_resolve(
    fake_repo: tuple[Path, Path],
) -> None:
    """The image identity itself, not the checkout, is what must fail closed."""
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(
        fake_repo,
        "status",
        "--json",
        env={
            "FAKE_GIT_REVISION": "current-revision",
            "FAKE_IMAGE_REVISION": "stale-revision",
            "FAKE_IMAGE_ID": f"sha256:{'d' * 64}",
        },
    )

    assert result.returncode == 4
    assert "Rerun ./install.sh" in result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 0
    assert "CMD=build" not in log


@pytest.mark.parametrize(
    ("public_args", "platform_args"),
    (
        (("status", "--json"), ("status", "--json")),
        (("release", "info", "--json"), ("release-info", "--json")),
        (("release", "check", "--json"), ("release-check", "--json")),
        (("release", "check", "--tui"), ("release-check", "--tui")),
        (
            (
                "release",
                "upgrade",
                "--target-version",
                "1.2.3",
                "--timeout-seconds",
                "120",
                "--dry-run",
                "--confirm",
                "--json",
                "--plain",
                "--no-color",
                "--non-interactive",
                "--verbose",
            ),
            (
                "release-upgrade",
                "--target-version",
                "1.2.3",
                "--timeout-seconds",
                "120",
                "--dry-run",
                "--confirm",
                "--json",
                "--plain",
                "--no-color",
                "--non-interactive",
                "--verbose",
            ),
        ),
        (("database", "status", "--json"), ("database-status", "--json")),
        (("backup", "create", "--json"), ("backup-create", "--json")),
        (("backup", "list", "--json"), ("backup-list", "--json")),
        (
            ("backup", "verify", "backups/example.dump.enc", "--json"),
            (
                "backup-verify",
                "--backup",
                "backups/example.dump.enc",
                "--json",
            ),
        ),
        (
            (
                "database",
                "restore-parity-capture",
                "--output",
                "backups/restore/baseline.json",
                "--label",
                "baseline",
                "--confirm",
                "--json",
            ),
            (
                "database-restore-parity-capture",
                "--output",
                "backups/restore/baseline.json",
                "--label",
                "baseline",
                "--confirm",
                "--json",
            ),
        ),
        (
            (
                "database",
                "restore-parity-verify",
                "--baseline",
                "backups/restore/baseline.json",
                "--output",
                "backups/restore/compact.json",
                "--label",
                "compact",
                "--confirm",
                "--json",
            ),
            (
                "database-restore-parity-verify",
                "--baseline",
                "backups/restore/baseline.json",
                "--output",
                "backups/restore/compact.json",
                "--label",
                "compact",
                "--confirm",
                "--json",
            ),
        ),
    ),
)
def test_compact_lifecycle_commands_use_fixed_one_shot_platform_operations(
    fake_repo: tuple[Path, Path],
    public_args: tuple[str, ...],
    platform_args: tuple[str, ...],
) -> None:
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(
        fake_repo,
        *public_args,
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": compact_image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 1
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "start") == 0
    assert _count_action(fake_repo, "up") == 0
    assert "CMD=container inspect" not in log
    assert "CMD=build" not in log
    assert _arg_sequence("fs-cli", *platform_args) in log


@pytest.mark.parametrize(
    ("public_args", "platform_args"),
    (
        (("maintenance", "status", "--json"), ("maintenance-status", "--json")),
        (
            ("maintenance", "enable", "--confirm", "--json"),
            ("maintenance-enable", "--confirm", "--json"),
        ),
        (
            ("maintenance", "disable", "--confirm", "--json"),
            ("maintenance-disable", "--confirm", "--json"),
        ),
    ),
)
def test_compact_maintenance_preserves_the_host_management_guard(
    fake_repo: tuple[Path, Path],
    public_args: tuple[str, ...],
    platform_args: tuple[str, ...],
) -> None:
    """The host client guards host journals before invoking the native marker owner."""
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(
        fake_repo,
        *public_args,
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_IMAGE_ID": compact_image},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "run") == 1
    assert _arg_sequence("fs-cli", *platform_args) in log
    assert "ARG=0:0" not in log
    assert _count_action(fake_repo, "up") == 0
    assert "CMD=build" not in log


@pytest.mark.parametrize(
    "args",
    (
        # The two the peer measured on a READY Compact installation: exit 8 and
        # "rerun ./install.sh, then retry".
        ("config", "show"),
        ("stack", "status"),
        # And the two a previous version of this test pinned as dispatched into a
        # retained runtime -- green only because the fake supplied a container a
        # Compact installation does not have by construction.
        (
            "config",
            "set",
            "--deployment-profile",
            "scalable",
            "--json",
            "--non-interactive",
        ),
        ("stack", "recover", "--operation-id", "operation-1", "--confirm", "--json"),
    ),
)
def test_compact_refuses_the_commands_it_has_no_runtime_for(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    """AC17-R140: accepted, failed to start a container, and advised a reinstall.

    A reinstall cannot create a retained CLI container on a profile that has none, so
    the operator of a working installation was told to reinstall it. The launcher knew
    and said so in its help; it now refuses like `environment`, naming the reason and
    the path that does work, before Docker is asked anything.
    """
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        *args,
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_CONTAINER_RUNTIME_REVISION": "retained-scalable-runtime",
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 2, result.stderr
    assert "Unsupported Compact platform command" in result.stderr
    assert f"{args[0]} runs in a retained CLI container" in result.stderr
    assert "reinstalling will not help" in result.stderr
    assert "rerun ./install.sh from updated sources" in result.stderr
    assert "Remediation: rerun ./install.sh, then retry" not in result.stderr
    assert _log(fake_repo) == ""


@pytest.mark.parametrize(
    "image_revision",
    (None, "stale-revision"),
)
def test_doctor_never_repairs_missing_or_stale_cli_image(
    fake_repo: tuple[Path, Path], image_revision: str | None
) -> None:
    result = _run_launcher(
        fake_repo,
        "--json",
        "doctor",
        env={"FAKE_IMAGE_REVISION": image_revision},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["details"]["findings"][0]["check_id"] == "doctor_runtime"
    log = _log(fake_repo)
    assert "CMD=build" not in log
    assert "ACTION=start" not in log
    assert "ACTION=up" not in log
    assert "ACTION=rm" not in log


def test_source_doctor_reuses_image_when_cli_build_inputs_are_unchanged(
    fake_repo: tuple[Path, Path],
) -> None:
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value="flow-steward-cli:0.1.0",
    )
    with (fake_repo[0] / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("FS_INSTALL_MODE=source\n")
    _write_fake_host_diagnostics_python(fake_repo, "[]")
    canonical_payload = '{"schema":"flow-steward.cli.v1","command":"doctor","result":{}}'

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={
            **_CONFIGURATION_SECTION,
            "FAKE_GIT_REVISION": "b" * 40,
            "FAKE_IMAGE_REVISION": "a" * 40,
            "FAKE_GIT_CHANGED_PATHS": "",
            "FAKE_COMPOSE_STDOUT": canonical_payload,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == canonical_payload
    log = _log(fake_repo)
    assert "CMD=build" not in log
    assert "ACTION=start" not in log
    assert "ACTION=up" not in log
    assert "ACTION=rm" not in log


def test_exact_prebuilt_doctor_accepts_honest_older_cli_revision(
    fake_repo: tuple[Path, Path],
) -> None:
    exact_image = f"sha256:{'a' * 64}"
    _write_selected_cli_image(
        fake_repo,
        environment="production",
        value=exact_image,
    )
    repo, _log_path = fake_repo
    with (repo / ".env.prod").open("a", encoding="utf-8") as environment_file:
        for key in (
            "FS_APP_IMAGE",
            "FS_WORKER_IMAGE",
            "FS_CONTROL_IMAGE",
            "MCP_IMAGE",
        ):
            environment_file.write(f"{key}={exact_image}\n")
    _write_fake_host_diagnostics_python(fake_repo, "[]")
    canonical_payload = '{"schema":"flow-steward.cli.v1","command":"doctor","result":{}}'

    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={
            **_CONFIGURATION_SECTION,
            "FAKE_GIT_REVISION": "b" * 40,
            "FAKE_IMAGE_REVISION": "a" * 40,
            "FAKE_IMAGE_ID": exact_image,
            "FAKE_COMPOSE_STDOUT": canonical_payload,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == canonical_payload
    log = _log(fake_repo)
    assert "CMD=build" not in log
    assert "ACTION=start" not in log
    assert "ACTION=up" not in log
    assert "ACTION=rm" not in log


def test_exact_prebuilt_command_reuses_honest_older_cli_revision(
    fake_repo: tuple[Path, Path],
) -> None:
    exact_image = f"sha256:{'a' * 64}"
    _write_selected_cli_image(
        fake_repo,
        environment="production",
        value=exact_image,
    )
    repo, _log_path = fake_repo
    with (repo / ".env.prod").open("a", encoding="utf-8") as environment_file:
        for key in (
            "FS_APP_IMAGE",
            "FS_WORKER_IMAGE",
            "FS_CONTROL_IMAGE",
            "MCP_IMAGE",
        ):
            environment_file.write(f"{key}={exact_image}\n")

    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_GIT_REVISION": "b" * 40,
            "FAKE_IMAGE_REVISION": "a" * 40,
            "FAKE_IMAGE_ID": exact_image,
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert "CMD=build" not in log
    assert _count_action(fake_repo, "exec") == 1


def test_launcher_uses_explicit_installer_state_root_for_runtime_identity(
    fake_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, _log_path = fake_repo
    state_dir = tmp_path / "installed-state"
    state_dir.mkdir()
    exact_image = f"sha256:{'a' * 64}"
    env_path = repo / ".env.prod"
    env_path.write_text(
        "\n".join(
            (
                f"FS_APP_IMAGE={exact_image}",
                f"FS_WORKER_IMAGE={exact_image}",
                f"FS_CONTROL_IMAGE={exact_image}",
                f"FS_CLI_IMAGE={exact_image}",
                f"MCP_IMAGE={exact_image}",
                f"FS_CANDIDATE_STATE_DIR={state_dir}",
                f"FS_CANDIDATE_PROD_ENV={env_path}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "compose.env").write_text(
        "\n".join(
            (
                "FS_STACK_ENVIRONMENT=production",
                "FS_COMPOSE_PROJECT_NAME=isolated-installed-project",
                "FS_COMPOSE_FILES=infra/docker/docker-compose.prod.yaml",
                "FS_ENV_FILE=.env.prod",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FS_INSTALLER_STATE_DIR": str(state_dir),
            "FAKE_CONTAINER_CANDIDATE_STATE_SOURCE": str(state_dir),
            "FAKE_GIT_REVISION": "b" * 40,
            "FAKE_IMAGE_REVISION": "a" * 40,
            "FAKE_IMAGE_ID": exact_image,
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert "CMD=build" not in log
    assert "-p isolated-installed-project" in log
    assert _count_action(fake_repo, "exec") == 1


def test_doctor_never_repairs_missing_cli_container(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "doctor",
        "--plain",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
        },
    )

    assert result.returncode == 4
    assert "failed: doctor_runtime" in result.stdout
    log = _log(fake_repo)
    assert "ACTION=start" not in log
    assert "ACTION=up" not in log
    assert "ACTION=rm" not in log


@pytest.mark.parametrize(
    "env",
    (
        {"FAKE_CONTAINER_RUNNING": "false"},
        {"FAKE_CONTAINER_RUNTIME_REVISION": "0" * 64},
    ),
)
def test_doctor_never_repairs_stopped_or_stale_cli_container(
    fake_repo: tuple[Path, Path], env: dict[str, str]
) -> None:
    result = _run_launcher(
        fake_repo,
        "doctor",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision", **env},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["details"]["findings"][0]["check_id"] == "doctor_runtime"
    log = _log(fake_repo)
    assert "CMD=container inspect" in log
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "start") == 0
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "run") == 0
    assert "CMD=build" not in log


def test_reachable_doctor_streams_host_findings_to_canonical_python(
    fake_repo: tuple[Path, Path],
) -> None:
    _repo, log_path = fake_repo
    host_findings = json.dumps(
        [
            {
                "id": "host_platform",
                "category": "core",
                "title": "Host platform",
                "status": "pass",
                "detail": "Detected Darwin on arm64.",
                "remediation": "No action required.",
            },
            {
                "id": "default_ports",
                "category": "core",
                "title": "Default host ports",
                "status": "fail",
                "detail": "Default host ports are already in use: 55432/tcp.",
                "remediation": "Free port 55432/tcp.",
            },
        ],
        separators=(",", ":"),
    )
    _write_executable(
        log_path.parent / "bin/python3",
        """\
        #!/bin/sh
        printf '%s\\n' "$FAKE_HOST_FINDINGS"
        """,
    )
    canonical_payload = '{"schema":"flow-steward.cli.v1","command":"doctor","result":{}}'

    result = _run_launcher(
        fake_repo,
        "--json",
        "doctor",
        env={
            **_CONFIGURATION_SECTION,
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CAPTURE_STDIN": "1",
            "FAKE_HOST_FINDINGS": host_findings,
            "FAKE_COMPOSE_STDOUT": canonical_payload,
        },
    )

    assert result.returncode == 0
    assert result.stdout.strip() == canonical_payload
    assert f"STDIN={host_findings}" in _log(fake_repo)


@pytest.mark.parametrize(
    ("env", "check_id"),
    [
        ({"DOCKER_HOST": "tcp://127.0.0.1:2375"}, "docker_context"),
        ({"FAKE_CONTEXT_HOST": "ssh://builder.example"}, "docker_context"),
        ({"FAKE_ROOTLESS": "1"}, "docker_rootless"),
    ],
)
def test_exact_doctor_routes_host_boundary_failures_to_bounded_finding(
    fake_repo: tuple[Path, Path], env: dict[str, str], check_id: str
) -> None:
    result = _run_launcher(fake_repo, "doctor", "--json", env=env)

    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["error"]["details"]["findings"][0]["check_id"] == check_id
    assert "CMD=build" not in _log(fake_repo)


def test_exact_doctor_routes_unsupported_architecture_to_bounded_finding(
    fake_repo: tuple[Path, Path],
) -> None:
    _repo, log_path = fake_repo
    _write_executable(
        log_path.parent / "bin/uname",
        """\
        #!/bin/sh
        case "${1:-}" in
          -m) printf '%s\\n' 'riscv64' ;;
          -s) printf '%s\\n' 'Linux' ;;
          *) printf '%s\\n' 'Linux' ;;
        esac
        """,
    )

    result = _run_launcher(fake_repo, "doctor", "--plain")

    assert result.returncode == 4
    assert "failed: host_platform" in result.stdout
    assert "amd64 or arm64" in result.stdout
    assert "CMD=build" not in _log(fake_repo)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"FAKE_DOCKER_MODE": "daemon_down"}, "Docker daemon is not reachable"),
        ({"FAKE_DOCKER_MODE": "compose_missing"}, "Docker Compose v2 is not available"),
        ({"DOCKER_HOST": "tcp://127.0.0.1:2375"}, "Remote Docker endpoints are not supported"),
        (
            {"FAKE_CONTEXT_HOST": "ssh://builder.example"},
            "Remote Docker endpoints are not supported",
        ),
        ({"FAKE_ROOTLESS": "1"}, "Rootless Docker is not supported for this first release"),
    ],
)
def test_launcher_reports_distinct_preflight_failures(
    fake_repo: tuple[Path, Path], env: dict[str, str], expected: str
) -> None:
    result = _run_launcher(fake_repo, "--help", env=env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert expected in result.stderr
    if env.get("FAKE_DOCKER_MODE") == "compose_missing":
        assert "https://docs.docker.com/compose/install/linux/" in result.stderr
        assert "https://docs.docker.com/desktop/" in result.stderr
    assert "CMD=build" not in _log(fake_repo)


def test_local_unix_socket_context_is_accepted(fake_repo: tuple[Path, Path]) -> None:
    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_CONTEXT_HOST": "unix:///var/run/docker.sock",
            "FAKE_IMAGE_REVISION": "test-revision",
        },
    )

    assert result.returncode == 0
    assert "CMD=compose" in _log(fake_repo)


def test_docker_desktop_socket_gid_uses_container_visible_root_group(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, log_path = fake_repo
    socket_target = repo.parent / "docker-desktop.sock"
    socket_target.touch()
    socket_link = repo.parent / "docker.sock"
    socket_link.symlink_to(socket_target)
    _write_executable(
        log_path.parent / "bin/uname",
        """\
        #!/bin/sh
        case "${1:-}" in
          -m) printf '%s\\n' 'arm64' ;;
          -s) printf '%s\\n' 'Darwin' ;;
          *) printf '%s\\n' 'Darwin' ;;
        esac
        """,
    )
    _write_executable(
        log_path.parent / "bin/stat",
        """\
        #!/bin/sh
        case "$*" in
          "-L -c %g "*|"-L -f %g "*)
            printf '%s\\n' '20'
            exit 0
            ;;
          "-c %g "*|"-f %g "*)
            printf '%s\\n' '1'
            exit 0
            ;;
        esac
        exit 2
        """,
    )

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "DOCKER_SOCKET": str(socket_link),
            "FAKE_IMAGE_REVISION": "test-revision",
        },
    )

    assert result.returncode == 0
    assert "ENV_FS_DOCKER_GID=0" in _log(fake_repo)


def test_linux_docker_socket_gid_follows_symlink_target(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, log_path = fake_repo
    socket_target = repo.parent / "docker-engine.sock"
    socket_target.touch()
    socket_link = repo.parent / "docker.sock"
    socket_link.symlink_to(socket_target)
    _write_executable(
        log_path.parent / "bin/uname",
        """\
        #!/bin/sh
        case "${1:-}" in
          -m) printf '%s\\n' 'x86_64' ;;
          -s) printf '%s\\n' 'Linux' ;;
          *) printf '%s\\n' 'Linux' ;;
        esac
        """,
    )
    _write_executable(
        log_path.parent / "bin/stat",
        """\
        #!/bin/sh
        case "$*" in
          "-L -c %g "*|"-L -f %g "*)
            printf '%s\\n' '998'
            exit 0
            ;;
          "-c %g "*|"-f %g "*)
            printf '%s\\n' '1'
            exit 0
            ;;
        esac
        exit 2
        """,
    )

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "DOCKER_SOCKET": str(socket_link),
            "FAKE_IMAGE_REVISION": "test-revision",
        },
    )

    assert result.returncode == 0
    assert "ENV_FS_DOCKER_GID=998" in _log(fake_repo)


def test_matching_image_revision_reuses_versioned_image(fake_repo: tuple[Path, Path]) -> None:
    version = (fake_repo[0] / "VERSION").read_text(encoding="utf-8").strip()
    result = _run_launcher(fake_repo, "--help", env={"FAKE_IMAGE_REVISION": "test-revision"})

    assert result.returncode == 0
    log = _log(fake_repo)
    assert f"flow-steward-cli:{version}" in log
    assert "CMD=build" not in log


def test_changed_git_revision_rebuilds_with_safe_build_args(fake_repo: tuple[Path, Path]) -> None:
    version = (fake_repo[0] / "VERSION").read_text(encoding="utf-8").strip()
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=f"flow-steward-cli:{version}",
    )
    result = _run_launcher(fake_repo, "--help", env={"FAKE_IMAGE_REVISION": "old-revision"})

    assert result.returncode == 0
    log = _log(fake_repo)
    assert "CMD=build" in log
    assert "--target cli" in log
    assert f"-t flow-steward-cli:{version}" in log
    assert "FS_BUILD_REVISION=test-revision" in log
    assert not any(secret in log.lower() for secret in ("password", "secret", "token"))


def test_source_development_reuses_cli_image_when_cli_build_inputs_are_unchanged(
    fake_repo: tuple[Path, Path],
) -> None:
    selected_image = "flow-steward-cli:0.1.0"
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=selected_image,
    )
    with (fake_repo[0] / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("FS_INSTALL_MODE=source\n")

    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_GIT_REVISION": "b" * 40,
            "FAKE_IMAGE_REVISION": "a" * 40,
            "FAKE_GIT_CHANGED_PATHS": "",
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert f"CMD=image inspect {selected_image} " in log
    assert "CMD=build" not in log


@pytest.mark.parametrize(
    "git_env",
    (
        {"FAKE_GIT_CHANGED_PATHS": "requirements-cli.lock"},
        {"FAKE_GIT_DIFF_FAIL": "1"},
    ),
    ids=("changed-input", "unknown-history"),
)
def test_source_development_rebuilds_cli_for_changed_or_unknown_build_inputs(
    fake_repo: tuple[Path, Path],
    git_env: dict[str, str],
) -> None:
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value="flow-steward-cli:0.1.0",
    )
    with (fake_repo[0] / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("FS_INSTALL_MODE=source\n")

    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_GIT_REVISION": "b" * 40,
            "FAKE_IMAGE_REVISION": "a" * 40,
            **git_env,
        },
    )

    assert result.returncode == 0, result.stderr
    assert (
        _log(fake_repo)
        .splitlines()
        .count(
            "CMD=build --target cli --build-arg FS_BUILD_REVISION="
            + "b" * 40
            + " -t flow-steward-cli:0.1.0 -f "
            + str(fake_repo[0] / "infra/docker/Dockerfile")
            + " "
            + str(fake_repo[0])
        )
        == 1
    )


@pytest.mark.parametrize("environment", ["development", "production"])
def test_selected_cli_image_is_used_for_build_inspection_and_runtime_start(
    fake_repo: tuple[Path, Path],
    environment: str,
) -> None:
    selected_image = (
        f"fs-inst-{environment}-cli:acceptance"
        if environment == "development"
        else f"registry.example/flow-steward-cli@sha256:{'d' * 64}"
    )
    _write_selected_cli_image(
        fake_repo,
        environment=environment,
        value=f'  "{selected_image}"  ',
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "old-revision",
            "FAKE_CONTAINER_PRESENT": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert f"CMD=image inspect {selected_image} " in log
    if environment == "development":
        assert f"-t {selected_image}" in log
    else:
        assert "CMD=build " not in log
    assert f"ENV_FS_CLI_IMAGE={selected_image}" in log
    assert _count_action(fake_repo, "up") == 1
    assert "flow-steward-cli:" not in log


def test_production_refuses_incomplete_official_image_set_without_local_build(
    fake_repo: tuple[Path, Path],
) -> None:
    selected = f"registry.example/flow-steward-cli@sha256:{'d' * 64}"
    _write_selected_cli_image(fake_repo, environment="production", value=selected)
    repo, _log_path = fake_repo
    (repo / ".env.prod").write_text(
        "\n".join(
            (
                f"FS_CLI_IMAGE={selected}",
                "FS_INSTALL_MODE=official",
                f"FS_CANDIDATE_STATE_DIR={repo / '.flow-steward'}",
                f"FS_CANDIDATE_PROD_ENV={repo / '.env.prod'}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "old-revision", "FAKE_CONTAINER_PRESENT": "0"},
    )

    assert result.returncode == 4
    assert "local image builds are disabled" in result.stderr
    assert "CMD=build " not in _log(fake_repo)


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(
    ("container_running", "expected_start"),
    [("true", 0), ("false", 1)],
    ids=("running-reuse", "stopped-start"),
)
def test_selected_cli_image_reuses_or_starts_exact_runtime_without_shared_tag(
    fake_repo: tuple[Path, Path],
    environment: str,
    container_running: str,
    expected_start: int,
) -> None:
    selected_image = f"fs-inst-{environment}-cli:acceptance"
    _write_selected_cli_image(
        fake_repo,
        environment=environment,
        value=selected_image,
    )

    result = _run_launcher(
        fake_repo,
        "--version",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_RUNNING": container_running,
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert f"CMD=image inspect {selected_image} " in log
    assert sum(line.startswith("CMD=image inspect") for line in log.splitlines()) == 1
    assert f"ENV_FS_CLI_IMAGE={selected_image}" in log
    assert "CMD=build" not in log
    assert _count_action(fake_repo, "ps") == 0
    assert _count_action(fake_repo, "start") == expected_start
    assert _count_action(fake_repo, "up") == 0
    assert "flow-steward-cli:" not in log


@pytest.mark.parametrize(
    ("environment", "selected_value"),
    [
        ("development", None),
        ("development", "   "),
        ("production", None),
        ("production", "   "),
    ],
    ids=(
        "development-absent",
        "development-blank",
        "production-absent",
        "production-blank",
    ),
)
def test_launcher_uses_version_image_only_when_selected_value_is_empty(
    fake_repo: tuple[Path, Path],
    environment: str,
    selected_value: str | None,
) -> None:
    repo, _log_path = fake_repo
    _write_selected_cli_image(
        fake_repo,
        environment=environment,
        value=selected_value,
    )
    version_image = f"flow-steward-cli:{(repo / 'VERSION').read_text(encoding='utf-8').strip()}"

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert f"CMD=image inspect {version_image} " in log
    assert f"ENV_FS_CLI_IMAGE={version_image}" in log


def test_selected_cli_image_is_parsed_without_source_or_eval(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    side_effect = tmp_path / "operator-env-was-executed"
    selected_image = f"$(touch {side_effect})"
    _write_selected_cli_image(
        fake_repo,
        environment="production",
        value=selected_image,
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    assert selected_image in _log(fake_repo)
    assert not side_effect.exists()


def test_compose_env_whitelist_is_parsed_without_source_or_eval(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _ = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir()
    (state_dir / "runtime.env").write_text("FS_SECRET_KEY=not-read-by-launcher\n", encoding="utf-8")
    (state_dir / "compose.env").write_text(
        "\n".join(
            [
                '  FS_COMPOSE_PROJECT_NAME = "custom-flow"  ',
                "FS_COMPOSE_FILES = 'infra/docker/docker-compose.prod.yaml' ",
                "FS_ENV_FILE = '.flow-steward/runtime.env'",
                "FS_STACK_ENVIRONMENT=local_development",
                "UNKNOWN_COMPOSE_ARG=--profile dangerous",
                "FS_SECRET_KEY=must-not-be-exported",
            ]
        ),
        encoding="utf-8",
    )
    result = _run_launcher(fake_repo, "users", "list", env={"FAKE_IMAGE_REVISION": "test-revision"})

    assert result.returncode == 0
    log = _log(fake_repo)
    assert "custom-flow" in log
    assert "docker-compose.prod.yaml" in log
    assert ".flow-steward/runtime.env" in log
    assert "ENV_FS_STACK_ENVIRONMENT=local_development" in log
    assert "dangerous" not in log
    assert "must-not-be-exported" not in log


def test_nonexistent_parent_path_fallback_is_portable_awk(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _ = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir()
    (state_dir / "runtime.env").write_text("", encoding="utf-8")
    (state_dir / "compose.env").write_text(
        "FS_COMPOSE_FILES=missing/../infra/docker/docker-compose.yaml\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    assert "runtime paths are invalid" not in result.stderr
    assert "ACTION=exec" in _log(fake_repo)


def test_build_output_is_retained_and_state_directories_are_owner_only(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _ = fake_repo
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=f"flow-steward-cli:{(repo / 'VERSION').read_text(encoding='utf-8').strip()}",
    )
    result = _run_launcher(fake_repo, "--help", env={"FAKE_IMAGE_REVISION": "old-revision"})

    assert result.returncode == 0
    assert "fake build output" in _bootstrap_log(repo).read_text(encoding="utf-8")
    for path in (
        repo / ".flow-steward",
        repo / ".flow-steward/logs",
        repo / ".flow-steward/secret-input",
    ):
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_quiet_build_emits_heartbeat_after_five_seconds(fake_repo: tuple[Path, Path]) -> None:
    repo, log_path = fake_repo
    version = (repo / "VERSION").read_text(encoding="utf-8").strip()
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=f"flow-steward-cli:{version}",
    )
    # The fake build stays quiet until the heartbeat has been seen, so the result
    # does not depend on how fast a loaded host schedules the launcher loop.
    release_file = log_path.parent / "build-release"
    started = time.monotonic()
    process = subprocess.Popen(
        [str(repo / "flow-steward"), "--help"],
        cwd=repo,
        env=_launcher_env(
            fake_repo,
            {
                "FAKE_IMAGE_REVISION": "old-revision",
                "FAKE_BUILD_RELEASE_FILE": str(release_file),
                "FAKE_BUILD_OUTPUT": "late build output",
            },
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        stderr = b""
        deadline = time.monotonic() + 60
        while (
            b"Still building Flow Steward CLI image" not in stderr and time.monotonic() < deadline
        ):
            ready, _, _ = select.select([process.stderr.fileno()], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(process.stderr.fileno(), 4096)
            if not chunk:
                break
            stderr += chunk
        heartbeat_after = time.monotonic() - started
        release_file.touch()
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert b"Still building Flow Steward CLI image" in stderr
    # The launcher compares whole seconds, so five quiet seconds can read as just over four.
    assert heartbeat_after >= 4
    assert process.returncode == 0


def test_verbose_streams_build_output_and_still_retains_log(fake_repo: tuple[Path, Path]) -> None:
    repo, _ = fake_repo
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=f"flow-steward-cli:{(repo / 'VERSION').read_text(encoding='utf-8').strip()}",
    )
    result = _run_launcher(
        fake_repo,
        "--verbose",
        "--help",
        env={"FAKE_IMAGE_REVISION": "old-revision", "FAKE_BUILD_OUTPUT": "visible build output"},
    )

    assert result.returncode == 0
    assert "visible build output" in result.stderr
    assert "visible build output" in _bootstrap_log(repo).read_text(encoding="utf-8")
    assert "ARG=--verbose" in _log(fake_repo)


def test_build_failure_reports_concise_cause_tail_and_log_path(
    fake_repo: tuple[Path, Path],
) -> None:
    version = (fake_repo[0] / "VERSION").read_text(encoding="utf-8").strip()
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=f"flow-steward-cli:{version}",
    )
    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "old-revision", "FAKE_DOCKER_MODE": "build_fail"},
    )

    assert result.returncode == 17
    assert "Failed to build Flow Steward CLI image" in result.stderr
    assert "safe final build line" in result.stderr
    assert ".flow-steward/logs/cli-bootstrap-" in result.stderr


def test_valid_runtime_reuses_container_without_build_start_or_run(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "--version",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0
    log = _log(fake_repo)
    assert "CMD=build" not in log
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "exec") == 1
    assert "ARG=run" not in log
    assert (
        _arg_sequence(
            "fs-cli",
            "python",
            "-m",
            "core.entrypoints.cli.lifecycle_click_app",
            "--version",
        )
        in log
    )


@pytest.mark.parametrize(
    ("args", "service", "delegated_environment"),
    [
        (
            ("quality", "lint", "--mode", "audit", "--plain"),
            "fs-audit",
            "FS_QUALITY_RUNTIME_DELEGATED=1",
        ),
        (
            ("quality", "release-gate", "--plain"),
            "fs-audit",
            "FS_QUALITY_RUNTIME_DELEGATED=1",
        ),
        (
            ("extensions", "test", "acme.crm-sync", "--plain"),
            "fs-test-runner",
            "FS_EXTENSION_TEST_RUNTIME=1",
        ),
        *[
            (
                ("testing", command, "--project-id", "proj_1", "--plain"),
                "fs-test-runner",
                "FS_EXTENSION_TEST_RUNTIME=1",
            )
            for command in (
                "smoke-test",
                "nightfire-regression",
                "fuzz-api",
                "fuzz-mcp",
                "fuzz-ui",
                "cutover-preflight",
                "cutover-smoke",
            )
        ],
    ],
)
def test_dependency_heavy_commands_run_in_their_owned_service(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
    service: str,
    delegated_environment: str,
) -> None:
    result = _run_launcher(
        fake_repo,
        *args,
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 1
    assert _count_action(fake_repo, "exec") == 0
    assert (
        _arg_sequence(
            "run",
            "--no-deps",
            "--rm",
            "-T",
            "-e",
            delegated_environment,
            service,
            "python",
            "-m",
            "core.entrypoints.cli.click_app",
            *args,
        )
        in log
    )


def test_a_declared_test_env_name_reaches_the_delegated_runtime(
    fake_repo: tuple[Path, Path],
) -> None:
    """A delegated runtime is a fresh container: exported host variables do not reach it.

    An extension's tests read an input Core has no name for — a disposable DSN, a
    sandbox URL. Without it they skip, and a skipped suite reads as a green one.
    Only the NAME travels: `-e NAME` with no `=` makes compose take the value from
    the launcher's own environment, so no value lands on a command line or in a
    config file.
    """
    result = _run_launcher(
        fake_repo,
        "extensions",
        "test",
        "acme.crm-sync",
        "--test-env",
        "ACME_SANDBOX_DSN",
        "--plain",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "ACME_SANDBOX_DSN": "dbname=acme user=acme host=pg-fs port=5432",
            "ACME_UNDECLARED": "must-not-cross",
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _arg_sequence("-e", "FS_EXTENSION_TEST_RUNTIME=1", "-e", "ACME_SANDBOX_DSN") in log
    assert "ARG=ACME_UNDECLARED" not in log
    # The value never appears anywhere, so nothing can log or persist it.
    assert "dbname=acme" not in log
    assert "ARG=user=acme" not in log


def test_the_inline_option_form_is_understood(fake_repo: tuple[Path, Path]) -> None:
    result = _run_launcher(
        fake_repo,
        "extensions",
        "test",
        "acme.crm-sync",
        "--test-env=ACME_SANDBOX_DSN",
        "--plain",
        env={"FAKE_IMAGE_REVISION": "test-revision", "ACME_SANDBOX_DSN": "kept"},
    )

    assert result.returncode == 0, result.stderr
    assert _arg_sequence("-e", "ACME_SANDBOX_DSN") in _log(fake_repo)


def test_a_malformed_name_is_skipped_rather_than_killing_the_launcher(
    fake_repo: tuple[Path, Path],
) -> None:
    """`1BAD` is not a shell variable name.

    The runtime's contract is to skip a malformed name with a reason, so the
    launcher applies the same grammar. Reading such a name back through `eval`
    was a `bad substitution` that killed the launcher instead.
    """
    result = _run_launcher(
        fake_repo,
        "extensions",
        "test",
        "acme.crm-sync",
        "--test-env",
        "1BAD",
        "--test-env",
        "ok_lower",
        "--test-env",
        "A-B",
        "--test-env",
        "GOOD_ONE",
        "--plain",
        env={"FAKE_IMAGE_REVISION": "test-revision", "GOOD_ONE": "kept"},
    )

    assert result.returncode == 0, result.stderr
    assert "bad substitution" not in result.stderr.lower()
    for malformed in ("1BAD", "ok_lower", "A-B"):
        assert malformed in result.stderr, f"{malformed} should be reported as ignored"
    log = _log(fake_repo)
    assert _arg_sequence("-e", "GOOD_ONE") in log
    # The malformed names still reach the command itself, where the runtime warns
    # about them too; what must not happen is compose being asked to export one.
    for malformed in ("1BAD", "ok_lower", "A-B"):
        assert _arg_sequence("-e", malformed) not in log


def test_no_declared_test_env_changes_nothing(fake_repo: tuple[Path, Path]) -> None:
    result = _run_launcher(
        fake_repo,
        "extensions",
        "test",
        "acme.crm-sync",
        "--plain",
        env={"FAKE_IMAGE_REVISION": "test-revision", "ACME_SANDBOX_DSN": "must-not-cross"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert "ACME_SANDBOX_DSN" not in log
    assert _arg_sequence("-e", "FS_EXTENSION_TEST_RUNTIME=1", "fs-test-runner") in log


@pytest.mark.parametrize(
    "args",
    [
        ("users", "list", "--plain"),
        ("extensions", "list", "--plain"),
        ("projects", "list", "--plain"),
        ("workflows", "list", "--project-id", "proj_1", "--plain"),
        ("jobs", "list", "--plain"),
        ("reports", "get", "report_1", "--plain"),
        ("localization", "status", "--plain"),
    ],
)
def test_product_commands_execute_in_the_existing_app_runtime(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    """Routing a product command through fs-cli must break this contract."""
    result = _run_launcher(
        fake_repo,
        *args,
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 1
    assert "ARG=FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli" in log
    assert f"ARG=FS_DOCKER_HOST_PROJECT_ROOT={fake_repo[0].resolve()}" not in log
    assert (
        _arg_sequence(
            "fs-app",
            "python",
            "-m",
            "core.entrypoints.cli.click_app",
            *args,
        )
        in log
    )
    assert "ARG=fs-cli" not in log


def test_diagnostics_report_is_saved_to_one_file_with_the_host_facts(
    fake_repo: tuple[Path, Path],
) -> None:
    report = {"schema_version": "flow-steward.system-diagnostic-report.v1", "source": "cli"}
    envelope = {"schema": "flow-steward.cli.v1", "ok": True, "result": {"report": report}}
    result = _run_launcher(
        fake_repo,
        "reports",
        "diagnostics",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CAPTURE_STDIN": "1",
            "FAKE_COMPOSE_STDOUT": json.dumps(envelope),
        },
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    saved = sorted(
        (fake_repo[0] / ".flow-steward/diagnostics").glob("flow-steward-diagnostics-*.json")
    )
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8")) == report
    assert saved[0].stat().st_mode & 0o777 == 0o600
    assert str(saved[0]) in result.stdout
    log = _log(fake_repo)
    assert (
        _arg_sequence(
            "fs-app",
            "python",
            "-m",
            "core.entrypoints.cli.click_app",
            "reports",
            "diagnostics",
            "--json",
            "--input-json",
            "-",
        )
        in log
    )
    stdin_lines = [line for line in log.splitlines() if line.startswith("STDIN={")]
    assert stdin_lines
    assert "host" in json.loads(stdin_lines[-1].removeprefix("STDIN="))


def test_diagnostics_report_failure_leaves_no_file(fake_repo: tuple[Path, Path]) -> None:
    envelope = {
        "schema": "flow-steward.cli.v1",
        "ok": False,
        "error": {"code": "service_unavailable", "message": "database is down"},
    }
    result = _run_launcher(
        fake_repo,
        "reports",
        "diagnostics",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CAPTURE_STDIN": "1",
            "FAKE_COMPOSE_STDOUT": json.dumps(envelope),
        },
        timeout=60,
    )

    assert result.returncode == 4
    assert "database is down" in result.stderr
    assert not list((fake_repo[0] / ".flow-steward/diagnostics").glob("*.json"))


def test_diagnostics_report_with_json_answers_like_any_product_command(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "reports",
        "diagnostics",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "exec") == 1
    assert not (fake_repo[0] / ".flow-steward/diagnostics").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("database", "status", "--plain"),
        ("database", "upgrade", "--maintenance-confirmed", "--confirm"),
    ],
)
def test_database_commands_run_in_the_configured_release_not_the_running_app(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    """AC17-R160: an update with a migration stopped at its schema check, with fs-app still
    on the previous image. `database upgrade` exec'd there saw none of the release's new
    migrations -- "applied": [] -- and the installer would not move on until it had."""
    result = _run_launcher(fake_repo, *args, env={"FAKE_IMAGE_REVISION": "test-revision"})

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "run") == 1
    assert (
        _arg_sequence(
            "--entrypoint", "python", "fs-app", "-m", "core.entrypoints.cli.click_app", *args
        )
        in log
    )
    assert "ARG=--rm" in log
    assert "ARG=--no-deps" in log


@pytest.mark.parametrize("command", ["restore-parity-capture", "restore-parity-verify"])
def test_restore_parity_commands_use_lifecycle_runtime(
    fake_repo: tuple[Path, Path],
    command: str,
) -> None:
    result = _run_launcher(
        fake_repo,
        "database",
        command,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert (
        _arg_sequence(
            "fs-cli",
            "python",
            "-m",
            "core.entrypoints.cli.lifecycle_click_app",
            "database",
            command,
            "--help",
        )
        in log
    )
    assert "ARG=fs-app" not in log


def test_scalable_release_upgrade_uses_independent_cli_runner(
    fake_repo: tuple[Path, Path],
) -> None:
    args = ("release", "upgrade", "--target-version", "0.7.1", "--confirm", "--json")
    result = _run_launcher(fake_repo, *args, env={"FAKE_IMAGE_REVISION": "test-revision"})

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "run") == 1
    log = _log(fake_repo)
    assert "ARG=--rm" in log
    assert "ARG=--no-deps" in log
    assert _arg_sequence("fs-cli", "-m", "core.entrypoints.cli.lifecycle_click_app", *args) in log


def test_isolated_upgrade_preserves_explicit_auth_without_host_buildx_context(
    fake_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    auth_dir = tmp_path / "operator auth"
    auth_dir.mkdir()
    auth_file = auth_dir / "config.json"
    auth_file.write_text(
        json.dumps({"auths": {}, "currentContext": "host-only-context"}), encoding="utf-8"
    )
    original = auth_file.read_bytes()
    host_buildx = "/host-only-buildx"
    buildx_dir = "/tmp/flow-steward-buildx"  # pragma: allowlist secret - metadata path
    result = _run_launcher(
        fake_repo,
        "release",
        "upgrade",
        "--target-version",
        "0.7.1",
        "--confirm",
        "--json",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "DOCKER_CONFIG": str(auth_dir),
            "BUILDX_CONFIG": host_buildx,
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _arg_sequence("--volume", f"{auth_file}:{auth_file}:ro") in log
    assert _arg_sequence("-e", f"DOCKER_CONFIG={auth_dir}") in log
    assert _arg_sequence("-e", f"BUILDX_CONFIG={buildx_dir}") in log
    assert _arg_sequence("-e", "DOCKER_CONTEXT=default") in log
    assert _arg_sequence("-e", f"BUILDX_CONFIG={host_buildx}") not in log
    assert auth_file.read_bytes() == original
    assert not (auth_dir / "buildx").exists()


@pytest.mark.parametrize("invalid_context", ["missing", "unreadable", "colon"])
def test_isolated_upgrade_rejects_invalid_auth_before_runner_creation(
    fake_repo: tuple[Path, Path], tmp_path: Path, invalid_context: str
) -> None:
    auth_dir = tmp_path / ("operator:auth" if invalid_context == "colon" else "operator-auth")
    auth_dir.mkdir()
    auth_file = auth_dir / "config.json"
    if invalid_context != "missing":
        auth_file.write_text("{}", encoding="utf-8")
    if invalid_context == "unreadable":
        auth_file.chmod(0)

    result = _run_launcher(
        fake_repo,
        "release",
        "upgrade",
        "--target-version",
        "0.7.1",
        "--confirm",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision", "DOCKER_CONFIG": str(auth_dir)},
    )

    assert result.returncode == 2, result.stderr
    assert "DOCKER_CONFIG" in result.stderr
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 0


@pytest.mark.parametrize(
    "args",
    [
        (
            "extensions",
            "install",
            "--archive",
            "dist/extensions/acme.zip",
            "--plain",
        ),
        ("extensions", "uninstall", "acme.example", "--confirm", "--plain"),
    ],
)
def test_extension_archive_mutations_run_in_the_one_shot_manager(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    result = _run_launcher(
        fake_repo,
        *args,
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 1
    assert _count_action(fake_repo, "exec") == 0
    assert (
        _arg_sequence(
            "run",
            "--no-deps",
            "--rm",
            "-T",
            "fs-extension-manager",
            "python",
            "-P",
            "-m",
            "core.entrypoints.cli.click_app",
            *args,
        )
        in log
    )
    assert "ARG=fs-app" not in log


def test_unclassified_testing_command_does_not_inherit_test_runner_route(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "testing",
        "future-command",
        "--plain",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 1
    assert "fs-test-runner" not in _log(fake_repo)


def test_test_runner_command_refuses_production_without_compose_mutation(
    fake_repo: tuple[Path, Path],
) -> None:
    _write_selected_cli_image(
        fake_repo,
        environment="production",
        value="fs-inst-production-cli:acceptance",
    )

    result = _run_launcher(
        fake_repo,
        "extensions",
        "test",
        "acme.crm-sync",
        "--plain",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 2
    assert "flow-steward environment switch development" in result.stderr
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 0


def test_audit_target_command_preserves_stdin_and_exit_code(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "testing",
        "smoke-test",
        "--project-id",
        "proj_1",
        "--plain",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CAPTURE_STDIN": "1",
            "FAKE_COMPOSE_EXIT": "23",
        },
        input_text="y\n",
    )

    assert result.returncode == 23
    assert "STDIN=y" in _log(fake_repo)


def test_launcher_adds_clone_bindings_from_selected_paths_not_ambient_environment(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    repo, _log_path = fake_repo
    _write_selected_cli_image(fake_repo, environment="development", value="flow-steward-cli:test")
    extensions = tmp_path / "selected-extensions"
    transfers = tmp_path / "selected-transfers"
    logs = tmp_path / "selected-logs"
    extensions.mkdir()
    transfers.mkdir()
    (logs / "artifacts").mkdir(parents=True)
    with (repo / ".env").open("a", encoding="utf-8") as stream:
        stream.write(
            "FS_EXTENSIONS_DIR=../../../selected-extensions\n"
            f"FS_BACKUPS_DIR={transfers}\nFS_LOGS_DIR={logs}\n"
        )
    with (repo / ".flow-steward/compose.env").open("a", encoding="utf-8") as stream:
        stream.write("FS_FILE_STORAGE=local_fs\n")
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FS_EXTENSIONS_DIR": "/ambient/wrong-extensions",
            "FS_BACKUPS_DIR": "/ambient/wrong-transfers",
            "FS_LOGS_DIR": "/ambient/wrong-logs",
        },
    )
    technical = _runtime_log(repo).read_text(encoding="utf-8") if result.returncode != 0 else ""
    assert result.returncode == 0, result.stderr + technical
    calls = [
        block
        for block in _log(fake_repo).split("CMD=")
        if "ACTION=exec\n" in block or "ACTION=up\n" in block
    ]
    assert calls
    log = "\n".join(calls)
    assert _arg_sequence("-f", str(repo / "infra/docker/scalable/cli-host-directories.yaml")) in log
    assert (
        _arg_sequence("-f", str(repo / "infra/docker/scalable/cli-host-directories-artifacts.yaml"))
        in log
    )
    assert f"ENV_FS_EXTENSIONS_DIR={extensions}\n" in log
    assert f"ENV_FS_BACKUPS_DIR={transfers}\n" in log
    assert f"ENV_FS_LOGS_DIR={logs}\n" in log
    assert "ENV_FS_EXTENSIONS_DIR=/ambient/" not in log
    assert "ENV_FS_BACKUPS_DIR=/ambient/" not in log


def test_launcher_prepares_private_default_transfer_root_during_runtime_repair(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    assert not (repo / "backups").exists()
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
            "FAKE_REQUIRE_TRANSFER_ROOT": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert (repo / "backups").is_dir()
    assert (repo / "backups").stat().st_mode & 0o777 == 0o700
    assert _count_action(fake_repo, "up") == 1


def test_launcher_refuses_missing_custom_transfer_root_without_creating_it(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    repo, _log_path = fake_repo
    _write_selected_cli_image(fake_repo, environment="development", value="flow-steward-cli:test")
    transfers = tmp_path / "missing-custom-transfer"
    with (repo / ".env").open("a", encoding="utf-8") as stream:
        stream.write(f"FS_BACKUPS_DIR={transfers}\n")
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
            "FAKE_REQUIRE_TRANSFER_ROOT": "1",
        },
    )
    assert result.returncode == 8
    assert "FS_BACKUPS_DIR" in result.stderr
    assert not transfers.exists()
    assert not (repo / "backups").exists()
    assert _count_action(fake_repo, "up") == 0


@pytest.mark.parametrize("custom", [False, True])
def test_launcher_rejects_symlinked_transfer_root_before_runtime_repair(
    fake_repo: tuple[Path, Path], tmp_path: Path, custom: bool
) -> None:
    repo, _log_path = fake_repo
    actual = tmp_path / "actual-transfer"
    actual.mkdir()
    selected = tmp_path / "custom-transfer" if custom else repo / "backups"
    selected.symlink_to(actual, target_is_directory=True)
    if custom:
        _write_selected_cli_image(
            fake_repo, environment="development", value="flow-steward-cli:test"
        )
        with (repo / ".env").open("a", encoding="utf-8") as stream:
            stream.write(f"FS_BACKUPS_DIR={selected}\n")
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_CONTAINER_PRESENT": "0"},
    )
    assert result.returncode == 8
    assert "FS_BACKUPS_DIR" in result.stderr
    assert _count_action(fake_repo, "up") == 0
    assert selected.is_symlink()
    assert list(actual.iterdir()) == []


def test_valid_runtime_normalizes_docker_desktop_mount_sources(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "--version",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_DOCKER_SOURCE_PREFIX": "/host_mnt",
        },
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "exec") == 1


def test_valid_runtime_normalizes_equivalent_host_mount_source_alias(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    repo, _log_path = fake_repo
    repo_alias = tmp_path / "repo-alias"
    repo_alias.symlink_to(repo, target_is_directory=True)

    result = _run_launcher(
        fake_repo,
        "--version",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_APP_SOURCE": str(repo_alias),
        },
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "exec") == 1


def test_missing_runtime_is_repaired_once_before_command(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
        },
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "exec") == 1
    assert _arg_sequence(
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--force-recreate",
        "fs-cli",
    ) in _log(fake_repo)
    assert not (repo / ".flow-steward/runtime/cli-runtime.lock").exists()
    assert stat.S_IMODE((repo / ".flow-steward/runtime").stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "stale_env",
    [
        {"FAKE_CONTAINER_IMAGE_ID": "sha256:wrong-cli-image"},
        {"FAKE_CONTAINER_RUNTIME_REVISION": "0" * 64},
        {"FAKE_CONTAINER_PROJECT": "wrong-project"},
        {"FAKE_CONTAINER_SERVICE": "wrong-service"},
        {"FAKE_CONTAINER_RUNTIME_LABEL": "false"},
        {"FAKE_CONTAINER_CONTRACT_LABEL": "2"},
        {"FAKE_CONTAINER_USER": "999:999"},
    ],
    ids=[
        "wrong-exact-image-id",
        "wrong-runtime-revision",
        "wrong-project-label",
        "wrong-service-label",
        "wrong-runtime-label",
        "wrong-contract-label",
        "wrong-configured-user",
    ],
)
def test_stopped_or_stale_runtime_is_recreated_before_command(
    fake_repo: tuple[Path, Path],
    stale_env: dict[str, str],
) -> None:
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={"FAKE_IMAGE_REVISION": "test-revision", **stale_env},
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "exec") == 1


def test_a_runtime_running_a_lifecycle_operation_is_not_replaced(
    fake_repo: tuple[Path, Path],
) -> None:
    """AC17-R156: `config apply` rewrites the env file its own fs-cli was started from,
    so the next launcher saw a stale identity and recreated fs-cli -- killing the apply
    inside it (exit 137) between Apply and Verify. The runbook tells operators to watch
    an apply with `status`, so this was the documented way to break one."""
    result = _run_launcher(
        fake_repo,
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_GROUP_ADD": "999999,",  # the identity no longer matches
            "FAKE_LIFECYCLE_OPERATION": "c15d47fe-config-apply",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "lifecycle-probe") >= 1
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "exec") == 1  # the command ran in the current runtime
    assert "c15d47fe-config-apply" in result.stderr


@pytest.mark.parametrize("stale", ["identity", "stopped"])
def test_a_runtime_is_not_repaired_while_install_sh_runs(
    fake_repo: tuple[Path, Path], stale: str
) -> None:
    """AC17-R162 (L1): `stack stop` six seconds into ./install.sh repaired fs-cli at the
    moment the installer was replacing it; the installer failed on "removal of
    container", its lock went with it, and the stop ran and took the site down."""
    root, _ = fake_repo
    state = root / ".flow-steward"
    state.mkdir(exist_ok=True)
    (state / "installer-runtime.cid").write_text("a1b2c3d4e5f6a7b8c9d0\n", encoding="utf-8")
    stale_env = (
        {"FAKE_CONTAINER_GROUP_ADD": "999999,"}
        if stale == "identity"
        else {"FAKE_CONTAINER_RUNNING": "false"}
    )
    result = _run_launcher(
        fake_repo,
        "stack",
        "stop",
        "--confirm",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_INSTALLER_CID": "a1b2c3d4e5f6a7b8c9d0",
            "FAKE_INSTALLER_RUNNING": "true",
            **stale_env,
        },
    )

    assert result.returncode == 6, result.stderr
    assert "./install.sh is running (installer container a1b2c3d4e5f6)" in result.stderr
    assert _count_action(fake_repo, "installer-inspect") == 1
    for action in ("up", "start", "rm", "exec"):
        assert _count_action(fake_repo, action) == 0, action


@pytest.fixture
def running_install_sh(fake_repo: tuple[Path, Path], tmp_path: Path):
    """A live host process whose command line is install.sh, recorded as install.sh does."""
    import subprocess

    script = tmp_path / "install.sh"
    script.write_text("sleep 60\n", encoding="utf-8")
    process = subprocess.Popen(["bash", str(script)])
    root, _ = fake_repo
    state = root / ".flow-steward"
    state.mkdir(exist_ok=True)
    (state / "installer-host.pid").write_text(f"{process.pid}\n", encoding="utf-8")
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize(
    "command",
    [
        ("stack", "stop", "--confirm"),
        ("maintenance", "enable", "--confirm"),
        ("backup", "create"),
    ],
    ids=["stack-stop", "maintenance-enable", "backup-create"],
)
def test_a_mutating_command_is_refused_for_the_whole_install_sh_run(
    fake_repo: tuple[Path, Path], running_install_sh: int, command: tuple[str, ...]
) -> None:
    """AC17-R162 (L1), found live: 25 s into an update the installer container did not
    exist yet -- install.sh was still preparing the management runtime -- and `stack
    stop` stopped the stack under it. Before the container, and before anything that
    command would touch: nothing is asked of Docker at all."""
    result = _run_launcher(fake_repo, *command, env={"FAKE_IMAGE_REVISION": "test-revision"})

    assert result.returncode == 6, result.stderr
    assert f"./install.sh is running (process {running_install_sh})" in result.stderr
    assert "ACTION=" not in _log(fake_repo)


def test_a_compact_command_is_refused_while_install_sh_runs(
    fake_repo: tuple[Path, Path], running_install_sh: int
) -> None:
    """Compact maintenance takes no lifecycle lock, so the host installation guard
    must refuse it before dispatching to the runtime operator."""
    _write_compact_installation(fake_repo, image="flow-steward-compact:test")

    result = _run_launcher(fake_repo, "maintenance", "enable", "--confirm")

    assert result.returncode == 6, result.stderr
    assert "./install.sh is running" in result.stderr
    assert "ACTION=run" not in _log(fake_repo)
    assert "ACTION=exec" not in _log(fake_repo)


def test_reading_is_still_allowed_while_install_sh_runs(
    fake_repo: tuple[Path, Path], running_install_sh: int
) -> None:
    """The green direction: status, and the read-only subcommands, still answer."""
    result = _run_launcher(
        fake_repo, "stack", "status", env={"FAKE_IMAGE_REVISION": "test-revision"}
    )

    assert "./install.sh is running" not in result.stderr
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("who", ["dead", "not-install"])
def test_a_record_of_no_running_install_sh_does_not_block(
    fake_repo: tuple[Path, Path], tmp_path: Path, who: str
) -> None:
    import subprocess

    root, _ = fake_repo
    state = root / ".flow-steward"
    state.mkdir(exist_ok=True)
    process = subprocess.Popen(["sleep", "60"])
    try:
        if who == "dead":
            process.kill()
            process.wait()
        (state / "installer-host.pid").write_text(f"{process.pid}\n", encoding="utf-8")
        result = _run_launcher(
            fake_repo, "stack", "stop", "--confirm", env={"FAKE_IMAGE_REVISION": "test-revision"}
        )
    finally:
        process.kill()
        process.wait()

    assert "./install.sh is running" not in result.stderr


def test_a_finished_installers_record_does_not_block_a_repair(
    fake_repo: tuple[Path, Path],
) -> None:
    """The green direction: a record left by an installer that is no longer running."""
    root, _ = fake_repo
    state = root / ".flow-steward"
    state.mkdir(exist_ok=True)
    (state / "installer-runtime.cid").write_text("a1b2c3d4e5f6a7b8c9d0\n", encoding="utf-8")
    result = _run_launcher(
        fake_repo,
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_GROUP_ADD": "999999,",
            "FAKE_INSTALLER_CID": "a1b2c3d4e5f6a7b8c9d0",
            "FAKE_INSTALLER_RUNNING": "false",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "installer-inspect") == 1  # non-vacuity: it asked
    assert _count_action(fake_repo, "up") == 1


def test_a_stale_runtime_with_nothing_running_is_still_replaced(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "status",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_CONTAINER_GROUP_ADD": "999999,"},
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "lifecycle-probe") >= 1  # non-vacuity: it asked
    assert _count_action(fake_repo, "up") == 1


def test_exact_stopped_runtime_is_started_without_recreation(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_RUNNING": "false",
        },
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "start") == 1
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "exec") == 1


def test_production_runtime_reuses_exact_installer_candidate_mounts(
    fake_repo: tuple[Path, Path],
) -> None:
    _write_selected_cli_image(
        fake_repo,
        environment="production",
        value="flow-steward-cli:test",
    )

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "rm") == 0
    assert _count_action(fake_repo, "exec") == 1


def _write_external_development_state(fake_repo: tuple[Path, Path], state_dir: Path) -> None:
    repo, _log_path = fake_repo
    state_dir.mkdir()
    (repo / ".env").write_text("FS_CLI_IMAGE=flow-steward-cli:test\n", encoding="utf-8")
    (state_dir / "compose.env").write_text(
        "\n".join(
            (
                "FS_STACK_ENVIRONMENT=local_development",
                "FS_COMPOSE_PROJECT_NAME=dev",
                "FS_COMPOSE_FILES=infra/docker/docker-compose.yaml",
                "FS_ENV_FILE=.env",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_development_runtime_reuses_exact_external_installer_state_mounts(
    fake_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    state_dir = tmp_path / "selected-installer-state"
    _write_external_development_state(fake_repo, state_dir)

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FS_INSTALLER_STATE_DIR": str(state_dir),
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_SELECTED_STATE_MOUNTS": "correct",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "exec") == 1


@pytest.mark.parametrize(
    "mounts",
    ["missing", "wrong-alias", "wrong-identity"],
    ids=["missing-selected-state-mounts", "wrong-config-alias", "wrong-host-identity"],
)
def test_development_runtime_repairs_missing_or_wrong_external_installer_state_mounts(
    fake_repo: tuple[Path, Path], tmp_path: Path, mounts: str
) -> None:
    state_dir = tmp_path / "selected-installer-state"
    _write_external_development_state(fake_repo, state_dir)

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FS_INSTALLER_STATE_DIR": str(state_dir),
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_SELECTED_STATE_MOUNTS": mounts,
        },
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "exec") == 1


def test_production_runtime_repairs_a_wrong_candidate_mount(
    fake_repo: tuple[Path, Path],
) -> None:
    _write_selected_cli_image(
        fake_repo,
        environment="production",
        value="flow-steward-cli:test",
    )

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_CANDIDATE_STATE_SOURCE": "/wrong/candidate-state",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "exec") == 1


@pytest.mark.parametrize(
    "stale_env",
    [
        {"FAKE_CONTAINER_APP_MOUNT": "1", "FAKE_CONTAINER_APP_SOURCE": "/wrong/app"},
        {"FAKE_CONTAINER_HOST_RW": "false"},
        {"FAKE_CONTAINER_SOCKET_SOURCE": "/wrong/docker.sock"},
        {"FAKE_CONTAINER_RUNTIME_SOURCE": "/wrong/installer-runtime"},
        {"FAKE_CONTAINER_GROUP_ADD": "999999,"},
        {"FAKE_CONTAINER_PRIVILEGED": "true"},
        {"FAKE_CONTAINER_EXTRA_SOCKET_MOUNT": "1"},
    ],
    ids=[
        "wrong-app-mount",
        "wrong-host-self-bind-mode",
        "wrong-docker-socket",
        "wrong-installer-state",
        "wrong-docker-group",
        "privileged-container",
        "extra-docker-socket-mount",
    ],
)
def test_runtime_with_wrong_mount_or_docker_group_contract_is_recreated(
    fake_repo: tuple[Path, Path],
    stale_env: dict[str, str],
) -> None:
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={"FAKE_IMAGE_REVISION": "test-revision", **stale_env},
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "exec") == 1


def test_runtime_bootstrap_retries_once_then_executes_command_once(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
            "FAKE_BOOTSTRAP_MODE": "retry",
        },
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 2
    assert _count_action(fake_repo, "rm") == 1
    assert _count_action(fake_repo, "exec") == 1
    assert _arg_sequence("rm", "-sf", "fs-cli") in _log(fake_repo)


def test_runtime_bootstrap_failure_reports_protected_log_and_never_runs_command(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
            "FAKE_BOOTSTRAP_MODE": "fail",
            "FAKE_BOOTSTRAP_OUTPUT": (
                "fake fs-cli start failure "
                "FS_SECRET_KEY=unredacted-secret DB_PASSWORD=unredacted-password "
                "Authorization: Bearer unredacted-bearer"
            ),
        },
    )

    assert result.returncode == 8
    assert result.stdout == ""
    assert "Flow Steward CLI runtime bootstrap failed" in result.stderr
    assert "Cause:" in result.stderr
    assert "Remediation:" in result.stderr
    assert ".flow-steward/logs/cli-runtime-" in result.stderr
    assert _count_action(fake_repo, "up") == 2
    assert _count_action(fake_repo, "rm") == 1
    assert _count_action(fake_repo, "exec") == 0
    technical_log = _runtime_log(repo)
    technical_output = technical_log.read_text(encoding="utf-8")
    assert "fake fs-cli start failure" in technical_output
    assert "[REDACTED]" in technical_output
    assert "unredacted-secret" not in technical_output
    assert "unredacted-password" not in technical_output
    assert "unredacted-bearer" not in technical_output
    assert stat.S_IMODE(technical_log.stat().st_mode) == 0o600


def test_missing_exact_image_id_fails_closed_before_container_or_command(
    fake_repo: tuple[Path, Path],
) -> None:
    version = (fake_repo[0] / "VERSION").read_text(encoding="utf-8").strip()
    _write_selected_cli_image(
        fake_repo,
        environment="development",
        value=f"flow-steward-cli:{version}",
    )
    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID_FAIL": "1",
        },
    )

    assert result.returncode == 8
    assert result.stdout == ""
    assert "exact CLI image ID" in result.stderr
    assert ".flow-steward/logs/cli-runtime-" in result.stderr
    assert _count_action(fake_repo, "ps") == 0
    assert _count_action(fake_repo, "exec") == 0


def test_runtime_identity_reads_only_the_top_level_active_revision(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir()
    top_level_revision = "a" * 64
    nested_revision = "b" * 64
    (state_dir / "active-revision.json").write_text(
        "{\n"
        '  "candidate": {\n'
        f'    "revision": "{nested_revision}"\n'
        "  },\n"
        f'  "revision": "{top_level_revision}"\n'
        "}\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    exported_revisions = {
        line.removeprefix("ENV_FS_CLI_RUNTIME_REVISION=")
        for line in _log(fake_repo).splitlines()
        if line.startswith("ENV_FS_CLI_RUNTIME_REVISION=")
        and line != "ENV_FS_CLI_RUNTIME_REVISION="
    }
    assert len(exported_revisions) == 1
    assert len(exported_revisions.pop()) == 64
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "exec") == 1


def test_runtime_identity_changes_when_env_file_contents_change(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, log_path = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir()
    runtime_env = state_dir / "runtime.env"
    runtime_env.write_text("MCP_BEARER_TOKEN=first-secret\n", encoding="utf-8")
    (state_dir / "compose.env").write_text(
        "FS_ENV_FILE=.flow-steward/runtime.env\n",
        encoding="utf-8",
    )

    first = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )
    assert first.returncode == 0, first.stderr
    first_revision = next(
        line.removeprefix("ENV_FS_CLI_RUNTIME_REVISION=")
        for line in _log(fake_repo).splitlines()
        if line.startswith("ENV_FS_CLI_RUNTIME_REVISION=")
        and line != "ENV_FS_CLI_RUNTIME_REVISION="
    )

    log_path.write_text("", encoding="utf-8")
    runtime_env.write_text("MCP_BEARER_TOKEN=rotated-secret\n", encoding="utf-8")
    second = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_RUNTIME_REVISION": first_revision,
        },
    )

    assert second.returncode == 0, second.stderr
    assert _count_action(fake_repo, "up") == 1
    assert "first-secret" not in _log(fake_repo)
    assert "rotated-secret" not in _log(fake_repo)


def test_launcher_and_installer_pass_the_same_descriptor_to_runtime_identity_helper(
    fake_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A caller-specific descriptor drift must change the reusable-runtime digest."""
    repo, _log_path = fake_repo
    (repo / ".env").write_text("FS_TEST_VALUE=1\n", encoding="utf-8")
    helper = repo / "core/infrastructure/cli_runtime/runtime_identity.sh"
    forwarded_helper = helper.with_name("runtime_identity.forwarded.sh")
    helper.rename(forwarded_helper)
    launcher_capture = tmp_path / "launcher-runtime-identity.args"
    installer_capture = tmp_path / "installer-runtime-identity.args"
    _write_executable(
        helper,
        """\
        #!/bin/sh
        set -eu
        : "${FS_RUNTIME_IDENTITY_ARGS_CAPTURE:?}"
        printf '%s\\n' "$@" > "$FS_RUNTIME_IDENTITY_ARGS_CAPTURE"
        exec "$(dirname "$0")/runtime_identity.forwarded.sh" "$@"
        """,
    )

    launcher = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FS_RUNTIME_IDENTITY_ARGS_CAPTURE": str(launcher_capture),
            "FS_STACK_ENVIRONMENT": "local_development",
        },
    )

    assert launcher.returncode == 0, launcher.stderr
    launcher_arguments = tuple(launcher_capture.read_text(encoding="utf-8").splitlines())
    launcher_descriptor = dict(zip(launcher_arguments[::2], launcher_arguments[1::2], strict=True))
    launcher_revision = next(
        line.removeprefix("ENV_FS_CLI_RUNTIME_REVISION=")
        for line in _log(fake_repo).splitlines()
        if line.startswith("ENV_FS_CLI_RUNTIME_REVISION=")
        and line != "ENV_FS_CLI_RUNTIME_REVISION="
    )

    class IdentityHelperExecutor:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        def run(self, command, **kwargs):
            normalized_command = tuple(str(part) for part in command)
            self.commands.append(normalized_command)
            if normalized_command[0] == "git":
                return ProcessOutcome(
                    command=normalized_command,
                    returncode=0,
                    stdout="test-revision\\n",
                )
            assert normalized_command[0] == str(helper)
            environment = {**os.environ, **kwargs["environment"]}
            environment["FS_RUNTIME_IDENTITY_ARGS_CAPTURE"] = str(installer_capture)
            completed = subprocess.run(
                normalized_command,
                cwd=kwargs["cwd"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            return ProcessOutcome(
                command=normalized_command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

    monkeypatch.setattr(
        installer_gateway_module.os, "getuid", lambda: int(launcher_descriptor["--host-uid"])
    )
    monkeypatch.setattr(
        installer_gateway_module.os, "getgid", lambda: int(launcher_descriptor["--host-gid"])
    )
    monkeypatch.setattr(
        gateway_cli_runtime,
        "_docker_socket_group_id",
        lambda: int(launcher_descriptor["--docker-gid"]),
    )
    process = IdentityHelperExecutor()
    gateway = DockerInstallerGateway(
        repo_root=repo,
        state_store=InstallerStateStore(repo / ".flow-steward"),
        process=process,
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        environment={},
        sleep=lambda _seconds: None,
    )

    installer_context = gateway._cli_runtime_context(InstallConfig(), log=None)
    installer_arguments = tuple(installer_capture.read_text(encoding="utf-8").splitlines())

    assert process.commands == [
        ("git", "-C", str(repo.resolve()), "rev-parse", "HEAD"),
        (str(helper), *installer_arguments),
    ]
    assert installer_arguments == (
        *launcher_arguments,
        "--env-file-digest-from-environment",
    )
    assert installer_context.runtime_revision == launcher_revision


def test_invalid_active_revision_fails_with_installation_remediation(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo
    state_dir = repo / ".flow-steward"
    state_dir.mkdir()
    (state_dir / "active-revision.json").write_text(
        '{"revision":"not-a-valid-revision"}\n',
        encoding="utf-8",
    )

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "runtime identity" in result.stderr
    assert "rerun ./install.sh" in result.stderr
    assert _count_action(fake_repo, "ps") == 0
    assert _count_action(fake_repo, "exec") == 0


def test_non_tty_exec_preserves_child_exit_and_spaced_args_without_retry(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "--json",
        "users",
        "create",
        "--email",
        "two words@example.test",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_COMPOSE_EXIT": "23",
            "NO_COLOR": None,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        },
    )

    assert result.returncode == 23
    log = _log(fake_repo)
    assert "ARG=--ansi\nARG=never" in log
    assert "ARG=--progress\nARG=quiet" in log
    assert (
        _arg_sequence(
            "exec",
            "-T",
            "-e",
            "TERM=xterm-256color",
            "-e",
            "COLORTERM=truecolor",
            "-e",
            "FS_CLI_OPERATION_LOG_ROOT=/app/logs/cli",
            "-e",
            "FS_CLI_HOST_NO_COLOR=",
            "fs-app",
            "python",
            "-m",
            "core.entrypoints.cli.click_app",
            "--json",
            "users",
            "create",
            "--email",
            "two words@example.test",
        )
        in log
    )
    assert "ARG=FS_CLI_HOST_NO_COLOR=" in log
    assert "ARG=two words@example.test" in log
    assert _count_action(fake_repo, "exec") == 1
    assert _count_action(fake_repo, "up") == 0
    assert "ARG=run" not in log


def test_launcher_forwards_absolute_host_project_root_to_cli_container(
    fake_repo: tuple[Path, Path],
) -> None:
    repo, _log_path = fake_repo

    result = _run_launcher(
        fake_repo,
        "stack",
        "status",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0
    log = _log(fake_repo)
    assert "ARG=-e" in log
    assert f"ARG=FS_DOCKER_HOST_PROJECT_ROOT={repo.resolve()}" in log


def test_launcher_forwards_redirected_stdin_to_fresh_command(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "testing",
        "import",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CAPTURE_STDIN": "1",
        },
        input_text="input with spaces and unicode λ\n",
    )

    assert result.returncode == 0
    assert "STDIN=input with spaces and unicode λ" in _log(fake_repo)
    assert _count_action(fake_repo, "exec") == 1


def test_launcher_preserves_terminal_color_environment_for_compose(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision", "NO_COLOR": "1", "TERM": "dumb"},
    )

    assert result.returncode == 0
    log = _log(fake_repo)
    assert "ENV_NO_COLOR_STATE=set" in log
    assert "ENV_NO_COLOR_VALUE=1" in log
    assert "ARG=-e" in log
    assert "ARG=NO_COLOR=1" in log
    assert "ENV_TERM=dumb" in log


def test_launcher_overrides_warm_container_terminal_capabilities_per_command(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "--help",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "NO_COLOR": None,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        },
    )

    assert result.returncode == 0
    log = _log(fake_repo)
    assert "ARG=TERM=xterm-256color" in log
    assert "ARG=COLORTERM=truecolor" in log


@pytest.mark.parametrize(
    ("host_env", "expected_state"),
    [
        ({"NO_COLOR": None}, "ENV_NO_COLOR_STATE=unset"),
        ({"NO_COLOR": ""}, "ENV_NO_COLOR_STATE=set\nENV_NO_COLOR_VALUE="),
    ],
)
def test_launcher_does_not_forward_absent_or_empty_no_color_to_container(
    fake_repo: tuple[Path, Path],
    host_env: dict[str, str | None],
    expected_state: str,
) -> None:
    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision", **host_env},
    )

    assert result.returncode == 0
    log = _log(fake_repo)
    assert expected_state in log
    assert "ARG=FS_CLI_HOST_NO_COLOR=" in log
    assert "ARG=NO_COLOR=" not in log


@pytest.mark.skipif(sys.platform == "win32", reason="PTY contract is POSIX-only")
def test_interactive_tty_omits_no_tty_flag(fake_repo: tuple[Path, Path]) -> None:
    import pty

    repo, log_path = fake_repo
    master_in, slave_in = pty.openpty()
    master_out, slave_out = pty.openpty()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{log_path.parent / 'bin'}:/usr/bin:/bin",
            "FAKE_DOCKER_LOG": str(log_path),
            "FAKE_DOCKER_STATE": str(log_path.parent / "docker.state"),
            "FAKE_GIT_REVISION": "test-revision",
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": "sha256:expected-cli-image",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        }
    )
    env.pop("NO_COLOR", None)
    try:
        result = subprocess.run(
            [str(repo / "flow-steward"), "--help"],
            cwd=repo,
            env=env,
            stdin=slave_in,
            stdout=slave_out,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    finally:
        os.close(master_in)
        os.close(slave_in)
        os.close(master_out)
        os.close(slave_out)

    assert result.returncode == 0
    assert _arg_sequence(
        "exec",
        "-e",
        "TERM=xterm-256color",
        "-e",
        "COLORTERM=truecolor",
        "-e",
        f"FS_DOCKER_HOST_PROJECT_ROOT={repo.resolve()}",
        "-e",
        "FS_CLI_HOST_NO_COLOR=",
        "fs-cli",
        "python",
        "-m",
        "core.entrypoints.cli.lifecycle_click_app",
        "--help",
    ) in _log(fake_repo)
    assert "ARG=FS_CLI_HOST_NO_COLOR=" in _log(fake_repo)
    assert "ARG=-T" not in _log(fake_repo)


def test_launcher_diagnostics_stay_on_stderr_and_preserve_json_stdout(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "--json",
        "users",
        "list",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_COMPOSE_STDOUT": '{"ok":true}'},
    )

    assert result.returncode == 0
    assert result.stdout == '{"ok":true}\n'
    assert "Flow Steward CLI: preparing management runtime..." in result.stderr


def test_concurrent_missing_runtime_repairs_once_then_executes_both_commands(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    repo, _log_path = fake_repo
    barrier_dir = tmp_path / "exec-barrier"
    env = _launcher_env(
        fake_repo,
        {
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_CONTAINER_PRESENT": "0",
            "FAKE_BOOTSTRAP_SLEEP": "1",
            "FAKE_EXEC_BARRIER_DIR": str(barrier_dir),
        },
    )
    processes = [
        subprocess.Popen(
            [str(repo / "flow-steward"), "stack", "status"],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]

    results = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "exec") == 2
    assert _log(fake_repo).splitlines().count("EXEC_LOCK_PRESENT=0") == 2
    assert not (repo / ".flow-steward/runtime/cli-runtime.lock").exists()


@pytest.mark.parametrize("owner_pid", [None, 99_999_999], ids=["absent-pid", "dead-pid"])
def test_stale_repair_lock_is_recovered_after_owner_validation(
    fake_repo: tuple[Path, Path],
    owner_pid: int | None,
) -> None:
    repo, _log_path = fake_repo
    lock_dir = repo / ".flow-steward/runtime/cli-runtime.lock"
    lock_dir.mkdir(parents=True)
    if owner_pid is not None:
        (lock_dir / "pid").write_text(f"{owner_pid}\n", encoding="utf-8")

    result = _run_launcher(
        fake_repo,
        "--help",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_CONTAINER_PRESENT": "0"},
    )

    assert result.returncode == 0
    assert _count_action(fake_repo, "up") == 1
    assert _count_action(fake_repo, "exec") == 1
    assert not lock_dir.exists()


def test_live_repair_lock_is_never_stolen(fake_repo: tuple[Path, Path]) -> None:
    repo, _log_path = fake_repo
    lock_dir = repo / ".flow-steward/runtime/cli-runtime.lock"
    lock_dir.mkdir(parents=True)
    owner_file = lock_dir / "pid"
    owner_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    process = subprocess.Popen(
        [str(repo / "flow-steward"), "--help"],
        cwd=repo,
        env=_launcher_env(
            fake_repo,
            {"FAKE_IMAGE_REVISION": "test-revision", "FAKE_CONTAINER_PRESENT": "0"},
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(2.25)
        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 143
    assert "Waiting for another Flow Steward CLI launcher" in stderr
    assert owner_file.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "exec") == 0


def test_redirected_streams_preserve_unicode_ansi_json_and_exit_code(
    fake_repo: tuple[Path, Path],
) -> None:
    payload = '{"message":"λ \\u001b[32mgreen\\u001b[0m"}\n'
    child_stderr = "child Ω \\x1b[31merror\\x1b[0m"
    result = _run_launcher(
        fake_repo,
        "--json",
        "users",
        "list",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_ECHO_STDIN": "1",
            "FAKE_COMPOSE_STDERR": child_stderr,
            "FAKE_COMPOSE_EXIT": "23",
        },
        input_text=payload,
    )

    assert result.returncode == 23
    assert result.stdout == payload
    assert child_stderr in result.stderr
    assert "Flow Steward CLI:" not in result.stdout
    assert _count_action(fake_repo, "exec") == 1


def test_sigint_returns_130_without_retry_and_leaves_runtime_reusable(
    fake_repo: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    repo, _log_path = fake_repo
    ready_path = tmp_path / "signal-child-ready"
    process = subprocess.Popen(
        [str(repo / "flow-steward"), "stack", "status"],
        cwd=repo,
        env=_launcher_env(
            fake_repo,
            {
                "FAKE_IMAGE_REVISION": "test-revision",
                "FAKE_SIGNAL_CHILD": "1",
                "FAKE_SIGNAL_READY": str(ready_path),
            },
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_path.exists()
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 130
    assert _count_action(fake_repo, "exec") == 1
    assert _count_action(fake_repo, "up") == 0
    assert not (repo / ".flow-steward/runtime/cli-runtime.lock").exists()


@pytest.mark.parametrize(
    "args",
    (
        ("users", "list", "--json"),
        ("projects", "list", "--json"),
        ("jobs", "list", "--json"),
        ("workflows", "list", "--json"),
    ),
)
def test_compact_product_commands_reach_the_compact_runtime_container(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    # AC17-R51: the product CLI ships in the Compact image and works there -- the auditor
    # created an admin with it by hand inside the container. What was missing was dispatch:
    # run_product_command exec'd into `fs-app`, a service the Compact compose set does not
    # define, so the allowlist had to refuse the whole group to avoid a Docker error.
    #
    # AC17-R88: `users issue-bootstrap-token` used to be the first case here and was wrong.
    # Reaching the container is not the same as working: that one writes host installation
    # state and recreates the application container, so it failed inside the runtime with
    # "Flow Steward has not been installed". This test was pinning a dispatch that led
    # nowhere. It is refused on Compact now, and covered by its own test.
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        *args,
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert "Unsupported Compact platform command." not in result.stderr
    assert (
        _arg_sequence(
            "compact-runtime",
            "python",
            "-m",
            "core.entrypoints.cli.click_app",
            *args,
        )
        in log
    )
    assert "ARG=fs-app" not in log


def test_scalable_product_commands_still_reach_the_app_container(
    fake_repo: tuple[Path, Path],
) -> None:
    result = _run_launcher(
        fake_repo,
        "projects",
        "list",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert (
        _arg_sequence(
            "fs-app", "python", "-m", "core.entrypoints.cli.click_app", "projects", "list"
        )
        in log
    )
    assert "ARG=compact-runtime" not in log


@pytest.mark.parametrize(
    "arguments",
    (
        (
            "database",
            "upgrade",
            "--maintenance-confirmed",
            "--author",
            "operator",
            "--confirm",
            "--json",
            "--plain",
            "--no-color",
            "--non-interactive",
            "--verbose",
        ),
        ("database", "upgrade", "--maintenance-confirmed", "--confirm"),
    ),
)
@pytest.mark.parametrize("no_color", ("", "1"))
def test_compact_database_uses_candidate_image_and_runtime_secret_adapter(
    fake_repo: tuple[Path, Path],
    arguments: tuple[str, ...],
    no_color: str,
) -> None:
    """Database maintenance needs the complete product closure and persisted Compact keys."""
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        *arguments,
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
            "NO_COLOR": no_color,
        },
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert _count_action(fake_repo, "run") == 1
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "start") == 0
    assert "ARG=--rm" in log
    assert "ARG=--no-deps" in log
    assert (
        _arg_sequence(
            "--entrypoint",
            "python",
            "compact-runtime",
            "-m",
            "core.entrypoints.cli.compact_container_cli",
            *arguments,
        )
        in log
    )
    assert "ARG=fs-cli" not in log
    assert "ARG=--dsn" not in log
    assert "ARG=--dsn-file" not in log
    assert "ARG=--dsn-stdin" not in log


@pytest.mark.parametrize("exit_code", (2, 8))
def test_compact_database_preserves_the_product_command_failure(
    fake_repo: tuple[Path, Path],
    exit_code: int,
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    result = _run_launcher(
        fake_repo,
        "database",
        "upgrade",
        "--maintenance-confirmed",
        "--confirm",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_EXIT": str(exit_code),
        },
    )

    assert result.returncode == exit_code
    assert _count_action(fake_repo, "run") == 1
    assert _count_action(fake_repo, "exec") == 0


@pytest.mark.parametrize(
    ("options", "error"),
    (
        (("--arbitrary-command",), "Unsupported Compact platform option"),
        (("--dsn",), "Unsupported Compact platform option"),
        (("--environment",), "Unsupported Compact platform option"),
        (("--label", "unexpected"), "Unsupported Compact database upgrade option"),
        (("--target-version", "1.0.0"), "Unsupported Compact database upgrade option"),
        (("--dry-run",), "Unsupported Compact database upgrade option"),
    ),
)
def test_compact_database_keeps_the_fixed_launcher_option_boundary(
    fake_repo: tuple[Path, Path],
    options: tuple[str, ...],
    error: str,
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    result = _run_launcher(
        fake_repo,
        "database",
        "upgrade",
        "--maintenance-confirmed",
        "--confirm",
        *options,
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_IMAGE_ID": image},
    )

    assert result.returncode == 2
    assert error in result.stderr
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 0


@pytest.mark.parametrize("flags", ((), ("--confirm",), ("--maintenance-confirmed",)))
def test_compact_database_requires_maintenance_and_confirmation_before_product_run(
    fake_repo: tuple[Path, Path], flags: tuple[str, ...]
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    result = _run_launcher(
        fake_repo,
        "database",
        "upgrade",
        *flags,
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_IMAGE_ID": image},
    )

    assert result.returncode == 2
    assert "requires --maintenance-confirmed and --confirm" in result.stderr
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "exec") == 0


def test_compact_database_status_keeps_the_restricted_platform_dispatcher(
    fake_repo: tuple[Path, Path],
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    result = _run_launcher(
        fake_repo,
        "database",
        "status",
        "--json",
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_IMAGE_ID": image},
    )

    assert result.returncode == 0, result.stderr
    assert _arg_sequence("fs-cli", "database-status") in _log(fake_repo)
    assert "ARG=compact-runtime" not in _log(fake_repo)


@pytest.mark.parametrize(
    ("args", "service"),
    (
        (("quality", "lint"), "fs-audit"),
        (("testing", "smoke-test"), "fs-test-runner"),
        (("extensions", "test", "demo"), "fs-test-runner"),
        (("extensions", "install", "demo", "--confirm"), "fs-extension-manager"),
    ),
)
def test_compact_names_the_tooling_service_it_does_not_have(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
    service: str,
) -> None:
    # These commands run in dedicated tooling services that the Compact compose set does not
    # define. The ADR asks for an actionable diagnostic before effects rather than a Docker
    # error about a missing service -- and never the flat "Unsupported Compact platform
    # command." that used to answer every one of the 84 unreachable leaves alike.
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        *args,
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 2
    assert service in result.stderr
    assert "Compact" in result.stderr
    assert _count_action(fake_repo, "exec") == 0
    assert _count_action(fake_repo, "run") == 0


@pytest.mark.parametrize(
    "args",
    (("environment", "list", "--json"), ("release", "list", "--json")),
)
def test_compact_does_not_widen_the_retained_cli_path(
    fake_repo: tuple[Path, Path],
    args: tuple[str, ...],
) -> None:
    # These two are in lifecycle_click_app's domain set, so they look like they belong on the
    # retained path with config and stack. They must not be put there: that path overrides
    # FS_COMPOSE_FILES to docker-compose.prod.yaml and exec's a long-running `fs-cli`, and a
    # Compact install has neither -- its own compose declares fs-cli as a one-shot
    # platform_runtime runner. `config` and `stack` already fail that way on a live Compact
    # install, and adding more commands to the path would only spread the failure.
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        *args,
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert result.returncode == 2
    log = _log(fake_repo)
    assert f"ARG={fake_repo[0] / 'infra/docker/docker-compose.prod.yaml'}" not in log
    assert (
        _arg_sequence("fs-cli", "python", "-m", "core.entrypoints.cli.lifecycle_click_app")
        not in log
    )


def test_compact_help_matches_what_the_launcher_actually_dispatches(
    fake_repo: tuple[Path, Path],
) -> None:
    # The original complaint was "it is in the image and you cannot reach it". Reachable but
    # undiscoverable is the same complaint one level up, and advertised-but-broken is worse
    # than an honest refusal: `config` and `stack` are listed as supported today and die with
    # "The final fs-cli start attempt failed" on a real Compact install.
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(fake_repo, "--help")

    assert result.returncode == 0, result.stderr
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    advertised = result.stdout.split("Product commands", 1)[1].split("Not available", 1)[0]
    advertised_groups = {group.strip() for group in advertised.splitlines()[1:] if group.strip()}
    registered_groups = {spec.path[0] for spec in PUBLIC_COMMAND_SPECS}
    assert advertised_groups <= registered_groups
    assert "tasks" not in advertised_groups
    for group in ("users", "projects", "workflows", "jobs", "reports", "localization"):
        assert group in result.stdout, group
    # named as refused, with the reason
    assert "quality lint" in result.stdout
    assert "testing" in result.stdout
    # named as a missing route rather than a missing capability
    assert "extensions install" in result.stdout
    # named as refused, with the reason and the path that does work (AC17-R140)
    assert "Refused on a Compact installation" in result.stdout
    assert "config, stack" in result.stdout
    assert "rerun ./install.sh from updated sources" in result.stdout
    assert _log(fake_repo) == ""


@pytest.mark.parametrize(
    "command",
    [
        "users issue-bootstrap-token",
        "extensions install",
        "extensions uninstall",
        "extensions test",
        "testing smoke-test",
        "testing nightfire-regression",
        "testing fuzz-api",
        "testing fuzz-mcp",
        "testing fuzz-ui",
        "testing cutover-preflight",
        "testing cutover-smoke",
        "quality lint",
        "quality release-gate",
    ],
)
@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_compact_refused_execution_paths_still_reach_their_actual_help(
    fake_repo: tuple[Path, Path],
    command: str,
    help_flag: str,
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)
    args = (*command.split(), help_flag)

    result = _run_launcher(
        fake_repo,
        *args,
        env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_IMAGE_ID": image},
    )

    assert result.returncode == 0, result.stderr
    assert "Product commands" not in result.stdout
    log = _log(fake_repo)
    assert (
        _arg_sequence("compact-runtime", "python", "-m", "core.entrypoints.cli.click_app", *args)
        in log
    )
    assert _count_action(fake_repo, "exec") == 1
    assert _count_action(fake_repo, "run") == 0
    assert _count_action(fake_repo, "up") == 0
    assert _count_action(fake_repo, "start") == 0
    assert _count_action(fake_repo, "rm") == 0


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_compact_help_as_a_field_value_does_not_bypass_the_execution_refusal(
    fake_repo: tuple[Path, Path],
    help_flag: str,
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(fake_repo, "extensions", "install", "--archive", help_flag)

    assert result.returncode == 2
    assert "missing route" in result.stderr
    assert _log(fake_repo) == ""


def test_compact_product_command_help_is_not_answered_with_the_root_help(
    fake_repo: tuple[Path, Path],
) -> None:
    """AC17-R84: the root help told the operator to do this, then answered itself.

    `Product commands (run `flow-steward COMMAND --help` for each)` was printed by the very
    page that `flow-steward users --help` returned: the launcher treated `--help` anywhere
    on the line as a request for the root help and exited. The argument was swallowed with
    no error, so there was no way to discover a product command's subcommands or options
    from the CLI itself.
    """
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    for args in (("users", "--help"), ("users", "create", "--help"), ("projects", "--help")):
        result = _run_launcher(
            fake_repo,
            *args,
            env={"FAKE_IMAGE_REVISION": "test-revision", "FAKE_IMAGE_ID": compact_image},
        )
        assert result.returncode == 0, result.stderr
        assert "Usage: flow-steward [OPTIONS] COMMAND" not in result.stdout, (
            f"`flow-steward {' '.join(args)}` was answered with the root help"
        )
        assert "Product commands" not in result.stdout


def test_compact_root_and_platform_help_still_render_the_launcher_page(
    fake_repo: tuple[Path, Path],
) -> None:
    """The control: the root help must keep working, for a bare --help and for the
    platform commands the launcher implements itself rather than the runtime."""
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    for args in (("--help",), ("-h",), ("backup", "--help"), ("status", "--help")):
        result = _run_launcher(fake_repo, *args)
        assert result.returncode == 0, f"{args}: {result.stderr}"
        assert "Usage: flow-steward [OPTIONS] COMMAND" in result.stdout
        assert _log(fake_repo) == ""


def test_compact_release_help_exposes_upgrade_options_without_starting_docker(
    fake_repo: tuple[Path, Path],
) -> None:
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    group = _run_launcher(fake_repo, "release", "--help")
    upgrade = _run_launcher(fake_repo, "release", "upgrade", "--help")

    assert group.returncode == 0, group.stderr
    for command in ("release info", "release check", "release upgrade"):
        assert command in group.stdout
    assert upgrade.returncode == 0, upgrade.stderr
    for option in ("--target-version", "--dry-run", "--timeout-seconds", "--confirm", "--tui"):
        assert option in upgrade.stdout
    assert "Product commands" not in upgrade.stdout
    assert _log(fake_repo) == ""


def test_compact_refuses_the_installer_owned_bootstrap_token_command(
    fake_repo: tuple[Path, Path],
) -> None:
    """AC17-R88: the command reached the runtime and then failed as if nothing was installed.

    On a healthy Compact installation `flow-steward users issue-bootstrap-token --confirm`
    answered "Flow Steward has not been installed." (configuration_unavailable), while
    `./install.sh` on the same installation answered "already installed" -- each tool
    pointing at the other. Routing was never the problem: issuing a token writes the
    installer state file and .env.prod and then recreates the application container, none
    of which exists inside the runtime, and the service it names is `fs-app`, which the
    Compact profile does not define. It cannot work there, so it says so.
    """
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(fake_repo, "users", "issue-bootstrap-token", "--confirm")

    assert result.returncode == 2, result.stdout
    combined = result.stdout + result.stderr
    assert "installer operation" in combined
    assert "./install.sh" in combined
    # AC17-R93 part 4: `users create` runs in the runtime and needs no token, so the
    # refusal names the command that actually creates the first administrator rather
    # than only the one that mints a link.
    assert "users create" in combined
    assert "--role admin" in combined
    # it must not repeat the lie that nothing is installed
    assert "has not been installed" not in combined
    assert _log(fake_repo) == ""


def test_compact_still_runs_the_other_user_commands(fake_repo: tuple[Path, Path]) -> None:
    """The control: one installer-owned command is refused, the group still dispatches."""
    compact_image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=compact_image)

    result = _run_launcher(fake_repo, "users", "list")

    combined = result.stdout + result.stderr
    assert "installer operation" not in combined
    assert "Unsupported Compact platform command" not in combined


def test_the_proxy_fragment_command_reaches_the_compact_runtime(
    fake_repo: tuple[Path, Path],
) -> None:
    """AC17-R113: the launcher keeps its own allowlist, and it is checked first.

    The command was first added to the `config` family, which the launcher dispatches
    to a retained fs-cli container the Compact profile does not have. Moving it to the
    top level fixed the dispatch and not the allowlist: `case` in the launcher refuses
    anything it does not name, before the image is asked for an opinion, so the Python
    command registry can be entirely correct and the user still meets "Unsupported
    Compact platform command."

    That is the fourth surface this branch has found which one layer knows about and
    another does not -- source slices, the CLI stage's COPY lines, the launcher
    allowlist -- and it is the reason a green registry test is not evidence here.
    """
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        "proxy-fragment",
        env={
            "FAKE_IMAGE_REVISION": "test-revision",
            "FAKE_IMAGE_ID": image,
            "FAKE_COMPOSE_STDOUT": '{"ok":true}',
        },
    )

    assert "Unsupported Compact platform command." not in result.stderr
    assert result.returncode == 0, result.stderr
    assert _arg_sequence("fs-cli", "proxy-fragment") in _log(fake_repo)


@pytest.mark.parametrize("environment", ["development", "production"])
def test_scalable_bootstrap_recovery_uses_retained_management_runtime(fake_repo, environment):
    _write_selected_cli_image(fake_repo, environment=environment, value="flow-steward-cli:test")
    result = _run_launcher(
        fake_repo,
        "--json",
        "users",
        "issue-bootstrap-token",
        "--confirm",
        env={"FS_STACK_ENVIRONMENT": environment, "FAKE_IMAGE_REVISION": "test-revision"},
    )
    assert result.returncode == 0, result.stderr
    assert _arg_sequence(
        "fs-cli",
        "python",
        "-m",
        "core.entrypoints.cli.bootstrap_recovery",
        "--json",
        "users",
        "issue-bootstrap-token",
        "--confirm",
    ) in _log(fake_repo)
    assert "core.entrypoints.cli.click_app" not in _log(fake_repo)


def test_bootstrap_recovery_forwards_selected_descriptor(fake_repo):
    repo, _ = fake_repo
    selected_env = repo / "selected.env"
    project_name = "custom-recovery"
    selected_env.write_text(f"FS_COMPOSE_PROJECT_NAME={project_name}\n")
    (repo / "custom.yaml").write_text("services: {}\n")
    result = _run_launcher(
        fake_repo,
        "users",
        "issue-bootstrap-token",
        "--json",
        "--confirm",
        env={
            "FS_ENV_FILE": str(selected_env),
            "FS_COMPOSE_PROJECT_NAME": project_name,
            "FS_COMPOSE_FILES": "infra/docker/docker-compose.yaml custom.yaml",
            "FAKE_IMAGE_REVISION": "test-revision",
        },
    )
    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert f"ARG=FS_ENV_FILE={selected_env}" in log
    assert f"ARG=FS_COMPOSE_PROJECT_NAME={project_name}" in log
    assert "ARG=FS_COMPOSE_FILES=" in log and str(repo / "custom.yaml") in log
    assert "core.entrypoints.cli.bootstrap_recovery" in log


def test_bootstrap_help_does_not_activate_management_recovery(fake_repo):
    result = _run_launcher(fake_repo, "users", "issue-bootstrap-token", "--help")
    assert result.returncode == 0, result.stderr
    assert "core.entrypoints.cli.bootstrap_recovery" not in _log(fake_repo)
    assert "--force-recreate" not in _log(fake_repo)
