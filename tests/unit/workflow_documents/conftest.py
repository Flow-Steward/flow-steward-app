"""Frozen workflow report fixtures (schema v1 section 10)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "workflow_reports"


def _load(name: str) -> Any:
    def reject(token: str) -> Any:
        raise ValueError(f"non-finite JSON token {token}")

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"), parse_constant=reject)


@pytest.fixture
def definition() -> dict[str, Any]:
    return copy.deepcopy(_load("sales_report_definition.json"))


@pytest.fixture
def report_inputs() -> dict[str, Any]:
    return copy.deepcopy(_load("sales_report_inputs.json"))


@pytest.fixture
def declared(report_inputs: dict[str, Any]) -> list[dict[str, Any]]:
    return report_inputs["declared_inputs"]


@pytest.fixture
def values(report_inputs: dict[str, Any]) -> dict[str, Any]:
    return report_inputs["values"]


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return copy.deepcopy(_load("sales_report_snapshot.json"))
