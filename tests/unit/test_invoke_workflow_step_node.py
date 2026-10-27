from __future__ import annotations

import sys
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests._step_run_state import step_run_state


def test_invoke_message_carries_the_active_workflow_step_identity(monkeypatch):
    from core.application.orchestration.agents import common
    from core.application.orchestration.invoke_workflow_step_node_runtime import (
        _append_agent_message,
    )

    monkeypatch.setattr(common, "publish_agent_message", lambda *_args, **_kwargs: True)
    state = step_run_state(
        {
            "step_id": "call_weather_child",
            "display_name": "Call weather child",
            "step_kind": "invoke_workflow",
        },
        attempt=2,
    )

    message = _append_agent_message(
        state=state,
        from_agent="invoke_workflow",
        skill="invoke_workflow",
        status="pass",
        summary="Child completed",
        artifacts={"_invoke_workflow": {"child_job_id": "child-42"}},
    )

    assert message["artifacts"]["step_id"] == "call_weather_child"
    assert message["artifacts"]["step_display_name"] == "Call weather child"
    assert message["artifacts"]["step_kind"] == "invoke_workflow"
    assert message["step_id"] == "call_weather_child"
    assert message["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert message["attempt"] == 2


def test_invoke_message_preserves_actual_step_start_time(monkeypatch):
    from core.application.orchestration.agents import common
    from core.application.orchestration.invoke_workflow_step_node_runtime import (
        _append_agent_message,
    )

    monkeypatch.setattr(common, "publish_agent_message", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "core.application.orchestration.invoke_workflow_step_node_runtime._now_iso",
        lambda: "2026-09-10T08:09:04.250000Z",
    )

    message = _append_agent_message(
        state={},
        from_agent="invoke_workflow",
        skill="invoke_workflow",
        status="pass",
        summary="Child completed",
        started_at="2026-09-10T08:09:01Z",
    )

    assert message["timestamps"]["started_at"] == "2026-09-10T08:09:01Z"
    assert message["timestamps"]["completed_at"] == "2026-09-10T08:09:04.250000Z"


def test_invoke_output_state_writer_drops_host_owned_test_mode_target():
    from core.application.orchestration.invoke_workflow_step_node_results import (
        _set_nested_state_update,
    )

    updates: dict = {}
    _set_nested_state_update(
        state={"test_mode": False},
        updates=updates,
        state_target="state.test_mode",
        value=True,
    )

    assert updates == {}


class _FakeDB:
    def __init__(self, workflows: dict[str, dict]):
        self._workflows = {
            str(k): self._with_default_callable_channel(v) for k, v in (workflows or {}).items()
        }

    @staticmethod
    def _with_default_callable_channel(row: dict) -> dict:
        payload = dict(row or {})
        settings = dict(payload.get("settings") or {})
        callable_settings = settings.get("callable")
        if isinstance(callable_settings, dict) and "trigger_channels" not in callable_settings:
            settings["callable"] = {**callable_settings, "trigger_channels": ["parent_workflow"]}
            payload["settings"] = settings
        return payload

    def get_workflow(self, workflow_id: str):
        row = self._workflows.get(str(workflow_id or ""))
        return dict(row) if isinstance(row, dict) else None


class _FakeLogger:
    def __init__(
        self,
        *,
        child_job_id: int = 901,
        child_messages: list[dict] | None = None,
        child_status_sequence: list[str] | None = None,
        child_job_extra: dict | None = None,
        parent_job: dict | None = None,
    ):
        self.child_job_id = int(child_job_id)
        self.child_messages = list(child_messages or [])
        self.child_status_sequence = [str(item or "") for item in (child_status_sequence or [])]
        self.child_job_extra = dict(child_job_extra or {})
        self.parent_job = dict(parent_job or {})
        self._child_status_index = 0
        self.create_calls: list[dict] = []
        self.runtime_calls: list[dict] = []
        self.meta_calls: list[dict] = []
        self.finish_calls: list[dict] = []
        self.update_job_calls: list[dict] = []
        self._child_status_override: str | None = None
        self._child_meta: dict = {}
        self.outbox_marks: list[dict] = []
        self.finalize_calls: list[dict] = []

    def get_job(self, job_id: int):
        if int(job_id) == self.child_job_id:
            if self._child_status_override is not None:
                status = self._child_status_override
            elif self.child_status_sequence:
                idx = min(self._child_status_index, len(self.child_status_sequence) - 1)
                status = self.child_status_sequence[idx]
                self._child_status_index += 1
            else:
                status = "pending"
            return {
                "id": self.child_job_id,
                "project_id": 7,
                "status": status,
                "meta": dict(self._child_meta),
                **self.child_job_extra,
            }
        parent = {
            "id": int(job_id),
            "project_id": 7,
            "project_name": "demo-project",
            "status": "in_progress",
        }
        parent.update(self.parent_job)
        parent["id"] = int(job_id)
        parent.setdefault("project_id", 7)
        return parent

    def create_job(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        self._child_meta = dict(kwargs.get("meta") or {})
        return self.child_job_id

    def create_job_dispatch_outbox(self, job_id, **kwargs):
        return True

    def renew_job_dispatch_preparation_lease(self, job_id, **kwargs):
        return True

    def finalize_job_dispatch_payload(self, job_id, **kwargs):
        self.finalize_calls.append({"job_id": int(job_id), **dict(kwargs)})
        self._child_meta.update(dict(kwargs.get("meta_patch") or {}))
        return True

    def update_queue_delivery_meta(
        self,
        job_id: int,
        *,
        queue_delivery: dict,
        runtime_journal: list,
        status=None,
        expected_dispatch_states=None,
    ) -> bool:
        self._child_meta["queue_delivery"] = dict(queue_delivery)
        self._child_meta["runtime_journal"] = list(runtime_journal or [])
        return True

    def update_job_dispatch_outbox_envelope(self, job_id: int, *, encrypted_envelope: str):
        return bool(encrypted_envelope)

    def mark_job_dispatch_outbox(self, job_id: int, *, status: str, last_error=None):
        self.outbox_marks.append(
            {"job_id": int(job_id), "status": status, "last_error": last_error}
        )
        return True

    def log_event(self, *args, **kwargs):
        return None

    def record_public_progress_transition(self, **kwargs):
        self.runtime_calls.append({"public_progress": dict(kwargs)})

    def update_job_runtime(self, job_id: int, **kwargs):
        self.runtime_calls.append({"job_id": int(job_id), **dict(kwargs)})

    def merge_job_meta(self, job_id: int, patch: dict | None = None):
        self.meta_calls.append({"job_id": int(job_id), "patch": dict(patch or {})})

    def finish_job(self, job_id: int, **kwargs):
        self.finish_calls.append({"job_id": int(job_id), **dict(kwargs)})

    def update_job(self, job_id: int, **kwargs):
        payload = {"job_id": int(job_id), **dict(kwargs)}
        self.update_job_calls.append(payload)
        if int(job_id) == self.child_job_id:
            status = str(kwargs.get("status") or "").strip()
            if status:
                self._child_status_override = status

    def get_agent_messages(self, job_id: int, **kwargs):
        _ = kwargs
        if int(job_id) != self.child_job_id:
            return []
        return list(self.child_messages)


@pytest.fixture(autouse=True)
def _application_submission_repositories(monkeypatch: pytest.MonkeyPatch):
    """Keep orchestration doubles at the application repository boundary."""
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.jobs import task_enqueue_runtime
    from core.application.orchestration import invoke_workflow_step_node

    artifact_plane = ArtifactDatasetPlane()
    monkeypatch.setattr(
        invoke_workflow_step_node,
        "_job_invocation_artifact_plane",
        lambda: artifact_plane,
    )

    class _OutboxRepositoryDouble:
        def __init__(self, logger):
            self.logger = logger

        def replace_envelope(self, *, job_id, encrypted_envelope):
            return self.logger.update_job_dispatch_outbox_envelope(
                job_id, encrypted_envelope=encrypted_envelope
            )

        def mark(self, *, job_id, status, last_error=None):
            return self.logger.mark_job_dispatch_outbox(
                job_id, status=status, last_error=last_error
            )

        def renew_preparation_lease(self, *, job_id, owner_token, lease_seconds):
            return self.logger.renew_job_dispatch_preparation_lease(
                job_id, owner_token=owner_token, lease_seconds=lease_seconds
            )

        def finalize_prepared_payload(self, *, job_id, **kwargs):
            return self.logger.finalize_job_dispatch_payload(job_id, **kwargs)

    class _SubmissionRepositoryDouble:
        def __init__(self, logger, _transaction_conn):
            self.logger = logger

        def create(self, *, encrypted_envelope, outbox_status, **job_fields):
            owner_token = job_fields.pop("preparation_owner_token", None)
            lease_seconds = job_fields.pop("preparation_lease_seconds", 300)
            job_id = self.logger.create_job(**job_fields)
            self.logger.create_job_dispatch_outbox(
                job_id,
                encrypted_envelope=encrypted_envelope,
                status=outbox_status,
                preparation_owner_token=owner_token,
                preparation_lease_seconds=lease_seconds,
            )
            return job_id

    monkeypatch.setattr(
        task_enqueue_runtime,
        "_dispatch_outbox_repository",
        lambda logger: _OutboxRepositoryDouble(logger),
    )
    monkeypatch.setattr(
        "core.application.jobs.task_dispatch_common._dispatch_outbox_repository",
        lambda logger: _OutboxRepositoryDouble(logger),
    )
    monkeypatch.setattr(
        "core.application.jobs.job_submission_repository.JobSubmissionRepository",
        _SubmissionRepositoryDouble,
    )


def _install_runtime_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflows: dict[str, dict] | None = None,
    projected_workflows: dict[str, dict] | None = None,
    enqueue_error: Exception | None = None,
):
    monkeypatch.setenv("ENCRYPTION_KEY", "test-invoke-workflow-dispatch-key")
    workflows = dict(workflows or {})
    projected_workflows = dict(projected_workflows or {})
    fake_db = _FakeDB(workflows)
    enqueue_calls: list[dict] = []
    events: list[dict] = []
    runtime_updates: list[dict] = []

    fake_common = ModuleType("core.application.orchestration.agents.common")

    def _emit_event(*args, **kwargs):
        events.append({"args": args, "kwargs": kwargs})

    def _update_runtime_truth(*args, **kwargs):
        runtime_updates.append({"args": args, "kwargs": kwargs})

    fake_common.emit_event = _emit_event
    fake_common.update_runtime_truth = _update_runtime_truth
    fake_common.publish_agent_message = lambda *_args, **_kwargs: None

    fake_registry_mod = ModuleType("core.application.registry.registry_manager")

    class _RegistryManager:
        @staticmethod
        def instance():
            return SimpleNamespace(
                db_store=fake_db,
                workflows=SimpleNamespace(
                    get_workflow=lambda workflow_id: (
                        SimpleNamespace(**projected_workflows[workflow_id])
                        if workflow_id in projected_workflows
                        else None
                    )
                ),
            )

    fake_registry_mod.RegistryManager = _RegistryManager

    fake_worker_queue = ModuleType("core.infrastructure.runtime.worker.queue_publisher")

    def _enqueue_task(**kwargs):
        enqueue_calls.append(dict(kwargs))
        if enqueue_error is not None:
            raise enqueue_error

    fake_worker_queue.enqueue_task = _enqueue_task

    monkeypatch.setitem(sys.modules, "core.application.orchestration.agents.common", fake_common)
    monkeypatch.setitem(
        sys.modules, "core.application.registry.registry_manager", fake_registry_mod
    )
    monkeypatch.setitem(
        sys.modules, "core.infrastructure.runtime.worker.queue_publisher", fake_worker_queue
    )

    from core.application.orchestration import invoke_workflow_step_node as node

    links: list[dict] = []

    def _link_child(_conn, *, parent_job_id, child_job_id, entry):
        # The SQL link is owned by tests/integration; here it is recorded.
        links.append({"parent_job_id": parent_job_id, "child_job_id": child_job_id, **entry})

    def _await_child(state, _current, *, bl, child_job_id, invocation_key, child_revision):
        # The durable pause is owned by test_invoke_workflow_child_wait; these
        # tests drive the child outcome through the same seam as the finite path.
        del invocation_key, child_revision
        return node._wait_for_terminal_child(
            bl,
            child_job_id,
            parent_job_id=str(state.get("job_id") or ""),
            parent_agent="",
            child_workflow_id="",
        )

    # An in-memory artifact plane: the tests never depend on whatever plane
    # an earlier test left bound in the process.
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    isolated_plane = ArtifactDatasetPlane()
    monkeypatch.setattr(node, "_job_invocation_artifact_plane", lambda: isolated_plane)
    monkeypatch.setattr(node, "link_child_in_transaction", _link_child)
    monkeypatch.setattr(node, "await_child", _await_child)
    monkeypatch.setattr(node, "_mark_child_consumed", lambda *_args: None, raising=False)

    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.application.workflows.workflow_definition_snapshot import definition_digest

    def _archived_child(_repository, _job_id):
        child_id, child = next(iter({**fake_db._workflows, **projected_workflows}.items()))
        definition = {"settings": child.get("settings", {}), "steps": [], "groups": []}
        return {
            "workflow_id": child_id,
            "revision": 1,
            "definition_jsonb": definition,
            "digest": definition_digest(definition),
        }

    monkeypatch.setattr(WorkflowDefinitionSnapshotRepository, "load_for_job", _archived_child)

    return {
        "enqueue_calls": enqueue_calls,
        "events": events,
        "runtime_updates": runtime_updates,
        "links": links,
    }


def _base_state(step: dict | None = None) -> dict:
    """The parent run state, its cursor on *step* when one is given."""
    base = {
        "job_id": 77,
        "task_id": "TASK-77",
        "task_summary": "Build release plan",
        "project_name": "demo-project",
        "project_config": {"safe_mode": True},
        "mode": "brownfield",
        "workflow_id": "wf_parent",
        "reentry_context": {"existing": "keep"},
        "agent_messages": [],
    }
    return step_run_state(step, **base) if step is not None else base


def _capture_append_messages(monkeypatch: pytest.MonkeyPatch, node_module):
    captured: list[dict] = []

    def _fake_append_agent_message(**kwargs):
        message = {
            "from_agent": kwargs.get("from_agent"),
            "skill": kwargs.get("skill"),
            "status": kwargs.get("status"),
            "summary": kwargs.get("summary"),
            "artifacts": dict(kwargs.get("artifacts") or {}),
            "errors": list(kwargs.get("errors") or []),
        }
        captured.append(message)
        return message

    monkeypatch.setattr(node_module, "_append_agent_message", _fake_append_agent_message)
    return captured


@pytest.mark.parametrize(
    "selector",
    ["state.rows[abc].sku", "state.rows[0].sku | default('x')"],
)
def test_invoke_step_rejects_malformed_input_selector_before_registry_or_enqueue(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(monkeypatch, workflows={})
    captured = _capture_append_messages(monkeypatch, node)
    registry_module = sys.modules["core.application.registry.registry_manager"]
    monkeypatch.setattr(
        registry_module.RegistryManager,
        "instance",
        staticmethod(lambda: pytest.fail("registry lookup must not run")),
    )
    step = {
        "agent": "invoke_workflow",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "child-workflow",
                "input_mapping": {"sku": selector},
            }
        },
    }

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result["agent_messages"][-1]["status"] == "fail"
    assert captured[-1]["artifacts"] == {
        "_invoke_workflow_error": "invalid_input_selector",
        "selector": selector,
        "path": "override.invoke_workflow.input_mapping.sku",
    }
    assert runtime["runtime_updates"] == []
    assert runtime["enqueue_calls"] == []


