"""Narrow one pytest partition to the modules of a selection plan (ADR 0068).

Usage: ``python -m ...impact_plan_filter PLAN SELECTION...`` where SELECTION is
the partition's pytest arguments: module paths, or directories with
``--ignore=`` exclusions. Prints ``@full`` for a whole-suite plan, otherwise
the planned modules inside the partition, one per line.
"""

from __future__ import annotations

import json
import sys
from pathlib import PurePosixPath

FULL = "@full"


def planned_partition(plan: dict[str, object], selection: list[str]) -> list[str] | None:
    if plan.get("full") is not False:
        return None
    modules = plan.get("modules")
    if not isinstance(modules, list):
        return None
    ignored = [
        PurePosixPath(item.removeprefix("--ignore="))
        for item in selection
        if item.startswith("--ignore=")
    ]
    roots = [PurePosixPath(item) for item in selection if not item.startswith("-")]

    def inside(module: str) -> bool:
        path = PurePosixPath(module)
        within = any(path == root or root in path.parents for root in roots)
        return within and not any(path == item or item in path.parents for item in ignored)

    return sorted(str(module) for module in modules if inside(str(module)))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        print("usage: impact_plan_filter PLAN SELECTION...", file=sys.stderr)
        return 2
    with open(arguments[0], encoding="utf-8") as handle:
        plan = json.load(handle)
    planned = planned_partition(plan, arguments[1:])
    print(FULL if planned is None else "\n".join(planned))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
