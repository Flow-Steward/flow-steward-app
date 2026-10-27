"""Private fixed read-only product probes for the trusted host management runtime."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from core.infrastructure.runtime_env import runtime_env

# Capture before importing any package that could initialize repository configuration.
_PROCESS_ENVIRONMENT = runtime_env.copy()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in (["count"], ["status"]):
        return 2
    try:
        from core.application.auth.bootstrap_queries import (
            bootstrap_runtime_status,
            bootstrap_user_count,
        )

        if arguments == ["status"]:
            payload = bootstrap_runtime_status(_PROCESS_ENVIRONMENT)
        else:
            payload = bootstrap_user_count(_PROCESS_ENVIRONMENT)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:
        # Private transport errors never serialize DB errors, credentials or environment.
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
