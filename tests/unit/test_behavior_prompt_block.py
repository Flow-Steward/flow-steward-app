from __future__ import annotations

from core.application.orchestration.agents.behavior_prompt_block import build_behavior_prompt_block


def test_behavior_prompt_block_preserves_project_rules() -> None:
    block = build_behavior_prompt_block(
        project_rules_context={"rules_excerpt": "Always add regression tests."},
    )
    assert "Execution policy context:" in block
    assert "Project rules to preserve:" in block
    assert "Always add regression tests." in block


def test_behavior_prompt_block_trims_project_rules() -> None:
    block = build_behavior_prompt_block(
        project_rules_context={"rules_text": "Long project rules"},
        project_rules_limit=8,
    )
    assert "Long..." in block


def test_behavior_prompt_block_returns_empty_without_context() -> None:
    assert build_behavior_prompt_block() == ""
