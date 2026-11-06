"""A child workflow's report reaches the parent only from its validated final contract."""

from __future__ import annotations

from typing import Any

from core.application.orchestration.invoke_workflow_step_node_results import (
    _collect_child_outputs,
)

REPORT = {"kind": "fs.report", "schema_version": 1, "title": "Child report"}


class _Logger:
    def __init__(self, contract: dict[str, Any]) -> None:
        self._contract = contract

    def get_job(self, _job_id: str) -> dict[str, Any]:
        return {"meta": {"workflow_outputs": self._contract}}

    def get_agent_messages(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "envelope": {
                    "artifacts": {
                        "sales_report": {"forged": True},
                        "note": "from a message",
                    }
                }
            }
        ]


def _contract(*, ok: bool) -> dict[str, Any]:
    return {
        "ok": ok,
        "outputs": {"sales_report": REPORT, "note": "final"},
        "interface": {"enabled": True, "report_output": "sales_report"},
    }


def test_a_validated_child_forwards_its_report() -> None:
    resolved = _collect_child_outputs(_Logger(_contract(ok=True)), "7", ["sales_report", "note"])
    assert resolved == {"sales_report": REPORT, "note": "final"}


def test_fallbacks_never_supply_the_child_report() -> None:
    resolved = _collect_child_outputs(_Logger(_contract(ok=False)), "7", ["sales_report", "note"])
    assert "sales_report" not in resolved
    assert resolved["note"] == "from a message"
