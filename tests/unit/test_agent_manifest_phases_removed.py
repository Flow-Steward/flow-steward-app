"""E5: agents have no phases; a manifest that still carries them is an error."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.extensions.extension_descriptor import ExtensionDescriptorArtifacts
from core.application.registry.agent_manifest import AgentManifest
from core.application.registry.db_store_agents_mixin import RegistryDBStoreAgentsMixin
from core.entrypoints.admin.agents_models import AgentPatch, AgentSave

ROOT = Path(__file__).resolve().parents[2]


def _row() -> dict:
    return {"agent_id": "writer", "display_name": "Writer", "_skills": [], "_tools": []}


def test_the_manifest_has_no_phases() -> None:
    manifest = AgentManifest.from_db_row(_row())

    assert not hasattr(manifest, "phases")
    assert "phases" not in manifest.to_dict()


def test_the_schema_has_no_agent_phases_column() -> None:
    schema = (ROOT / "db/schema/001_schema_part_03.sql").read_text(encoding="utf-8")
    table = schema.split("CREATE TABLE public.registry_agents (", 1)[1].split(");", 1)[0]

    assert "phases" not in table


def test_saving_an_agent_with_phases_is_refused_before_any_write() -> None:
    store = SimpleNamespace()
    with pytest.raises(ValueError, match="Agent field 'phases' is not accepted"):
        RegistryDBStoreAgentsMixin.save_agent(store, {"agent_id": "writer", "phases": []})


@pytest.mark.parametrize(
    "model, payload",
    [
        (AgentSave, {"agent_id": "writer", "display_name": "Writer", "category": "marketing"}),
        (AgentPatch, {"display_name": "Writer"}),
    ],
)
def test_the_admin_api_refuses_phases_instead_of_ignoring_them(model, payload) -> None:
    model.model_validate(payload)

    with pytest.raises(ValidationError, match="Agent field 'phases' is not accepted"):
        model.model_validate({**payload, "phases": ["development"]})


def test_an_extension_agent_with_phases_is_refused_before_install() -> None:
    snapshot = {"agent_id": "writer", "display_name": "Writer", "phases": ["development"]}

    with pytest.raises(ValidationError, match="Agent 'writer': Agent field 'phases'"):
        ExtensionDescriptorArtifacts.model_validate(
            {"agents": [{"agent_id": "writer", "snapshot": snapshot}]}
        )

    snapshot.pop("phases")
    ExtensionDescriptorArtifacts.model_validate(
        {"agents": [{"agent_id": "writer", "snapshot": snapshot}]}
    )
