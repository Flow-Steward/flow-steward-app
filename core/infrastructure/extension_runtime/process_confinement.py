"""How a local extension process is confined while it runs (isolation phase 1).

An installed extension is executable code its installation's owner trusts; it
runs as the service's own OS user and can reach what that user can reach (files,
network). Phase 1 limits the damage a faulty or runaway extension can do. It is
not a sandbox against a deliberately malicious one. On every invocation:

- **its processes end with it**: the extension's process group is killed at
  timeout, on an output overflow and after the run, and so is every process
  still carrying the run's environment marker (``FS_EXTENSION_RUN``), which
  finds descendants that left the group (``start_new_session``, ``setsid``).
  A descendant that clears its own environment is not found;
- **per-process resource limits**: CPU time, address space and the size of one
  written file (not a budget shared by all its processes), no core dumps;
- **no new privileges** (Linux): a setuid program it runs gains nothing;
- **a scratch directory of its own** as ``TMPDIR``, removed after the run.

Limits are applied by a short launcher that then ``exec``s the extension's
command (``preexec_fn`` is not safe in a threaded service). On Linux, a
protection that cannot be applied stops the run with exit code 70 instead of
running the extension unconfined.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import uuid
from typing import Any

MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
FILE_SIZE_LIMIT_BYTES = 1024 * 1024 * 1024
CPU_SECONDS_FLOOR = 30

# Applies the limits, then becomes the extension's command. Runs with -I.
# On Linux a protection that cannot be applied stops the run (exit 70): an
# extension must not start believing it is confined when it is not. A limit is
# never raised above the hard limit the service already runs under.
_LAUNCHER = (
    "import json, os, resource, sys\n"
    "limits = json.loads(sys.argv[1])\n"
    "linux = sys.platform.startswith('linux')\n"
    "def refuse(what, error):\n"
    "    sys.stderr.write('extension confinement could not be applied: %s (%s)\\n' % (what, error))\n"
    "    os._exit(70)\n"
    "for name, value in limits['rlimits'].items():\n"
    "    kind = getattr(resource, name, None)\n"
    "    if kind is None:\n"
    "        if linux:\n"
    "            refuse(name, 'unknown limit')\n"
    "        continue\n"
    "    try:\n"
    "        _soft, hard = resource.getrlimit(kind)\n"
    "        if hard != resource.RLIM_INFINITY:\n"
    "            value = min(value, hard)\n"
    "        resource.setrlimit(kind, (value, value))\n"
    "    except (ValueError, OSError) as error:\n"
    "        if linux:\n"
    "            refuse(name, error)\n"
    "if limits['no_new_privs'] and linux:\n"
    "    import ctypes\n"
    "    libc = ctypes.CDLL(None, use_errno=True)\n"
    "    if libc.prctl(38, 1, 0, 0, 0) != 0:\n"
    "        refuse('no_new_privs', os.strerror(ctypes.get_errno()))\n"
    "command = sys.argv[2:]\n"
    "os.execvp(command[0], command)\n"
)

# Every process of one invocation carries this variable in its environment; it
# finds the descendants that left the process group (start_new_session, setsid).
RUN_MARKER_ENV = "FS_EXTENSION_RUN"
CONFINEMENT_FAILED_EXIT = 70


def cpu_seconds_for(timeout_seconds: int | float) -> int:
    """CPU time an invocation may use: twice its wall-clock budget, at least a floor."""
    return max(CPU_SECONDS_FLOOR, int(timeout_seconds) * 2)


def confined_command(command: list[str], *, timeout_seconds: int | float) -> list[str]:
    """``command`` wrapped so it starts with the limits applied."""
    limits: dict[str, Any] = {
        "rlimits": {
            "RLIMIT_CPU": cpu_seconds_for(timeout_seconds),
            "RLIMIT_AS": MEMORY_LIMIT_BYTES,
            "RLIMIT_FSIZE": FILE_SIZE_LIMIT_BYTES,
            "RLIMIT_CORE": 0,
        },
        "no_new_privs": True,
    }
    return [
        sys.executable,
        "-I",
        "-c",
        _LAUNCHER,
        json.dumps(limits),
        *[str(part) for part in command],
    ]


def scratch_directory() -> str:
    """A private directory for one invocation's temporary files."""
    return tempfile.mkdtemp(prefix="fs-extension-")


def new_run_marker() -> str:
    return uuid.uuid4().hex


def run_environment(env: dict[str, str], scratch: str, marker: str) -> dict[str, str]:
    """The invocation's environment: its scratch directory and its run marker."""
    return {
        **env,
        "TMPDIR": scratch,
        "TEMP": scratch,
        "TMP": scratch,
        RUN_MARKER_ENV: marker,
    }


def remove_scratch(scratch: str) -> None:
    shutil.rmtree(scratch, ignore_errors=True)


def kill_marked_processes(marker: str) -> int:
    """Kill every process still carrying this invocation's marker; how many were found.

    Linux only (``/proc``). A descendant that cleared its own environment is not
    found: that is the documented limit of phase 1.
    """
    proc = "/proc"
    if not marker or not os.path.isdir(proc):
        return 0
    needle = f"{RUN_MARKER_ENV}={marker}".encode()
    found = 0
    for entry in os.listdir(proc):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"{proc}/{entry}/environ", "rb") as handle:
                environ = handle.read()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found += 1
            with contextlib.suppress(OSError):
                os.kill(int(entry), signal.SIGKILL)
    return found


def kill_process_group(pid: int) -> None:
    """Kill every process left in the group the extension started."""
    if not hasattr(os, "killpg"):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL)


__all__ = [
    "CONFINEMENT_FAILED_EXIT",
    "CPU_SECONDS_FLOOR",
    "FILE_SIZE_LIMIT_BYTES",
    "MEMORY_LIMIT_BYTES",
    "RUN_MARKER_ENV",
    "confined_command",
    "cpu_seconds_for",
    "kill_marked_processes",
    "kill_process_group",
    "new_run_marker",
    "remove_scratch",
    "run_environment",
    "scratch_directory",
]
