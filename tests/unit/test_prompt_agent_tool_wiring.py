from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_manifest():
    manifest = MagicMock()
    manifest.agent_id = "composer"
    manifest.display_name = "Developer"
    manifest.tools = ["search_tool"]
    manifest.skills = ["correct_draft"]
    manifest.config = {}
    manifest.max_iterations = 10
    manifest.prompts = MagicMock()
    manifest.prompts.system = "You are a test agent."
    manifest.prompts.skill_prompts = {}
    manifest.prompts.context_template = ""
    manifest.prompts.output_format = ""
    manifest.prompts.context_sections = {}
    manifest.prompts.examples = {}
    return manifest


def _make_state() -> dict:
    return {
        "project_name": "test-project",
        "project_config": {},
        "task_id": "TASK-123",
        "task_summary": "Draft the weekly summary",
    }


def test_prompt_agent_resolves_tools_via_typed_catalog_when_available():
    from core.application.orchestration.agents.prompt_agent import PromptAgent

    agent = PromptAgent(_make_manifest())
    resolver = MagicMock()
    resolved_tool = MagicMock()
    resolved_tool.name = "search_code"
    resolver.resolve_catalog.return_value = [resolved_tool]

    tools_registry = MagicMock()
    tools_registry.get_enabled_catalog_entry.return_value = {
        "tool_id": "search_tool",
        "implementation_kind": "internal",
        "implementation": {"kind": "internal", "type": "internal"},
        "interface": {"operations": [{"name": "search_code"}]},
    }

    fake_rm = SimpleNamespace(
        instance=lambda: SimpleNamespace(
            tool_resolver=resolver,
            tools=tools_registry,
        )
    )

    with patch.dict(
        sys.modules,
        {"core.application.registry.registry_manager": SimpleNamespace(RegistryManager=fake_rm)},
    ):
        tools = agent._resolve_tools(_make_state(), skill="correct_draft")

    assert tools == [resolved_tool]
    tools_registry.get_enabled_catalog_entry.assert_called_once_with("search_tool")
    resolver.resolve_catalog.assert_called_once_with(
        [
            {
                "tool_id": "search_tool",
                "implementation_kind": "internal",
                "implementation": {"kind": "internal", "type": "internal"},
                "interface": {"operations": [{"name": "search_code"}]},
            }
        ],
        runtime_context={"source": "prompt_agent.resolve_tools"},
    )
    resolver.resolve.assert_not_called()


def _named_tool(name: str):
    tool = MagicMock()
    tool.name = name
    return tool


def _internal_catalog_entry(tool_id: str) -> dict:
    return {
        "tool_id": tool_id,
        "implementation_kind": "internal",
        "implementation": {"kind": "internal", "type": "internal"},
        "interface": {"operations": [{"name": "invoke"}]},
    }


def _registry_module(resolver, tools_registry):
    fake_rm = SimpleNamespace(
        instance=lambda: SimpleNamespace(
            tool_resolver=resolver,
            tools=tools_registry,
        )
    )
    return {"core.application.registry.registry_manager": SimpleNamespace(RegistryManager=fake_rm)}


def test_agent_uses_only_authored_typed_catalog_tools():
    from core.application.orchestration.agents.prompt_agent import PromptAgent

    manifest = _make_manifest()
    manifest.agent_id = "reviewer"
    manifest.tools = ["workflow_ops"]
    manifest.skills = ["review_summary"]

    agent = PromptAgent(manifest)
    resolver = MagicMock()
    resolver.resolve_catalog.return_value = [_named_tool("workflow_list")]

    tools_registry = MagicMock()
    tools_registry.get_enabled_catalog_entry.side_effect = _internal_catalog_entry

    with patch.dict(sys.modules, _registry_module(resolver, tools_registry)):
        tools = agent._resolve_tools(_make_state(), skill="review_summary")

    names = [tool.name for tool in tools]
    assert names == ["workflow_list"]
    assert "invoke" not in names
    resolver.resolve_catalog.assert_called_once()
    resolver.resolve.assert_not_called()


def test_registry_only_policy_receives_only_registry_declared_tools():
    from core.application.orchestration.agents.prompt_agent import PromptAgent

    manifest = _make_manifest()
    manifest.agent_id = "writer"
    manifest.tools = ["workflow_ops", "sheet_ops"]
    manifest.skills = ["draft_summary"]

    agent = PromptAgent(manifest)
    resolver = MagicMock()
    resolver.resolve_catalog.return_value = [
        _named_tool("workflow_list"),
        _named_tool("sheet_read"),
    ]

    tools_registry = MagicMock()
    tools_registry.get_enabled_catalog_entry.side_effect = _internal_catalog_entry
    state = {
        **_make_state(),
        "project_policy": {"tool_policy_mode": "registry_only"},
    }

    with patch.dict(sys.modules, _registry_module(resolver, tools_registry)):
        tools = agent._resolve_tools(state, skill="draft_summary")

    names = [tool.name for tool in tools]
    assert names == ["workflow_list", "sheet_read"]
    resolver.resolve.assert_not_called()