def test_invoke_step_fails_when_spec_missing(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(monkeypatch, workflows={})
    captured = _capture_append_messages(monkeypatch, node)

    step = {"agent": "planner", "skill": "create_plan", "override": {}}

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result.get("agent_messages")
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert "missing" in str(msg["summary"]).lower()


def test_invoke_step_fails_on_input_mapping_resolution_error(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger()
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"ticket_id": "state.missing_ticket"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(
        node,
        "_resolve_invoke_input",
        lambda _state, _selector, key: (None, f"invoke.input_mapping.{key} missing source"),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result.get("agent_messages")
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert "input mapping" in str(msg["summary"]).lower()
    assert msg["artifacts"].get("_invoke_workflow_error") == "input_mapping_failed"
    assert msg["errors"]


@pytest.mark.parametrize("has_anchor", [False, True])
def test_invoke_step_resolves_callable_child_from_extension_projection(
    monkeypatch: pytest.MonkeyPatch,
    has_anchor: bool,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "extension.install-a.demo_child": {
                "workflow_id": "extension.install-a.demo_child",
                "projection_mode": "derived",
                "source_install_id": "install-a",
                "revision": 17,
                "settings": {"callable": {"enabled": False}},
            }
        }
        if has_anchor
        else {},
        projected_workflows={
            "extension.install-a.demo_child": {
                "workflow_id": "extension.install-a.demo_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "expected_inputs": [],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(child_job_id=933)
    step = {
        "agent": "invoke_workflow_step",
        "skill": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "extension.install-a.demo_child",
                "input_mapping": {},
            }
        },
    }
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert runtime["enqueue_calls"], captured[-1]
    assert "expected_workflow_revision" not in fake_logger.create_calls[0]["meta"]
    assert captured[-1]["status"] == "pass"
    assert result.get("agent_messages")


def test_child_enqueue_keeps_secret_only_in_runtime_dispatch_envelope(
    monkeypatch: pytest.MonkeyPatch,
):
    import json

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.jobs.job_invocation_artifacts import load_replay_snapshot
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.registry.workflow_contract import WorkflowInterface
    from core.application.registry.workflow_secret_defaults import workflow_secret_launch_snapshot
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection
    from core.infrastructure.runtime.test_mode import parse_test_mode
    from core.infrastructure.security.secret_reference import SecretReference, is_secret_reference

    child_settings = {
        "callable": {
            "enabled": True,
            "trigger_channels": ["parent_workflow"],
            "expected_inputs": [{"name": "credential", "type": "secret", "required": True}],
            "output_mode": "none",
        }
    }
    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": child_settings,
            }
        },
    )
    fake_logger = _FakeLogger(child_job_id=934)
    artifact_plane = ArtifactDatasetPlane()
    step = {
        "agent": "invoke_workflow_step",
        "skill": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"credential": "state.inputs.store_api_token"},
            }
        },
    }
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_job_invocation_artifact_plane", lambda: artifact_plane)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )
    reference = SecretReference(
        name="store_api_token",
        ciphertext="inline-child-ciphertext",
    ).to_wire()
    parent_interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["api"],
                "expected_inputs": [
                    {"name": "store_api_token", "type": "secret", "required": True}
                ],
                "output_mode": "none",
            }
        }
    )
    state = {
        **_base_state(step),
        "inputs": {"store_api_token": reference},
        "_workflow_secret_ref_fingerprints": workflow_secret_launch_snapshot(
            parent_interface,
            inputs={"store_api_token": reference},
        ),
    }

    node.invoke_workflow_step_node(state)

    persisted_meta = fake_logger.create_calls[0]["meta"]
    assert "inline-child-ciphertext" not in json.dumps(persisted_meta)
    assert "ref_key" not in json.dumps(persisted_meta)
    assert "inputs" not in persisted_meta
    assert "task_envelope" not in persisted_meta
    dispatched_state = runtime["enqueue_calls"][0]["extra_state"]
    assert is_secret_reference(dispatched_state["inputs"]["credential"])
    assert (
        dispatched_state["_workflow_secret_ref_fingerprints"]
        == state["_workflow_secret_ref_fingerprints"]
    )
    snapshot = load_replay_snapshot(
        artifact_plane=artifact_plane,
        pointer=fake_logger._child_meta["replay_snapshot"],
        project_id="7",
        job_id="934",
    )
    assert (
        snapshot["_workflow_secret_ref_fingerprints"] == state["_workflow_secret_ref_fingerprints"]
    )
    rerun_state, _ = build_rerun_workflow_selection(
        {"id": "934", "meta": {}},
        replay_snapshot=snapshot,
        to_json_obj=lambda value: value,
        parse_test_mode=parse_test_mode,
    )
    assert (
        rerun_state["_workflow_secret_ref_fingerprints"]
        == state["_workflow_secret_ref_fingerprints"]
    )
    assert is_secret_reference(rerun_state["inputs"]["credential"])


