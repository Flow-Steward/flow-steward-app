"""AC19: create the deterministic workflow the profile benchmark submits.

Runs inside the application container, where the registry database is reachable.
The workload is created through the workflow portability service and inspected as
stored, using the same import boundary as ordinary workflow authoring.

The workload is a chain of transform steps over a JSON array: no model, no external
network, so a run measures the platform -- queue, worker, checkpoints, database --
and not a provider.
"""

from __future__ import annotations

import itertools
from typing import Any

import yaml

from core.application.management.scope import normalize_scope
from core.application.registry.workflow_contract import DEFAULT_TRIGGER_CHANNELS
from core.application.workflows.workflow_portability import (
    BUNDLE_KIND,
    SCHEMA_VERSION,
    WorkflowPortabilityService,
)

BENCHMARK_WORKFLOW_PREFIX = "fs_benchmark_platform"
# Ownership: what cleanup and replacement may touch. Revision: what may be reused as is.
_OWNERSHIP_TAG = "ac19-benchmark"
_REVISION_MARKER = "ac19-v2"


def benchmark_workflow_id(steps: int) -> str:
    return f"{BENCHMARK_WORKFLOW_PREFIX}_{steps}"


def _portable_workflow(steps: int) -> dict[str, Any]:
    chain: list[dict[str, Any]] = []
    # A `json` workflow input must be an object, so the rows travel inside one.
    source = "state.inputs.payload.rows"
    for index in range(steps):
        target = f"state.stage_{index}"
        chain.append(
            {
                "step_id": f"stage_{index}",
                "step_kind": "transform",
                "override": {
                    "transform": {
                        "input": {"source": source},
                        "mode": "list",
                        "fields": [
                            {"from": "sku", "to": "sku", "actions": [{"action": "text.trim"}]},
                            {"from": "title", "to": "title", "actions": [{"action": "text.trim"}]},
                            {"from": "quantity", "to": "quantity"},
                        ],
                        "result_target": target,
                    }
                },
            }
        )
        source = target
    return {
        "workflow_id": benchmark_workflow_id(steps),
        "display_name": f"Benchmark: {steps} transform steps",
        "description": "AC19 profile benchmark workload. Safe to delete.",
        "enabled": False,
        "tags": [_OWNERSHIP_TAG, _REVISION_MARKER],
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": list(DEFAULT_TRIGGER_CHANNELS),
                "expected_inputs": [{"name": "payload", "type": "json"}],
                "expected_outputs": [],
            }
        },
        "steps": [*chain, {"step_id": "workflow_success", "step_kind": "stop_success"}],
        "groups": [],
        "for_each": [],
        "execution_graph": _linear_graph(
            [str(step["step_id"]) for step in chain] + ["workflow_success"]
        ),
    }


def _linear_graph(step_ids: list[str]) -> dict[str, Any]:
    """Each step's success leads to the next; the last is the Stop."""
    return {
        "schema_version": 1,
        "entry": {"kind": "step", "id": step_ids[0]},
        "edges": [
            {
                "source": {"kind": "step", "id": source},
                "outcome": {"name": "success"},
                "target": {"kind": "step", "id": target},
                "rework": False,
            }
            for source, target in itertools.pairwise(step_ids)
        ],
    }


def prepare_benchmark_workflow(*, store: Any, scope: Any, steps: int) -> dict[str, Any]:
    """Create the benchmark workflow once; an existing one of this revision is reused."""
    if steps < 1:
        raise ValueError("steps must be at least 1")
    workflow_id = benchmark_workflow_id(steps)
    existing = store.get_workflow(workflow_id)
    existing_tags = (existing or {}).get("tags") or []
    if existing is not None and _OWNERSHIP_TAG not in existing_tags:
        raise RuntimeError(
            f"workflow {workflow_id} exists but was not created by this benchmark; "
            "delete it or choose another step count"
        )
    if existing is not None and _REVISION_MARKER in existing_tags:
        return {"workflow_id": workflow_id, "steps": steps, "created": False}
    if existing is not None:
        store.delete_workflow(workflow_id)  # an older revision of this benchmark's own

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "metadata": {},
        "workflows": [_portable_workflow(steps)],
        "dependencies": {},
        "import_hints": {"default_enabled": False, "import_state": "draft"},
    }
    resolved_scope = normalize_scope(scope)
    WorkflowPortabilityService(
        store,
        import_transaction_factory=(
            getattr(store, "workflow_import_transaction", None)
            or getattr(store, "_transaction", None)
        ),
    ).apply_import(
        yaml.safe_dump(bundle, sort_keys=False),
        owner_account_id=resolved_scope.account_id,
        author=resolved_scope.actor_id or "profile-benchmark",
    )
    workflow = store.get_workflow(workflow_id)
    workflow["enabled"] = True
    tags = list(workflow.get("tags") or [])
    for tag in (_OWNERSHIP_TAG, _REVISION_MARKER):
        if tag not in tags:
            tags.append(tag)
    workflow["tags"] = tags
    store.save_workflow(workflow)
    return {"workflow_id": workflow_id, "steps": steps, "created": True}


def remove_benchmark_workflows(*, store: Any) -> list[str]:
    """Delete the workflows this tool created -- tagged with its marker -- and nothing else."""
    removed: list[str] = []
    for workflow in store.list_workflows() or []:
        workflow_id = str(workflow.get("workflow_id") or "")
        tags = workflow.get("tags") or []
        if workflow_id.startswith(BENCHMARK_WORKFLOW_PREFIX) and _OWNERSHIP_TAG in tags:
            store.delete_workflow(workflow_id)
            removed.append(workflow_id)
    return sorted(removed)


__all__ = [
    "BENCHMARK_WORKFLOW_PREFIX",
    "benchmark_workflow_id",
    "prepare_benchmark_workflow",
    "remove_benchmark_workflows",
]
