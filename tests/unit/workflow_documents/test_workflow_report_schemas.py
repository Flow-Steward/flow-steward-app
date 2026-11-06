"""The package schemas are the published contract, not a second copy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.application.workflow_documents.report_schemas_v1 import (
    DEFINITION_SCHEMA_V1,
    SNAPSHOT_SCHEMA_V1,
)

DOC = Path(__file__).resolve().parents[3] / "docs" / "architecture" / "workflow-report-schema-v1.md"


def _doc_block(heading: str) -> dict:
    text = DOC.read_text(encoding="utf-8")
    match = re.compile(r"```json\n(.*?)\n```", re.S).search(text, text.index(heading))
    assert match is not None
    return json.loads(match.group(1))


@pytest.mark.parametrize(
    ("heading", "schema"),
    [
        ("### 9.1 ReportDefinition v1", DEFINITION_SCHEMA_V1),
        ("### 9.2 ReportSnapshot v1", SNAPSHOT_SCHEMA_V1),
    ],
)
def test_package_schema_matches_the_published_contract(heading: str, schema: dict) -> None:
    assert schema == _doc_block(heading)
    Draft202012Validator.check_schema(schema)


def test_every_object_in_both_schemas_is_closed() -> None:
    def walk(node: object) -> list[dict]:
        found: list[dict] = []
        if isinstance(node, dict):
            if node.get("type") == "object":
                found.append(node)
            for value in node.values():
                found.extend(walk(value))
        elif isinstance(node, list):
            for value in node:
                found.extend(walk(value))
        return found

    objects = walk(DEFINITION_SCHEMA_V1) + walk(SNAPSHOT_SCHEMA_V1)
    assert objects
    assert all(item.get("additionalProperties") is False for item in objects)