def test_invoke_step_ignores_empty_optional_child_input_mapping(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [
                            {"name": "request", "type": "paragraph", "required": True},
                            {"name": "task_type", "type": "string", "required": False},
                        ],
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=932,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "plan": {"title": "Release plan"},
                    }
                },
            }
        ],
    )

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {
                    "request": "state.task_summary",
                    "task_type": "state.task_type",
                },
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setattr(
        node,
        "_resolve_invoke_input",
        lambda state, selector, _key: (
            (state.get("task_summary"), "")
            if selector == "state.task_summary"
            else (state.get("task_type"), "")
        ),
    )
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert runtime["enqueue_calls"], "child workflow should be enqueued"
    extra_state = runtime["enqueue_calls"][0].get("extra_state") or {}
    inputs = extra_state.get("inputs") or {}
    assert inputs.get("request") == "Build release plan"
    assert inputs.get("task_summary") == "Build release plan"
    assert "task_type" not in inputs
    assert "workflow_input_payload" not in extra_state
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"].get("child_plan") == {"title": "Release plan"}
    assert result.get("agent_messages")


def test_invoke_step_blocks_direct_self_recursion(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(monkeypatch, workflows={})
    captured = _capture_append_messages(monkeypatch, node)

    step = {
        "agent": "invoke_workflow",
        "skill": "run_child",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_parent",
                "input_mapping": {},
                "output_mapping": {},
            }
        },
    }

    result = node.invoke_workflow_step_node(_base_state(step))

    assert runtime["enqueue_calls"] == []
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert msg["artifacts"]["_invoke_workflow_error"] == "recursive_workflow_invocation"
    assert msg["artifacts"]["_invoke_workflow_cycle_path"] == "wf_parent -> wf_parent"
    assert result.get("agent_messages")


def test_invoke_step_blocks_calling_any_ancestor_workflow(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(monkeypatch, workflows={})
    captured = _capture_append_messages(monkeypatch, node)

    step = {
        "agent": "invoke_workflow",
        "skill": "run_parent",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_parent",
                "input_mapping": {},
                "output_mapping": {},
            }
        },
    }
    state = dict(
        _base_state(step),
        workflow_id="wf_grandchild",
        parent_workflow_id="wf_child",
        invoke_workflow_ancestry=["wf_parent", "wf_child", "wf_grandchild"],
    )

    result = node.invoke_workflow_step_node(state)

    assert runtime["enqueue_calls"] == []
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert msg["artifacts"]["_invoke_workflow_error"] == "recursive_workflow_invocation"
    assert msg["artifacts"]["_invoke_workflow_ancestry"] == [
        "wf_parent",
        "wf_child",
        "wf_grandchild",
    ]
    assert msg["artifacts"]["_invoke_workflow_cycle_path"] == (
        "wf_parent -> wf_child -> wf_grandchild -> wf_parent"
    )
    assert result.get("agent_messages")


def test_invoke_step_fails_when_child_status_not_completed(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(child_job_id=915)

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "failed", "error": "child exploded"},
            "failed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert runtime["enqueue_calls"], "child workflow should be enqueued"
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert msg["artifacts"].get("_invoke_workflow_child_status") == "failed"
    assert msg["artifacts"].get("_invoke_workflow_child_job_id") == 915
    # Job Details showed "No step inputs were recorded" for a failed child.
    assert msg["artifacts"]["input_payload"] == {"task_summary": _base_state(step)["task_summary"]}
    assert msg["artifacts"]["output_payload"] == {"child_status": "failed", "child_job": "915"}
    assert result.get("agent_messages")


def test_invoke_step_marks_child_failed_when_enqueue_raises(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
        enqueue_error=RuntimeError("mq unavailable"),
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(child_job_id=944)

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        lambda self, **kwargs: None,
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert fake_logger.finish_calls == [], "child path must not use direct finish_job"
    assert fake_logger.outbox_marks[-1]["job_id"] == 944
    assert fake_logger.outbox_marks[-1]["status"] == "failed"
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert "enqueue failed" in str(msg["summary"]).lower()
    assert result.get("agent_messages")


def test_invoke_step_success_maps_child_outputs_to_artifacts_and_state(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "plan", "type": "json", "required": True},
                            {"name": "meta", "type": "json", "required": True},
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=977,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "plan": {"title": "Release plan"},
                        "meta": {"source": "child"},
                    }
                },
            }
        ],
    )

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {
                    "plan": "child_plan",
                    "meta": "state.reentry_context.child_meta",
                },
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert runtime["enqueue_calls"], "child workflow should be enqueued"
    assert result["reentry_context"]["existing"] == "keep"
    assert result["reentry_context"]["child_meta"] == {"source": "child"}
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["child_plan"] == {"title": "Release plan"}
    assert msg["artifacts"]["child_meta"] == {"source": "child"}
    assert msg["artifacts"]["_state_patch"]["reentry_context"]["child_meta"] == {"source": "child"}
    assert msg["artifacts"]["_state_patch"]["workflow_data"]["reentry_context"]["child_meta"] == {
        "source": "child"
    }
    assert msg["artifacts"]["_invoke_workflow"]["child_job_id"] == 977
    assert result.get("agent_messages")


def test_invoke_step_adopts_declared_child_artifact_for_parent_retention(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {"callable": {"enabled": True}},
                "steps": [
                    {
                        "step_id": "export",
                        "step_kind": "file_from_base64",
                        "override": {
                            "file_from_base64": {
                                "source": "state.inputs.body",
                                "result_target": "state.catalog",
                            },
                            "returned_files": [
                                {"result": "file", "key": "catalog", "required": True}
                            ],
                        },
                    }
                ],
            }
        },
    )
    artifact_plane = ArtifactDatasetPlane()
    handle, _metadata = artifact_plane.create_artifact(
        owner_scope={"project_id": "7", "job_id": "977"},
        run_id="977",
        step_id="export",
        filename="catalog.csv",
        content_type="text/csv",
        body=b"sku,qty\nA,1\n",
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=977,
        child_messages=[
            {
                "id": 1,
                "envelope": {"artifacts": {"catalog": f"artifact:{handle.artifact_id}"}},
            }
        ],
    )
    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "artifact_mapping": {"catalog": "child_catalog"},
            }
        },
    }
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_job_invocation_artifact_plane", lambda: artifact_plane)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert captured[-1]["status"] == "pass"
    assert captured[-1]["artifacts"]["child_catalog"] == f"artifact:{handle.artifact_id}"
    transferred = artifact_plane.get_artifact(handle.artifact_id)
    assert transferred is not None
    assert transferred["owner_scope"] == {"project_id": "7", "job_id": "77"}
    assert transferred["producer_job_id"] == "977"
    assert result.get("agent_messages")


def test_invoke_step_collects_declared_outputs_from_state_patch_artifacts(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "child_marker", "type": "text", "required": True}
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=978,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "_state_patch": {
                            "output": {
                                "child_marker": "done",
                            }
                        }
                    }
                },
            }
        ],
    )

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"child_marker": "state.reentry_context.child_marker"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result["reentry_context"]["child_marker"] == "done"
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["child_marker"] == "done"
    assert msg["artifacts"]["_state_patch"]["reentry_context"]["child_marker"] == "done"
    assert msg["artifacts"]["_invoke_workflow"]["child_job_id"] == 978


