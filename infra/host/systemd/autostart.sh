#!/usr/bin/env bash
# Legacy host-unit migration only. Docker owns current startup and failure retries.
set -euo pipefail

fail() { echo "Flow Steward auto-start: $* Rerun ./install.sh to refresh this installation." >&2; exit 4; }
valid_project() { [[ "$1" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; }

read_manifest() {
  local manifest=$1 cid service extra
  [[ -f "$manifest" && ! -L "$manifest" ]] || fail "Container record is unavailable."
  ids=(); services=()
  {
    IFS= read -r project || fail "Empty container record."
    valid_project "$project" || fail "Invalid Compose project."
    while read -r cid service extra; do
      [[ "$cid" =~ ^[a-f0-9]{64}$ && "$service" =~ ^[a-z0-9][a-z0-9_-]*$ && -z "$extra" ]] || fail "Invalid container record."
      ids+=("$cid"); services+=("$service")
    done
  } < "$manifest"
  [[ ${#ids[@]} -gt 0 ]] || fail "No installed containers recorded."
}

verify_ownership() {
  local i identity
  # Validate every container before any mutation, including before a stop.
  for i in "${!ids[@]}"; do
    identity=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "${ids[$i]}") || fail "Recorded container ${ids[$i]:0:12} is missing."
    [[ "$identity" == "$project|${services[$i]}" ]] || fail "Container ownership changed."
  done
}

infrastructure() {
  case "$1" in pg-fs|rabbitmq-fs|seaweedfs|traefik) return 0 ;; *) return 1 ;; esac
}

stop_recorded() {
  local i phase service
  local batch=()
  verify_ownership
  # Drain job consumers while their API, database and broker remain available.
  # Docker stops IDs in one invocation concurrently, so use ordered phases.
  for phase in consumers application infrastructure; do
    batch=()
    for i in "${!ids[@]}"; do
      service=${services[$i]}
      case "$service" in
        fs-worker|control-plane|fs-extension-manager) [[ "$phase" == consumers ]] || continue ;;
        pg-fs|rabbitmq-fs|seaweedfs|traefik) [[ "$phase" == infrastructure ]] || continue ;;
        *) [[ "$phase" == application ]] || continue ;;
      esac
      batch+=("${ids[$i]}")
    done
    if [[ ${#batch[@]} -gt 0 ]]; then docker stop --time 190 "${batch[@]}" >/dev/null; fi
  done
}

start_recorded() {
  local i health deadline
  verify_ownership
  for i in "${!ids[@]}"; do
    if infrastructure "${services[$i]}"; then docker start "${ids[$i]}" >/dev/null; fi
  done
  deadline=$((SECONDS + 300))
  for i in "${!ids[@]}"; do
    infrastructure "${services[$i]}" || continue
    while :; do
      health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${ids[$i]}") || fail "Infrastructure container disappeared."
      case "$health" in
        healthy|none) break ;;
        unhealthy) fail "${services[$i]} is unhealthy; application was not started." ;;
        starting) [[ $SECONDS -lt $deadline ]] || fail "Infrastructure readiness timed out."; sleep 2 ;;
        *) fail "Unknown infrastructure health state." ;;
      esac
    done
  done
  for i in "${!ids[@]}"; do
    if ! infrastructure "${services[$i]}"; then docker start "${ids[$i]}" >/dev/null; fi
  done
  echo "Flow Steward auto-start: started installed containers for $project." >&2
}

legacy_paths() {
  local repo=$1 state_dir=$2 system_root=${3:-}
  [[ -f "$state_dir/compose.env" && ! -L "$state_dir/compose.env" ]] || fail "Installed Compose configuration is missing or symlinked."
  project=$(sed -n 's/^FS_COMPOSE_PROJECT_NAME=//p' "$state_dir/compose.env")
  valid_project "$project" || fail "Installed Compose project is invalid."
  directory="$system_root/etc/flow-steward/autostart/$project"
  unit="flow-steward-$project.service"
  unit_file="$system_root/etc/systemd/system/$unit"
}

