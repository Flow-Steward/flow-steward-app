#!/usr/bin/env bash
# Make sure a container started next sees the host's current files, on Docker Desktop.
#
# Docker Desktop shares host folders with its Linux VM over virtiofs, and the VM's
# kernel caches what it has looked up there: directory entries, inode numbers and
# file sizes. The host tells the VM when to forget an entry. When that stops -- under
# the Docker VMM engine the host-side notifier has been seen blocked for days, logging
# "waiting for notification buffer (inval_entry)" to Docker Desktop's monitor.log --
# a container started on a folder the VM has already seen reads the old view:
#   - a file created since is missing (vite: "Could not load ...");
#   - a file removed or renamed away (`git mv a.py b.py`) is still listed and read,
#     and can come back as NUL bytes (mypy: "source code string cannot contain null
#     bytes");
#   - a file replaced by rename -- how editors, git and `.env.prod` rewrites save --
#     keeps its old size, so the new content is cut to it (a truncated `.env.prod`).
# A folder the VM has never seen is always right, which is why a fresh clone per gate
# run, or a copied stand folder, used to hide the problem.
#
# `refresh` does two things before the caller starts its container:
#   1. Drops the VM's dentry, inode and page caches, so the next lookup goes back to
#      the host. That repairs the part the VM's kernel caches. It needs a privileged
#      container, which anyone who can reach the Docker socket can already start, and
#      only slows other running containers' next disk reads.
#   2. With <probe-dir>, proves the result: it replaces one file there by rename and
#      creates another, then reads both back through a fresh bind mount. If the
#      container still sees the old view, Docker Desktop itself is holding stale
#      state; this exits 3 with what to do, so a gate never reports on files it could
#      not see and an install never applies a truncated configuration.
# On any engine other than Docker Desktop it does nothing. A refused cache drop or a
# probe container that cannot start is reported and not fatal; only a probe that ran
# and saw stale files fails the caller.
#
# Usage: docker_desktop_file_cache.sh refresh <local-image> [<probe-dir>]
#   <local-image>  exists locally and contains `sh`; it is never pulled.
#   <probe-dir>    an existing, writable host directory inside the folder the caller
#                  is about to mount; it keeps one small probe file between runs.
#   The `docker` on PATH and its environment (DOCKER_HOST, DOCKER_CONFIG, ...) select
#   the engine, exactly as for the caller's own `docker run`.
set -euo pipefail

usage() {
  echo "Usage: $(basename -- "$0") refresh <local-image> [<probe-dir>]" >&2
}

if [[ $# -lt 2 || $# -gt 3 || "$1" != "refresh" || -z "$2" ]]; then
  usage
  exit 2
fi
image="$2"
probe_dir="${3:-}"
if [[ $# -eq 3 && ( -z "${probe_dir}" || ! -d "${probe_dir}" ) ]]; then
  echo "[docker-desktop] Probe directory does not exist: ${probe_dir}" >&2
  exit 2
fi

engine="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
if [[ "${engine}" != "Docker Desktop" ]]; then
  exit 0
fi

if ! docker run --rm --pull never --privileged --network none --user 0:0 \
  --entrypoint sh "${image}" -c 'sync && echo 3 > /proc/sys/vm/drop_caches' \
  >/dev/null 2>&1; then
  echo "[docker-desktop] Could not drop the VM's file cache (privileged container refused);" >&2
  echo "[docker-desktop] containers may read stale files from host folders mounted before." >&2
fi

if [[ -z "${probe_dir}" ]]; then
  exit 0
fi

probe_dir="$(cd -- "${probe_dir}" && pwd -P)"
kept_name=".docker-desktop-mount-probe"
random_hex="$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
nonce="$(date +%s)-$$-${random_hex}"
fresh_name="${kept_name}-${nonce}"
# A different length every run: a cached size shows up as truncated or padded content.
padding="$(printf '%*s' "$((16#${random_hex:0:4} % 97))" '' | tr ' ' '.')"
expected="${nonce}${padding}"
cleanup_probe() {
  rm -f -- "${probe_dir}/${fresh_name}" "${probe_dir}/${kept_name}.next"
}
trap cleanup_probe EXIT
printf '%s\n' "${expected}" > "${probe_dir}/${kept_name}.next"
mv -f -- "${probe_dir}/${kept_name}.next" "${probe_dir}/${kept_name}"
printf '%s\n' "${nonce}" > "${probe_dir}/${fresh_name}"

# Line 1: the size the container is told; line 2: the content it reads; then the
# directory listing. Each is a separate way the stale view has shown up.
if ! seen="$(
  docker run --rm --pull never --network none --user 0:0 \
    -v "${probe_dir}:/probe:ro" --entrypoint sh "${image}" -c \
    'f=/probe/'"${kept_name}"'; stat -c %s "$f"; printf "%s\n" "$(cat "$f")"; ls -a /probe' \
    2>/dev/null
)"; then
  echo "[docker-desktop] Could not start a container to check that host files are current." >&2
  exit 0
fi

seen_size="$(printf '%s\n' "${seen}" | sed -n 1p)"
seen_content="$(printf '%s\n' "${seen}" | sed -n 2p)"
fresh_visible=0
if printf '%s\n' "${seen}" | sed -n '3,$p' | grep -Fxq -- "${fresh_name}"; then
  fresh_visible=1
fi
if [[ "${seen_size}" == "$((${#expected} + 1))" && "${seen_content}" == "${expected}" && "${fresh_visible}" -eq 1 ]]; then
  exit 0
fi

cat >&2 <<EOF
[docker-desktop] Containers see a stale copy of ${probe_dir}:
[docker-desktop]   a file replaced on the host has size ${seen_size:-?} there (expected $((${#expected} + 1))) and reads ${#seen_content} characters (expected ${#expected});
[docker-desktop]   a file created on the host is $([[ "${fresh_visible}" -eq 1 ]] && echo visible || echo missing) in the container's listing.
[docker-desktop] Docker Desktop's file sharing is holding old state that dropping the VM's
[docker-desktop] cache does not clear, so anything run now would read wrong files. Restart
[docker-desktop] Docker Desktop (whale menu > Restart) and rerun. If it keeps coming back,
[docker-desktop] switch Settings > General > Virtual Machine Manager from Docker VMM to
[docker-desktop] Apple Virtualization framework (docs/GUIDE.md, launcher conditions).
EOF
exit 3
