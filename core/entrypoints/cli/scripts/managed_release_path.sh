# Sourced by both host launchers. Keep this file compatible with /bin/sh.

flow_steward_public_release_repository="Flow-Steward/flow-steward-app"

flow_steward_has_managed_release() {
  [ -e "$1/.flow-steward/bootstrap-release" ] || [ -L "$1/.flow-steward/bootstrap-release" ] ||
    [ -e "$1/.flow-steward/release-checkout" ] || [ -L "$1/.flow-steward/release-checkout" ]
}

flow_steward_read_release_pin() {
  managed_pin="$1/.flow-steward/bootstrap-release"
  [ -f "$managed_pin" ] && [ -O "$managed_pin" ] && [ ! -L "$managed_pin" ] &&
    read -r managed_tag managed_commit managed_project managed_extra < "$managed_pin" &&
    [ -n "$managed_tag" ] && [ -n "$managed_commit" ] && [ -z "$managed_extra" ] || return 1
  case "$managed_project" in
    ''|[!a-z0-9]*|*[!a-z0-9_-]*) return 1 ;;
  esac
}

flow_steward_managed_checkout() (
  state_dir="$1/.flow-steward"
  record="$state_dir/release-checkout"
  [ -d "$state_dir" ] && [ -O "$state_dir" ] && [ ! -L "$state_dir" ] &&
    [ -f "$record" ] && [ -O "$record" ] && [ ! -L "$record" ] || return 1
  IFS= read -r directory < "$record" || return 1
  case "$directory" in
    *[!a-zA-Z0-9.]*|release.) return 1 ;;
    release.*) ;;
    *) return 1 ;;
  esac
  checkout="$state_dir/$directory/checkout"
  [ -O "$state_dir/$directory" ] && [ ! -L "$state_dir/$directory" ] &&
    [ -O "$checkout" ] && [ ! -L "$checkout" ] && [ -d "$checkout/.git" ] &&
    [ -x "$checkout/install.sh" ] && [ -x "$checkout/flow-steward" ] || return 1
  printf '%s\n' "$checkout"
)

flow_steward_managed_project_matches() (
  checkout=$1
  for env_file in "$checkout/.flow-steward/compose.env" "$checkout/.env" "$checkout/.env.prod"; do
    [ -e "$env_file" ] || [ -L "$env_file" ] || continue
    [ -f "$env_file" ] && [ ! -L "$env_file" ] || return 1
    # Compose state can override the launch environment. Check every assignment,
    # including an empty value, which would restore the shared default project.
    awk -v project="$2" -v env_file="$env_file" '
      /^[[:space:]]*(export[[:space:]]+)?FS_COMPOSE_PROJECT_NAME[[:space:]]*=/ {
        seen = 1
        sub(/^[^=]*=[[:space:]]*/, "")
        sub(/[[:space:]]*$/, "")
        quote = substr($0, 1, 1)
        if ((quote == "\"" || quote == sprintf("%c", 39)) && substr($0, length($0)) == quote) {
          $0 = substr($0, 2, length($0) - 2)
        }
        if ($0 != project) exit 1
      }
      END { if (env_file ~ /\/compose\.env$/ && !seen) exit 1 }
    ' "$env_file" || return 1
  done
)

flow_steward_can_select_release() (
  root=$1
  for existing in .env .env.prod .flow-steward/stack.toml .flow-steward/compose.env \
    .flow-steward/install-state.json .flow-steward/installed-release.json \
    .flow-steward/pending-release.json .flow-steward/release-handoff.json; do
    if [ -e "$root/$existing" ] || [ -L "$root/$existing" ]; then
      return 1
    fi
  done

  # A detached exact tag is an explicit version selection. A branch can happen
  # to point at a release commit and still needs the fresh installation path.
  [ -e "$root/.git" ] || return 0
  if git -C "$root" symbolic-ref -q HEAD >/dev/null 2>&1; then
    return 0
  fi
  version=$(cat "$root/VERSION" 2>/dev/null) || return 0
  head=$(git -C "$root" rev-parse --verify HEAD 2>/dev/null) || return 0
  tag_commit=$(git -C "$root" rev-parse --verify "refs/tags/v$version^{commit}" 2>/dev/null) || return 0
  [ "$head" != "$tag_commit" ]
)
