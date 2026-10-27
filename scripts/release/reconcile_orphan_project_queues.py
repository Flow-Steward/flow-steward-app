#!/usr/bin/env python3
"""Reconcile zero-depth RabbitMQ queue pairs whose project no longer exists."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PROJECT_QUEUE = re.compile(
    r"^platform\.project\."
    r"(?P<project>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?P<dead_letter>\.dead_letter)?$"
)
CONFIRMATION = "DELETE_ORPHAN_PROJECT_QUEUES"


class QueueReconciliationAmbiguity(RuntimeError):
    """Raised when an orphan queue cannot be deleted without guessing."""


def plan_orphan_queue_reconciliation(
    queue_rows: list[dict[str, Any]],
    *,
    existing_project_ids: set[str],
) -> dict[str, list[str]]:
    """Return exact orphan queue pairs, aborting if any absent project is ambiguous."""
    existing = {str(project_id).strip().lower() for project_id in existing_project_ids}
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in queue_rows:
        name = str(row.get("name") or "").strip()
        match = PROJECT_QUEUE.fullmatch(name)
        if match is None:
            continue
        project_id = match.group("project").lower()
        kind = "dead_letter" if match.group("dead_letter") else "primary"
        if kind in grouped.setdefault(project_id, {}):
            raise QueueReconciliationAmbiguity(
                f"duplicate {kind} queue inventory for project {project_id}"
            )
        grouped[project_id][kind] = {
            "name": name,
            "messages": int(row.get("messages") or 0),
            "consumers": int(row.get("consumers") or 0),
        }

    plan: dict[str, list[str]] = {}
    ambiguous: list[str] = []
    for project_id, pair in sorted(grouped.items()):
        if project_id in existing:
            continue
        if set(pair) != {"primary", "dead_letter"}:
            ambiguous.append(f"{project_id}:incomplete_pair={sorted(pair)}")
            continue
        nonzero = [
            row["name"] for row in pair.values() if row["messages"] != 0 or row["consumers"] != 0
        ]
        if nonzero:
            ambiguous.append(f"{project_id}:nonzero_or_consumed={sorted(nonzero)}")
            continue
        plan[project_id] = [pair["primary"]["name"], pair["dead_letter"]["name"]]
    if ambiguous:
        raise QueueReconciliationAmbiguity("; ".join(ambiguous))
    return plan


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"docker {' '.join(args)} failed")
    return result.stdout


def _queue_inventory(rabbitmq_container: str) -> list[dict[str, Any]]:
    output = _docker(
        "exec",
        rabbitmq_container,
        "rabbitmqctl",
        "list_queues",
        "--quiet",
        "name",
        "messages",
        "consumers",
    )
    rows: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        fields = raw_line.split("\t")
        if len(fields) != 3 or PROJECT_QUEUE.fullmatch(fields[0].strip()) is None:
            continue
        try:
            rows.append(
                {
                    "name": fields[0].strip(),
                    "messages": int(fields[1]),
                    "consumers": int(fields[2]),
                }
            )
        except ValueError as exc:
            raise RuntimeError(f"invalid RabbitMQ queue inventory line: {raw_line!r}") from exc
    return rows


def _project_ids(postgres_container: str) -> set[str]:
    output = _docker(
        "exec",
        postgres_container,
        "sh",
        "-lc",
        'exec psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        "-c 'select id::text from projects order by id'",
    )
    return {line.strip().lower() for line in output.splitlines() if line.strip()}


def _delete_queue(rabbitmq_container: str, queue_name: str) -> None:
    _docker(
        "exec",
        rabbitmq_container,
        "rabbitmqctl",
        "delete_queue",
        "--if-empty",
        "--if-unused",
        queue_name,
    )


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--rabbitmq-container", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")

    evidence: dict[str, Any] = {
        "schema_version": "flow-steward-orphan-project-queue-reconciliation-v1",
        "apply": bool(args.apply),
        "postgres_container": args.postgres_container,
        "rabbitmq_container": args.rabbitmq_container,
        "before": [],
        "planned": {},
        "deleted": [],
        "after": [],
        "status": "failed",
    }
    try:
        before = _queue_inventory(args.rabbitmq_container)
        projects = _project_ids(args.postgres_container)
        evidence["before"] = before
        plan = plan_orphan_queue_reconciliation(before, existing_project_ids=projects)
        evidence["planned"] = plan
        if args.apply:
            for queue_names in plan.values():
                for queue_name in queue_names:
                    _delete_queue(args.rabbitmq_container, queue_name)
                    evidence["deleted"].append(queue_name)
        after = _queue_inventory(args.rabbitmq_container)
        evidence["after"] = after
        remaining = {
            row["name"] for row in after if any(row["name"] in names for names in plan.values())
        }
        if args.apply and remaining:
            raise RuntimeError(f"planned queues remain after reconciliation: {sorted(remaining)}")
        evidence["status"] = "applied" if args.apply else "planned"
    except QueueReconciliationAmbiguity as exc:
        evidence["status"] = "ambiguous"
        evidence["error"] = str(exc)
        evidence["after"] = list(evidence["before"])
        if args.evidence:
            _write_evidence(args.evidence, evidence)
        print(json.dumps(evidence, sort_keys=True))
        return 2
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        evidence["error"] = str(exc)
        if args.evidence:
            _write_evidence(args.evidence, evidence)
        print(json.dumps(evidence, sort_keys=True))
        return 1

    if args.evidence:
        _write_evidence(args.evidence, evidence)
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
