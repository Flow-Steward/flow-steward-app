"""Canvas notes use workflow settings and the existing artifact plane, never jobs."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_canvas_annotation_contract import (
    WorkflowAnnotationsRevisionConflictError,
)
from core.application.workflows.workflow_canvas_annotation_service import (
    WorkflowCanvasAnnotationError,
    WorkflowCanvasAnnotationService,
)
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    create_workflow_with_definition,
    preserve_service_owned_settings,
)
from core.application.workflows.workflow_portability import WorkflowPortabilityService


class Repository:
    def __init__(self):
        self.workflow = {
            "workflow_id": "annotated",
            "account_id": "account-1",
            "revision": 3,
            "settings": {"execution_graph": {"entry": None, "edges": []}, "custom": "keep"},
            "steps": [],
            "groups": [],
        }
        self.fail_save = False

    def get_workflow(self, workflow_id):
        return copy.deepcopy(self.workflow) if workflow_id == "annotated" else None

    def save_workflow_canvas_annotations(
        self,
        workflow_id,
        *,
        account_id,
        annotations,
        expected_revision,
        expected_annotations_revision,
    ):
        if self.fail_save:
            raise RuntimeError("storage unavailable")
        if expected_revision != self.workflow["revision"]:
            raise WorkflowRevisionConflictError(workflow_id, expected_revision)
        settings = self.workflow["settings"]
        if expected_annotations_revision != settings.get("canvas_annotations_revision", 0):
            raise WorkflowAnnotationsRevisionConflictError(
                workflow_id, expected_annotations_revision
            )
        settings["canvas_annotations"] = copy.deepcopy(annotations)
        settings["canvas_annotations_revision"] = expected_annotations_revision + 1
        return settings["canvas_annotations_revision"]


class Plane:
    def __init__(self):
        self.rows = {}
        self.bodies = {}
        self.serial = 0

    def create_artifact(self, **data):
        self.serial += 1
        artifact_id = f"image-{self.serial}"
        self.bodies[artifact_id] = data.pop("body")
        self.rows[artifact_id] = {
            **data,
            "artifact_id": artifact_id,
            "size_bytes": len(self.bodies[artifact_id]),
        }
        return SimpleNamespace(artifact_id=artifact_id), self.rows[artifact_id].copy()

    def get_artifact(self, artifact_id):
        return copy.deepcopy(self.rows.get(artifact_id))

    def read_artifact_bytes(self, artifact_id, *, max_size_bytes):
        body = self.bodies.get(artifact_id)
        if body and len(body) > max_size_bytes:
            raise ValueError("too large")
        return body

    def delete_artifact(self, artifact_id):
        self.rows.pop(artifact_id, None)
        self.bodies.pop(artifact_id, None)


SCOPE = {"workflow_id": "annotated", "account_id": "account-1"}
NOTE = {
    "id": "note-1",
    "text": "# Before running\nCheck the input",
    "x": 120,
    "y": -40,
    "width": 280,
    "height": 200,
}
PNG = b"\x89PNG\r\n\x1a\nimage-test-body"


@pytest.fixture
def env():
    repository, plane = Repository(), Plane()
    return repository, plane, WorkflowCanvasAnnotationService(repository, plane)


def save(service, *, revision=3, note=None):
    return service.save_note(
        **SCOPE,
        annotation=note or NOTE,
        expected_revision=3 if revision is not None else None,
        expected_annotations_revision=revision - 3 if revision is not None else 0,
    )


def upload(service, *, revision=4):
    return service.set_image(
        **SCOPE,
        annotation_id="note-1",
        expected_revision=3,
        expected_annotations_revision=revision - 3,
        filename="diagram.png",
        content_type="image/png",
        body=PNG,
    )


def test_clone_images_have_independent_ownership_and_survive_source_deletion(env):
    repository, plane, service = env
    save(service)
    upload(service)
    with service.clone_annotations(
        repository.workflow, target_workflow_id="copy", account_id="account-1"
    ) as notes:
        cloned_image = notes[0]["image"]["artifact_id"]
    service.purge_workflow_images(repository.workflow)
    assert plane.bodies[cloned_image] == PNG
    assert plane.rows[cloned_image]["owner_scope"]["workflow_id"] == "copy"
    assert notes[0]["text"] == NOTE["text"]


def test_database_uuid_scope_works_with_real_artifact_metadata_storage():
    from uuid import UUID

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    repository = Repository()
    account = UUID("00000000-0000-0000-0000-000000000002")
    repository.workflow["account_id"] = account
    repository.workflow["settings"]["canvas_annotations"] = [NOTE.copy()]
    plane = ArtifactDatasetPlane()
    service = WorkflowCanvasAnnotationService(repository, plane)
    result = service.set_image(
        workflow_id="annotated",
        account_id=account,
        annotation_id="note-1",
        expected_revision=3,
        expected_annotations_revision=0,
        filename="diagram.png",
        content_type="image/png",
        body=PNG,
    )
    meta = plane.get_artifact(result["annotations"][0]["image"]["artifact_id"])
    assert meta["owner_scope"]["account_id"] == str(account)
    assert service.preview_image(
        workflow_id="annotated", account_id=str(account), annotation_id="note-1"
    ) == (PNG, "image/png")
    with service.clone_annotations(
        repository.workflow, target_workflow_id="copy", account_id=str(account)
    ) as notes:
        assert (
            plane.get_artifact(notes[0]["image"]["artifact_id"])["owner_scope"]["workflow_id"]
            == "copy"
        )


def test_clone_failure_cleans_new_images_without_touching_source(env):
    repository, plane, service = env
    save(service)
    upload(service)
    original_ids = set(plane.rows)
    with (
        pytest.raises(RuntimeError, match="clone failed"),
        service.clone_annotations(
            repository.workflow, target_workflow_id="copy", account_id="account-1"
        ),
    ):
        raise RuntimeError("clone failed")
    assert set(plane.rows) == original_ids
    assert service.preview_image(**SCOPE, annotation_id="note-1") == (PNG, "image/png")


def test_note_round_trip_preserves_workflow_settings_and_execution(env):
    repository, _, service = env
    save(service)
    saved = service.list_notes(**SCOPE)
    assert saved == {"revision": 3, "annotations_revision": 1, "annotations": [NOTE]}
    assert repository.workflow["settings"]["custom"] == "keep"
    assert repository.workflow["settings"]["execution_graph"] == {"entry": None, "edges": []}
    assert repository.workflow["steps"] == []
    save(service, revision=4, note={**NOTE, "text": "Edited", "x": 620, "width": 420})
    assert service.list_notes(**SCOPE)["annotations"][0] == {
        **NOTE,
        "text": "Edited",
        "x": 620,
        "width": 420,
    }


def test_stale_note_save_is_rejected_without_overwriting(env):
    _, _, service = env
    save(service)
    with pytest.raises(WorkflowAnnotationsRevisionConflictError):
        save(service, note={**NOTE, "text": "Stale"})
    assert service.list_notes(**SCOPE)["annotations"] == [NOTE]


def test_image_is_workflow_owned_and_previewable_without_a_job(env):
    _, plane, service = env
    save(service)
    upload(service)
    note = service.list_notes(**SCOPE)["annotations"][0]
    artifact = plane.rows[note["image"]["artifact_id"]]
    assert artifact["owner_scope"] == {
        **SCOPE,
        "purpose": "workflow_canvas_annotation",
        "annotation_id": "note-1",
    }
    assert artifact["retention_class"] == "persistent"
    assert service.preview_image(**SCOPE, annotation_id="note-1") == (PNG, "image/png")
    save(service, revision=5, note={**NOTE, "text": "New caption"})
    assert service.list_notes(**SCOPE)["annotations"][0]["image"] == note["image"]


def test_failed_image_save_removes_new_artifact_and_keeps_old_image(env):
    repository, plane, service = env
    save(service)
    upload(service)
    repository.fail_save = True
    with pytest.raises(RuntimeError, match="storage unavailable"):
        upload(service, revision=5)
    assert list(plane.rows) == ["image-1"]
    assert service.preview_image(**SCOPE, annotation_id="note-1") == (PNG, "image/png")


def test_delete_note_removes_only_its_owned_image(env):
    _, plane, service = env
    save(service)
    upload(service)
    service.delete_note(
        **SCOPE,
        annotation_id="note-1",
        expected_revision=3,
        expected_annotations_revision=2,
    )
    assert plane.rows == {}
    assert service.list_notes(**SCOPE) == {
        "revision": 3,
        "annotations_revision": 3,
        "annotations": [],
    }


@pytest.mark.parametrize("scope_field", ["workflow_id", "account_id", "annotation_id"])
def test_foreign_artifact_is_neither_previewed_nor_deleted(env, scope_field):
    _, plane, service = env
    save(service)
    upload(service)
    plane.rows["image-1"]["owner_scope"][scope_field] = "someone-else"
    with pytest.raises(WorkflowCanvasAnnotationError) as caught:
        service.preview_image(**SCOPE, annotation_id="note-1")
    assert caught.value.status_code == 404
    service.delete_note(
        **SCOPE,
        annotation_id="note-1",
        expected_revision=3,
        expected_annotations_revision=2,
    )
    assert "image-1" in plane.rows


def test_other_account_cannot_read_or_write_notes(env):
    _, _, service = env
    with pytest.raises(WorkflowCanvasAnnotationError) as caught:
        service.list_notes(workflow_id="annotated", account_id="other")
    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    "patch",
    [
        {"x": float("nan")},
        {"height": -1},
        {"text": "x" * 20001},
        {"id": "../image"},
        {"image": {"artifact_id": "foreign"}},
    ],
)
def test_invalid_note_payload_is_refused_without_writing(env, patch):
    repository, _, service = env
    with pytest.raises(WorkflowCanvasAnnotationError):
        save(service, note={**NOTE, **patch})
    assert repository.workflow["revision"] == 3


def test_note_write_requires_revision(env):
    _, _, service = env
    with pytest.raises(WorkflowCanvasAnnotationError) as caught:
        save(service, revision=None)
    assert caught.value.status_code == 428


def test_general_metadata_save_cannot_overwrite_notes_or_inject_artifact_refs(env):
    repository, _, service = env
    save(service)
    payload = preserve_service_owned_settings(
        {"settings": {"custom": "new value", "canvas_annotations": []}}, repository.workflow
    )
    assert payload["settings"]["canvas_annotations"] == [NOTE]
    assert payload["settings"]["custom"] == "new value"


def test_portable_notes_keep_text_and_layout_without_installation_local_image_ids(env):
    repository, _, service = env
    save(service)
    upload(service)
    exported = WorkflowPortabilityService(repository)._workflow_definition(repository.workflow)
    assert exported["settings"]["canvas_annotations"] == [NOTE]
    assert "canvas_annotations_revision" not in exported["settings"]
    assert "image" in service.list_notes(**SCOPE)["annotations"][0]


@pytest.mark.parametrize(
    "notes",
    [[{**NOTE, "width": -1}], [NOTE, NOTE], [{**NOTE, "image": {"artifact_id": "foreign"}}]],
)
def test_import_refuses_malformed_notes_and_artifact_references_before_writing(notes):
    from tests.unit.test_workflow_definition_service import _CreateStore

    store = _CreateStore()
    with pytest.raises(WorkflowDefinitionChangeError):
        create_workflow_with_definition(
            store,
            {
                "workflow_id": "imported",
                "enabled": False,
                "settings": {"canvas_annotations": notes},
                "steps": [],
                "groups": [],
            },
            author="tester",
        )
    assert store.saved == []


@pytest.mark.parametrize(
    "content_type,body", [("image/svg+xml", b"<svg/>"), ("image/png", b"<html>not an image</html>")]
)
def test_active_content_and_mislabeled_images_are_refused(env, content_type, body):
    _, plane, service = env
    save(service)
    with pytest.raises(WorkflowCanvasAnnotationError) as caught:
        service.set_image(
            **SCOPE,
            annotation_id="note-1",
            expected_revision=3,
            expected_annotations_revision=1,
            filename="image.png",
            content_type=content_type,
            body=body,
        )
    assert caught.value.status_code == 415
    assert plane.rows == {}


def test_clone_keeps_note_text_when_its_image_was_removed(env):
    repository, plane, service = env
    save(service)
    upload(service)
    plane.delete_artifact("image-1")
    with service.clone_annotations(
        repository.workflow, target_workflow_id="copy", account_id="account-1"
    ) as notes:
        assert notes == [NOTE]
    assert plane.rows == {}


def test_import_initializes_local_presentation_revision():
    from tests.unit.test_workflow_definition_service import _CreateStore

    store = _CreateStore()
    create_workflow_with_definition(
        store,
        {
            "workflow_id": "imported",
            "enabled": False,
            "steps": [],
            "groups": [],
            "settings": {"canvas_annotations": [NOTE], "canvas_annotations_revision": 91},
        },
        author="tester",
    )
    assert store.saved[0]["settings"]["canvas_annotations_revision"] == 0
    assert store.saved[0]["settings"]["canvas_annotations"] == [NOTE]
