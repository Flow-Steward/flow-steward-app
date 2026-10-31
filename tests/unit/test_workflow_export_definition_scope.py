"""A workflow export's definitions stay within what the exporter may see."""

from __future__ import annotations

from typing import Any

from core.application.workflows.workflow_portability_artifacts import (
    WorkflowPortabilityArtifactsMixin,
)
from core.application.workflows.workflow_portability_common import _redact_definitions


class _GuardrailDb:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def list_guardrails(self, *, account_id: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["account_id"] == account_id]

    def get_project(self, project_id: str) -> dict[str, Any]:
        return {"name": f"Project {project_id}"}


def _exporter(rows: list[dict[str, Any]]) -> WorkflowPortabilityArtifactsMixin:
    exporter = WorkflowPortabilityArtifactsMixin.__new__(WorkflowPortabilityArtifactsMixin)
    exporter.db = _GuardrailDb(rows)
    return exporter


def _rule(rule_id: str, project_id: str | None) -> dict[str, Any]:
    return {
        "id": rule_id,
        "account_id": "acct",
        "project_id": project_id,
        "_agents": ["agent-1"],
        "prompt_text": f"prompt of {rule_id}",
    }


def test_restricted_export_leaves_out_other_projects_guardrails() -> None:
    exporter = _exporter([_rule("global", None), _rule("own", "p-a"), _rule("other", "p-b")])

    restricted = exporter._export_guardrails(
        {"agent-1"}, account_id="acct", allowed_project_ids=frozenset({"p-a"})
    )
    unrestricted = exporter._export_guardrails({"agent-1"}, account_id="acct")

    assert [rule["id"] for rule in restricted] == ["global", "own"]
    assert [rule["id"] for rule in unrestricted] == ["global", "own", "other"]


def test_credential_field_defaults_are_masked_inside_nested_schemas() -> None:
    schema = {
        "type": "object",
        "properties": {
            "password": {
                "allOf": [{"type": "string", "default": "private-schema-value-8271"}],
                "items": {"examples": ["nested-secret"]},
            },
            "city": {"allOf": [{"default": "Riga"}]},
        },
    }

    redacted = _redact_definitions(schema)

    password = redacted["properties"]["password"]
    assert password["allOf"][0] == {"type": "string", "default": "[redacted]"}
    assert password["items"] == {"examples": "[redacted]"}
    assert redacted["properties"]["city"] == {"allOf": [{"default": "Riga"}]}
