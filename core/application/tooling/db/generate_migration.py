"""CLI entrypoint: scaffold a new post-1.0 SQL migration file.

Usage:
    flow-steward database generate-migration add_status_index

Creates ``db/migrations/`` (if missing) and writes a timestamped, SQL-only
migration template named ``YYYYMMDDHHMMSS_slug.sql``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from core.application.tooling.db import migration_runner


def generate_migration_file(
    *,
    name: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Create one SQL migration file and return structured metadata."""
    path = migration_runner.create_migration_file(
        name,
        root=Path(root) if root is not None else None,
    )
    return {
        "path": str(path),
        "filename": path.name,
        "root": str(Path(root)) if root is not None else str(migration_runner.REPO_ROOT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a timestamped SQL migration file under db/migrations/ "
            "(YYYYMMDDHHMMSS_slug.sql) for post-1.0 database changes."
        )
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Short description of the change, e.g. add_status_index.",
    )
    args = parser.parse_args(argv)

    result = generate_migration_file(name=args.name)
    print(f"[OK] Created migration: {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
