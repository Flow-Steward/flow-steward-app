"""A file input's test file: attached once, copied into every test run that passes none."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.application.extensions.artifact_plane_values import ArtifactHandle
from core.application.registry.workflow_contract import WorkflowContractField, WorkflowInterface
from core.application.registry.workflow_secret_defaults import preserve_secret_references
from core.application.workflows.test_run_submission_service import (
    TestRunInputRejected,
    materialize_test_run_envelope,
    submit_test_run,
)
from core.application.workflows.workflow_portability_common import _redact_sensitive
from core.application.workflows.workflow_sample_file_service import (
    TestRunSampleFiles,
    WorkflowSampleFileError,
    WorkflowSampleFileService,
    preview_sample_file,
)

PDF = b"%PDF-1.7\n" + b"x" * 64


class _Plane:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.bodies: dict[str, bytes] = {}
        self.bound: list[dict[str, str]] = []
        self.counter = 0

    def create_artifact(self, **kwargs: Any) -> tuple[ArtifactHandle, dict[str, Any]]:
        self.counter += 1
        artifact_id = f"art_{self.counter}"
        meta = {
            "artifact_id": artifact_id,
            "owner_scope": dict(kwargs["owner_scope"]),
            "filename": kwargs["filename"],
            "content_type": kwargs["content_type"],
            "retention_class": kwargs["retention_class"],
            "size_bytes": len(kwargs["body"]),
            "sha256": "a" * 64,
        }
        self.rows[artifact_id] = meta
        self.bodies[artifact_id] = kwargs["body"]
        return ArtifactHandle(artifact_id=artifact_id), dict(meta)

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.rows.get(artifact_id)
        return dict(row) if row else None

    def read_artifact_bytes(self, artifact_id: str, *, max_size_bytes: int) -> bytes | None:
        return self.bodies.get(artifact_id)

    def delete_artifact(self, artifact_id: str) -> bool:
        self.bodies.pop(artifact_id, None)
        return self.rows.pop(artifact_id, None) is not None

    def bind_replay_owner(self, *, artifact_id: str, project_id: str, job_id: str) -> dict:
        self.bound.append({"artifact_id": artifact_id, "project_id": project_id, "job_id": job_id})
        self.rows[artifact_id]["owner_scope"]["job_id"] = job_id
        return dict(self.rows[artifact_id])


class _Repository:
    def __init__(self, workflow: dict[str, Any]) -> None:
        self.workflow = workflow
        self.saves: list[dict[str, Any]] = []
        self.fail_save = False

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.workflow) if workflow_id == self.workflow["workflow_id"] else None

    def save_workflow(
        self, data: dict[str, Any], *, author: str, expected_revision: int | None
    ) -> int:
        if self.fail_save:
            raise RuntimeError("database is down")
        assert expected_revision == self.workflow["revision"]
        self.workflow = copy.deepcopy(data)
        self.workflow["revision"] = expected_revision + 1
        self.saves.append(copy.deepcopy(data))
        return self.workflow["revision"]


def _workflow(inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "workflow_id": "wf-parse",
        "account_id": "acc-1",
        "revision": 3,
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": inputs
                if inputs is not None
                else [
                    {"name": "invoice", "type": "file", "required": True},
                    {"name": "note", "type": "string"},
                ],
            }
        },
    }


def _input_row(repository: _Repository, name: str) -> dict[str, Any]:
    rows = repository.workflow["settings"]["callable"]["expected_inputs"]
    return next(row for row in rows if row["name"] == name)


def _attach(service: WorkflowSampleFileService, *, revision: int = 3, body: bytes = PDF) -> dict:
    return service.set_sample_file(
        workflow_id="wf-parse",
        field_name="invoice",
        account_id="acc-1",
        filename="invoice.pdf",
        content_type="application/pdf",
        body=body,
        author="operator",
        expected_revision=revision,
    )


def test_attaching_a_test_file_stores_it_for_the_workflow_and_records_it_on_the_input() -> None:
    plane = _Plane()
    repository = _Repository(_workflow())

    result = _attach(WorkflowSampleFileService(repository, plane))

    sample = _input_row(repository, "invoice")["sample_file"]
    assert result["sample_file"] == sample
    assert result["revision"] == 4
    assert sample["filename"] == "invoice.pdf"
    assert sample["size_bytes"] == len(PDF)
    stored = plane.rows[sample["artifact_id"]]
    assert stored["retention_class"] == "persistent"
    assert stored["owner_scope"] == {
        "account_id": "acc-1",
        "workflow_id": "wf-parse",
        "purpose": "workflow_sample_file",
        "field_name": "invoice",
    }


def test_replacing_a_test_file_deletes_the_old_one_and_clearing_deletes_the_last() -> None:
    plane = _Plane()
    repository = _Repository(_workflow())
    service = WorkflowSampleFileService(repository, plane)

    first = _attach(service)["sample_file"]["artifact_id"]
    second = _attach(service, revision=4)["sample_file"]["artifact_id"]

    assert first not in plane.rows
    assert second in plane.rows

    service.clear_sample_file(
        workflow_id="wf-parse",
        field_name="invoice",
        account_id="acc-1",
        author="operator",
        expected_revision=5,
    )
    assert "sample_file" not in _input_row(repository, "invoice")
    assert plane.rows == {}


def test_a_failed_save_leaves_no_orphaned_file() -> None:
    plane = _Plane()
    repository = _Repository(_workflow())
    repository.fail_save = True
    service = WorkflowSampleFileService(repository, plane)

    with pytest.raises(RuntimeError):
        _attach(service)

    assert plane.rows == {}


@pytest.mark.parametrize(
    ("field_name", "filename", "body", "revision", "code"),
    [
        ("note", "a.pdf", PDF, 3, "workflow_input_not_file"),
        ("missing", "a.pdf", PDF, 3, "workflow_input_not_found"),
        ("invoice", "../a.pdf", PDF, 3, "workflow_sample_file_name_invalid"),
        ("invoice", "a.pdf", b"", 3, "workflow_sample_file_empty"),
        (
            "invoice",
            "a.pdf",
            b"x" * (50 * 1024 * 1024 + 1),
            3,
            "workflow_sample_file_too_large",
        ),
        ("invoice", "a.pdf", PDF, None, "workflow_revision_required"),
    ],
)
def test_an_invalid_attachment_is_refused_before_anything_is_stored(
    field_name: str, filename: str, body: bytes, revision: int | None, code: str
) -> None:
    plane = _Plane()
    service = WorkflowSampleFileService(_Repository(_workflow()), plane)

    with pytest.raises(WorkflowSampleFileError) as error:
        service.set_sample_file(
            workflow_id="wf-parse",
            field_name=field_name,
            account_id="acc-1",
            filename=filename,
            content_type="application/pdf",
            body=body,
            author="operator",
            expected_revision=revision,
        )

    assert error.value.code == code
    assert plane.rows == {}


def test_another_account_cannot_attach_a_file() -> None:
    service = WorkflowSampleFileService(_Repository(_workflow()), _Plane())

    with pytest.raises(WorkflowSampleFileError) as error:
        service.set_sample_file(
            workflow_id="wf-parse",
            field_name="invoice",
            account_id="acc-2",
            filename="a.pdf",
            content_type="application/pdf",
            body=PDF,
            author="intruder",
            expected_revision=3,
        )

    assert error.value.status_code == 403


def test_an_ordinary_save_keeps_the_stored_file_and_ignores_one_the_client_sends() -> None:
    stored = {
        "callable": {
            "expected_inputs": [
                {"name": "invoice", "type": "file", "sample_file": {"artifact_id": "art_mine"}}
            ]
        }
    }
    incoming = {
        "callable": {
            "expected_inputs": [
                {"name": "invoice", "type": "file", "sample_file": {"artifact_id": "art_theirs"}},
                {"name": "other", "type": "file", "sample_file": {"artifact_id": "art_theirs"}},
            ]
        }
    }

    merged = preserve_secret_references(incoming, stored)["callable"]["expected_inputs"]

    assert merged[0]["sample_file"] == {"artifact_id": "art_mine"}
    assert "sample_file" not in merged[1]
    retyped = preserve_secret_references(
        {"callable": {"expected_inputs": [{"name": "invoice", "type": "string"}]}}, stored
    )
    assert "sample_file" not in retyped["callable"]["expected_inputs"][0]


def test_the_contract_carries_a_test_file_only_on_a_file_input_and_export_drops_it() -> None:
    sample = {"artifact_id": "art_1", "filename": "a.pdf", "size_bytes": 10, "junk": "x"}
    file_field = WorkflowContractField(name="invoice", type="file", sample_file=sample)
    text_field = WorkflowContractField(name="note", type="string", sample_file=sample)

    assert file_field.dump()["sample_file"] == {
        "artifact_id": "art_1",
        "filename": "a.pdf",
        "size_bytes": 10,
    }
    assert file_field.public_dump()["sample_file"]["filename"] == "a.pdf"
    assert "sample_file" not in text_field.dump()
    assert (
        "sample_file"
        not in _redact_sensitive({"expected_inputs": [file_field.dump()]})["expected_inputs"][0]
    )


def _interface(sample: dict[str, Any] | None) -> WorkflowInterface:
    return WorkflowInterface(
        enabled=True,
        trigger_channels=["api"],
        expected_inputs=[
            WorkflowContractField(name="invoice", type="file", required=True, sample_file=sample),
        ],
    )


def test_a_dry_run_shows_the_test_file_and_a_run_without_one_still_requires_a_file() -> None:
    envelope, _ = materialize_test_run_envelope(
        workflow_id="wf-parse",
        project_id="project-1",
        task_envelope={"inputs": {}},
        workflow_interface=_interface({"artifact_id": "art_1", "filename": "a.pdf"}),
        sample_files=preview_sample_file,
    )

    assert envelope["inputs"]["invoice"]["filename"] == "a.pdf"
    assert envelope["inputs"]["invoice"]["source_ref"] == "sample:invoice"
    assert "artifact_id" not in envelope["inputs"]["invoice"]
    assert envelope["files"] == [envelope["inputs"]["invoice"]]
    without_test_file = _interface(None)

    with pytest.raises(TestRunInputRejected):
        materialize_test_run_envelope(
            workflow_id="wf-parse",
            project_id="project-1",
            task_envelope={"inputs": {}},
            workflow_interface=without_test_file,
            sample_files=preview_sample_file,
        )


def _sample_plane() -> tuple[_Plane, dict[str, Any]]:
    plane = _Plane()
    repository = _Repository(_workflow())
    sample = _attach(WorkflowSampleFileService(repository, plane))["sample_file"]
    return plane, sample


def _copies(plane: _Plane, *, workflow_account_id: str = "acc-1") -> TestRunSampleFiles:
    return TestRunSampleFiles(
        artifact_plane=plane,
        workflow_id="wf-parse",
        workflow_account_id=workflow_account_id,
        account_id="acc-1",
        project_id="project-1",
    )


def _submit(plane: _Plane, sample: dict[str, Any], *, inputs: dict, enqueue=None) -> tuple:
    captured: dict[str, Any] = {}

    def default_enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    class _Jobs:
        def get_job(self, job_id: str) -> dict[str, Any]:
            return {"id": 41, "thread_id": "thread-41"}

    copies = _copies(plane)
    result = submit_test_run(
        enqueue=enqueue or default_enqueue,
        job_store=_Jobs(),
        kind="workflow",
        workflow_id="wf-parse",
        project_id="project-1",
        task_id="task-1",
        task_summary="Parse",
        task_envelope={"inputs": inputs},
        project_name="Demo",
        project_config={},
        workflow_interface=_interface(sample),
        sample_files=copies,
    )
    return result, captured, copies


def test_a_test_run_that_passes_no_file_gets_its_own_copy_bound_to_the_job() -> None:
    plane, sample = _sample_plane()

    _result, captured, copies = _submit(plane, sample, inputs={})

    passed = captured["extra_state"]["inputs"]["invoice"]
    assert passed["artifact_id"] != sample["artifact_id"]
    assert passed["filename"] == "invoice.pdf"
    assert passed["field_name"] == "invoice"
    assert captured["extra_state"]["files"] == [passed]
    assert plane.bodies[passed["artifact_id"]] == PDF
    copy_owner = plane.rows[passed["artifact_id"]]["owner_scope"]
    assert copy_owner["project_id"] == "project-1"
    assert copy_owner["job_id"] == "41"
    assert plane.bound == [
        {"artifact_id": passed["artifact_id"], "project_id": "project-1", "job_id": "41"}
    ]
    # The attached file stays the workflow's own, ready for the next run.
    assert sample["artifact_id"] in plane.rows
    assert "job_id" not in plane.rows[sample["artifact_id"]]["owner_scope"]
    assert copies.copies[0]["artifact_id"] == passed["artifact_id"]


def test_a_test_run_that_passes_a_file_does_not_use_the_test_file() -> None:
    plane, sample = _sample_plane()
    own = {"artifact_id": "art_own", "filename": "mine.pdf"}

    _result, captured, _copies_made = _submit(plane, sample, inputs={"invoice": own})

    assert captured["extra_state"]["inputs"]["invoice"] == own
    assert "files" not in captured["extra_state"]
    assert len(plane.rows) == 1


def test_a_run_that_is_not_launched_deletes_its_copy() -> None:
    plane, sample = _sample_plane()

    def failing_enqueue(**_kwargs: Any) -> int:
        raise RuntimeError("queue is down")

    with pytest.raises(RuntimeError):
        _submit(plane, sample, inputs={}, enqueue=failing_enqueue)

    assert list(plane.rows) == [sample["artifact_id"]]


def test_a_test_file_that_is_not_this_workflows_is_refused() -> None:
    plane, sample = _sample_plane()
    other_account = _copies(plane, workflow_account_id="acc-2")
    same_account = _copies(plane)

    with pytest.raises(RuntimeError, match="missing"):
        other_account.copy("invoice", sample)
    with pytest.raises(RuntimeError, match="missing"):
        same_account.copy("invoice", {"artifact_id": "art_unknown"})


def test_deleting_the_workflow_deletes_its_test_files_only() -> None:
    plane, sample = _sample_plane()
    _handle, foreign = plane.create_artifact(
        owner_scope={"account_id": "acc-2", "workflow_id": "wf-parse", "purpose": "x"},
        run_id="r",
        step_id="s",
        filename="f",
        content_type="t",
        retention_class="persistent",
        body=b"1",
    )
    workflow = _workflow(
        [
            {"name": "invoice", "type": "file", "sample_file": sample},
            {
                "name": "other",
                "type": "file",
                "sample_file": {"artifact_id": foreign["artifact_id"]},
            },
        ]
    )

    WorkflowSampleFileService(None, plane).purge_workflow_samples(workflow)

    assert list(plane.rows) == [foreign["artifact_id"]]


def test_the_test_file_limit_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_WORKFLOW_TEST_FILE_MAX_BYTES", "2048")
    service = WorkflowSampleFileService(_Repository(_workflow()), _Plane())

    with pytest.raises(WorkflowSampleFileError) as error:
        _attach(service, body=b"x" * 2049)

    assert error.value.code == "workflow_sample_file_too_large"
    assert "FS_WORKFLOW_TEST_FILE_MAX_BYTES" in str(error.value)
    assert _attach(service, body=b"x" * 2048)["sample_file"]["size_bytes"] == 2048