def test_invoke_step_collects_declared_outputs_from_child_checkpoint_state(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.infrastructure.runtime.control import state_reader

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "review_cycle": {
                "workflow_id": "review_cycle",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "review_report", "type": "paragraph", "required": True},
                            {
                                "name": "developer_verification",
                                "type": "object",
                                "required": True,
                            },
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=979,
        child_job_extra={"thread_id": "child-thread-979"},
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "workflow_terminal": {"status": "pass"},
                    }
                },
            }
        ],
    )
    monkeypatch.setattr(
        state_reader,
        "get_thread_state_values",
        lambda thread_id, *, force_refresh=False: (
            {
                "outputs": {
                    "review_report": "Review passed.",
                    "developer_verification": {
                        "checks_run": [{"name": "pytest", "result": "pass"}]
                    },
                }
            }
            if thread_id == "child-thread-979" and force_refresh
            else {}
        ),
    )

    step = {
        "agent": "invoke_workflow",
        "skill": "run_story",
        "override": {
            "invoke_workflow": {
                "workflow_id": "review_cycle",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {
                    "review_report": "review_report",
                    "developer_verification": "developer_verification",
                },
            }
        },
    }
    monkeypatch.setattr(
        node,
        "_resolve_invoke_input",
        lambda state, selector, key: (state.get("task_summary"), ""),
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["review_report"] == "Review passed."
    assert msg["artifacts"]["developer_verification"] == {
        "checks_run": [{"name": "pytest", "result": "pass"}]
    }
    assert msg["artifacts"]["_invoke_workflow"]["child_job_id"] == 979
    assert result["agent_messages"]


def test_invoke_step_collects_transform_output_from_child_workflow_data(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.infrastructure.runtime.control import state_reader

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "catalog_rows", "type": "json", "required": True}
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    rows = [{"sku": "SKU-1"}, {"sku": "SKU-2"}]
    fake_logger = _FakeLogger(
        child_job_id=981,
        child_job_extra={"thread_id": "child-thread-981"},
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "step_kind": "transform",
                        "transform": {
                            "ok": True,
                            "detail": "transform step executed",
                            "errors": [],
                            "result_target": "state.catalog_rows",
                        },
                    }
                },
            }
        ],
    )
    monkeypatch.setattr(
        state_reader,
        "get_thread_state_values",
        lambda thread_id, *, force_refresh=False: (
            {"workflow_data": {"catalog_rows": rows}}
            if thread_id == "child-thread-981" and force_refresh
            else {}
        ),
    )
    step = {
        "agent": "invoke_workflow",
        "skill": "run_child",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"catalog_rows": "state.imported_rows"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result["imported_rows"] == rows
    assert captured[-1]["status"] == "pass"
    assert captured[-1]["artifacts"]["imported_rows"] == rows


def test_invoke_step_can_map_story_outputs_to_state_evidence(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "review_cycle": {
                "workflow_id": "review_cycle",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "code_output", "type": "text", "required": True},
                            {"name": "git_diff", "type": "text", "required": True},
                            {"name": "files_changed", "type": "json", "required": True},
                            {"name": "workspace_state", "type": "text", "required": True},
                            {"name": "item_verdict", "type": "text", "required": True},
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=1007,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "code_output": "Implemented story slice",
                        "git_diff": "diff --git a/a.py b/a.py",
                        "files_changed": ["a.py", "b.py"],
                        "workspace_state": "clean",
                        "item_verdict": "pass",
                    }
                },
            }
        ],
    )

    step = {
        "agent": "facilitator",
        "skill": "draft_sections",
        "override": {
            "invoke_workflow": {
                "workflow_id": "review_cycle",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {
                    "code_output": "state.code_output",
                    "git_diff": "state.git_diff",
                    "files_changed": "state.files_changed",
                    "workspace_state": "state.workspace_state",
                    "item_verdict": "item_verdict",
                },
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result["code_output"] == "Implemented story slice"
    assert result["git_diff"] == "diff --git a/a.py b/a.py"
    assert result["files_changed"] == ["a.py", "b.py"]
    assert result["workspace_state"] == "clean"
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["git_diff"] == "diff --git a/a.py b/a.py"
    assert msg["artifacts"]["files_changed"] == ["a.py", "b.py"]
    assert msg["artifacts"]["workspace_state"] == "clean"
    assert msg["artifacts"]["item_verdict"] == "pass"


def test_invoke_step_prefers_latest_child_outputs_after_rework(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "review_cycle": {
                "workflow_id": "review_cycle",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "review_verdict", "type": "text", "required": True},
                            {"name": "blocking_issues", "type": "json", "required": True},
                            {"name": "checks_run", "type": "json", "required": True},
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=1017,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "review_verdict": "fail",
                        "blocking_issues": ["lint error"],
                        "checks_run": [{"check": "linting", "result": "fail"}],
                    }
                },
            },
            {
                "id": 2,
                "envelope": {
                    "artifacts": {
                        "review_verdict": "pass",
                        "blocking_issues": [],
                        "checks_run": [{"check": "linting", "result": "pass"}],
                    }
                },
            },
        ],
    )

    step = {
        "agent": "facilitator",
        "skill": "draft_sections",
        "override": {
            "invoke_workflow": {
                "workflow_id": "review_cycle",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {
                    "review_verdict": "state.review_verdict",
                    "blocking_issues": "state.blocking_issues",
                    "checks_run": "state.checks_run",
                },
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result["review_verdict"] == "pass"
    assert result["blocking_issues"] == []
    assert result["checks_run"] == [{"check": "linting", "result": "pass"}]
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["_invoke_workflow"]["child_job_id"] == 1017


def test_invoke_step_allows_missing_optional_child_outputs(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "review_cycle": {
                "workflow_id": "review_cycle",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "code_output", "type": "text", "required": True},
                            {"name": "commit_sha", "type": "text", "required": False},
                        ],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=1018,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "code_output": "Implemented story slice",
                    }
                },
            }
        ],
    )

    step = {
        "agent": "facilitator",
        "skill": "draft_sections",
        "override": {
            "invoke_workflow": {
                "workflow_id": "review_cycle",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {
                    "code_output": "state.code_output",
                    "commit_sha": "state.commit_sha",
                },
            }
        },
    }
    monkeypatch.setattr(
        node,
        "_resolve_invoke_input",
        lambda state, selector, key: (state.get("task_summary"), ""),
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert result["code_output"] == "Implemented story slice"
    assert "commit_sha" not in result
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["_invoke_workflow"]["child_job_id"] == 1018
    assert msg["artifacts"]["_invoke_workflow"]["declared_outputs"] == ["code_output", "commit_sha"]
    assert "commit_sha" not in msg["artifacts"]


def test_invoke_step_propagates_test_mode_to_child_job(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=978,
        child_messages=[
            {"id": 1, "envelope": {"artifacts": {"plan": {"ok": True}}}},
        ],
    )

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    state = _base_state(step)
    state["test_mode"] = True
    node.invoke_workflow_step_node(state)

    assert runtime["enqueue_calls"], "child workflow should be enqueued"
    enqueue_payload = runtime["enqueue_calls"][0]
    assert enqueue_payload["extra_state"]["test_mode"] is True
    assert fake_logger.create_calls, "child job should be created"
    assert fake_logger.create_calls[0]["meta"]["test_mode"] is True


def test_invoke_step_executes_child_inline_without_queue_enqueue(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.child_execution_runtime import (
        ChildExecutionRuntime,
        bind_child_execution_runtime,
    )

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=999,
        child_messages=[{"id": 1, "envelope": {"artifacts": {"plan": {"ok": True}}}}],
        parent_job={"worker_id": "worker-1", "meta": {"invoke_lane_placement": True}},
    )
    inline_calls: list[dict] = []

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    child_runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda task_message: (
            inline_calls.append(dict(task_message))
            or {"action": "ack", "final_status": "completed"}
        ),
    )
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    with bind_child_execution_runtime(child_runtime):
        result = node.invoke_workflow_step_node(_base_state(step))

    assert inline_calls, "inline execution path should call worker runtime"
    assert runtime["enqueue_calls"] == [], "inline execution must not enqueue child task"
    msg = captured[-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["child_plan"] == {"ok": True}
    assert result.get("agent_messages")


def test_inline_child_execution_does_not_inherit_parent_callback_context() -> None:
    from core.application.orchestration.child_execution_runtime import (
        ChildExecutionRuntime,
        bind_child_execution_runtime,
    )
    from core.application.orchestration.invoke_workflow_step_node_runtime import (
        _execute_child_job_inline,
    )

    parent_callback_context = ContextVar("parent_callback_context", default="isolated")
    parent_token = parent_callback_context.set("parent-job")
    observed_contexts: list[str] = []
    child_runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda _task_message: (
            observed_contexts.append(parent_callback_context.get())
            or {"action": "ack", "final_status": "completed"}
        ),
    )

    try:
        with bind_child_execution_runtime(child_runtime):
            result = _execute_child_job_inline({"job_id": "child-job"})
        assert parent_callback_context.get() == "parent-job"
    finally:
        parent_callback_context.reset(parent_token)

    assert result["action"] == "ack"
    assert observed_contexts == ["isolated"]


def test_invoke_step_without_bound_child_runtime_uses_durable_queue(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {"callable": {"enabled": True}},
            }
        },
    )
    fake_logger = _FakeLogger(
        child_job_id=1002,
        parent_job={"worker_id": "worker-1", "meta": {"invoke_lane_placement": True}},
    )
    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    assert len(runtime["enqueue_calls"]) == 1
    assert runtime["enqueue_calls"][0]["job_id"] == 1002
    assert fake_logger.outbox_marks[-1]["status"] == "published"
    assert result.get("agent_messages")


def test_invoke_step_inline_retry_falls_back_to_durable_queue(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.child_execution_runtime import (
        ChildExecutionRuntime,
        bind_child_execution_runtime,
    )

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {"callable": {"enabled": True}},
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=1000,
        parent_job={"worker_id": "worker-1", "meta": {"invoke_lane_placement": True}},
    )
    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    child_runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda _task_message: {
            "action": "retry",
            "reason": "job_lookup_failed",
        },
    )
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    with bind_child_execution_runtime(child_runtime):
        result = node.invoke_workflow_step_node(_base_state(step))

    assert len(runtime["enqueue_calls"]) == 1
    assert runtime["enqueue_calls"][0]["job_id"] == 1000
    assert fake_logger.outbox_marks[-1]["status"] == "published"
    assert captured[-1]["status"] == "pass"
    assert result.get("agent_messages")


def test_invoke_step_inline_exception_does_not_fallback_to_enqueue(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.child_execution_runtime import (
        ChildExecutionRuntime,
        bind_child_execution_runtime,
    )

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=1001,
        parent_job={"worker_id": "worker-1", "meta": {"invoke_lane_placement": True}},
    )

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    child_runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda _task_message: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "failed", "error": "inline failure"},
            "failed",
        ),
    )

    with bind_child_execution_runtime(child_runtime):
        result = node.invoke_workflow_step_node(_base_state(step))

    assert runtime["enqueue_calls"] == [], "inline path must not enqueue after inline failure"
    msg = captured[-1]
    assert msg["status"] == "fail"
    assert msg["artifacts"].get("_invoke_workflow_error") == "enqueue_failed"
    assert result.get("agent_messages")