verify_legacy_unit() {
  local repo=$1
  [[ ! -L "$unit_file" && ! -L "$directory" && ! -L "$directory/checkout" ]] || fail "Refusing a symlinked service destination; manual administrator review is required."
  [[ -f "$unit_file" && $(head -n 1 "$unit_file") == "# Flow Steward owned auto-start unit: $project" ]] || fail "A different system service already uses this name; manual administrator review is required."
  [[ -f "$directory/checkout" && $(cat "$directory/checkout") == "$repo" ]] || fail "Service belongs to another checkout; manual administrator review is required."
  # Base-file hook removal cannot neutralize administrator-created drop-ins.
  # Refuse even an empty directory: generated legacy units never created one.
  local system_root=${unit_file%/etc/systemd/system/*} unit_directory drop_in
  for unit_directory in etc/systemd/system run/systemd/system usr/local/lib/systemd/system usr/lib/systemd/system lib/systemd/system; do
    for drop_in in "$unit.d" service.d; do
      [[ ! -e "$system_root/$unit_directory/$drop_in" && ! -L "$system_root/$unit_directory/$drop_in" ]] || fail "Legacy systemd drop-ins require manual administrator review before retirement."
    done
  done
}

check_legacy_unit() {
  local repo=$1 state_dir=$2 system_root=${3:-} project directory unit unit_file
  [[ -e "$state_dir/compose.env" || -L "$state_dir/compose.env" ]] || return 0
  legacy_paths "$repo" "$state_dir" "$system_root"
  [[ -e "$unit_file" || -L "$unit_file" ]] || return 0
  verify_legacy_unit "$repo"
  printf 'Flow Steward: legacy host service %s conflicts with Docker autostart. Retire it explicitly before installation/upgrade:\n  sudo bash %q retire %q %q\n' \
    "$unit" "$repo/infra/host/systemd/autostart.sh" "$repo" "$state_dir" >&2
  return 4
}

retire_unit() {
  local repo=$1 state_dir=$2 system_root=${3:-} project directory unit unit_file
  legacy_paths "$repo" "$state_dir" "$system_root"
  if [[ -e "$unit_file" || -L "$unit_file" ]]; then
    verify_legacy_unit "$repo"
    # Remove old cleanup before systemctl can execute it. Never stop containers
    # while transferring startup ownership to their Docker restart policies.
    sed '/^[[:space:]]*ExecStop[[:space:]]*=/d; /^[[:space:]]*ExecStopPost[[:space:]]*=/d' "$unit_file" > "$unit_file.new"
    chmod 644 "$unit_file.new"
    mv -f "$unit_file.new" "$unit_file"
    systemctl daemon-reload
    systemctl disable "$unit"
    systemctl stop "$unit"
    rm -f -- "$unit_file" "$directory/runner.sh" "$directory/containers" "$directory/checkout"
    rmdir "$directory"
    systemctl daemon-reload
  fi
  echo "Flow Steward: legacy host service retired; Docker owns container autostart." >&2
}

main() {
  local action=${1:-} repo state_dir
  case "$action" in
    start|stop)
      [[ $# == 2 ]] || fail "Expected a container record."
      read_manifest "$2"
      if [[ "$action" == start ]]; then start_recorded; else stop_recorded; fi
      ;;
    check|retire)
      [[ $# == 3 ]] || fail "Expected an installation checkout and state directory."
      repo=$(cd -- "$2" && pwd -P)
      state_dir=$3
      if [[ -d "$state_dir" ]]; then state_dir=$(cd -- "$state_dir" && pwd -P); fi
      if [[ "$action" == check ]]; then
        check_legacy_unit "$repo" "$state_dir"
      else
        [[ $(id -u) == 0 ]] || fail "Explicit legacy service retirement requires an administrator."
        retire_unit "$repo" "$state_dir"
      fi
      ;;
    *) fail "Expected check, retire, start, or stop." ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
