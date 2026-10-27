#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

case "${1:-}" in
  backup|config|configure|doctor|down|environment|frontend-build|generate-secret-key|logs|rebuild|release|restart|secret-key|stack|start|status|stop|up)
    echo "./install.sh no longer accepts subcommands. Run ./install.sh to install/resume, or ./flow-steward for management." >&2
    exit 2
    ;;
esac

if [[ "$(id -u)" == "0" ]]; then
  echo "Flow Steward installer must not be run as root; rerun it as the deployment owner without sudo." >&2
  exit 4
fi

# The management runtime this script prepares also runs `./flow-steward doctor`, set by
# the launcher: the same image, mounts and privileges as an install, one implementation
# for both profiles. A plain doctor only reads -- no host record, no state directory
# made or re-moded, its bootstrap log kept outside the installation; `doctor --fix`
# owns the installation like an install does.
MANAGEMENT_MODULE="${FS_MANAGEMENT_MODULE:-installer}"
case "${MANAGEMENT_MODULE}" in
  installer | doctor) ;;
  *)
    echo "Unknown Flow Steward management runtime: ${MANAGEMENT_MODULE}" >&2
    exit 2
    ;;
esac
management_read_only=0
if [[ "${MANAGEMENT_MODULE}" == "doctor" ]]; then
  management_read_only=1
  for arg in "$@"; do
    if [[ "${arg}" == "--fix" ]]; then
      management_read_only=0
    fi
  done
fi

host_input_mounts=()
installer_tls_mode="local"
expect_tls_mode=0
for arg in "$@"; do
  if [[ ${expect_tls_mode} -eq 1 ]]; then
    installer_tls_mode="${arg}"
    expect_tls_mode=0
  elif [[ "${arg}" == "--tls-mode" ]]; then
    expect_tls_mode=1
  elif [[ "${arg}" == --tls-mode=* ]]; then
    installer_tls_mode="${arg#*=}"
  fi
