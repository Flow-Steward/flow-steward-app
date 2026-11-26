#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$1" && pwd -P)
input_count=$2
shift 2
input_indices=()
for ((i = 0; i < input_count; i++)); do
  input_indices+=("$1")
  shift
done
install_args=("$@")
# shellcheck source=managed_release_path.sh
source "$root/core/entrypoints/cli/scripts/managed_release_path.sh"
state_dir="$root/.flow-steward"
pin="$state_dir/bootstrap-release"
checkout_record="$state_dir/release-checkout"
repository="https://github.com/$flow_steward_public_release_repository"
tag_pattern='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
project_pattern='^[a-z0-9][a-z0-9_-]*$'
stage=""
import_temp=""

fail() {
  printf 'Flow Steward installer: %s\n' "$1" >&2
  exit 4
}

cleanup_stage() {
  if [[ -n "$import_temp" ]]; then
    rm -f -- "$import_temp"
  fi
  [[ -n "$stage" ]] || return 0
  if [[ -f "$checkout_record" && "$(cat "$checkout_record")" == "${stage##*/}" ]]; then
    rm -f -- "$stage/pin" "$stage/checkout-record"
  else
    rm -rf -- "$stage"
  fi
}

prepare_stage() {
  if [[ -z "$stage" ]]; then
    stage=$(mktemp -d "$state_dir/release.XXXXXXXX")
  fi
}

