from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.application.orchestration import graph_runtime


class _RegistryStub:
    def __init__(self, *, seeded: bool) -> None:
        self.reload_calls = 0
        self._seeded = seeded
        self._sync_state()

    def _sync_state(self) -> None:
        if self._seeded:
            self.agents = SimpleNamespace(list_all=lambda: [SimpleNamespace(agent_id="writer")])
            self.workflows = SimpleNamespace(
                list_all=lambda: [SimpleNamespace(workflow_id="weekly_report")]
            )
            self.skills = SimpleNamespace(
                list_all=lambda: [SimpleNamespace(skill_id="draft_summary")]
            )
            self.tools = SimpleNamespace(
                list_all=lambda: [
                    SimpleNamespace(tool_id="workflow_ops"),
                ]
            )
            return

        self.agents = SimpleNamespace(list_all=list)
        self.workflows = SimpleNamespace(list_all=list)
        self.skills = SimpleNamespace(list_all=list)
        self.tools = SimpleNamespace(list_all=lambda: [SimpleNamespace(tool_id="workflow_ops")])

    def reload_all(self) -> None:
        self.reload_calls += 1
        self._seeded = True
        self._sync_state()


class _AlwaysEmptyRegistryStub(_RegistryStub):
    def reload_all(self) -> None:
        self.reload_calls += 1
        self._seeded = False
        self._sync_state()


def test_build_registry_graph_reloads_stale_empty_registry_before_compile() -> None:
    registry = _RegistryStub(seeded=False)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.graph.builder.build_graph_from_registry",
            return_value="compiled-graph",
        ) as build_mock,
    ):
        compiled = graph_runtime.build_registry_graph(checkpointer="cp")

    assert compiled == "compiled-graph"
    assert registry.reload_calls == 1
    assert len(registry.agents.list_all()) == 1
    assert len(registry.workflows.list_all()) == 1
    assert build_mock.call_args.args[0] is registry
    assert build_mock.call_args.kwargs["checkpointer"] == "cp"
    assert build_mock.call_args.kwargs["validate_contracts"] is True


def test_build_registry_graph_skips_reload_for_ready_registry() -> None:
    registry = _RegistryStub(seeded=True)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.graph.builder.build_graph_from_registry",
            return_value="compiled-graph",
        ),
    ):
        compiled = graph_runtime.build_registry_graph(checkpointer="cp")

    assert compiled == "compiled-graph"
    assert registry.reload_calls == 1


def test_build_registry_graph_logs_info_when_registry_stays_unseeded() -> None:
    registry = _AlwaysEmptyRegistryStub(seeded=False)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.graph.builder.build_graph_from_registry",
            return_value="compiled-graph",
        ),
        patch.object(graph_runtime, "logger") as logger_mock,
    ):
        compiled = graph_runtime.build_registry_graph(checkpointer="cp")

    assert compiled == "compiled-graph"
    assert registry.reload_calls == 1
    logger_mock.info.assert_any_call("Registry still appears unseeded after graph-build reload")


def test_build_registry_graph_can_skip_registry_refresh_for_read_only_runtime() -> None:
    registry = _RegistryStub(seeded=True)

    with patch(
        "core.application.orchestration.graph.builder.build_graph_from_registry",
        return_value="compiled-graph",
    ) as build_mock:
        compiled = graph_runtime.build_registry_graph(
            checkpointer="cp",
            registry=registry,
            refresh_registry=False,
        )

    assert compiled == "compiled-graph"
    assert registry.reload_calls == 0
    assert build_mock.call_args.args[0] is registry
    assert build_mock.call_args.kwargs["checkpointer"] == "cp"
