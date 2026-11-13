"""An extension skill reaches the agent runtime with its declared inputs and outputs.

A skill shipped in an extension bundle declares `expected_inputs` and
`expected_outputs`. The runtime builds the assigned skill's manifest from the
projected bundle row, and `SkillManifest.from_db_row` reads declared I/O from
`_inputs` / `_outputs`, as a registry skill row carries them. The projected row
had neither, so the phase-result schema allowed no artifacts and the platform
refused the model's answer ("Additional properties are not allowed").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.orchestration.models import build_phase_result_json_schema
from core.application.registry.db_store_contracts_mixin import RegistryDBStoreContractsMixin
from core.application.registry.db_store_projected_skills_mixin import (
    projected_skill_contract_rows,
)
from core.application.registry.projected_artifact_rows import (
    projected_artifact_rows,
    projection_aware_catalog_row,
)
from core.application.registry.skill_manifest import SkillManifest
from core.application.registry.step_output_contract import resolve_step_output_contract

SKILL_ID = "extension.install-a.summarize_report"
PROMPT_TEMPLATE = "Summarize the report for {{ audience }}."


def _bundle_skill(**overrides: object) -> dict[str, object]:
    skill: dict[str, object] = {
        "skill_id": "summarize_report",
        "display_name": "Summarize report",
        "instructions": ["Write one section per heading."],
        "prompt_template": PROMPT_TEMPLATE,
        "expected_inputs": [
            {"name": "audience", "type": "text", "required": True, "description": "Audience."},
            {"name": "tone"},
            {"description": "nameless entries are not declarations"},
        ],
        "expected_outputs": [
            {
                "name": "sections",
                "type": "array",
                "required": True,
                "description": "One summary per report heading.",
            },
            {"name": "notes", "type": "text", "required": False, "description": "Notes."},
        ],
    }
    skill.update(overrides)
    return skill


@pytest.fixture
def projected_store(monkeypatch):
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.active_extension_ids_for_account",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.active_extension_versions_for_account",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "core.application.registry.projected_artifact_rows.resolve_projection_dependency_availability",
        lambda *_args, **_kwargs: {"new_runs_blocked": False},
    )

    def _store(skill: dict[str, object]) -> SimpleNamespace:
        projection = {
            "install_id": "install-a",
            "release_id": "release-a",
            "artifact_id": "summarize_report",
            "artifact_kind": "skill",
            "projection": skill,
        }
        return SimpleNamespace(
            extension_runtime_store=SimpleNamespace(list_projections=lambda **_kwargs: [projection])
        )

    return _store


def _anchor_row() -> dict[str, object]:
    """The install-owned registry anchor: declared I/O, no executable content."""
    outputs = projected_skill_contract_rows(
        _bundle_skill()["expected_outputs"], default_required=True
    )
    return {
        "skill_id": SKILL_ID,
        "display_name": "Summarize report",
        "source_install_id": "install-a",
        "prompt_template": "",
        "_outputs": outputs,
        "output_schema": RegistryDBStoreContractsMixin._build_contract_schema(outputs),
    }


def test_projected_skill_row_carries_declared_io_as_registry_rows(projected_store) -> None:
    (row,) = projected_artifact_rows(projected_store(_bundle_skill()), "skill", "skill_id")

    assert row["_inputs"] == [
        {"name": "audience", "type": "text", "description": "Audience.", "required": True},
        {"name": "tone", "type": "text", "description": "", "required": False},
    ]
    assert row["_outputs"] == [
        {
            "name": "sections",
            "type": "array",
            "description": "One summary per report heading.",
            "required": True,
        },
        {"name": "notes", "type": "text", "description": "Notes.", "required": False},
    ]
    assert row["output_schema"]["required"] == ["sections"]
    assert set(row["output_schema"]["properties"]) == {"sections", "notes"}
    assert row["input_schema"]["required"] == ["audience"]


def test_projected_skill_keeps_an_authored_output_schema(projected_store) -> None:
    authored = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"sections_json": {"type": "string"}},
        "required": ["sections_json"],
    }
    (row,) = projected_artifact_rows(
        projected_store(_bundle_skill(output_schema=authored)), "skill", "skill_id"
    )

    assert row["output_schema"] == authored
    assert [output["name"] for output in row["_outputs"]] == ["sections", "notes"]


def test_assigned_projected_skill_manifest_has_declared_io_and_prompt_template(
    projected_store,
) -> None:
    """The run-time path: the projection replaces the anchor row, then a manifest is built."""
    row = projection_aware_catalog_row(
        projected_store(_bundle_skill()),
        "skill",
        "skill_id",
        SKILL_ID,
        _anchor_row(),
        account_id=DEFAULT_ACCOUNT_ID,
    )
    manifest = SkillManifest.from_db_row(row)

    assert [(o["name"], o["type"], o["required"]) for o in manifest.expected_outputs] == [
        ("sections", "array", True),
        ("notes", "text", False),
    ]
    assert [(i["name"], i["required"]) for i in manifest.expected_inputs] == [
        ("audience", True),
        ("tone", False),
    ]
    assert manifest.prompt_template == PROMPT_TEMPLATE
    assert manifest.instructions == ["Write one section per heading."]


def test_phase_result_schema_of_a_projected_skill_has_its_array_and_string_outputs(
    projected_store,
) -> None:
    row = projection_aware_catalog_row(
        projected_store(_bundle_skill()),
        "skill",
        "skill_id",
        SKILL_ID,
        _anchor_row(),
        account_id=DEFAULT_ACCOUNT_ID,
    )
    manifest = SkillManifest.from_db_row(row)
    db = SimpleNamespace(
        get_skill=lambda _skill_id: _anchor_row(),
        _build_contract_schema=RegistryDBStoreContractsMixin._build_contract_schema,
    )

    contract = resolve_step_output_contract(
        db,
        agent_id="",
        skill_id=SKILL_ID,
        account_id=DEFAULT_ACCOUNT_ID,
        skill_manifest=manifest,
    )
    artifacts = build_phase_result_json_schema(contract.rows)["properties"]["artifacts"]

    assert artifacts["additionalProperties"] is False
    assert sorted(artifacts["required"]) == ["notes", "sections"]
    assert artifacts["properties"]["sections"]["type"] == "array"
    assert artifacts["properties"]["notes"]["anyOf"][0]["type"] == "string"
    validator = Draft202012Validator(artifacts)
    assert validator.is_valid({"sections": ["Overview"], "notes": "Built."})
    assert validator.is_valid({"sections": ["Overview"], "notes": None})
    assert not validator.is_valid({"sections": ["Overview"], "notes": "x", "extra": 1})
