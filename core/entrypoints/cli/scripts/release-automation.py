#!/usr/bin/env python3
"""CLI adapter for official release workflow safety checks."""

from __future__ import annotations

import argparse
import json
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
    stable_version,
)
from core.application.tooling.release_notes import (  # noqa: E402
    prepare_release_notes,
    previous_published_tag,
    select_publication_notes,
)
from core.infrastructure.runtime_env import runtime_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("identity", "notes", "preflight", "evidence"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--preflight-output", type=Path)
    parser.add_argument("--previous-tag")
    parser.add_argument("--release-history", type=Path)
    parser.add_argument("--expected-notes-sha256")
    parser.add_argument("--existing-body", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "notes":
            if (args.root / "VERSION").read_text().strip() != stable_version(args.tag):
                raise ValueError("Release notes tag differs from VERSION")
            if args.release_history is not None:
                history = json.loads(args.release_history.read_text(encoding="utf-8"))
                previous = previous_published_tag(history, args.tag)
            elif args.previous_tag:
                previous = args.previous_tag
            else:
                parser.error("notes requires --release-history or --previous-tag")
            notes = prepare_release_notes(
                args.root,
                args.tag,
                previous,
                expected_sha256=args.expected_notes_sha256,
            )
            existing = args.existing_body.read_text(encoding="utf-8") if args.existing_body else ""
            print(
                json.dumps(
                    {
                        "previous_tag": previous,
                        "notes_sha256": notes.sha256,
                        "body": select_publication_notes(notes, existing),
                    }
                )
            )
            return 0
        version, revision = release_identity(args.root.resolve(), args.tag)
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
            available = preflight_registry(args.artifact_dir, version, revision, namespaces)
            if args.preflight_output is not None:
                args.preflight_output.write_text(
                    json.dumps({"available": available}) + "\n", encoding="utf-8"
                )
        else:
            if args.evidence is None:
                parser.error("evidence requires --evidence")
            require_complete_release(
                args.evidence,
                version=version,
                revision=revision,
                namespaces=namespaces,
                artifact_dir=args.artifact_dir,
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Release automation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
