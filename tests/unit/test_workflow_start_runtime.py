from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def test_direct_inputs_enqueue_builds_task_envelope_and_preserves_inputs():
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    enqueue_task = MagicMock(return_value="job-17")

    job_id = enqueue_workflow_start(
        enqueue_task_fn=enqueue_task,
        task_id="task-17",
        task_summary="Launch catalog sync",
        task_source="webhook",
        project_id="17",
        project_name="catalog",
        project_config={},
        selected_workflow_id="wf_catalog_sync",
        workflow_selection_source="entry_binding",
        entry_path="webhook",
        input_contract="direct_inputs",
        source_system="webhook",
        requested_workflow_id="wf_catalog_sync",
        source_event_id="evt-17",
        source_object_id="obj-17",
        mode="greenfield",
        inputs={
            "description": "Sync latest catalog rows",
            "priority": "high",
            "sku": "sku-17",
        },
    )

    assert job_id == "job-17"
    extra_state = enqueue_task.call_args.kwargs["extra_state"]
    task_envelope = extra_state["task_envelope"]
    assert task_envelope["project_id"] == "17"
    assert task_envelope["requested_workflow_id"] == "wf_catalog_sync"
    assert task_envelope["title"] == "Launch catalog sync"
    assert task_envelope["description"] == "Sync latest catalog rows"
    assert task_envelope["inputs"]["sku"] == "sku-17"
    assert extra_state["inputs"]["sku"] == "sku-17"
    assert enqueue_task.call_args.kwargs["mode"] == "greenfield"
    invocation = enqueue_task.call_args.kwargs["meta"]["invocation"]
    assert invocation["channel"] == "webhook"
    assert invocation["trigger_family"] == "incoming_webhook"
    assert invocation["mapped_inputs"]["sku"] == "sku-17"
    assert invocation["source_keys"] == ["description", "priority", "sku"]
    assert invocation["size_bytes"] > 0
    assert len(invocation["sha256"]) == 64
    assert "execution_envelope" not in enqueue_task.call_args.kwargs["meta"]
    assert "task_envelope" not in enqueue_task.call_args.kwargs["meta"]
    replay_snapshot = enqueue_task.call_args.kwargs["replay_snapshot"]
    assert replay_snapshot["workflow_id"] == "wf_catalog_sync"
    assert replay_snapshot["inputs"]["sku"] == "sku-17"
    assert replay_snapshot["task_envelope"]["inputs"]["sku"] == "sku-17"
    assert enqueue_task.call_args.kwargs["meta"]["mode"] == "greenfield"


def test_task_envelope_invocation_projects_effective_inputs_with_secret_redaction():
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    enqueue_task = MagicMock(return_value="job-envelope")
    effective_inputs = {
        "customer_code": "declared-secret",
        "message": "Vilnius webhook form audit b68ec399",
        "nested": {
            "api_key": "nested-secret",  # pragma: allowlist secret - redaction fixture
            "region": "eu",
        },
    }

    enqueue_workflow_start(
        enqueue_task_fn=enqueue_task,
        task_id="task-envelope",
        task_summary="Launch webhook audit",
        task_source="api",
        project_id="18",
        project_name="catalog",
        project_config={},
        selected_workflow_id="acs_public_sse_status_v3_20260830",
        workflow_selection_source="explicit_request",
        entry_path="manual_api",
        input_contract="task_envelope",
        source_system="api",
        task_envelope={"inputs": effective_inputs},
        inputs={},
        declared_secret_inputs={"customer_code"},
    )

    persisted = enqueue_task.call_args.kwargs
    invocation = persisted["meta"]["invocation"]
    assert invocation["mapped_inputs"] == {
        "customer_code": "[REDACTED]",
        "message": "Vilnius webhook form audit b68ec399",
        "nested": {"api_key": "[REDACTED]", "region": "eu"},
    }
    assert invocation["source_keys"] == ["customer_code", "message", "nested"]
    assert invocation["size_bytes"] == 134
    assert (
        invocation["sha256"]
        == (
            "611a605f8042225d67d77ace50fa9f086a61c6daa462abe7cd15b95e53ae5f6c"  # pragma: allowlist secret - expected SHA-256
        )
    )
    assert invocation["request_context_summary"]["body_keys"] == [
        "customer_code",
        "message",
        "nested",
    ]
    assert persisted["replay_snapshot"]["inputs"] == effective_inputs