done
expect_path_value=0
input_option=""
for arg in "$@"; do
  if [[ ${expect_path_value} -eq 1 ]]; then
    if [[ "${arg}" == -* ]]; then
      echo "Installer file-path option requires a value." >&2
      exit 3
    fi
    input_path="${arg}"
    current_option="${input_option}"
    expect_path_value=0
  else
    input_path=""
    current_option=""
    case "${arg}" in
      --s3-credentials-path|--certificate-path|--private-key-path)
        expect_path_value=1
        input_option="${arg}"
        ;;
      --s3-credentials-path=*|--certificate-path=*|--private-key-path=*)
        input_path="${arg#*=}"
        current_option="${arg%%=*}"
        ;;
    esac
  fi
  if [[ -n "${input_path}" && ( "${input_path}" == ../* || "${input_path}" == */../* || "${input_path}" == */.. ) ]]; then
    echo "Relative installer file paths must stay inside ${ROOT_DIR}." >&2
    exit 3
  fi
  if [[ -n "${input_path}" && "${input_path}" == /* ]]; then
    if [[ "${current_option}" != "--s3-credentials-path" && "${installer_tls_mode}" != "provided" ]]; then
      case "${input_path}" in
        "${ROOT_DIR}"/*) ;;
        *)
          echo "Generated TLS output paths must be inside ${ROOT_DIR}." >&2
          exit 3
          ;;
      esac
      continue
    fi
    if [[ ! -f "${input_path}" ]]; then
      echo "Installer input file is unavailable: ${input_path}" >&2
      exit 5
    fi
    host_input_mounts+=("${input_path}")
  elif [[ -n "${input_path}" && "${input_path}" == '~'* ]]; then
    echo "Expand '~' in installer file paths before invoking ./install.sh." >&2
    exit 3
  fi
done
if [[ ${expect_path_value} -eq 1 ]]; then
  echo "Installer file-path option requires a value." >&2
  exit 3
fi

echo "Flow Steward installer: preparing management runtime..." >&2

run_with_host_python() {
  local python_bin="${PYTHON_BIN:-}"
  if [[ -z "${python_bin}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      python_bin="python3"
    elif command -v python >/dev/null 2>&1; then
      python_bin="python"
    else
      echo "Flow Steward installer host-Python fallback requires Python 3.11 or newer." >&2
      exit 127
    fi
  fi
  if ! "${python_bin}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  then
    echo "Flow Steward installer host-Python fallback requires Python 3.11 or newer." >&2
    exit 1
  fi
  exec "${python_bin}" -m "core.entrypoints.cli.${MANAGEMENT_MODULE}" "$@"
}

if [[ "${FS_INSTALLER_USE_HOST_PYTHON:-0}" == "1" ]]; then
  run_with_host_python "$@"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is unavailable. Install Docker and rerun ./install.sh." >&2
  echo "Linux/server: https://docs.docker.com/engine/install/" >&2
  echo "Mac, Windows or Linux desktop: https://docs.docker.com/desktop/ (includes Compose)." >&2
  exit 127
fi

# AC17: both host entrypoints are POSIX shell and call these directly. The union
# is checked, not just the ones install.sh needs: an operator whose install
# succeeds goes on to run ./flow-steward, and a missing utility there is harder
# to place than one named here.
#
# Presence is checked, never the OS name. A `uname`-based branch would miss the
# cases that actually bite -- a trimmed container image or a minimal Alpine --
# and would claim to know about platforms nobody has tested.
#
# This does not help someone starting from cmd.exe or PowerShell: there the
# script never runs at all. It catches the other case, a POSIX shell that is
# incomplete.
missing_host_utilities=""
for host_utility in awk basename chmod date dirname grep id mktemp sed stat tail tee tr uname wc; do
  if ! command -v "${host_utility}" >/dev/null 2>&1; then
    missing_host_utilities="${missing_host_utilities} ${host_utility}"
  fi
done
if [[ -n "${missing_host_utilities}" ]]; then
  echo "Flow Steward needs these POSIX utilities on PATH:${missing_host_utilities}." >&2
  echo "On Windows run ./install.sh from Git Bash or WSL; elsewhere install the" >&2
  echo "missing coreutils, then rerun ./install.sh." >&2
  exit 127
fi

docker_context_endpoint() {
  docker context inspect --format '{{(index .Endpoints "docker").Host}}' 2>/dev/null
}

docker_config_uses_host_helper() {
  local config_file="$1"
  [[ -f "${config_file}" ]] && grep -Eq '"(credsStore|credHelpers)"[[:space:]]*:' "${config_file}"
}

installer_temp_dirs=()
installer_host_record=""
cleanup_installer_temp_dirs() {
  local temp_dir recorded
  for temp_dir in "${installer_temp_dirs[@]:-}"; do
    if [[ -n "${temp_dir}" && -d "${temp_dir}" ]]; then
      rm -rf -- "${temp_dir}"
    fi
  done
  # Only this run's record: another run's stays until that run ends.
  if [[ -n "${installer_host_record}" && -s "${installer_host_record}" ]]; then
    recorded="$(<"${installer_host_record}")"
    if [[ "${recorded}" == "$$" ]]; then
      rm -f -- "${installer_host_record}"
    fi
  fi
}
trap cleanup_installer_temp_dirs EXIT

resolved_endpoint="$(docker_context_endpoint || true)"
if [[ "${resolved_endpoint}" == unix://* ]]; then
  resolved_socket="${resolved_endpoint#unix://}"
elif [[ -S /var/run/docker.sock ]]; then
  resolved_socket="/var/run/docker.sock"
  resolved_endpoint="unix://${resolved_socket}"
else
  echo "Flow Steward installer could not resolve a Unix Docker socket from the active context." >&2
  exit 7
fi

bootstrap_docker_config="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-docker-config.XXXXXX")"
bootstrap_buildx_config="${bootstrap_docker_config}/buildx"
mkdir -p "${bootstrap_buildx_config}"
installer_temp_dirs+=("${bootstrap_docker_config}")

host_docker() {
  DOCKER_CONFIG="${bootstrap_docker_config}" \
    BUILDX_CONFIG="${bootstrap_buildx_config}" \
    docker --host "${resolved_endpoint}" "$@"
}

STATE_DIR="${FS_INSTALLER_STATE_DIR:-${ROOT_DIR}/.flow-steward}"
LOG_DIR="${STATE_DIR}/logs"
LIFECYCLE_STATE_DIR="${ROOT_DIR}/.flow-steward"
LIFECYCLE_OPERATIONS_DIR="${LIFECYCLE_STATE_DIR}/operations"
if [[ ${management_read_only} -eq 1 ]]; then
  if [[ -d "${STATE_DIR}" ]]; then
    STATE_DIR="$(cd -- "${STATE_DIR}" && pwd -P)"
  fi
  management_log_dir="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-doctor.XXXXXX")"
  installer_temp_dirs+=("${management_log_dir}")
  BOOTSTRAP_LOG="${management_log_dir}/doctor-bootstrap.log"
else
  mkdir -p "${LOG_DIR}"
  mkdir -p "${LIFECYCLE_OPERATIONS_DIR}"
  chmod 700 "${STATE_DIR}" "${LOG_DIR}"
  chmod 700 "${LIFECYCLE_STATE_DIR}" "${LIFECYCLE_OPERATIONS_DIR}"
  STATE_DIR="$(cd -- "${STATE_DIR}" && pwd -P)"
  LOG_DIR="${STATE_DIR}/logs"
  BOOTSTRAP_LOG="${LOG_DIR}/install-bootstrap-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
fi
: > "${BOOTSTRAP_LOG}"
chmod 600 "${BOOTSTRAP_LOG}"
# AC17-R162 (L1): from here on this run owns the installation, and ./flow-steward refuses
# what would change it -- from here, not from when the installer container starts: the
# management runtime is built or checked first, which can take longer than a minute.
# A record of a run still going is left alone; that run's lock refuses this one below.
installer_host_record="${STATE_DIR}/installer-host.pid"
previous_installer_pid=""
if [[ -s "${installer_host_record}" ]]; then
  previous_installer_pid="$(<"${installer_host_record}")"
fi
if [[ ${management_read_only} -eq 1 ]]; then
  installer_host_record=""  # a plain doctor owns nothing
elif ! [[ "${previous_installer_pid}" =~ ^[0-9]+$ ]] ||
  ! kill -0 "${previous_installer_pid}" >/dev/null 2>&1 ||
  ! { if [[ -r "/proc/${previous_installer_pid}/cmdline" ]]; then
    tr '\000' ' ' < "/proc/${previous_installer_pid}/cmdline"
  else
    ps -p "${previous_installer_pid}" -o command= 2>/dev/null
  fi; } | grep -q 'install\.sh'; then
  printf '%s\n' "$$" > "${installer_host_record}"
  chmod 600 "${installer_host_record}"
fi

if ! host_docker info --format 'Docker Server Version: {{.ServerVersion}}' >"${BOOTSTRAP_LOG}" 2>&1; then
  echo "Flow Steward installer could not reach Docker:" >&2
  tail -n 40 "${BOOTSTRAP_LOG}" >&2
  echo "Start Docker or correct socket permissions, then rerun ./install.sh. Log: ${BOOTSTRAP_LOG}" >&2
  exit 7
fi

INSTALLER_IMAGE="${FS_INSTALLER_IMAGE:-flow-steward-installer:local}"
INSTALLER_DOCKERFILE="${ROOT_DIR}/infra/docker/installer/Dockerfile"
if [[ ! -f "${INSTALLER_DOCKERFILE}" ]]; then
  echo "Installer image definition is missing: ${INSTALLER_DOCKERFILE}" >&2
  exit 4
fi

if [[ "${INSTALLER_IMAGE}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  if ! host_docker image inspect "${INSTALLER_IMAGE}" >> "${BOOTSTRAP_LOG}" 2>&1; then
    echo "Pinned installer image is unavailable: ${INSTALLER_IMAGE}" >&2
    echo "Restore that exact local image or unset FS_INSTALLER_IMAGE to build it. Log: ${BOOTSTRAP_LOG}" >&2
    exit 7
  fi
  echo "Flow Steward installer: reusing pinned management runtime ${INSTALLER_IMAGE}." >&2
else
  BUILDX_NO_DEFAULT_ATTESTATIONS=1 \
    host_docker build -t "${INSTALLER_IMAGE}" - < "${INSTALLER_DOCKERFILE}" > >(tee -a "${BOOTSTRAP_LOG}" >&2) 2>&1 &
  build_pid=$!
  heartbeat_interval="${FS_INSTALLER_HEARTBEAT_SECONDS:-5}"
  (
    while kill -0 "${build_pid}" >/dev/null 2>&1; do
      sleep "${heartbeat_interval}"
      if kill -0 "${build_pid}" >/dev/null 2>&1; then
        echo "Flow Steward installer: still preparing (${heartbeat_interval}s heartbeat)..." >&2
      fi
    done
  ) &
  heartbeat_pid=$!
  set +e
  wait "${build_pid}"
  build_code=$?
  set -e
  kill "${heartbeat_pid}" >/dev/null 2>&1 || true
  wait "${heartbeat_pid}" >/dev/null 2>&1 || true
  if [[ ${build_code} -ne 0 ]]; then
    echo "Installer image preparation failed. Full output: ${BOOTSTRAP_LOG}" >&2
    exit 7
  fi
fi

docker_run_args=(
  run
  --rm
  --add-host host.docker.internal:host-gateway
  -v "${ROOT_DIR}:${ROOT_DIR}"
  -w "${ROOT_DIR}"
  -e HOME=/tmp
  -e PYTHONPYCACHEPREFIX=/tmp/flow-steward-pycache
  -e GIT_CONFIG_COUNT=1
  -e GIT_CONFIG_KEY_0=safe.directory
  -e "GIT_CONFIG_VALUE_0=${ROOT_DIR}"
  -e "FS_INSTALLER_STATE_DIR=${STATE_DIR}"
  -e FS_INSTALLER_USE_HOST_PYTHON=0
  -e DOCKER_CONTEXT=default
  -e BUILDX_CONFIG=/tmp/buildx
)

case "${STATE_DIR}/" in
  "${ROOT_DIR}/"*) ;;
  *)
    # Docker would create a missing source directory; a plain doctor creates nothing.
    if [[ ${management_read_only} -eq 0 || -d "${STATE_DIR}" ]]; then
      docker_run_args+=(-v "${STATE_DIR}:${STATE_DIR}")
    fi
    ;;
esac

# orphan_installer: which installation this container serves, run by which ./install.sh
# process -- so a doctor can tell one nobody runs any more from one that is working.
docker_run_args+=(
  --label "com.flow-steward.management=${MANAGEMENT_MODULE}"
  --label "com.flow-steward.installation=${ROOT_DIR}"
  --label "com.flow-steward.host-pid=$$"
  -e "FS_INSTALLER_HOST_PID=$$"
)
if [[ "${MANAGEMENT_MODULE}" == "doctor" ]]; then
  # The configuration checks the launcher ran, and the ./install.sh it found alive.
  docker_run_args+=(-e FS_DOCTOR_CONFIGURATION_REPORT -e FS_DOCTOR_LIVE_INSTALLER_PID)
fi
if [[ -t 0 ]]; then
  docker_run_args+=(-i)
fi
if [[ -t 0 && -t 1 ]]; then
  docker_run_args+=(-t)
fi

for env_name in \
  FS_COMPOSE_PROJECT_NAME \
  HTTP_PORT \
  POSTGRES_HOST_PORT \
  RABBITMQ_AMQP_HOST_PORT \
  RABBITMQ_MANAGEMENT_HOST_PORT \
  MAILPIT_SMTP_PORT \
  MAILPIT_WEB_PORT \
  MAILPIT_HTTP_PORT \
  NEO4J_HTTP_HOST_PORT \
  NEO4J_BOLT_HOST_PORT \
  FRONTEND_HOST_PORT \
  MCP_HOST_PORT \
  SONAR_PORT \
  SEAWEEDFS_S3_HOST_PORT \
  SEAWEEDFS_MASTER_HOST_PORT \
  FS_RELEASE_REPOSITORY \
  FS_RELEASE_REGISTRIES \
  FS_APP_IMAGE \
  FS_WORKER_IMAGE \
  FS_CONTROL_IMAGE \
  FS_FRONTEND_IMAGE \
  MCP_IMAGE \
  FS_CLI_IMAGE \
  POSTGRES_IMAGE \
  FS_COMPACT_IMAGE \
  FS_COMPACT_IMAGE_REGISTRY_REF \
  FS_COMPACT_IMAGE_REVISION \
  FS_COMPACT_IMAGE_PLATFORM \
  AWS_CA_BUNDLE \
  NO_COLOR \
  TERM; do
  if [[ -n "${!env_name:-}" ]]; then
    docker_run_args+=(-e "${env_name}")
  fi
done

docker_config_mount="${DOCKER_CONFIG:-${HOME:-}/.docker}"
if [[ -n "${docker_config_mount}" && -d "${docker_config_mount}" ]]; then
  if docker_config_uses_host_helper "${docker_config_mount}/config.json"; then
    docker_config_mount="$(mktemp -d "${TMPDIR:-/tmp}/flow-steward-docker-config.XXXXXX")"
    installer_temp_dirs+=("${docker_config_mount}")
  fi
  container_docker_config=/tmp/flow-steward-docker-config
  docker_run_args+=(-v "${docker_config_mount}:${container_docker_config}:ro" -e "DOCKER_CONFIG=${container_docker_config}")
fi
if [[ -n "${DOCKER_CERT_PATH:-}" && -d "${DOCKER_CERT_PATH}" ]]; then
  docker_run_args+=(-v "${DOCKER_CERT_PATH}:${DOCKER_CERT_PATH}:ro" -e DOCKER_CERT_PATH)
fi
docker_run_args+=(-v "${resolved_socket}:/var/run/docker.sock")
host_platform="$(uname -s)"
if [[ "${host_platform}" == "Darwin" ]]; then
  socket_gid="$(stat -Lf '%g' "${resolved_socket}" 2>/dev/null || true)"
else
  socket_gid="$(stat -Lc '%g' "${resolved_socket}" 2>/dev/null || true)"
fi
runtime_socket_gid=""
if [[ "${socket_gid}" =~ ^[0-9]+$ ]]; then
  docker_run_args+=(--group-add "${socket_gid}")
  runtime_socket_gid="${socket_gid}"
fi
if [[ "${host_platform}" == "Darwin" ]]; then
  docker_run_args+=(--group-add 0)
  runtime_socket_gid=0
fi
if [[ -n "${runtime_socket_gid}" ]]; then
  docker_run_args+=(-e "FS_DOCKER_GID=${runtime_socket_gid}")
fi
installer_runtime_cidfile="${STATE_DIR}/installer-runtime.cid"
if [[ ${management_read_only} -eq 1 ]]; then
  # Never the installer's own record: a doctor during an install must not remove it.
  installer_runtime_cidfile="${management_log_dir}/runtime.cid"
fi
rm -f -- "${installer_runtime_cidfile}"
docker_run_args+=(--cidfile "${installer_runtime_cidfile}")
if command -v id >/dev/null 2>&1; then
  docker_run_args+=(--user "$(id -u):$(id -g)")
fi
if [[ ${#host_input_mounts[@]} -gt 0 ]]; then
  for input_path in "${host_input_mounts[@]}"; do
    docker_run_args+=(-v "${input_path}:${input_path}:ro")
  done
fi

installer_runtime_pid=""
installer_runtime_signal=""
forward_installer_runtime_signal() {
  local requested_signal="$1"
  local attempt runtime_cid
  installer_runtime_signal="${requested_signal}"
  for attempt in $(seq 1 50); do
    if [[ -s "${installer_runtime_cidfile}" ]]; then
      runtime_cid="$(<"${installer_runtime_cidfile}")"
      if [[ "${runtime_cid}" =~ ^[0-9a-f]{12,64}$ ]]; then
        host_docker container kill --signal "${requested_signal}" "${runtime_cid}" >/dev/null 2>&1 || true
      fi
      return
    fi
    if [[ -n "${installer_runtime_pid}" ]] && ! kill -0 "${installer_runtime_pid}" >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done
}
trap 'forward_installer_runtime_signal INT' INT
trap 'forward_installer_runtime_signal TERM' TERM
# A dropped SSH session hangs the terminal up. Without this the Docker client died with
# the shell and an interactive installer container -- a form waiting for input -- kept
# running for as long as nobody noticed: three were found after 22 hours.
trap 'forward_installer_runtime_signal TERM' HUP

set +e
docker "${docker_run_args[@]}" "${INSTALLER_IMAGE}" -m "core.entrypoints.cli.${MANAGEMENT_MODULE}" "$@" <&0 &
installer_runtime_pid=$!
wait "${installer_runtime_pid}"
installer_runtime_code=$?
installer_runtime_cid=""
if [[ -s "${installer_runtime_cidfile}" ]]; then
  installer_runtime_cid="$(<"${installer_runtime_cidfile}")"
fi
# AC17-R162: the Docker client can exit on its own -- 125, "container ... is not
# running" -- while the installer container goes on, holding the installation's locks
# with nobody watching and no record left for ./flow-steward to see. Stop it the way a
# signal does; the installer resumes from its journal on the next run.
if [[ -z "${installer_runtime_signal}" ]] &&
  [[ "${installer_runtime_cid}" =~ ^[0-9a-f]{12,64}$ ]] &&
  [[ "$(host_docker container inspect --format '{{.State.Running}}' "${installer_runtime_cid}" 2>/dev/null || true)" == "true" ]]; then
  echo "Flow Steward installer: the Docker client exited (${installer_runtime_code}) while installer container ${installer_runtime_cid:0:12} kept running; stopping it so it does not hold this installation unobserved. Rerun ./install.sh: it resumes where it stopped." >&2
  installer_runtime_signal="client-exit"
  host_docker container kill --signal TERM "${installer_runtime_cid}" >/dev/null 2>&1 || true
fi
if [[ -n "${installer_runtime_signal}" ]]; then
  (
    sleep 12
    kill -TERM "${installer_runtime_pid}" >/dev/null 2>&1 || true
    sleep 1
    kill -KILL "${installer_runtime_pid}" >/dev/null 2>&1 || true
  ) &
  installer_runtime_watchdog=$!
  wait "${installer_runtime_pid}" >/dev/null 2>&1 || true
  kill "${installer_runtime_watchdog}" >/dev/null 2>&1 || true
  wait "${installer_runtime_watchdog}" >/dev/null 2>&1 || true
  runtime_running=""
  for _attempt in $(seq 1 12); do
    if [[ "${installer_runtime_cid}" =~ ^[0-9a-f]{12,64}$ ]]; then
      runtime_running="$(host_docker container inspect --format '{{.State.Running}}' "${installer_runtime_cid}" 2>/dev/null || true)"
    fi
    [[ "${runtime_running}" != "true" ]] && break
    sleep 1
  done
  if [[ "${runtime_running}" == "true" ]]; then
    host_docker container kill --signal TERM "${installer_runtime_cid}" >/dev/null 2>&1 || true
    sleep 1
  fi
  if [[ "${installer_runtime_cid}" =~ ^[0-9a-f]{12,64}$ ]] && [[ "$(host_docker container inspect --format '{{.State.Running}}' "${installer_runtime_cid}" 2>/dev/null || true)" == "true" ]]; then
    host_docker container kill --signal KILL "${installer_runtime_cid}" >/dev/null 2>&1 || true
  fi
  installer_runtime_code=10
fi
set -e
trap - INT TERM HUP
rm -f -- "${installer_runtime_cidfile}"
if [[ "${installer_runtime_code}" == 0 ]]; then
  installer_help_only=0
  for installer_argument in "$@"; do
    case "$installer_argument" in -h|--help|--version) installer_help_only=1 ;; esac
  done
  if [[ "$installer_help_only" == 0 ]]; then
    bash "${ROOT_DIR}/infra/host/systemd/autostart.sh" configure "${ROOT_DIR}" "${STATE_DIR}" >&2
  fi
fi
exit "${installer_runtime_code}"