choose_release() {
  local latest tag_prefix refs project_hash
  command -v curl >/dev/null 2>&1 || fail "Install curl to select the public stable release."
  if [[ -z "$requested_project" ]]; then
    project_hash=$(printf '%s' "$root" | git hash-object --stdin)
    project="flow-steward-${project_hash:0:16}"
  else
    project=$requested_project
  fi

  tag_prefix="$repository/releases/tag/"
  latest=$(curl -q --fail --silent --show-error --proto '=https' --max-time 20 \
    --output /dev/null --write-out '%{redirect_url}' "$repository/releases/latest") ||
    fail "Could not select a stable release from GitHub. Restore connectivity and rerun ./install.sh."
  tag="${latest#"$tag_prefix"}"
  [[ "$latest" == "$tag_prefix"* && "$tag" =~ $tag_pattern ]] ||
    fail "GitHub did not identify a stable Flow Steward release."

  refs=$(git -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=30 ls-remote --exit-code --tags \
    "$repository.git" "refs/tags/$tag" "refs/tags/$tag^{}") ||
    fail "Could not resolve $tag. Restore GitHub connectivity and rerun ./install.sh."
  commit=$(awk -v ref="refs/tags/$tag" '
    $2 == ref { object = $1 }
    $2 == ref "^{}" { peeled = $1 }
    END { print (peeled ? peeled : object) }
  ' <<< "$refs")
  [[ "$commit" =~ ^[a-f0-9]{40}$ ]] || fail "GitHub did not return an exact commit for $tag."
}

verify_pinned_checkout() {
  local checkout=$1 head tagged version
  head=$(git -C "$checkout" rev-parse --verify HEAD) &&
    tagged=$(git -C "$checkout" rev-parse --verify "refs/tags/$tag^{commit}") &&
    version=$(cat "$checkout/VERSION") &&
    [[ "$head" == "$commit" && "$tagged" == "$commit" && "$version" == "${tag#v}" ]] &&
    git -C "$checkout" diff --quiet && git -C "$checkout" diff --cached --quiet
}

import_relative_inputs() {
  local index arg option prefix source_path physical_path parent mode import_dir digest relative_path
  import_dir="$checkout/.flow-steward/import"
  for index in ${input_indices[@]+"${input_indices[@]}"}; do
    arg=${install_args[index]}
    case "$arg" in
      --*=*) option=${arg%%=*}; prefix="$option=" ;;
      *) option=${install_args[index-1]}; prefix="" ;;
    esac
    source_path=${arg#"$prefix"}
    [[ -f "$source_path" ]] || fail "Installer input file is unavailable: $source_path"
    if [[ -L "$source_path" ]]; then
      [[ "$option" != "--s3-credentials-path" ]] || fail "The S3 credentials file must be a regular file."
      physical_path=$(realpath "$source_path") || fail "Could not resolve installer input: $source_path"
    else
      parent=$(cd -- "${source_path%/*}" && pwd -P) || fail "Could not resolve installer input: $source_path"
      physical_path="$parent/${source_path##*/}"
    fi
    [[ "$physical_path" == "$root/"* ]] ||
      fail "Relative installer file paths must stay inside the launch checkout. Pass an absolute path for an external input."
    if [[ "$option" == "--s3-credentials-path" ]]; then
      mode=$(stat -c '%a' "$physical_path" 2>/dev/null || stat -f '%Lp' "$physical_path") &&
        [[ "$mode" =~ ^[0-7]{1,4}$ ]] && (( (8#$mode & 077) == 0 )) ||
        fail "The S3 credentials file must use mode 0600 or stricter."
    fi
    [[ ! -L "$checkout/.flow-steward" && ! -L "$import_dir" ]] ||
      fail "The installer input directory must be inside the managed checkout."
    mkdir -p "$import_dir"
    chmod 700 "$import_dir"
    digest=$(printf '%s' "$source_path" | git hash-object --stdin)
    relative_path=".flow-steward/import/host-$digest"
    [[ ! -L "$checkout/$relative_path" && ! -d "$checkout/$relative_path" ]] ||
      fail "The saved installer input path is invalid."
    import_temp=$(mktemp "$import_dir/.input.XXXXXXXX")
    cat "$physical_path" > "$import_temp"
    mv -f -- "$import_temp" "$checkout/$relative_path"
    import_temp=""
    install_args[index]="$prefix$relative_path"
  done
}

[[ ! -L "$state_dir" && ( ! -e "$state_dir" || ( -d "$state_dir" && -O "$state_dir" ) ) ]] ||
  fail "The managed release state directory must be owned by the deployment user."
command -v git >/dev/null 2>&1 || fail "Install Git to download and verify the release checkout."
requested_project="${FS_COMPOSE_PROJECT_NAME:-}"
[[ -z "$requested_project" || "$requested_project" =~ $project_pattern ]] ||
  fail "FS_COMPOSE_PROJECT_NAME must be a lowercase Compose project name."
export GIT_TERMINAL_PROMPT=0
original_umask=$(umask)
umask 077
mkdir -p "$state_dir"
chmod 700 "$state_dir"
trap cleanup_stage EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [[ ! -e "$pin" && ! -L "$pin" ]]; then
  [[ ! -e "$checkout_record" && ! -L "$checkout_record" ]] || fail "The saved release pin is missing."
  choose_release
  prepare_stage
  printf '%s\t%s\t%s\n' "$tag" "$commit" "$project" > "$stage/pin"
  # A concurrent first run may have selected a release. Read the winning record.
  ln "$stage/pin" "$pin" 2>/dev/null || [[ -f "$pin" ]] || fail "Could not save the selected release."
fi
flow_steward_read_release_pin "$root" &&
  [[ "$managed_tag" =~ $tag_pattern && "$managed_commit" =~ ^[a-f0-9]{40}$ ]] ||
  fail "The saved release pin is invalid. Inspect $pin before retrying."
tag=$managed_tag commit=$managed_commit project=$managed_project
[[ -z "$requested_project" || "$requested_project" == "$project" ]] ||
  fail "This installation is bound to Compose project $project. Rerun with that name or omit FS_COMPOSE_PROJECT_NAME."

if [[ ! -e "$checkout_record" && ! -L "$checkout_record" ]]; then
  prepare_stage
  printf 'Flow Steward installer: downloading %s for Compose project %s...\n' "$tag" "$project" >&2
  # The parent is private. Keep the release files readable by their container UIDs.
  (umask 022; git clone --quiet --single-branch --branch "$tag" -c advice.detachedHead=false \
    -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=30 "$repository.git" "$stage/checkout") ||
    fail "Could not download $tag. Rerun ./install.sh to retry that release."
  verify_pinned_checkout "$stage/checkout" && [[ -x "$stage/checkout/install.sh" && -x "$stage/checkout/flow-steward" ]] ||
    fail "The downloaded files do not match the recorded release $tag at $commit."
  printf '%s\n' "${stage##*/}" > "$stage/checkout-record"
  ln "$stage/checkout-record" "$checkout_record" 2>/dev/null || [[ -f "$checkout_record" ]] ||
    fail "Could not save the release checkout location."
fi
checkout=$(flow_steward_managed_checkout "$root") || fail "The saved release checkout is unavailable."
flow_steward_managed_project_matches "$checkout" "$project" ||
  fail "The release checkout configuration conflicts with its saved Compose project $project."
if [[ ! -f "$checkout/.flow-steward/installed-release.json" ]]; then
  verify_pinned_checkout "$checkout" || fail "The checkout no longer matches the recorded release $tag at $commit."
fi

import_relative_inputs
cleanup_stage
trap - EXIT INT TERM HUP
umask "$original_umask"
export FS_COMPOSE_PROJECT_NAME="$project"
exec "$checkout/install.sh" ${install_args[@]+"${install_args[@]}"}