def test_wait_for_terminal_child_heartbeats_parent_job_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    fake_logger = _FakeLogger(
        child_job_id=1201,
        child_status_sequence=["pending", "pending", "completed"],
    )
    current_time = [0.0]

    def _fake_time() -> float:
        return current_time[0]

    def _fake_sleep(seconds: float) -> None:
        current_time[0] += float(seconds)

    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    monkeypatch.setattr(node.time, "time", _fake_time)
    monkeypatch.setattr(node.time, "sleep", _fake_sleep)

    row, status = node._wait_for_terminal_child(
        fake_logger,
        1201,
        parent_job_id=77,
        parent_agent="facilitator",
        child_workflow_id="review_cycle",
    )

    assert status == "completed"
    assert row
    assert row.get("id") == 1201
    parent_runtime_calls = [call for call in fake_logger.runtime_calls if call.get("job_id") == 77]
    assert parent_runtime_calls
    assert all(call.get("heartbeat_now") is True for call in parent_runtime_calls)
    assert all("current_phase" not in call for call in parent_runtime_calls)
    assert all(call.get("current_agent") == "facilitator" for call in parent_runtime_calls)


def test_wait_for_terminal_child_timeout_requests_cancel_by_default(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    fake_logger = _FakeLogger(
        child_job_id=1301,
        child_status_sequence=["pending", "pending", "pending"],
    )
    current_time = [0.0]

    def _fake_time() -> float:
        return current_time[0]

    def _fake_sleep(seconds: float) -> None:
        current_time[0] += float(seconds)

    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS", "120")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    monkeypatch.setenv("FS_INVOKE_CHILD_CANCEL_ON_TIMEOUT", "1")
    monkeypatch.setattr(node.time, "time", _fake_time)
    monkeypatch.setattr(node.time, "sleep", _fake_sleep)
    terminal_calls: list[dict] = []
    monkeypatch.setattr(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        lambda _self, **kwargs: terminal_calls.append(dict(kwargs)),
    )

    row, status = node._wait_for_terminal_child(
        fake_logger,
        1301,
        parent_job_id=77,
        parent_agent="facilitator",
        child_workflow_id="review_cycle",
    )

    assert status == "timeout"
    assert fake_logger.update_job_calls == []
    assert terminal_calls == [
        {
            "job_id": "1301",
            "status": "cancelled",
            "error_message": "invoke_workflow timeout after 30s for child workflow 'review_cycle'",
            "error_code": "invoke_workflow_timeout",
            "category": "cancellation",
        }
    ]
    assert row
    assert str(row.get("status") or "").strip().lower() == "pending"


def test_wait_for_terminal_child_timeout_skips_cancel_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    fake_logger = _FakeLogger(
        child_job_id=1302,
        child_status_sequence=["pending", "pending", "pending"],
    )
    current_time = [0.0]

    def _fake_time() -> float:
        return current_time[0]

    def _fake_sleep(seconds: float) -> None:
        current_time[0] += float(seconds)

    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS", "120")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    monkeypatch.setenv("FS_INVOKE_CHILD_CANCEL_ON_TIMEOUT", "0")
    monkeypatch.setattr(node.time, "time", _fake_time)
    monkeypatch.setattr(node.time, "sleep", _fake_sleep)

    row, status = node._wait_for_terminal_child(
        fake_logger,
        1302,
        parent_job_id=77,
        parent_agent="facilitator",
        child_workflow_id="review_cycle",
    )

    assert status == "timeout"
    assert fake_logger.update_job_calls == []
    assert row
    assert str(row.get("status") or "").strip().lower() == "pending"


def test_resolve_invoke_child_timeout_seconds_clamps_to_cap(monkeypatch: pytest.MonkeyPatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "99999")
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS", "120")

    assert node._resolve_invoke_child_timeout_seconds() == 120


def test_resolve_invoke_child_timeout_seconds_falls_back_for_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.orchestration import invoke_workflow_step_node as node

    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "invalid")
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS", "0")

    assert node._resolve_invoke_child_timeout_seconds() == 5400


# ---------------------------------------------------------------------------
# Tests for child task ID and summary helpers
# ---------------------------------------------------------------------------


def test_build_child_task_id_has_parent_workflow_step_and_timestamp_segments():
    from core.application.orchestration.invoke_workflow_step_node import _build_child_task_id

    task_id = _build_child_task_id("parent-task-abc", "story_loop", "review_cycle")
    assert task_id.startswith("parent-task-abc:invoke:review_cycle:story_loop:")
    # Exactly 5 colon-delimited parts: parent, "invoke", child, step, timestamp
    assert task_id.count(":") == 4


def test_build_child_summary_uses_base_format():
    from core.application.orchestration.invoke_workflow_step_node import _build_child_summary

    summary = _build_child_summary(
        "Parent task",
        "review_cycle",
        "story_loop",
    )
    assert summary == "[invoke_workflow:review_cycle] Parent task"


def test_build_child_summary_names_a_hook_never_its_storage_workflow():
    from core.application.orchestration.invoke_workflow_step_node import _build_child_summary

    with_parent = _build_child_summary(
        "Parent task", "hook_storage_wf_1", "story_loop", hook_label="Before finish"
    )
    without_parent = _build_child_summary(
        "", "hook_storage_wf_1", "story_loop", hook_label="Before finish"
    )

    assert with_parent == "[invoke_workflow:hook] Parent task"
    assert without_parent == "Hook 'Before finish' from step 'story_loop'"
    assert "hook_storage_wf_1" not in with_parent + without_parent


def test_derive_child_task_summary_prefers_scoped_item_over_parent_summary():
    from core.application.orchestration.invoke_workflow_step_node import _derive_child_task_summary

    child_summary = _derive_child_task_summary(
        "Build a new SaaS project management tool from scratch",
        "review_cycle",
        "story_loop",
        child_inputs={
            "task_summary": "Build a new SaaS project management tool from scratch",
            "current_story": {
                "id": "STORY-007",
                "title": "Add rate limiting middleware",
            },
        },
    )

    assert child_summary == "STORY-007: Add rate limiting middleware"


def test_derive_child_task_summary_preserves_explicit_child_scope():
    from core.application.orchestration.invoke_workflow_step_node import _derive_child_task_summary

    child_summary = _derive_child_task_summary(
        "Parent task",
        "review_cycle",
        "story_loop",
        child_inputs={
            "task_summary": "Implement tenant-aware project listing API",
            "current_story": {
                "id": "STORY-008",
                "title": "Tenant-aware project list",
            },
        },
    )

    assert child_summary == "Implement tenant-aware project listing API"


def test_invoke_step_uses_ephemeral_child_runtime_without_a_durable_parent(monkeypatch):
    import core.application.orchestration.invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_runtime_mod = ModuleType("core.application.workflows.platform_test_run_runtime")
    runtime_calls: list[dict] = []

    def _fake_run_workflow_test_run_ephemeral(**kwargs):
        runtime_calls.append(dict(kwargs))
        return {
            "status": "completed",
            "final_status": "completed",
            "outputs": {"plan": {"status": "ok", "source": "ephemeral-child"}},
            "artifacts": {},
            "trace": [{"phase_id": "complete", "status": "completed"}],
        }

    fake_runtime_mod.run_workflow_test_run_ephemeral = _fake_run_workflow_test_run_ephemeral
    monkeypatch.setitem(
        sys.modules,
        "core.application.workflows.platform_test_run_runtime",
        fake_runtime_mod,
    )

    step = {
        "agent": "planner",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "output_mapping": {"plan": "state.child_plan"},
            }
        },
    }
    monkeypatch.setattr(node, "_get_logger", lambda: _FakeLogger())

    state = dict(
        _base_state(step),
        job_id=None,
        project_id="project-77",
        project_config={"project_id": "project-77"},
        task_source="workflow_test_run",
        test_mode=True,
    )

    result = node.invoke_workflow_step_node(state)

    assert runtime_calls, "workflow_test_run should execute child workflow ephemerally"
    assert runtime["enqueue_calls"] == [], "ephemeral child run must not enqueue a normal child job"
    assert not runtime_calls[0]["extra_state"].get("job_id")
    assert result["child_plan"] == {"status": "ok", "source": "ephemeral-child"}
    assert captured[-1]["status"] == "pass"
    assert captured[-1]["artifacts"]["_invoke_workflow"]["child_workflow_id"] == "wf_child"


