"""Record a vitest run for impacted-test selection (ADR 0068).

Usage: ``python -m ...impact_vitest_result REPORT FRONTEND_DIR EXIT_STATUS OUTPUT_DIR``.
Reads vitest's JSON report and writes the ``result.json`` that
``impact_map.read_recorded_run`` reads: the exit status and the test files that
failed, relative to the frontend workspace, so a later selection reruns them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.application.tooling.quality.impact_recorder import RESULT_FILE


def failed_test_files(report: dict[str, object], frontend_dir: Path) -> list[str] | None:
    results = report.get("testResults")
    if not isinstance(results, list):
        return None
    failed: set[str] = set()
    root = frontend_dir.resolve()
    for result in results:
        if not isinstance(result, dict) or result.get("status") == "passed":
            continue
        try:
            failed.add(Path(str(result.get("name", ""))).resolve().relative_to(root).as_posix())
        except ValueError:
            return None  # A failing file outside the workspace cannot be named.
    return sorted(failed)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 4:
        print(
            "usage: impact_vitest_result REPORT FRONTEND_DIR EXIT_STATUS OUTPUT_DIR",
            file=sys.stderr,
        )
        return 2
    report_path, frontend_dir, exit_status, output_dir = arguments
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    failed = failed_test_files(report, Path(frontend_dir)) if isinstance(report, dict) else None
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "exitstatus": int(exit_status),
        "failed": failed or [],
        "maps": False,
        "recorded": failed is not None,
    }
    (output / RESULT_FILE).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