def test_enqueue_persists_only_idempotency_key_digest():
    from core.application.idempotency.service import RequestIdempotencyService
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    raw_key = "customer-secret-idempotency-key"
    enqueue_task = MagicMock(return_value="job-18")

    enqueue_workflow_start(
        enqueue_task_fn=enqueue_task,
        task_id="task-18",
        task_summary="Launch safely",
        task_source="webhook",
        project_id="18",
        project_name="catalog",
        project_config={},
        selected_workflow_id="wf_catalog_sync",
        workflow_selection_source="entry_binding",
        entry_path="webhook",
        input_contract="direct_inputs",
        source_system="webhook",
        idempotency_key=raw_key,
        inputs={"title": "sync"},
        job_meta={"idempotency_key": raw_key, "invocation": {"idempotency_key": raw_key}},
    )

    persisted = enqueue_task.call_args.kwargs
    digest = RequestIdempotencyService.digest_key(raw_key)
    assert raw_key not in json.dumps(persisted)
    assert persisted["meta"]["idempotency_key_digest"] == digest
    assert persisted["meta"]["invocation"]["idempotency_key_digest"] == digest
    assert "idempotency_key" not in persisted["meta"]
    assert "idempotency_key" not in persisted["meta"]["invocation"]


def test_invocation_snapshot_redacts_and_truncates_sensitive_payloads():
    from core.application.workflows.workflow_start_runtime import build_invocation_snapshot

    invocation = build_invocation_snapshot(
        task_source="webhook",
        entry_path="webhook",
        source_system="webhook",
        selected_workflow_id="wf_sensitive",
        workflow_selection_source="entry_binding",
        source_event_id="req-99",
        inputs={
            "api_key": "top-secret",
            "nested": {"password": "hunter2"},
            "notes": "x" * 10000,
        },
        invocation_patch={
            "request_context_summary": {
                "body_keys": ["api_key", "nested", "notes"],
                "form_keys": [],
                "query_keys": [],
                "file_count": 0,
                "file_names": [],
            }
        },
    )

    assert invocation["mapped_inputs"]["api_key"] == "[REDACTED]"
    assert invocation["mapped_inputs"]["nested"]["password"] == "[REDACTED]"
    assert invocation["mapped_inputs"]["notes"]["$summary"] == "string"
    assert invocation["mapped_inputs"]["notes"]["size_bytes"] == 10000
    assert invocation["truncated"] is True


def test_invocation_snapshot_redaction_does_not_set_truncated_without_size_clipping():
    from core.application.workflows.workflow_start_runtime import build_invocation_snapshot

    invocation = build_invocation_snapshot(
        task_source="webhook",
        entry_path="webhook",
        source_system="webhook",
        selected_workflow_id="wf_sensitive",
        workflow_selection_source="entry_binding",
        inputs={"token": "top-secret"},
    )

    assert invocation["mapped_inputs"]["token"] == "[REDACTED]"
    assert invocation["truncated"] is False


def test_invocation_snapshot_redacts_an_entire_secret_reference_under_an_arbitrary_name():
    from core.application.workflows.workflow_start_runtime import build_invocation_snapshot
    from core.infrastructure.security.secret_reference import SecretReference

    invocation = build_invocation_snapshot(
        task_source="api",
        entry_path="manual_api",
        source_system="api",
        selected_workflow_id="wf_sensitive",
        workflow_selection_source="explicit_request",
        inputs={
            "credential": SecretReference(
                name="credential",
                ciphertext="replayable-ciphertext",
            ).to_wire()
        },
    )

    assert invocation["mapped_inputs"]["credential"] == "[REDACTED]"
    assert "replayable-ciphertext" not in json.dumps(invocation)


