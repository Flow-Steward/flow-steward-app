"""Extension workflow notes share the portable workflow contract and stay private."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.application.extensions.command_core_persistence import _build_artifact_snapshots
from core.application.extensions.command_install_projection import ExtensionInstallProjectionService
from core.application.extensions.extension_descriptor import ExtensionDescriptorArtifacts
from core.application.extensions.lifecycle_descriptor_io import materialize_descriptor_artifacts
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.workflows.workflow_portability import WorkflowPortabilityService
from tests.unit.test_validate_extensions import _write_minimal_yaml_extension
from tests.unit.test_workflow_canvas_annotations import NOTE


def artifacts(notes):
    return {
        "workflows": [
            {
                "workflow_id": "annotated",
                "snapshot": {
                    "workflow_id": "annotated",
                    "settings": {"canvas_annotations": notes},
                    "steps": [],
                    "groups": [],
                },
            }
        ]
    }


@pytest.mark.parametrize(
    "notes",
    [
        [dict(NOTE, width=0)],
        [NOTE, NOTE],
        [dict(NOTE, image={"artifact_id": "foreign"})],
        [dict(NOTE, x=float("inf"))],
        {"invalid": NOTE},
    ],
)
def test_extension_notes_use_the_same_portable_validation(notes):
    payload = artifacts(notes)
    with pytest.raises(ValidationError, match="canvas_annotations"):
        ExtensionDescriptorArtifacts.model_validate(payload)


def test_referenced_workflow_yaml_uses_the_same_note_contract(tmp_path):
    snapshot = artifacts([NOTE])["workflows"][0]["snapshot"]
    # JSON is also valid YAML; exercise the actual bundle-local materialization.
    (tmp_path / "workflow.yaml").write_text(json.dumps(snapshot))
    descriptor = {
        "artifacts": {"workflows": [{"workflow_id": "annotated", "ref": "workflow.yaml"}]}
    }
    materialized, errors = materialize_descriptor_artifacts(descriptor, bundle_root=tmp_path)
    assert errors == []
    parsed = ExtensionDescriptorArtifacts.model_validate(materialized["artifacts"])
    assert parsed.workflows[0].snapshot["settings"]["canvas_annotations"] == [NOTE]
    assert "snapshot" not in descriptor["artifacts"]["workflows"][0]


@pytest.mark.parametrize("referenced", [False, True])
def test_extension_cli_validation_rejects_invalid_notes(tmp_path, referenced):
    from core.application.tooling.extensions.models import ExtensionValidationRequest
    from core.application.tooling.extensions.validate_extensions import validate_extensions

    bundle = _write_minimal_yaml_extension(tmp_path)
    data = artifacts([dict(NOTE, width=0)])
    if referenced:
        entry = data["workflows"][0]
        (bundle / "workflow.yaml").write_text(json.dumps(entry.pop("snapshot")))
        entry["ref"] = "workflow.yaml"
    (bundle / "extension-descriptor.yaml").write_text(json.dumps({"artifacts": data}))
    result = validate_extensions(
        ExtensionValidationRequest(extension_id="acme.demo-notifier", root=str(tmp_path))
    )
    assert result.exit_code == 1
    assert "canvas_annotations" in result.output


def test_install_and_update_preserve_notes_without_exposing_workflow_internals():
    store = ExtensionRuntimeStore()
    service = ExtensionInstallProjectionService(
        context=SimpleNamespace(runtime_store=lambda: store)
    )
    install = SimpleNamespace(install_id="notes-install")
    for revision, text in ((1, "Private author note"), (2, "Updated author note")):
        note = dict(NOTE, text=text)
        descriptor = {
            "artifacts": ExtensionDescriptorArtifacts.model_validate(artifacts([note])).model_dump()
        }
        release = SimpleNamespace(release_id=f"release-{revision}", extension_id="notes-extension")
        store.replace_artifact_snapshots(
            release.release_id, _build_artifact_snapshots(descriptor, release_id=release.release_id)
        )
        service.project_release(install_record=install, release=release, account_id="account-1")
        row = store.list_projections(account_id="account-1", only_active=True)[0]
        assert row.projection["settings"]["canvas_annotations"] == [note]
        workflow = {
            **deepcopy(row.projection),
            "source_install_id": row.install_id,
            "projection_mode": "derived",
        }
        public = WorkflowPortabilityService(SimpleNamespace()).redact_protected_workflow(workflow)
        # A protected author graph is read-only, not hidden: its notes stay readable.
        assert public["portability"]["structure_change_allowed"] is False
        assert public["protected_workflow"] is True
        assert public["settings"]["canvas_annotations"] == [note]
        assert workflow["settings"]["canvas_annotations"] == [note]


@pytest.mark.parametrize("mode", ["production", "development"])
def test_trusted_hook_install_update_works_in_each_mode(monkeypatch, mode):
    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    store = ExtensionRuntimeStore()
    service = ExtensionInstallProjectionService(
        context=SimpleNamespace(runtime_store=lambda: store)
    )
    install = SimpleNamespace(install_id="hook-install")
    for revision in (1, 2):
        hook = declaration(
            display_name=f"Trusted hook revision {revision}",
            input_schema={"type": "object", "properties": {}},
            input_mapping={},
            mutable_fields=[],
            additional_outputs={},
        )
        workflow = linear_workflow(
            {
                "step_id": "before",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": hook}},
            },
            workflow_id="trusted_hook",
            enabled=False,
        )
        payload = {"workflows": [{"workflow_id": "trusted_hook", "snapshot": workflow}]}
        descriptor = {
            "artifacts": ExtensionDescriptorArtifacts.model_validate(payload).model_dump()
        }
        release = SimpleNamespace(
            release_id=f"hook-release-{revision}", extension_id="hook-extension"
        )
        store.replace_artifact_snapshots(
            release.release_id, _build_artifact_snapshots(descriptor, release_id=release.release_id)
        )
        service.project_release(install_record=install, release=release, account_id="account-1")
        rows = store.list_projections(account_id="account-1", only_active=True)
        assert len(rows) == 1
        row = rows[0]
        assert row.release_id == release.release_id
        assert row.projection["steps"][0]["override"]["invoke_workflow"]["hook"] == hook
        protected = {
            **deepcopy(row.projection),
            "source_install_id": row.install_id,
            "source_release_id": row.release_id,
            "projection_mode": "derived",
        }
        public = WorkflowPortabilityService(SimpleNamespace()).redact_protected_workflow(protected)
        assert public["protected_workflow"] is True
        assert public["portability"]["structure_change_allowed"] is False
        assert public["portability"]["clone_allowed"] is False