def test_invoke_step_scopes_child_task_summary_from_child_inputs(monkeypatch):
    import json

    import core.application.orchestration.invoke_workflow_step_node as node

    current_story = {"id": "STORY-009", "title": "Ship project summary API"}
    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "review_cycle": {
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [
                            {"name": "story_summary", "type": "text", "required": True},
                            {"name": "current_story", "type": "object", "required": True},
                        ],
                        "expected_outputs": [{"name": "code_output"}],
                    }
                }
            }
        },
    )
    fake_logger = _FakeLogger(
        child_job_id=2004,
        child_messages=[{"envelope": {"artifacts": {"code_output": "# ok"}}}],
        child_status_sequence=["completed"],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    _capture_append_messages(monkeypatch, node)

    state = dict(
        _base_state(
            {
                "agent": "invoke_workflow",
                "skill": "draft_sections",
                "override": {
                    "invoke_workflow": {
                        "workflow_id": "review_cycle",
                        "input_mapping": {
                            "story_summary": "state.task_summary",
                            "current_story": "literal:" + json.dumps(current_story),
                        },
                        "output_mapping": {"code_output": "code_output"},
                    }
                },
            }
        ),
        task_summary="Build a new SaaS project management tool from scratch",
        workflow_id="weekly_report",
    )

    node.invoke_workflow_step_node(state)

    assert fake_logger.create_calls, "child job should have been created"
    child_summary = fake_logger.create_calls[0].get("task_summary", "")
    assert "STORY-009" in child_summary
    assert "Ship project summary API" in child_summary
    assert "Build a new SaaS project management tool from scratch" not in child_summary

    assert runtime["enqueue_calls"], "child workflow should be enqueued"
    extra_state = runtime["enqueue_calls"][0].get("extra_state") or {}
    inputs = extra_state.get("inputs") or {}
    assert extra_state.get("task_summary") == "STORY-009: Ship project summary API"
    assert inputs.get("task_summary") == "STORY-009: Ship project summary API"
    assert inputs.get("story_summary") == ("Build a new SaaS project management tool from scratch")
    task_envelope = extra_state.get("task_envelope") or {}
    assert task_envelope.get("requested_workflow_id") == "review_cycle"
    assert task_envelope.get("project_id") == "7"
    assert task_envelope.get("title") == "STORY-009: Ship project summary API"
    assert (task_envelope.get("inputs") or {}).get("story_summary") == (
        "Build a new SaaS project management tool from scratch"
    )
    child_meta = fake_logger.create_calls[0].get("meta") or {}
    assert "task_envelope" not in child_meta
    assert "inputs" not in child_meta
    assert "workflow_input_payload" not in child_meta
    assert child_meta["invocation"]["mapped_inputs"]["current_story"]["id"] == "STORY-009"


def test_invoke_step_never_grants_child_automatic_approval(monkeypatch):
    import core.application.orchestration.invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "review_cycle": {
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [],
                        "expected_outputs": [{"name": "code_output"}],
                    }
                }
            }
        },
    )
    fake_logger = _FakeLogger(
        child_job_id=2002,
        child_messages=[{"envelope": {"artifacts": {"code_output": "# ok"}}}],
        child_status_sequence=["completed"],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    _capture_append_messages(monkeypatch, node)

    state = dict(
        _base_state(
            {
                "agent": "invoke_workflow",
                "skill": "draft_sections",
                "override": {
                    "invoke_workflow": {
                        "workflow_id": "review_cycle",
                        "input_mapping": {},
                        "output_mapping": {"code_output": "code_output"},
                    }
                },
            }
        ),
        workflow_id="weekly_report",
    )

    node.invoke_workflow_step_node(state)

    assert runtime["enqueue_calls"], "child workflow should be enqueued"
    enqueue_payload = runtime["enqueue_calls"][0]
    extra_state = enqueue_payload.get("extra_state") or {}
    assert "invoke_child_auto_approve" not in extra_state
    # The child is linked to its parent in the creating transaction.
    assert runtime["links"]
    assert runtime["links"][0]["invocation_key"]


def test_invoke_step_allows_child_workflow_to_invoke_another_child(monkeypatch):
    import core.application.orchestration.invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [],
                        "expected_outputs": [{"name": "child_result", "type": "text"}],
                    }
                },
            },
            "wf_grandchild": {
                "workflow_id": "wf_grandchild",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [],
                        "expected_outputs": [{"name": "grandchild_result", "type": "text"}],
                    }
                },
            },
        },
    )
    _capture_append_messages(monkeypatch, node)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    parent_step = {
        "agent": "invoke_workflow",
        "skill": "run_child",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {},
                "output_mapping": {"child_result": "state.reentry_context.child_result"},
            }
        },
    }
    parent_logger = _FakeLogger(
        child_job_id=3101,
        child_messages=[{"envelope": {"artifacts": {"child_result": "child complete"}}}],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: parent_logger)

    parent_state = _base_state({**parent_step, "step_id": "call_child"})
    parent_result = node.invoke_workflow_step_node(parent_state)

    assert parent_result["reentry_context"]["child_result"] == "child complete"
    assert runtime["enqueue_calls"], "parent workflow should enqueue first child"
    child_extra_state = runtime["enqueue_calls"][-1]["extra_state"]
    assert child_extra_state[node.INVOKE_WORKFLOW_ANCESTRY_KEY] == ["wf_parent", "wf_child"]
    assert child_extra_state["invoke_parent_step_id"] == "call_child"
    assert "invoke_parent_phase_id" not in child_extra_state
    child_meta = parent_logger.create_calls[0]["meta"]
    assert child_meta["invoke_parent_step_id"] == "call_child"
    assert ":call_child:" in runtime["enqueue_calls"][-1]["task_id"]

    grandchild_step = {
        "agent": "invoke_workflow",
        "skill": "run_grandchild",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_grandchild",
                "input_mapping": {},
                "output_mapping": {"grandchild_result": "state.reentry_context.grandchild_result"},
            }
        },
    }
    grandchild_logger = _FakeLogger(
        child_job_id=3102,
        child_messages=[{"envelope": {"artifacts": {"grandchild_result": "grandchild complete"}}}],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: grandchild_logger)

    child_state = {
        **_base_state(grandchild_step),
        **child_extra_state,
        "job_id": 88,
        "task_id": "TASK-88",
        "agent_messages": [],
    }
    child_result = node.invoke_workflow_step_node(child_state)

    assert child_result["reentry_context"]["grandchild_result"] == "grandchild complete"
    assert runtime["enqueue_calls"][-1]["extra_state"][node.INVOKE_WORKFLOW_ANCESTRY_KEY] == [
        "wf_parent",
        "wf_child",
        "wf_grandchild",
    ]
    assert grandchild_logger.create_calls[0]["meta"][node.INVOKE_WORKFLOW_ANCESTRY_KEY] == [
        "wf_parent",
        "wf_child",
        "wf_grandchild",
    ]


INVOKE_EXECUTION_ID = "0d5a3c1e-7b2f-4e8a-9c6d-1f2e3a4b5c6d"
PREVIOUS_EXECUTION_ID = "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
CONTENT_PIPELINE_STEP = {
    "step_id": "run_pipeline",
    "agent": "invoke_workflow",
    "skill": "run_child",
    "override": {
        "invoke_workflow": {
            "workflow_id": "content_pipeline",
            "input_mapping": {},
            "output_mapping": {"workflow_output": "workflow_output"},
        }
    },
}


def _content_pipeline_runtime(monkeypatch):
    return _install_runtime_stubs(
        monkeypatch,
        workflows={
            "content_pipeline": {
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [],
                        "expected_outputs": [{"name": "workflow_output"}],
                    }
                }
            }
        },
    )


def _recorded_child(child_id: int, execution_id: str) -> dict:
    return {
        str(child_id): {
            "child_job_id": str(child_id),
            "child_workflow_id": "content_pipeline",
            "step_id": "run_pipeline",
            "step_execution_id": execution_id,
            "invocation_key": f"500:{execution_id}",
            "created_at": "2026-04-20T10:00:00+00:00",
        }
    }


def _pipeline_state(*, attempt: int = 1, **extra) -> dict:
    base = {key: value for key, value in _base_state().items() if key != "agent_messages"}
    base.update({"job_id": 500, "workflow_id": "parent_pipeline", **extra})
    return step_run_state(
        CONTENT_PIPELINE_STEP,
        step_execution_id=INVOKE_EXECUTION_ID,
        attempt=attempt,
        **base,
    )