def test_enqueue_metadata_never_contains_replayable_secret_ciphertext():
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start
    from core.infrastructure.security.secret_reference import SecretReference

    enqueue_task = MagicMock(return_value="job-secret")
    secret_wire = SecretReference(
        name="credential",
        ciphertext="replayable-ciphertext",
    ).to_wire()

    enqueue_workflow_start(
        enqueue_task_fn=enqueue_task,
        task_id="task-secret",
        task_summary="Launch safely",
        task_source="api",
        project_id="18",
        project_name="catalog",
        project_config={},
        selected_workflow_id="wf_sensitive",
        workflow_selection_source="explicit_request",
        entry_path="manual_api",
        input_contract="direct_inputs",
        source_system="api",
        inputs={"credential": secret_wire},
        job_meta={"task_envelope": {"inputs": {"credential": secret_wire}}},
    )

    persisted_meta = enqueue_task.call_args.kwargs["meta"]
    assert "replayable-ciphertext" not in json.dumps(persisted_meta)
    assert persisted_meta["invocation"]["mapped_inputs"]["credential"] == "[REDACTED]"
    assert "execution_envelope" not in persisted_meta
    assert "task_envelope" not in persisted_meta
    assert enqueue_task.call_args.kwargs["replay_snapshot"]["inputs"] == {"credential": secret_wire}


def test_dynamic_secret_stays_out_of_meta_and_in_exact_replay_candidate():
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start
    from core.infrastructure.security.secret_reference import SecretReference

    enqueue_task = MagicMock(return_value="job-secret")
    secret_wire = SecretReference(
        name="credential",
        ciphertext="replayable-ciphertext",
    ).to_wire()
    enqueue_workflow_start(
        enqueue_task_fn=enqueue_task,
        task_id="task-secret",
        task_summary="Launch safely",
        task_source="api",
        project_id="18",
        project_name="catalog",
        project_config={},
        selected_workflow_id="wf_sensitive",
        workflow_selection_source="explicit_request",
        entry_path="manual_api",
        input_contract="direct_inputs",
        source_system="api",
        inputs={"credential": secret_wire},
        job_meta={"task_envelope": {"inputs": {"credential": secret_wire}}},
    )
    persisted_meta = enqueue_task.call_args.kwargs["meta"]

    assert "rerun_requires_secret_inputs" not in persisted_meta
    assert "replayable-ciphertext" not in json.dumps(persisted_meta)
    assert enqueue_task.call_args.kwargs["replay_snapshot"]["inputs"] == {"credential": secret_wire}


def test_legacy_meta_cannot_be_used_as_retry_authority():
    from core.application.jobs.job_invocation_artifacts import JobReplaySnapshotError
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection

    with pytest.raises(JobReplaySnapshotError) as exc_info:
        build_rerun_workflow_selection(
            {
                "id": "legacy-job",
                "selected_workflow_key": "wf.legacy",
                "meta": {
                    "execution_envelope": {"inputs": {"invoice_id": "wrong"}},
                    "task_envelope": {"inputs": {"invoice_id": "also-wrong"}},
                    "invocation": {"mapped_inputs": {"invoice_id": "redacted-fallback"}},
                },
            },
            replay_snapshot=None,
            to_json_obj=lambda value: value if isinstance(value, dict) else {},
            parse_test_mode=lambda value, default=False: bool(value or default),
        )

    assert exc_info.value.code == "job.replay_snapshot_absent"


def test_rerun_rejects_snapshot_with_silently_defaulted_contract_fields():
    from core.application.jobs.job_invocation_artifacts import JobReplaySnapshotError
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection

    with pytest.raises(JobReplaySnapshotError) as exc_info:
        build_rerun_workflow_selection(
            {"id": "legacy-job", "meta": {}},
            replay_snapshot={
                "workflow_id": "wf.legacy",
                "inputs": {},
                "files": [],
                "test_mode": False,
            },
            to_json_obj=lambda value: value if isinstance(value, dict) else {},
            parse_test_mode=lambda value, default=False: bool(value or default),
        )

    assert exc_info.value.code == "job.replay_snapshot_partial"


