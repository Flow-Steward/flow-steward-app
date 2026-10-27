"""Capture allowlisted Compact trees through a root-only volume helper."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from core.application.clone.contracts import CloneDataClass, CloneEntry
from core.infrastructure.clone.capture_policy import clone_ignored_names
from core.infrastructure.clone.volume_metadata import inventory_volume_metadata

_SOURCE = Path("/source")
_CAPTURE = Path("/capture")
_ALLOWED_ROOTS = {
    CloneDataClass.LOCAL_ARTIFACTS: "artifacts",
    CloneDataClass.EXTENSIONS: "extensions",
    CloneDataClass.DURABLE_STATE: "state",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--durable-state-only", action="store_true")
    args = parser.parse_args()
    raw = json.load(sys.stdin)
    if not isinstance(raw, list):
        raise ValueError("Compact Clone capture request must be a list")
    requested: list[tuple[CloneDataClass, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Compact Clone capture request is invalid")
        data_class = CloneDataClass(str(item.get("data_class") or ""))
        archive_root = str(item.get("archive_root") or "")
        if _ALLOWED_ROOTS.get(data_class) != archive_root or archive_root in seen:
            raise ValueError("Compact Clone capture root is not allowlisted")
        seen.add(archive_root)
        requested.append((data_class, archive_root))

    if args.durable_state_only and requested != [(CloneDataClass.DURABLE_STATE, "state")]:
        raise ValueError("Compact mixed-placement capture requires state-only input")

    entries: list[CloneEntry] = []
    for data_class, archive_root in requested:
        source = _SOURCE / archive_root
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Compact Clone source root must be a real directory")
        if args.durable_state_only:
            source = source / ".flow-steward"
            if source.is_symlink() or not source.is_dir():
                raise ValueError("Compact Clone durable source must be a real directory")
        ignored = clone_ignored_names(data_class)
        captured = inventory_volume_metadata(
            source,
            archive_root=archive_root,
            data_class=data_class,
            ignored_names=ignored,
        )
        if args.validate_only:
            continue
        entries.extend(captured)
        shutil.copytree(
            source,
            _CAPTURE / archive_root,
            symlinks=True,
            ignore=lambda _directory, names, excluded=ignored: [
                name for name in names if name in excluded
            ],
        )
    json.dump([entry.model_dump(mode="json") for entry in entries], sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