@pytest.mark.parametrize("feedback", ["", "Fix the prior guardrail violation."])
def test_invoke_step_reuses_the_child_of_the_same_invocation_on_retry(monkeypatch, feedback):
    """A retry keeps the step execution, so it is the same invocation (§8.1)."""
    import core.application.orchestration.invoke_workflow_step_node as node

    existing_child_id = 2301
    runtime = _content_pipeline_runtime(monkeypatch)
    fake_logger = _FakeLogger(
        child_job_id=existing_child_id,
        child_messages=[{"envelope": {"artifacts": {"workflow_output": {"ok": True}}}}],
        child_status_sequence=["completed"],
        parent_job={
            "meta": {"invoke_children": _recorded_child(existing_child_id, INVOKE_EXECUTION_ID)}
        },
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    captured = _capture_append_messages(monkeypatch, node)

    result = node.invoke_workflow_step_node(
        _pipeline_state(attempt=2, **({"feedback": feedback} if feedback else {}))
    )

    assert fake_logger.create_calls == []
    assert fake_logger.meta_calls == []
    assert runtime["enqueue_calls"] == []
    assert captured[-1]["status"] == "pass"
    assert captured[-1]["artifacts"]["_invoke_workflow"]["child_job_id"] == str(existing_child_id)
    assert result["agent_messages"][-1]["artifacts"]["workflow_output"] == {"ok": True}
    resumed = [
        event
        for event in runtime["events"]
        if event["args"][2] == "invoke_workflow_resume_existing_child"
    ]
    assert resumed
    assert resumed[0]["args"][4]["step_execution_id"] == INVOKE_EXECUTION_ID


def test_invoke_step_starts_a_new_child_for_a_new_entry_of_the_step(monkeypatch):
    """A rework re-enters the step with a new execution: a new invocation."""
    import core.application.orchestration.invoke_workflow_step_node as node

    previous_child_id = 2401
    new_child_id = 2402
    runtime = _content_pipeline_runtime(monkeypatch)
    previous_fingerprint = node._child_input_fingerprint({})
    fake_logger = _FakeLogger(
        child_job_id=new_child_id,
        child_messages=[{"envelope": {"artifacts": {"workflow_output": {"fixed": True}}}}],
        child_status_sequence=["completed"],
        parent_job={
            "meta": {"invoke_children": _recorded_child(previous_child_id, PREVIOUS_EXECUTION_ID)}
        },
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    captured = _capture_append_messages(monkeypatch, node)

    feedback = "Fix the prior guardrail violation without changing unrelated files."
    result = node.invoke_workflow_step_node(_pipeline_state(feedback=feedback))

    assert fake_logger.create_calls, "a new entry should create a fresh child invocation"
    assert runtime["enqueue_calls"], "fresh child should be enqueued"
    extra_state = runtime["enqueue_calls"][0].get("extra_state") or {}
    child_meta = fake_logger.create_calls[0].get("meta") or {}

    assert extra_state.get("feedback") == feedback
    assert extra_state.get("invoke_parent_feedback") == feedback
    assert child_meta.get("invoke_parent_feedback_hash")
    assert child_meta.get("invoke_input_fingerprint") != previous_fingerprint
    assert captured[-1]["status"] == "pass"
    assert captured[-1]["artifacts"]["_invoke_workflow"]["child_job_id"] == new_child_id
    assert result["agent_messages"][-1]["artifacts"]["workflow_output"] == {"fixed": True}
    assert any(event["args"][2] == "invoke_workflow_start" for event in runtime["events"])
    assert not any(
        event["args"][2] == "invoke_workflow_resume_existing_child" for event in runtime["events"]
    )
    (recorded,) = runtime["links"]
    assert recorded["child_job_id"] == str(new_child_id)
    assert recorded["step_id"] == "run_pipeline"
    assert recorded["step_execution_id"] == INVOKE_EXECUTION_ID
    assert recorded["invocation_key"] == f"500:{INVOKE_EXECUTION_ID}"
    assert "phase_id" not in recorded


# ---------------------------------------------------------------------------
# Durable serial invocation and the lane boundary (T34, T17)
# ---------------------------------------------------------------------------

_CALLABLE_CHILD = {
    "workflow_id": "wf_child",
    "enabled": True,
    "settings": {"callable": {"enabled": True, "expected_inputs": [], "output_mode": "none"}},
}
_INVOKE_STEP = {
    "agent": "invoke_workflow_step",
    "skill": "invoke_workflow",
    "override": {"invoke_workflow": {"workflow_id": "wf_child", "input_mapping": {}}},
}


@pytest.mark.parametrize("inline_flag", ["1", "0"])
@pytest.mark.parametrize("task_source", ["manual", "workflow_test_run", "step_test_run"])
def test_a_serial_invoke_parks_on_its_child_and_settles_it_once_finished(
    monkeypatch, inline_flag, task_source
):
    """T44: the durable serial path ignores FS_INVOKE_INLINE_EXECUTION either way."""
    from core.application.orchestration import invoke_workflow_child_wait as child_wait
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.wait_step_runtime import child_pending_wait

    monkeypatch.setenv("FS_INVOKE_INLINE_EXECUTION", inline_flag)
    runtime = _install_runtime_stubs(monkeypatch, workflows={"wf_child": dict(_CALLABLE_CHILD)})
    # This test drives the real durable wait instead of the stub.
    monkeypatch.setattr(node, "await_child", child_wait.await_child)
    consumed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        node, "_mark_child_consumed", lambda *args: consumed.append(args), raising=False
    )
    fake_logger = _FakeLogger(child_job_id=905, child_status_sequence=["completed"])
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    inline = []
    monkeypatch.setattr(node, "_execute_child_job_inline", lambda message: inline.append(message))
    captured = _capture_append_messages(monkeypatch, node)

    state = dict(_base_state(_INVOKE_STEP), task_source=task_source, test_mode=True)
    pending = child_pending_wait(active_step(state), revision=1)
    state["pending_wait"] = pending
    wake = {"reason": "child", "wait_id": pending["wait_id"], "child_job_id": "905"}

    with patch("langgraph.types.interrupt", return_value=wake) as interrupt:
        update = node.invoke_workflow_step_node(state)

    payload = interrupt.call_args.args[0]
    assert (payload["kind"], payload["child_job_id"]) == ("child", "905")
    assert payload["invocation_key"] == f"77:{state['run_cursor']['step_execution_id']}"
    assert inline == [], "a serial invocation never runs its child in the parent's stack"
    assert runtime["enqueue_calls"], "the child runs as its own queued job"
    assert runtime["links"][0]["invocation_key"] == payload["invocation_key"]
    assert captured[-1]["status"] == "pass"
    assert consumed == [], "retention cannot be released before the parent checkpoint"
    assert update["pending_wait"] is None


def test_a_failed_child_fails_the_parent_step_through_its_policy(monkeypatch):
    from core.application.orchestration import invoke_workflow_child_wait as child_wait
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.wait_step_runtime import child_pending_wait

    _install_runtime_stubs(monkeypatch, workflows={"wf_child": dict(_CALLABLE_CHILD)})
    monkeypatch.setattr(node, "await_child", child_wait.await_child)
    monkeypatch.setattr(node, "_mark_child_consumed", lambda *args: None, raising=False)
    monkeypatch.setattr(
        node, "_get_logger", lambda: _FakeLogger(child_job_id=906, child_status_sequence=["failed"])
    )
    captured = _capture_append_messages(monkeypatch, node)
    state = _base_state(_INVOKE_STEP)
    pending = child_pending_wait(active_step(state), revision=1)
    state["pending_wait"] = pending

    with patch(
        "langgraph.types.interrupt",
        return_value={"reason": "child", "wait_id": pending["wait_id"], "child_job_id": "906"},
    ):
        node.invoke_workflow_step_node(state)

    assert captured[-1]["status"] == "fail"
    assert captured[-1]["artifacts"]["_invoke_workflow_error"] == "child_failed"


def test_a_lane_child_with_a_pause_in_its_definition_is_refused(monkeypatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    child = {
        **_CALLABLE_CHILD,
        "revision": 3,
        "steps": [{"step_id": "hold", "step_kind": "wait"}],
    }
    runtime = _install_runtime_stubs(monkeypatch, workflows={"wf_child": child})
    monkeypatch.setattr(
        node,
        "_get_logger",
        lambda: _FakeLogger(child_job_id=907, parent_job={"meta": {"invoke_lane_placement": True}}),
    )
    captured = _capture_append_messages(monkeypatch, node)

    node.invoke_workflow_step_node(_base_state(_INVOKE_STEP))

    assert captured[-1]["status"] == "fail"
    assert captured[-1]["artifacts"]["_invoke_workflow_error"] == "child_waits_in_lane"
    assert "hold" in captured[-1]["summary"]
    assert runtime["enqueue_calls"] == []


def test_a_lane_child_carries_the_lane_mark_to_its_descendants(monkeypatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(monkeypatch, workflows={"wf_child": dict(_CALLABLE_CHILD)})
    fake_logger = _FakeLogger(
        child_job_id=908,
        child_status_sequence=["completed"],
        parent_job={"meta": {"invoke_lane_placement": True}},
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent_job: False)
    finite: list[str] = []
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            finite.append(str(child_job_id))
            or ({"id": child_job_id, "status": "completed"}, "completed")
        ),
    )
    monkeypatch.setattr(
        node, "await_child", lambda *a, **k: pytest.fail("a lane child must not park")
    )

    node.invoke_workflow_step_node(_base_state(_INVOKE_STEP))

    assert finite == ["908"]
    assert fake_logger.create_calls[0]["meta"]["invoke_lane_placement"] is True


@pytest.mark.parametrize("lane", [False, True])
def test_existing_child_uses_its_archived_callable_contract_after_live_disable(monkeypatch, lane):
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.orchestration.run_definition import prime_run_definition
    from core.application.workflows.workflow_definition_snapshot import definition_digest

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "settings": {"callable": {"enabled": False}},
                "steps": [{"step_id": "live_wait", "step_kind": "wait"}],
            }
        },
    )
    definition = {
        "settings": _FakeDB._with_default_callable_channel(_CALLABLE_CHILD)["settings"],
        "steps": [],
        "groups": [],
    }
    ref = {
        "snapshot_id": "child-archive-audit",
        "revision": 1,
        "digest": definition_digest(definition),
    }
    prime_run_definition(ref, definition)
    state = _base_state(_INVOKE_STEP)
    fake_logger = _FakeLogger(
        child_job_id=905,
        child_status_sequence=["completed"],
        child_job_extra={
            "workflow_definition_snapshot_id": ref["snapshot_id"],
            "meta": {"workflow_definition_ref": ref},
        },
        parent_job={
            "meta": {
                "invoke_lane_placement": lane,
                "invoke_children": {
                    "905": {"invocation_key": f"77:{state['run_cursor']['step_execution_id']}"}
                },
            }
        },
    )
    fake_logger.conn = object()
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        WorkflowDefinitionSnapshotRepository,
        "load_for_job",
        lambda _self, _job: {
            "id": ref["snapshot_id"],
            "workflow_id": "wf_child",
            "revision": 1,
            "digest": ref["digest"],
            "definition_jsonb": definition,
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    node.invoke_workflow_step_node(state)
    assert captured[-1]["status"] == "pass", captured[-1]
    assert fake_logger.create_calls == []
    assert runtime["enqueue_calls"] == []


@pytest.mark.parametrize("race", ["link", "pin", "lookup"])
def test_concurrent_child_winner_uses_archived_output_contract(monkeypatch, race):
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
        WorkflowRevisionChangedError,
    )
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.workflows import workflow_start_runtime
    from core.application.workflows.workflow_definition_snapshot import definition_digest

    def child_contract(output):
        return {
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [],
                "expected_outputs": [{"name": output, "type": "string", "required": True}],
            }
        }

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "revision": 2,
                "settings": child_contract("bar"),
            },
        },
    )
    state = _base_state(
        {
            **_INVOKE_STEP,
            "override": {
                "invoke_workflow": {
                    "workflow_id": "wf_child",
                    "input_mapping": {},
                    "output_mapping": {"foo": "foo"},
                }
            },
        }
    )
    fake_logger = _FakeLogger(
        child_job_id=905,
        child_status_sequence=["completed"],
        child_messages=[{"envelope": {"artifacts": {"foo": "archived result"}}}],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    captured = _capture_append_messages(monkeypatch, node)
    definition = {"settings": child_contract("foo"), "steps": [], "groups": []}
    monkeypatch.setattr(
        WorkflowDefinitionSnapshotRepository,
        "load_for_job",
        lambda *_: {
            "workflow_id": "wf_child",
            "revision": 1,
            "digest": definition_digest(definition),
            "definition_jsonb": definition,
        },
    )

    def link_winner():
        fake_logger.parent_job["meta"] = {
            "invoke_children": {
                "905": {"invocation_key": f"77:{state['run_cursor']['step_execution_id']}"},
            }
        }

    attempts = []

    def race_at_enqueue(**_kwargs):
        assert _kwargs["job_meta"]["expected_workflow_revision"] == 2
        attempts.append(True)
        assert len(attempts) == 1, "winner replay must never enqueue another child"
        if race == "pin":
            link_winner()
            raise WorkflowRevisionChangedError("wf_child", 2, 3)
        raise node.InvokeChildAlreadyLinked("905")

    monkeypatch.setattr(workflow_start_runtime, "enqueue_workflow_start", race_at_enqueue)
    if race == "lookup":

        def race_at_lookup(_self, _workflow_id):
            link_winner()
            return {"workflow_id": "wf_child", "settings": {"callable": {"enabled": False}}}

        monkeypatch.setattr(_FakeDB, "get_workflow", race_at_lookup)

    node.invoke_workflow_step_node(state)

    assert captured[-1]["status"] == "pass", captured[-1]
    assert captured[-1]["artifacts"]["foo"] == "archived result"
    assert len(attempts) == (0 if race == "lookup" else 1)


def test_child_revision_conflict_without_committed_winner_still_refuses(monkeypatch):
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowRevisionChangedError,
    )
    from core.application.orchestration import invoke_workflow_step_node as node
    from core.application.workflows import workflow_start_runtime

    _install_runtime_stubs(monkeypatch, workflows={"wf_child": dict(_CALLABLE_CHILD)})
    fake_logger = _FakeLogger()
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    captured = _capture_append_messages(monkeypatch, node)

    def conflict(**_kwargs):
        raise WorkflowRevisionChangedError("wf_child", 1, 2)

    monkeypatch.setattr(workflow_start_runtime, "enqueue_workflow_start", conflict)

    node.invoke_workflow_step_node(_base_state(_INVOKE_STEP))

    assert captured[-1]["artifacts"]["_invoke_workflow_error"] == "enqueue_failed"
    assert fake_logger.create_calls == []