def test_rerun_invocation_snapshot_marks_restart_origin():
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection

    extra_state, rerun_meta = build_rerun_workflow_selection(
        {
            "id": 42,
            "meta": {
                "selected_workflow_id": "wf_restartable",
                "invocation": {
                    "mapped_inputs": {"email": "demo@example.com"},
                    "request_context_summary": {"body_keys": ["email"]},
                    "files": [],
                },
            },
        },
        replay_snapshot={
            "workflow_id": "wf_restartable",
            "inputs": {"email": "demo@example.com"},
            "files": [],
            "test_mode": False,
            "input_contract": "direct_inputs",
            "task_envelope": {"inputs": {"email": "demo@example.com"}, "files": []},
            "_workflow_secret_ref_fingerprints": [],
        },
        to_json_obj=lambda value: value if isinstance(value, dict) else {},
        parse_test_mode=lambda value, default=False: bool(value if value is not None else default),
    )

    assert extra_state["workflow_id"] == "wf_restartable"
    assert rerun_meta["rerun_from_job_id"] == "42"
    assert rerun_meta["invocation"]["channel"] == "job_restart"
    assert rerun_meta["invocation"]["trigger_family"] == "restart"
    assert rerun_meta["invocation"]["origin_job_id"] == "42"


def test_rerun_preserves_verified_snapshot_and_source_correlation():
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection

    envelope = {
        "envelope_id": "env-42",
        "title": "Sync catalog",
        "description": "Sync the selected catalog",
        "inputs": {"catalog_id": "cat-7"},
        "project_id": "project-7",
        "requested_workflow_id": "wf.catalog-sync",
        "files": [],
    }
    extra_state, rerun_meta = build_rerun_workflow_selection(
        {
            "id": 42,
            "selected_workflow_key": "wf.catalog-sync",
            "meta": {
                "execution_envelope": {"inputs": {"catalog_id": "wrong-meta-value"}},
                "source_system": "provider-webhook",
                "source_event_id": "event-9",
                "source_object_id": "issue-11",
                "input_contract": "task_envelope",
                "invocation": {
                    "mapped_inputs": {"catalog_id": "cat-7"},
                    "request_id": "request-9",
                    "entry_key": "catalog.updated",
                    "trigger_id": "trigger-3",
                    "request_context_summary": {"body_keys": ["catalog_id"]},
                    "files": [],
                },
            },
        },
        replay_snapshot={
            "workflow_id": "wf.catalog-sync",
            "inputs": {"catalog_id": "cat-7"},
            "files": [],
            "test_mode": False,
            "input_contract": "task_envelope",
            "task_envelope": envelope,
            "_workflow_secret_ref_fingerprints": [],
        },
        to_json_obj=lambda value: value if isinstance(value, dict) else {},
        parse_test_mode=lambda value, default=False: bool(value if value is not None else default),
    )

    assert extra_state["task_envelope"] == envelope
    assert extra_state["inputs"] == {"catalog_id": "cat-7"}
    assert "execution_envelope" not in rerun_meta
    assert rerun_meta["source_system"] == "provider-webhook"
    assert rerun_meta["source_event_id"] == "event-9"
    assert rerun_meta["source_object_id"] == "issue-11"
    assert rerun_meta["input_contract"] == "task_envelope"
    assert rerun_meta["invocation"]["request_id"] == "request-9"
    assert rerun_meta["invocation"]["entry_key"] == "catalog.updated"
    assert rerun_meta["invocation"]["trigger_id"] == "trigger-3"


def test_rerun_keeps_canonical_task_envelope_out_of_durable_meta():
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection

    envelope = {
        "envelope_id": "env-43",
        "title": "Restart candidate task",
        "description": "Restart candidate task",
        "inputs": {
            "title": "Restart candidate task",
            "description": "Restart candidate task",
            "priority": "medium",
        },
        "project_id": "project-7",
        "requested_workflow_id": "wf.restartable",
        "files": [],
    }

    extra_state, rerun_meta = build_rerun_workflow_selection(
        {
            "id": 43,
            "selected_workflow_key": "wf.restartable",
            "meta": {"task_envelope": {"inputs": {"title": "wrong-meta-value"}}},
        },
        replay_snapshot={
            "workflow_id": "wf.restartable",
            "inputs": envelope["inputs"],
            "files": [],
            "test_mode": False,
            "input_contract": "task_envelope",
            "task_envelope": envelope,
            "_workflow_secret_ref_fingerprints": [],
        },
        to_json_obj=lambda value: value if isinstance(value, dict) else {},
        parse_test_mode=lambda value, default=False: bool(value if value is not None else default),
    )

    assert extra_state["task_envelope"] == envelope
    assert extra_state["inputs"] == envelope["inputs"]
    assert "task_envelope" not in rerun_meta
