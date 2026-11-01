#!/usr/bin/env python3
"""Convert local n8n JSON into a disabled YAML candidate; never contact a provider."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _write_new(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--object-response-node", type=int, action="append", default=[])
    parser.add_argument("--parallel-fanout", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    destinations = [arguments.output] + ([arguments.report] if arguments.report else [])
    if len({path.resolve() for path in destinations}) != len(destinations) or any(
        path.exists() or path.is_symlink() or not path.parent.is_dir() for path in destinations
    ):
        parser.exit(2, "Use distinct new output files in existing directories.\n")
    # Explicit local helper root also permits Python's isolated (-I) mode.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from workflow_migration.n8n import convert_n8n
    except ModuleNotFoundError as error:
        if error.name != "yaml":
            raise
        parser.exit(2, "The converter requires PyYAML 6.x in this Python environment.\n")
    try:
        with arguments.source.open(encoding="utf-8") as stream:
            source = stream.read(1_000_001)
    except (OSError, UnicodeError):
        parser.exit(2, "Cannot read the source as UTF-8 JSON.\n")
    result = convert_n8n(
        source,
        workflow_id=arguments.workflow_id,
        object_response_nodes=arguments.object_response_node,
        parallel_fanout=arguments.parallel_fanout,
    )
    report = {key: value for key, value in result.items() if key != "bundle_yaml"}
    report_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    try:
        if result["bundle_yaml"] is not None:
            _write_new(arguments.output, result["bundle_yaml"])
        if arguments.report:
            _write_new(arguments.report, report_text)
        else:
            print(report_text, end="")
    except OSError:
        parser.exit(
            2, "Could not create a new output file; inspect destinations before retrying.\n"
        )
    return 0 if result["status"] == "converted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
