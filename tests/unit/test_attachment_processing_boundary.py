"""Contracts for extension-owned attachment processing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.application.orchestration.agents.common_runtime_helpers import build_context_block
from core.application.orchestration.state import OrchestratorState, OrchestratorStateModel
from core.application.workflows.workflow_entrypoint_submission import _staged_file_descriptors

ROOT = Path(__file__).resolve().parents[2]


def test_orchestrator_state_keeps_descriptors_without_derived_attachment_context() -> None:
    assert "attachments" in OrchestratorState.__annotations__
    assert "attachments" in OrchestratorStateModel.model_fields
    assert "attachment_context" not in OrchestratorState.__annotations__
    assert "attachment_context" not in OrchestratorStateModel.model_fields


def test_historical_attachment_context_is_ignored_by_prompt_construction() -> None:
    context = build_context_block(
        {
            "attachment_context": "legacy hidden parse",
            "project_config": {"stack": {}, "project_rules": []},
        },
        sections=["attachment_context"],
    )

    assert "legacy hidden parse" not in context
    assert "Uploaded Attachments" not in context


def test_staged_file_descriptors_preserve_public_attachment_metadata() -> None:
    descriptor = _staged_file_descriptors(
        [
            SimpleNamespace(
                filename="inventory.csv",
                mime_type="text/csv",
                size_bytes=15,
                sha256="a" * 64,
            )
        ]
    )

    assert descriptor == [
        {
            "source_ref": "multipart:files",
            "filename": "inventory.csv",
            "content_type": "text/csv",
            "size_bytes": 15,
            "sha256": "a" * 64,
        }
    ]


def test_runtime_dependencies_exclude_universal_parsers_but_keep_tabular_formats() -> None:
    runtime_requirements = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    dependency_text = "\n".join(
        (ROOT / filename).read_text(encoding="utf-8").lower()
        for filename in (
            "requirements-runtime.txt",
            "requirements-runtime.lock",
            "requirements-cli.lock",
        )
    )

    for removed in ("pdfplumber", "pdfminer-six", "python-docx"):
        assert removed not in dependency_text
    assert "openpyxl==" in runtime_requirements
    assert "polars==" in runtime_requirements


def test_registry_seed_has_no_implicit_attachment_prompt_source() -> None:
    seed = json.loads((ROOT / "db/seeds/registry_seed.json").read_text(encoding="utf-8"))
    pending: list[object] = [seed]
    attachment_sources: list[dict[str, object]] = []
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if current.get("source") == "attachment_context":
                attachment_sources.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)

    assert attachment_sources == []
