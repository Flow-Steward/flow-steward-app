"""Infrastructure adapter for process-table inspection and termination."""

from __future__ import annotations

import os
import signal
import time


def current_pid() -> int:
    return os.getpid()


def process_exists(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
        cmdline = raw.decode("utf-8", errors="replace").replace("\0", " ").strip()
        if cmdline:
            return cmdline
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def read_ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
        rparen = stat.rfind(")")
        if rparen == -1:
            return 0
        rest = stat[rparen + 2 :].split()
        return int(rest[1]) if len(rest) > 1 else 0
    except Exception:
        return 0


def read_rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm", encoding="utf-8", errors="replace") as fh:
            statm = fh.read().split()
        return round(int(statm[1]) * 4096 / 1024 / 1024, 1)
    except Exception:
        return 0.0


def build_proc_index() -> dict[int, dict]:
    index: dict[int, dict] = {}
    try:
        entries = os.listdir("/proc")
    except FileNotFoundError:
        return index

    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        index[pid] = {
            "pid": pid,
            "ppid": read_ppid(pid),
            "cmdline": read_cmdline(pid),
            "rss_mb": read_rss_mb(pid),
        }
    return index


def terminate_pid(pid: int) -> str:
    os.kill(pid, signal.SIGTERM)

    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "SIGTERM"

    try:
        os.kill(pid, signal.SIGKILL)
        return "SIGKILL"
    except ProcessLookupError:
        return "SIGTERM"
