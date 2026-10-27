"""Kept test output: values a step returns in test runs instead of doing its work.

A kept output is builder test data. It belongs to one step of one workflow, is
stored beside the workflow (never in its definition, revision, history or
export) and changes nothing in a real run. A workflow test run takes a snapshot
of the kept outputs when it is submitted; the step hub then returns those values
for the step instead of dispatching it (``kept_test_output_runtime``).

Keys are the step's output targets, as the builder shows them
(``state.shipment``); values are any JSON. Steps whose result is a decision or a
control-flow effect always run: a kept output could not stand in for them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

KEPT_OUTPUT_MAX_BYTES = 256_000
KEPT_OUTPUT_MAX_KEYS = 50
KEPT_OUTPUT_SOURCE_KINDS = frozenset({"run", "manual"})

# A decision, a wait, a stop, a state write or a loop control is the step's
# whole effect; returning stored values instead would change the run's path.
ALWAYS_RUN_STEP_KINDS = frozenset(
    {
        "if",
        "switch",
        "approval",
        "wait",
        "stop_success",
        "stop_fail",
        "set_variable",
        "batch",
        "break",
        "continue",
        "collect",
    }
)


class KeptTestOutputError(ValueError):
    """A kept output the builder may not store, with the HTTP status to report."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _text(value: object) -> str:
    return str(value or "").strip()


def step_kind_keeps_output(step_kind: str) -> bool:
    """Whether a step of *step_kind* may return a kept output in test runs."""
    return _text(step_kind).lower() not in ALWAYS_RUN_STEP_KINDS


def normalize_kept_outputs(outputs: object) -> dict[str, Any]:
    """The kept values as a JSON object of output targets, or an error naming why not."""
    if not isinstance(outputs, Mapping):
        raise KeptTestOutputError("A kept test output is an object of output values.")
    if not outputs:
        raise KeptTestOutputError("A kept test output needs at least one output value.")
    if len(outputs) > KEPT_OUTPUT_MAX_KEYS:
        raise KeptTestOutputError(
            f"A kept test output holds at most {KEPT_OUTPUT_MAX_KEYS} output values."
        )
    normalized: dict[str, Any] = {}
    for raw_key, value in outputs.items():
        key = _text(raw_key)
        if not key or len(key) > 200:
            raise KeptTestOutputError("Each kept output value needs the output it stands for.")
        normalized[key] = value
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise KeptTestOutputError("A kept test output must be plain JSON.") from exc
    if len(encoded.encode("utf-8")) > KEPT_OUTPUT_MAX_BYTES:
        raise KeptTestOutputError(
            f"A kept test output is at most {KEPT_OUTPUT_MAX_BYTES // 1000} KB of JSON."
        )
    return json.loads(encoded)


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    outputs = row.get("outputs_jsonb")
    if isinstance(outputs, str):
        outputs = json.loads(outputs)
    updated_at = row.get("updated_at")
    return {
        "step_id": _text(row.get("step_id")),
        "outputs": dict(outputs or {}),
        "source_kind": _text(row.get("source_kind")) or "manual",
        "source_job_id": _text(row.get("source_job_id")),
        "updated_by": _text(row.get("updated_by")),
        "updated_at": updated_at.isoformat()
        if isinstance(updated_at, datetime)
        else _text(updated_at),
    }


