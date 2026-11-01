#!/usr/bin/env python3
"""CLI adapter for official release workflow safety checks."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from core.application.tooling.release_automation import (  # noqa: E402
    preflight_registry,
    registry_namespaces,
    release_identity,
    require_complete_release,
)
from core.infrastructure.runtime_env import runtime_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("identity", "preflight", "evidence"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    try:
        version, revision = release_identity(ROOT, args.tag)
        if args.action == "identity":
            print(f"version={version}\nrevision={revision}")
            return 0
        namespaces = registry_namespaces(
            runtime_env.get("FS_RELEASE_GHCR_NAMESPACE", ""),
            runtime_env.get("FS_RELEASE_DOCKERHUB_NAMESPACE", ""),
        )
        if args.action == "preflight":
            if args.artifact_dir is None:
                parser.error("preflight requires --artifact-dir")
            preflight_registry(args.artifact_dir, version, revision, namespaces)
        else:
            if args.evidence is None:
                parser.error("evidence requires --evidence")
            require_complete_release(
                args.evidence, version=version, revision=revision, namespaces=namespaces
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Release automation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
