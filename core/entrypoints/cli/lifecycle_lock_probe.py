"""Tell the launcher whether a lifecycle operation is alive in this CLI runtime.

AC17-R156: `config apply` rewrites the env file the retained fs-cli was started from,
so the next launcher found the runtime stale and recreated it -- killing the apply that
was running inside. The lifecycle lock is a flock held inside this container, so only
a process here can see it. Exit 0 and print the operation id while one holds it; exit 3
otherwise.
"""

from __future__ import annotations

from pathlib import Path

from core.application.lifecycle.contracts import LifecycleLockStatus
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_IMAGE_ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    root = Path(str(runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or _IMAGE_ROOT)).resolve()
    lock = LifecycleStateStore(root).diagnose_active_lock()
    if lock.status is LifecycleLockStatus.ACTIVE:
        print(lock.operation_id or "unknown")
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