class WorkflowKeptTestOutputRepository:
    """``workflow_kept_test_outputs``: one row per account, workflow and step."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _cursor(self):
        return self.conn.cursor(row_factory=dict_row)

    def list(self, account_id: str, workflow_id: str) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT step_id, outputs_jsonb, source_kind, source_job_id, updated_by,
                          updated_at
                     FROM workflow_kept_test_outputs
                    WHERE account_id = %s AND workflow_id = %s
                    ORDER BY step_id""",
                (account_id, workflow_id),
            )
            return [_row(row) for row in cur.fetchall()]

    def save(
        self,
        account_id: str,
        workflow_id: str,
        step_id: str,
        *,
        outputs: Mapping[str, Any],
        source_kind: str,
        source_job_id: str,
        updated_by: str,
    ) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_kept_test_outputs
                       (account_id, workflow_id, step_id, outputs_jsonb, source_kind,
                        source_job_id, updated_by, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, now())
                   ON CONFLICT (account_id, workflow_id, step_id) DO UPDATE
                      SET outputs_jsonb = EXCLUDED.outputs_jsonb,
                          source_kind = EXCLUDED.source_kind,
                          source_job_id = EXCLUDED.source_job_id,
                          updated_by = EXCLUDED.updated_by,
                          updated_at = now()
                   RETURNING step_id, outputs_jsonb, source_kind, source_job_id, updated_by,
                             updated_at""",
                (
                    account_id,
                    workflow_id,
                    step_id,
                    json.dumps(dict(outputs), ensure_ascii=False),
                    source_kind,
                    source_job_id,
                    updated_by,
                ),
            )
            return _row(cur.fetchone())

    def delete(self, account_id: str, workflow_id: str, step_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """DELETE FROM workflow_kept_test_outputs
                    WHERE account_id = %s AND workflow_id = %s AND step_id = %s""",
                (account_id, workflow_id, step_id),
            )
            return bool(cur.rowcount)


class WorkflowKeptTestOutputService:
    """Keep, list and drop kept test outputs; the snapshot a test run takes of them."""

    def __init__(self, db: Any, *, actor: str = "") -> None:
        self.db = db
        self.actor = _text(actor)

    def list(self, *, account_id: str, workflow_id: str) -> dict[str, Any]:
        with self.db._transaction() as cur:
            rows = WorkflowKeptTestOutputRepository(cur.connection).list(
                _text(account_id), _text(workflow_id)
            )
        return {"workflow_id": _text(workflow_id), "kept_outputs": rows}

    def keep(
        self,
        *,
        account_id: str,
        workflow_id: str,
        step_id: str,
        step_kind: str,
        outputs: object,
        source_kind: str = "manual",
        source_job_id: str = "",
    ) -> dict[str, Any]:
        normalized_step = _text(step_id)
        if not normalized_step:
            raise KeptTestOutputError("A kept test output belongs to a step.")
        if not step_kind_keeps_output(step_kind):
            raise KeptTestOutputError("This kind of step always runs in tests.", status_code=409)
        kind = _text(source_kind) or "manual"
        if kind not in KEPT_OUTPUT_SOURCE_KINDS:
            raise KeptTestOutputError("A kept test output comes from a run or is written by hand.")
        values = normalize_kept_outputs(outputs)
        with self.db._transaction() as cur:
            return WorkflowKeptTestOutputRepository(cur.connection).save(
                _text(account_id),
                _text(workflow_id),
                normalized_step,
                outputs=values,
                source_kind=kind,
                source_job_id=_text(source_job_id)[:200] if kind == "run" else "",
                updated_by=self.actor,
            )

    def clear(self, *, account_id: str, workflow_id: str, step_id: str) -> dict[str, Any]:
        with self.db._transaction() as cur:
            removed = WorkflowKeptTestOutputRepository(cur.connection).delete(
                _text(account_id), _text(workflow_id), _text(step_id)
            )
        return {"workflow_id": _text(workflow_id), "step_id": _text(step_id), "removed": removed}

    def run_snapshot(
        self, *, account_id: str, workflow_id: str, step_kinds: Mapping[str, str]
    ) -> dict[str, dict[str, Any]]:
        """The kept outputs a test run starts with, for steps the definition still has."""
        listed = self.list(account_id=account_id, workflow_id=workflow_id)["kept_outputs"]
        return kept_outputs_for_run(listed, step_kinds)


def kept_outputs_for_run(
    rows: list[Mapping[str, Any]], step_kinds: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """Kept outputs of steps that exist in the run's definition and may stand in."""
    snapshot: dict[str, dict[str, Any]] = {}
    for row in rows:
        step_id = _text(row.get("step_id"))
        if step_id not in step_kinds or not step_kind_keeps_output(step_kinds[step_id]):
            continue
        outputs = row.get("outputs")
        if isinstance(outputs, Mapping) and outputs:
            snapshot[step_id] = dict(outputs)
    return snapshot


def kept_test_outputs_for_workflow(db: Any, workflow: Mapping[str, Any]) -> dict[str, Any]:
    """The kept-output snapshot a test run of *workflow* (a registry row) starts with."""
    steps = workflow.get("steps")
    step_kinds = {
        _text(step.get("step_id")): _text(step.get("step_kind")) or "agent"
        for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, Mapping) and _text(step.get("step_id"))
    }
    if not step_kinds or not callable(getattr(db, "_transaction", None)):
        return {}
    return WorkflowKeptTestOutputService(db).run_snapshot(
        account_id=_text(workflow.get("account_id")),
        workflow_id=_text(workflow.get("workflow_id")),
        step_kinds=step_kinds,
    )


__all__ = [
    "ALWAYS_RUN_STEP_KINDS",
    "KEPT_OUTPUT_MAX_BYTES",
    "KeptTestOutputError",
    "WorkflowKeptTestOutputRepository",
    "WorkflowKeptTestOutputService",
    "kept_outputs_for_run",
    "kept_test_outputs_for_workflow",
    "normalize_kept_outputs",
    "step_kind_keeps_output",
]
