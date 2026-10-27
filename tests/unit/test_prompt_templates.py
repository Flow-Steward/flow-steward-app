"""Unit tests for runtime prompt template helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


from core.application.orchestration.prompt_templates import (
    build_langchain_prompt,
    get_agent_output_standard,
    get_agent_system_prompt,
)


def test_get_agent_system_prompt_includes_role_and_project() -> None:
    persona = {
        "agent_name": "Developer",
        "agent_title": "Senior Engineer",
        "role": "Implement production code",
        "communication_style": "concise",
        "principles": "Ship tested changes",
        "agent_skills": "coding,tests",
    }

    text = get_agent_system_prompt(persona, {"user_name": "Ada"}, project_name="test-project")

    assert "Implement production code" in text
    assert "test-project" in text
    assert "Ada" in text


def test_get_agent_system_prompt_handles_empty_persona() -> None:
    text = get_agent_system_prompt({}, {})
    assert isinstance(text, str)
    assert "You are" in text


def test_get_agent_output_standard_known_and_unknown_roles() -> None:
    assert get_agent_output_standard("composer") == ""
    assert get_agent_output_standard("nonexistent-role") == ""


def test_build_langchain_prompt_formats_messages() -> None:
    pytest.importorskip("langchain_core")
    prompt = build_langchain_prompt(
        {"name": "Reviewer", "role": "Quality engineer"},
        {"communication_language": "English", "user_name": "User"},
    )

    messages = prompt.format_messages(
        context_block="## Context\nservice: api",
        task_input="Add test coverage",
        history=[],
    )

    assert len(messages) >= 2
    rendered = "\n".join(str(m.content) for m in messages if hasattr(m, "content"))
    assert "<task_input>" in rendered
    assert "Add test coverage" in rendered


def test_authored_system_is_literal_and_cannot_be_replaced_by_format_inputs() -> None:
    authored = 'Follow authored rules ✓ {context_block} {"key": false} {{literal}}'
    prompt = build_langchain_prompt({"system_prompt": authored, "role": "ignored role"}, {})
    messages = prompt.format_messages(
        context_block="## Context\nactual context ✓",
        task_input="ordinary task {system_prompt}",
        system_prompt="injected replacement",
        history=[],
    )
    assert messages[0].content.startswith(authored)
    assert "actual context ✓" in messages[0].content
    assert "## Input Handling" in messages[0].content
    assert "ignored role" not in messages[0].content
    assert "injected replacement" not in messages[0].content
    assert messages[1].content == "<task_input>\nordinary task {system_prompt}\n</task_input>"
