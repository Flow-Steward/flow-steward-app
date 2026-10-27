#!/usr/bin/env bash
# A boot trigger, not a container failure supervisor. Docker owns failure retries.
set -euo pipefail

fail() { echo "Flow Steward auto-start: $* Rerun ./install.sh to refresh this installation." >&2; exit 4; }
valid_project() { [[ "$1" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; }

read_profile() {
  profile=$(sed -n 's/^FS_DEPLOYMENT_PROFILE=//p' "$1/compose.env")
  case "$profile" in compact|scalable) ;; *) fail "Installed deployment profile is invalid." ;; esac
}

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

collect_manifest() {
  local repo=$1 state_dir=$2 compose_env cid service working_dir
  compose_env="$state_dir/compose.env"
  [[ -f "$compose_env" ]] || fail "Installed Compose configuration is missing."
  project=$(sed -n 's/^FS_COMPOSE_PROJECT_NAME=//p' "$compose_env")
  valid_project "$project" || fail "Installed Compose project is invalid."
  printf '%s\n' "$project"
  local count=0
  while IFS= read -r cid; do
    [[ -n "$cid" ]] || continue
    [[ "$cid" =~ ^[a-f0-9]{64}$ ]] || fail "Docker returned an invalid container ID."
    service=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$cid")
    working_dir=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid")
    [[ "$working_dir" == "$repo" || "$working_dir" == "$repo/infra/docker" ]] || fail "Compose project belongs to another checkout."
    [[ "$service" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "Docker returned an invalid service name."
    printf '%s %s\n' "$cid" "$service"
    count=$((count + 1))
  done < <(docker ps --no-trunc -q --filter "label=com.docker.compose.project=$project" --filter 'label=com.docker.compose.oneoff=False')
  [[ $count -gt 0 ]] || fail "No running installation found."
}

unit_text() {
  local project=$1 uid=$2 gid=$3 directory=$4
  cat <<EOF
# Flow Steward owned auto-start unit: $project
[Unit]
Description=Flow Steward $project startup
BindsTo=docker.service
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
User=$uid
Group=$gid
Environment=DOCKER_HOST=unix:///var/run/docker.sock
Environment=PATH=/usr/bin:/bin
ExecStart=/bin/bash $directory/runner.sh start $directory/containers
ExecStopPost=/bin/bash $directory/runner.sh stop $directory/containers
RemainAfterExit=yes
Restart=no
TimeoutStartSec=360
TimeoutStopSec=600

[Install]
WantedBy=multi-user.target docker.service
EOF
}

retire_unit() {
  local repo=$1 state_dir=$2 project directory unit unit_file
  project=$(sed -n 's/^FS_COMPOSE_PROJECT_NAME=//p' "$state_dir/compose.env")
  valid_project "$project" || fail "Installed Compose project is invalid."
  directory="/etc/flow-steward/autostart/$project"
  unit="flow-steward-$project.service"
  unit_file="/etc/systemd/system/$unit"
  if [[ -e "$unit_file" || -L "$unit_file" ]]; then
    [[ ! -L "$unit_file" && ! -L "$directory" ]] || fail "Refusing a symlinked service destination."
    [[ $(head -n 1 "$unit_file") == "# Flow Steward owned auto-start unit: $project" ]] || fail "A different system service already uses this name."
    [[ -f "$directory/checkout" && $(cat "$directory/checkout") == "$repo" ]] || fail "Service belongs to another checkout."
    # Scalable retains shared database/broker IDs. Retiring this control unit
    # must never invoke its old container cleanup against the new installation.
    sed '/^ExecStop=/d; /^ExecStopPost=/d' "$unit_file" > "$unit_file.new"
    chmod 644 "$unit_file.new"
    mv -f "$unit_file.new" "$unit_file"
    systemctl daemon-reload
    systemctl disable "$unit"
    systemctl stop "$unit"
    rm -f -- "$unit_file" "$directory/runner.sh" "$directory/containers" "$directory/checkout"
    rmdir "$directory"
    systemctl daemon-reload
  fi
  echo "Flow Steward: Scalable uses its existing Docker restart policies; no Compact host service is enabled." >&2
}

install_unit() {
  local repo=$1 script=$2 state_dir=$3 uid gid manifest project directory unit unit_file existed=0 enabled=0
  [[ $(id -u) == 0 ]] || fail "System service setup requires sudo."
  uid=$(stat -c '%u' "$repo"); gid=$(stat -c '%g' "$repo")
  [[ "$uid" != 0 && "${SUDO_UID:-$uid}" == "$uid" ]] || fail "Run the installer as the checkout owner."
  export DOCKER_HOST=unix:///var/run/docker.sock
  read_profile "$state_dir"
  if [[ "$profile" == scalable ]]; then retire_unit "$repo" "$state_dir"; return; fi
  manifest=$(collect_manifest "$repo" "$state_dir")
  project=${manifest%%$'\n'*}
  directory="/etc/flow-steward/autostart/$project"
  unit="flow-steward-$project.service"
  unit_file="/etc/systemd/system/$unit"
  [[ ! -L "$directory" && ! -L "$unit_file" ]] || fail "Refusing a symlinked service destination."
  if [[ -e "$unit_file" ]]; then
    [[ $(head -n 1 "$unit_file") == "# Flow Steward owned auto-start unit: $project" ]] || fail "A different system service already uses this name."
    [[ -f "$directory/checkout" && $(cat "$directory/checkout") == "$repo" ]] || fail "Service belongs to another checkout."
    existed=1
    if systemctl is-enabled --quiet "$unit"; then enabled=1; fi
  fi
  install -d -m 755 "$directory"
  install -m 755 "$script" "$directory/runner.sh"
  printf '%s\n' "$manifest" > "$directory/containers.new"
  chmod 644 "$directory/containers.new"
  mv -f "$directory/containers.new" "$directory/containers"
  printf '%s\n' "$repo" > "$directory/checkout"
  chmod 644 "$directory/checkout"
  unit_text "$project" "$uid" "$gid" "$directory" > "$unit_file"
  chmod 644 "$unit_file"
  systemd-analyze verify "$unit_file"
  systemctl daemon-reload
  if [[ $existed == 0 ]]; then
    systemctl enable --now "$unit"
  else
    # Refresh activation links after a unit upgrade, without undoing disable.
    if [[ $enabled == 1 ]]; then systemctl reenable "$unit"; fi
    # The explicit installer invocation has already started these containers.
    # Adopt them for stop control, even when future boot activation is disabled.
    systemctl start "$unit"
  fi
  echo "Flow Steward auto-start configured: $unit ($(systemctl is-enabled "$unit" || true))." >&2
  echo "Disable and stop: sudo systemctl disable --now $unit" >&2
}

configure() {
  local repo=$1 script=$2 state_dir=$3 endpoint manifest project directory unit
  if [[ $(uname -s) != Linux || ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null; then
    echo "Flow Steward auto-start: no host systemd; use this platform's startup settings." >&2
    return 0
  fi
  endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')}
  if [[ "$endpoint" != unix:///var/run/docker.sock && "$endpoint" != unix:///run/docker.sock ]]; then
    echo "Flow Steward auto-start: system Docker is not selected; no host service was changed." >&2
    return 0
  fi
  [[ $(stat -c '%u' "$repo") == "$(id -u)" ]] || fail "Run as the checkout owner."
  read_profile "$state_dir"
  if [[ "$profile" == scalable ]]; then
    project=$(sed -n 's/^FS_COMPOSE_PROJECT_NAME=//p' "$state_dir/compose.env")
    valid_project "$project" || fail "Installed Compose project is invalid."
    if [[ -e "/etc/systemd/system/flow-steward-$project.service" || -L "/etc/systemd/system/flow-steward-$project.service" ]]; then
      sudo -- /bin/bash "$script" install "$repo" "$state_dir"
    else
      echo "Flow Steward: Scalable uses its existing Docker restart policies; no Compact host service is needed." >&2
    fi
    return
  fi
  manifest=$(collect_manifest "$repo" "$state_dir")
  project=${manifest%%$'\n'*}
  directory="/etc/flow-steward/autostart/$project"
  unit="flow-steward-$project.service"
  if [[ -f "$directory/containers" && -f "$directory/checkout" && $(cat "$directory/checkout") == "$repo" ]] &&
    [[ $(cat "$directory/containers") == "$manifest" ]] && cmp -s "$script" "$directory/runner.sh" &&
    cmp -s <(unit_text "$project" "$(id -u)" "$(id -g)" "$directory") "/etc/systemd/system/$unit" &&
    systemctl is-active --quiet "$unit"; then
    echo "Flow Steward auto-start unchanged: $unit ($(systemctl is-enabled "$unit" || true))." >&2
    return 0
  fi
  command -v sudo >/dev/null || fail "Runtime is installed, but auto-start setup needs an administrator and sudo."
  echo "Flow Steward: sudo is needed to configure this installation's system auto-start." >&2
  sudo -- /bin/bash "$script" install "$repo" "$state_dir"
}

action=${1:-}
case "$action" in
  start|stop)
    [[ $# == 2 ]] || fail "Expected a container record."
    read_manifest "$2"
    if [[ "$action" == start ]]; then start_recorded; else stop_recorded; fi
    ;;
  configure|install)
    [[ $# == 3 ]] || fail "Expected an installation checkout and state directory."
    repo=$(cd -- "$2" && pwd -P)
    state_dir=$(cd -- "$3" && pwd -P)
    script=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")
    if [[ "$action" == configure ]]; then configure "$repo" "$script" "$state_dir"; else install_unit "$repo" "$script" "$state_dir"; fi
    ;;
  *) fail "Expected configure, install, start, or stop." ;;
esac
