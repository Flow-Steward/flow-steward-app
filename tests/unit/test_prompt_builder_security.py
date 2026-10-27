from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from core.application.orchestration.agents.prompt_builder import PromptBuilder


def test_build_jinja_context_uses_prompt_safe_project_config():
    builder = PromptBuilder()
    context = builder._build_jinja_context(
        {
            "task_summary": "demo",
            "project_config": {
                "project_name": "demo",
                "credentials": {"primary_dsn": "postgres://admin:secret@localhost/app"},
                "stack": {"language": "python"},
                "conventions": {"tone": "concise"},
                "project_connections": [{"name": "primary"}],
            },
        },
        {},
    )
    assert context["project_config"]["project_name"] == "demo"
    assert context["project_config"]["conventions"]["tone"] == "concise"
    assert "stack" not in context["project_config"]
    assert "credentials" not in context["project_config"]
    assert "project_connections" not in context["project_config"]


def test_resolve_path_blocks_forbidden_project_config_reference():
    builder = PromptBuilder()
    with pytest.raises(ValueError):
        builder._resolve_path(
            "project_config.credentials",
            {
                "project_config": {
                    "credentials": {"primary_dsn": "postgres://admin:secret@localhost/app"}
                }
            },
        )


def test_resolve_context_blocks_forbidden_template_placeholder():
    builder = PromptBuilder()
    with pytest.raises(ValueError):
        builder._resolve_context(
            "Credentials: {{ project_config.credentials }}",
            {
                "project_config": {
                    "credentials": {"primary_dsn": "postgres://admin:secret@localhost/app"}
                }
            },
            {"context_inject": {}},
        )


def test_resolve_context_rejects_legacy_project_config_placeholder():
    builder = PromptBuilder()
    with pytest.raises(ValueError, match="unsupported placeholder"):
        builder._resolve_context(
            "## Stack\n{project_config}",
            {"project_config": {"stack": {"language": "python", "framework": "fastapi"}}},
            {"context_inject": {}},
        )


def test_build_preserves_project_rules_without_management_chat_settings():
    builder = PromptBuilder()
    agent = SimpleNamespace(
        agent_id="composer",
        tools=[],
        prompts=SimpleNamespace(
            system="You are the composer.",
            skill_prompts={"correct_draft": "Fix the bug."},
            examples={},
            context_template="",
        ),
    )

    _system, user_prompt, _version = builder.build(
        agent=agent,
        skill="correct_draft",
        step={"context_inject": {}},
        tool_registry=None,
        state={
            "workflow_id": "maintenance",
            "task_type": "bugfix",
            "effort_hint": "small",
            "step_id": "development",
            "resolved_behavior": {"verbosity": "balanced", "question_budget": 2},
            "assistant_settings_context": {"provider": "openai", "model": "gpt-5.4"},
            "project_rules_context": {"rules_excerpt": "Do not bypass quality gates."},
        },
    )

    assert "Execution policy context" in user_prompt
    assert "- verbosity: balanced" not in user_prompt
    assert "- question_budget: 2" not in user_prompt
    assert "- provider: openai" not in user_prompt
    assert "Do not bypass quality gates." in user_prompt


# ── Agent context templates run in the Jinja sandbox ─────────────────────────
# A context template is written by an administrator or projected by an extension.
# Rendered by a plain jinja2.Environment it reached the operating system through
# a default global's Python object graph (CWE-1336).

_ESCAPES = [
    "{{ cycler.__init__.__globals__.os.popen('touch /tmp/fs-ssti-canary').read() }}",
    "{{ lipsum.__globals__['os'].popen('id').read() }}",
    "{{ joiner.__init__.__globals__ }}",
    "{{ namespace.__init__.__globals__ }}",
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{{ task_summary.__class__.__base__.__subclasses__() }}",
    "{% for x in range(3) %}{{ x }}{% endfor %}",
    "{% include 'secrets.txt' %}",
    "{% import 'macros' as m %}",
    "{{ 'x' * 2000000 }}",
]


@pytest.mark.parametrize("template", _ESCAPES)
def test_a_context_template_cannot_leave_the_sandbox(template, tmp_path, monkeypatch):
    canary = tmp_path / "canary"
    monkeypatch.chdir(tmp_path)
    builder = PromptBuilder()
    rendered = None
    with contextlib.suppress(ValueError):
        rendered = builder._resolve_context(
            template.replace("/tmp/fs-ssti-canary", str(canary)),
            {"task_summary": "demo"},
            {"context_inject": {}},
        )
    # Either refused or rendered as nothing useful: never a reach into Python.
    assert not canary.exists()
    if rendered is not None:
        assert "uid=" not in rendered
        assert "<class" not in rendered
        assert "function" not in rendered
        assert rendered.strip() in {"", "None"}


def test_a_plain_context_template_still_renders():
    builder = PromptBuilder()
    rendered = builder._resolve_context(
        "Task: {{ task_summary }}{% if missing %} never{% endif %} — {{ missing }}done",
        {"task_summary": "Draft the release notes"},
        {"context_inject": {}},
    )

    assert rendered == "Task: Draft the release notes — done"


def test_saving_an_agent_refuses_a_context_template_the_sandbox_would_refuse():
    from fastapi import HTTPException

    from core.entrypoints.admin.agents_helpers import _validate_agent_context_template

    with pytest.raises(HTTPException) as refused:
        _validate_agent_context_template({"context_template": "{% include 'x' %}"})
    assert refused.value.status_code == 400
    with pytest.raises(HTTPException):
        _validate_agent_context_template({"context_template": "{{ unclosed "})

    _validate_agent_context_template({"context_template": "Task: {{ task_summary }}"})