# --- Files returned from a child workflow through "Return from workflow" ------


_RETURNING_CHILD_STEPS = [
    {
        "step_id": "render_report",
        "display_name": "Render report",
        "step_kind": "file_from_base64",
        "override": {
            "file_from_base64": {
                "source": "state.inputs.report_b64",
                # A new name on every run: the output key must not depend on it.
                "filename": "{{ state.inputs.report_name }}",
                "result_target": "state.document",
            },
            "returned_files": [{"result": "file", "key": "report"}],
        },
    },
    {
        "step_id": "render_receipt",
        "display_name": "Render receipt",
        "step_kind": "file_from_base64",
        "override": {
            # The same variable as the report: the returned report must survive.
            "file_from_base64": {
                "source": "state.inputs.receipt_b64",
                "filename": "receipt.txt",
                "result_target": "state.document",
            },
            "returned_files": [{"result": "file", "key": "receipt"}],
        },
    },
    {
        "step_id": "render_draft",
        "display_name": "Render draft",
        "step_kind": "file_from_base64",
        "override": {
            "file_from_base64": {
                "source": "state.inputs.receipt_b64",
                "filename": "draft.txt",
                "result_target": "state.draft",
            }
        },
    },
    {
        "step_id": "export_attachments",
        "display_name": "Export attachments",
        "step_kind": "connector",
        "connector_id": "acme.mailbox",
        "output_mapping_jsonb": {"attachments": "state.attachments"},
        "override": {"returned_files": [{"result": "attachments", "key": "attachments"}]},
    },
]


def _returning_child_definition(*, untick_report: bool = False) -> dict:
    import copy

    from tests._workflow_definitions import linear_graph

    steps = [
        {**copy.deepcopy(step), "group_id": None, "for_each_id": None, "lane_id": None}
        for step in _RETURNING_CHILD_STEPS
    ]
    if untick_report:
        steps[0]["override"]["returned_files"] = []
    return {
        "workflow_id": "wf_child",
        "steps": steps,
        "groups": [],
        "for_each": [],
        "execution_graph": linear_graph([step["step_id"] for step in steps]),
        "settings": {"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}},
    }


def _run_returning_child(
    plane, *, child_job_id: str, report_name: str, untick_report: bool = False
) -> dict:
    """Run the child's steps through the real executors; return its final contract."""
    import base64

    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.returned_files_runtime import with_returned_files
    from core.application.orchestration.run_cursor import RunCursor, begin_step
    from core.application.registry.workflow_contract import interface_from_definition
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from tests._step_run_state import prime

    definition = _returning_child_definition(untick_report=untick_report)
    state: dict = {
        "agent_messages": [],
        **prime(definition),
        "account_id": "acc-1",
        "project_id": "7",
        "job_id": child_job_id,
        "workflow_id": "wf_child",
        "_artifact_plane": plane,
        "inputs": {
            "report_b64": base64.b64encode(f"report {report_name}".encode()).decode(),
            "report_name": report_name,
            "receipt_b64": base64.b64encode(b"receipt body").decode(),
        },
    }

    def _at(step_id: str) -> dict:
        cursor = begin_step(RunCursor(frames=[]), step_id)
        return {**state, "run_cursor": cursor.dump(), "current_step_id": step_id}

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        for step_id in ("render_report", "render_receipt", "render_draft"):
            update = execute_step_node(_at(step_id))
            assert update["agent_messages"][-1]["status"] == "pass"
            state.update(update)

    # The connector's own dispatch is out of scope here: its mapped write of a
    # file collection is captured exactly as the executor wrapper captures it.
    attachments = [
        f"artifact:{plane.create_artifact(owner_scope={'project_id': '7', 'job_id': child_job_id}, run_id=child_job_id, step_id='export_attachments', filename=name, content_type='text/plain', body=b'x')[0].artifact_id}"
        for name in ("a.txt", "b.txt")
    ]
    connector_update = {
        "attachments": attachments,
        "workflow_data": {**state.get("workflow_data", {}), "attachments": attachments},
    }
    state.update(with_returned_files(_at("export_attachments"), connector_update))

    result = WorkflowInterfaceService().validate_outputs(
        interface_from_definition(definition), state
    )
    assert result.ok is True, result.errors
    return {
        "ok": True,
        "outputs": result.outputs,
        "artifacts": result.artifacts,
        # Not part of the result: what the child's own steps last saw.
        "internal_document": state["workflow_data"]["document"],
    }


def _invoke_returning_child(
    monkeypatch, plane, *, child_job_id: int, final_contract: dict, untick_report: bool = False
):
    from core.application.orchestration import invoke_workflow_step_node as node

    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                **_returning_child_definition(untick_report=untick_report),
                "enabled": True,
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    fake_logger = _FakeLogger(
        child_job_id=child_job_id,
        child_messages=[
            {
                "id": 1,
                "envelope": {
                    "artifacts": {
                        "workflow_outputs": {
                            key: final_contract[key] for key in ("ok", "outputs", "artifacts")
                        }
                    }
                },
            }
        ],
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(node, "_job_invocation_artifact_plane", lambda: plane)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )
    step = {
        "step_id": "make_documents",
        "agent": "invoke_workflow",
        "step_kind": "invoke_workflow",
        "skill": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                # The parent's existing result mapping, unchanged between runs.
                "artifact_mapping": {
                    "report": "state.child_report",
                    "receipt": "state.child_receipt",
                    "attachments": "state.child_attachments",
                },
            },
            # The parent returns the child's report in turn.
            "returned_files": [{"result": "report", "key": "monthly_report"}],
        },
    }
    result = node.invoke_workflow_step_node(_base_state(step))
    return result, captured[-1]


def test_child_files_return_to_the_parent_through_one_stable_output_on_every_run(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    plane = ArtifactDatasetPlane()
    try:
        runs = [
            ("977", "report-2026-10-22T10-00-00.pdf"),
            ("978", "report-2026-10-23T09-12-44.pdf"),
        ]
        contracts = [
            _run_returning_child(plane, child_job_id=job_id, report_name=name)
            for job_id, name in runs
        ]
        for (job_id, name), contract in zip(runs, contracts, strict=True):
            # Unticked results stay inside the child; ticked ones keep their own file.
            assert sorted(contract["artifacts"]) == ["attachments", "receipt", "report"]
            assert contract["artifacts"]["report"] != contract["artifacts"]["receipt"]
            report = plane.get_artifact(contract["artifacts"]["report"].removeprefix("artifact:"))
            assert report["filename"] == name
            assert len(contract["artifacts"]["attachments"]) == 2

            result, message = _invoke_returning_child(
                monkeypatch, plane, child_job_id=int(job_id), final_contract=contract
            )

            assert message["status"] == "pass", message
            data = result["workflow_data"]
            assert data["child_report"] == contract["artifacts"]["report"]
            assert data["child_receipt"] == contract["artifacts"]["receipt"]
            assert data["child_attachments"] == contract["artifacts"]["attachments"]
            details = message["artifacts"]["_invoke_workflow"]["returned_files"]
            assert details["report"]["filename"] == name
            assert details["receipt"]["filename"] == "receipt.txt"
            assert [row["filename"] for row in details["attachments"]] == ["a.txt", "b.txt"]
            # Every returned file now belongs to the parent run.
            for handle in [data["child_report"], data["child_receipt"], *data["child_attachments"]]:
                adopted = plane.get_artifact(handle.removeprefix("artifact:"))
                assert adopted["owner_scope"]["job_id"] == "77"
                assert adopted["producer_job_id"] == job_id
            # And the parent returns the report under its own stable key.
            assert result["artifacts"] == {"monthly_report": contract["artifacts"]["report"]}
    finally:
        clear_run_definition_cache()


def test_an_unticked_result_stays_inside_the_child_and_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    plane = ArtifactDatasetPlane()
    try:
        contract = _run_returning_child(
            plane, child_job_id="979", report_name="report.pdf", untick_report=True
        )
        result, message = _invoke_returning_child(
            monkeypatch, plane, child_job_id=979, final_contract=contract, untick_report=True
        )
    finally:
        clear_run_definition_cache()

    # The child still wrote and used the file; it is just not part of its result.
    assert contract["internal_document"].startswith("artifact:")
    assert sorted(contract["artifacts"]) == ["attachments", "receipt"]
    assert message["status"] == "pass"
    invoke = message["artifacts"]["_invoke_workflow"]
    assert invoke["declared_artifacts"] == ["attachments", "receipt"]
    assert "report" not in invoke["returned_files"]
    assert "child_report" not in result["workflow_data"]
