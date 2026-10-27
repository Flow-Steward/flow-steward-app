from __future__ import annotations

import pytest

from core.application.management.task_submission import (
    TaskAttachmentDescriptor,
    TaskSubmissionValidationError,
    canonical_task_fingerprint,
    normalize_workflow_inputs,
)
from core.application.orchestration.task_envelope import TaskEnvelope


def test_first_class_inputs_become_the_only_normalized_workflow_payload() -> None:
    normalized = normalize_workflow_inputs(
        inputs={"invoice_id": "INV-42", "amount": 1250},
        extra_state={"source": "erp"},
        job_meta={"requested_by": "billing"},
    )

    assert normalized.inputs == {"invoice_id": "INV-42", "amount": 1250}
    assert normalized.extra_state == {"source": "erp"}
    assert normalized.job_meta == {"requested_by": "billing"}


def test_workflow_inputs_in_extra_state_are_rejected_semantically() -> None:
    with pytest.raises(TaskSubmissionValidationError) as captured:
        normalize_workflow_inputs(
            inputs={"invoice_id": "INV-42"},
            extra_state={"workflow_input_payload": {"invoice_id": "INV-43"}},
        )

    assert captured.value.code == "workflow_input_location_unsupported"
    assert not hasattr(captured.value, "status_code")


def test_attachment_fingerprint_is_order_independent_and_keeps_multiplicity() -> None:
    normalized = normalize_workflow_inputs(inputs={"invoice_id": "INV-42"})
    pdf = TaskAttachmentDescriptor(
        filename="invoice.pdf",
        media_type="Application/PDF",
        size=1024,
        sha256="A" * 64,
    )
    duplicate = TaskAttachmentDescriptor(
        filename="invoice.pdf",
        media_type="application/pdf",
        size=1024,
        sha256="a" * 64,
    )
    note = TaskAttachmentDescriptor(
        filename="note.txt",
        media_type="text/plain",
        size=12,
        sha256="b" * 64,
    )

    first = canonical_task_fingerprint(
        normalized,
        project_id="project-1",
        workflow_selector="default",
        resolved_workflow_id="invoice-workflow",
        task_summary="Process invoice",
        mode="standard",
        test_mode=False,
        task_source="api",
        attachments=[pdf, note, duplicate],
    )
    reordered = canonical_task_fingerprint(
        normalized,
        project_id="project-1",
        workflow_selector="default",
        resolved_workflow_id="invoice-workflow",
        task_summary="Process invoice",
        mode="standard",
        test_mode=False,
        task_source="api",
        attachments=[duplicate, pdf, note],
    )
    missing_duplicate = canonical_task_fingerprint(
        normalized,
        project_id="project-1",
        workflow_selector="default",
        resolved_workflow_id="invoice-workflow",
        task_summary="Process invoice",
        mode="standard",
        test_mode=False,
        task_source="api",
        attachments=[pdf, note],
    )

    assert first == reordered
    assert first != missing_duplicate


def test_fingerprint_includes_resolved_default_workflow_and_remaining_metadata() -> None:
    normalized = normalize_workflow_inputs(
        inputs={"invoice_id": "INV-42"},
        extra_state={"priority": "high"},
        job_meta={"attempt_policy": "strict"},
    )
    common = {
        "project_id": "project-1",
        "workflow_selector": "default",
        "task_summary": "Process invoice",
        "mode": "standard",
        "test_mode": False,
        "task_source": "api",
        "attachments": [],
    }

    original = canonical_task_fingerprint(normalized, resolved_workflow_id="invoice-v1", **common)
    changed_default = canonical_task_fingerprint(
        normalized, resolved_workflow_id="invoice-v2", **common
    )
    changed_meta = canonical_task_fingerprint(
        normalize_workflow_inputs(
            inputs={"invoice_id": "INV-42"},
            extra_state={"priority": "high"},
            job_meta={"attempt_policy": "relaxed"},
        ),
        resolved_workflow_id="invoice-v1",
        **common,
    )

    assert original != changed_default
    assert original != changed_meta


def test_fingerprint_excludes_generated_task_envelope_correlation_ids() -> None:
    first_envelope = TaskEnvelope(
        envelope_id="envelope-1",
        source_event_id="event-1",
        source_object_id="object-1",
        title="Process invoice",
        project_id="project-1",
        inputs={"invoice_id": "INV-42"},
    ).to_dict()
    second_envelope = TaskEnvelope(
        envelope_id="envelope-2",
        source_event_id="event-2",
        source_object_id="object-2",
        title="Process invoice",
        project_id="project-1",
        inputs={"invoice_id": "INV-42"},
    ).to_dict()
    first = normalize_workflow_inputs(
        inputs={"invoice_id": "INV-42"},
        extra_state={"task_envelope": first_envelope},
        job_meta={"task_envelope": first_envelope},
    )
    second = normalize_workflow_inputs(
        inputs={"invoice_id": "INV-42"},
        extra_state={"task_envelope": second_envelope},
        job_meta={"task_envelope": second_envelope},
    )
    common = {
        "project_id": "project-1",
        "workflow_selector": "invoice",
        "resolved_workflow_id": "invoice-v1",
        "task_summary": "Process invoice",
        "mode": "standard",
        "test_mode": False,
        "task_source": "api",
        "attachments": [],
    }

    assert canonical_task_fingerprint(first, **common) == canonical_task_fingerprint(
        second, **common
    )
