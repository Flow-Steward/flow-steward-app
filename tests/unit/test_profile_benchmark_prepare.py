"""Benchmark ownership must precede revision reuse."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.application.tooling.ops.profile_benchmark_prepare import prepare_benchmark_workflow


def test_revision_marker_without_ownership_does_not_allow_benchmark_reuse():
    class Store:
        def __init__(self):
            self.workflow = {
                "workflow_id": "fs_benchmark_platform_3",
                "tags": ["ac19-v2"],
                "enabled": False,
            }

        def get_workflow(self, workflow_id):
            assert workflow_id == self.workflow["workflow_id"]
            return deepcopy(self.workflow)

        def delete_workflow(self, workflow_id):
            raise AssertionError("Unowned workflow must not be deleted")

        def save_workflow(self, workflow, **kwargs):
            raise AssertionError("Unowned workflow must not be overwritten")

    store = Store()
    before = deepcopy(store.workflow)
    with pytest.raises(RuntimeError, match="not created by this benchmark"):
        prepare_benchmark_workflow(store=store, scope={"account_id": "acc_default"}, steps=3)
    assert store.workflow == before
