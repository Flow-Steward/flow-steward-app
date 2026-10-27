from __future__ import annotations

import json
import sys

import pytest

from scripts.release.reconcile_orphan_project_queues import (
    QueueReconciliationAmbiguity,
    plan_orphan_queue_reconciliation,
)

PROJECT = "00000000-0000-0000-0000-000000000123"


def test_reconciliation_selects_only_a_complete_zero_depth_pair_for_an_absent_project() -> None:
    plan = plan_orphan_queue_reconciliation(
        [
            {"name": f"platform.project.{PROJECT}", "messages": 0, "consumers": 0},
            {
                "name": f"platform.project.{PROJECT}.dead_letter",
                "messages": 0,
                "consumers": 0,
            },
            {"name": "platform.project.ready", "messages": 0, "consumers": 1},
        ],
        existing_project_ids=set(),
    )

    assert plan == {
        PROJECT: [
            f"platform.project.{PROJECT}",
            f"platform.project.{PROJECT}.dead_letter",
        ]
    }


@pytest.mark.parametrize(
    "rows",
    [
        [{"name": f"platform.project.{PROJECT}", "messages": 0, "consumers": 0}],
        [
            {"name": f"platform.project.{PROJECT}", "messages": 1, "consumers": 0},
            {
                "name": f"platform.project.{PROJECT}.dead_letter",
                "messages": 0,
                "consumers": 0,
            },
        ],
        [
            {"name": f"platform.project.{PROJECT}", "messages": 0, "consumers": 1},
            {
                "name": f"platform.project.{PROJECT}.dead_letter",
                "messages": 0,
                "consumers": 0,
            },
        ],
    ],
)
def test_reconciliation_aborts_on_an_incomplete_nonempty_or_consumed_orphan_pair(rows) -> None:
    with pytest.raises(QueueReconciliationAmbiguity):
        plan_orphan_queue_reconciliation(rows, existing_project_ids=set())


def test_ambiguous_dry_run_retains_the_before_inventory(monkeypatch, capsys) -> None:
    from scripts.release import reconcile_orphan_project_queues as reconciliation

    rows = [
        {"name": f"platform.project.{PROJECT}", "messages": 1, "consumers": 0},
        {
            "name": f"platform.project.{PROJECT}.dead_letter",
            "messages": 0,
            "consumers": 0,
        },
    ]
    monkeypatch.setattr(reconciliation, "_queue_inventory", lambda *_args: rows)
    monkeypatch.setattr(reconciliation, "_project_ids", lambda *_args: set())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reconcile_orphan_project_queues.py",
            "--postgres-container",
            "candidate-pg",
            "--rabbitmq-container",
            "candidate-rabbit",
        ],
    )

    assert reconciliation.main() == 2
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["status"] == "ambiguous"
    assert evidence["before"] == rows
    assert evidence["after"] == rows
