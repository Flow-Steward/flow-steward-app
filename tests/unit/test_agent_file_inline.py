"""Text files reach an agent whole, in the task message that history pruning keeps.

Paging a Markdown file through read_agent_file cost one model round per page, and
the oldest pages left the working history, so a long file was never seen whole.
"""

from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage

from core.application.orchestration.agent_file_policy import (
    AGENT_TEXT_READ_MAX_CHARS,
    agent_file_inline_max_chars,
    agent_file_page_chars,
)
from core.application.orchestration.agent_file_runtime import AgentFileAccessError
from core.application.orchestration.agents.agent_files import prepare_agent_files
from core.application.orchestration.agents.common_runtime_tool_classifiers import (
    _max_tool_result_chars,
)
from core.infrastructure.config import env_registry
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.runtime.compact.process_spec import _RUNTIME_ENV

ROOT = Path(__file__).resolve().parents[2]
INLINE = "FS_AGENT_FILE_INLINE_MAX_CHARS"
PAGE = "FS_AGENT_FILE_PAGE_CHARS"
TOOL_RESULT = "FS_AGENT_TOOL_RESULT_MAX_CHARS"
DEFAULTS = {INLINE: "2000000", PAGE: "20000", TOOL_RESULT: "6000"}


class Files:
    """Authorized text files; `read_text` behaves like AgentFileContext.read_text."""

    def __init__(self, **texts: str) -> None:
        self.texts = texts
        self.reads: list[tuple[str, int, int]] = []

    def list_files(self):
        return [
            {
                "name": name,
                "filename": f"{name}.md",
                "content_type": "text/markdown",
                "text_readable": True,
                "size_bytes": len(text.encode()),
            }
            for name, text in self.texts.items()
        ]

    def read_text(self, name, offset, max_chars):
        assert 1 <= max_chars <= AGENT_TEXT_READ_MAX_CHARS
        self.reads.append((name, offset, max_chars))
        text = self.texts[name]
        if text == "<binary>":
            raise AgentFileAccessError("File contains unsupported binary data")
        end = min(offset + max_chars, len(text))
        return {"text": text[offset:end], "next_offset": end, "eof": end == len(text)}


def _prepare(files: Files, scan=None):
    return prepare_agent_files(files, model=object(), provider="", scan_text=scan)


def test_a_markdown_file_is_attached_whole_without_a_read_tool():
    document = "# Spec\n" + "line of the spec\n" * 3000 + "THE END"
    files = Files(spec=document)

    instructions, tools, blocks = _prepare(files)

    assert f'<file name="spec">\n{document}\n</file>' in instructions
    assert tools == []
    assert blocks == []
    assert files.reads == [("spec", 0, AGENT_TEXT_READ_MAX_CHARS)]


def test_files_share_one_budget_and_the_rest_are_paged(monkeypatch):
    monkeypatch.setitem(runtime_env, INLINE, "1000")
    files = Files(first="a" * 700, second="b" * 400, third="c" * 300)

    instructions, tools, _ = _prepare(files)

    assert "a" * 700 in instructions
    assert "b" * 400 not in instructions
    assert "c" * 300 in instructions
    assert (
        "Read these text files with read_agent_file, following next_offset until eof: second."
        in (instructions)
    )
    assert [tool.name for tool in tools] == ["read_agent_file"]


def test_cyrillic_text_is_measured_in_characters_not_bytes(monkeypatch):
    monkeypatch.setitem(runtime_env, INLINE, "1000")
    text = "Привет" * 150  # 900 characters, 1,800 UTF-8 bytes
    instructions, tools, _ = _prepare(Files(ru=text))

    assert text in instructions
    assert tools == []


def test_a_file_that_cannot_fit_in_four_bytes_a_character_is_not_read(monkeypatch):
    monkeypatch.setitem(runtime_env, INLINE, "1000")
    files = Files(huge="x" * 4001)

    _, tools, _ = _prepare(files)

    assert files.reads == []
    assert [tool.name for tool in tools] == ["read_agent_file"]


def test_zero_budget_pages_every_file_with_the_configured_page(monkeypatch):
    monkeypatch.setitem(runtime_env, INLINE, "0")
    monkeypatch.setitem(runtime_env, PAGE, "1500")
    files = Files(spec="z" * 4000)

    instructions, tools, _ = _prepare(files)

    assert "z" * 1500 not in instructions
    page = tools[0].invoke({"name": "spec", "offset": 0})
    assert page.startswith("next_offset: 1500\neof: False\ntext:\n")
    assert files.reads == [("spec", 0, 1500)]


def test_an_unreadable_text_file_is_left_to_the_tool_to_report():
    instructions, tools, _ = _prepare(Files(bad="<binary>"))

    assert "<file" not in instructions
    with pytest.raises(AgentFileAccessError, match="binary"):
        tools[0].invoke({"name": "bad", "offset": 0})


def test_attached_text_passes_the_output_scan():
    instructions, _, _ = _prepare(
        Files(spec="token sk-live-123"), scan=lambda text: text.replace("sk-live-123", "[REDACTED]")
    )

    assert "sk-live-123" not in instructions
    assert '<file name="spec">\ntoken [REDACTED]\n</file>' in instructions


@pytest.mark.parametrize(
    ("reader", "name", "raw", "expected"),
    [
        (agent_file_inline_max_chars, INLINE, None, AGENT_TEXT_READ_MAX_CHARS),
        (agent_file_inline_max_chars, INLINE, "", AGENT_TEXT_READ_MAX_CHARS),
        (agent_file_inline_max_chars, INLINE, "lots", AGENT_TEXT_READ_MAX_CHARS),
        (agent_file_inline_max_chars, INLINE, "150000", 150_000),
        (agent_file_inline_max_chars, INLINE, "-5", 0),
        (agent_file_inline_max_chars, INLINE, "999999999", AGENT_TEXT_READ_MAX_CHARS),
        (agent_file_page_chars, PAGE, None, 20_000),
        (agent_file_page_chars, PAGE, "10", 1_000),
        (agent_file_page_chars, PAGE, "9999999", 200_000),
        (_max_tool_result_chars, TOOL_RESULT, None, 6_000),
        (_max_tool_result_chars, TOOL_RESULT, " 50000 ", 50_000),
        (_max_tool_result_chars, TOOL_RESULT, "x", 6_000),
    ],
)
def test_limits_default_and_clamp(monkeypatch, reader, name, raw, expected):
    if raw is None:
        monkeypatch.delitem(runtime_env, name, raising=False)
    else:
        monkeypatch.setitem(runtime_env, name, raw)
    assert reader() == expected


def test_a_file_page_longer_than_the_tool_result_cap_reaches_the_model_uncut(monkeypatch):
    from core.application.orchestration.agents import common
    from core.application.orchestration.agents.common import invoke_agent

    monkeypatch.setitem(runtime_env, INLINE, "0")
    monkeypatch.setitem(runtime_env, PAGE, "10000")
    monkeypatch.setitem(runtime_env, TOOL_RESULT, "1000")
    document = "y" * 9000 + "END"

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            pages = [msg.content for msg in messages if getattr(msg, "type", "") == "tool"]
            if pages:
                assert pages[0].endswith("END")
                assert "truncated" not in pages[0]
                return AIMessage(content="done")
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_agent_file", "args": {"name": "spec", "offset": 0}, "id": "c1"}
                ],
            )

    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: Model())
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        common, "_finalize_response_with_current_runtime_helpers", lambda **kw: kw["draft_response"]
    )
    result = invoke_agent(
        state={"project_config": {}},
        persona={"system_prompt": ""},
        platform_config={},
        task_input="Read",
        agent_name="test",
        file_context=Files(spec=document),
    )
    assert result.content == "done"


def test_the_whole_file_stays_in_the_task_message_across_tool_rounds(monkeypatch):
    from langchain_core.tools import StructuredTool

    from core.application.orchestration.agents import common
    from core.application.orchestration.agents.common import invoke_agent

    document = "# Spec\n" + "requirement\n" * 5000 + "LAST LINE"
    seen: list[str] = []

    def lookup(term: str) -> str:
        """Look a term up."""
        return f"result for {term}"

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        calls = 0

        def bind_tools(self, tools):
            assert "read_agent_file" not in [tool.name for tool in tools]
            return self

        def invoke(self, messages, **kwargs):
            self.calls += 1
            seen.append(messages[0].content)
            if self.calls <= 8:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"term": str(self.calls)},
                            "id": f"c{self.calls}",
                        }
                    ],
                )
            return AIMessage(content="done")

    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: Model())
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        common, "_finalize_response_with_current_runtime_helpers", lambda **kw: kw["draft_response"]
    )
    result = invoke_agent(
        state={"project_config": {}},
        persona={"system_prompt": ""},
        platform_config={},
        task_input="Check the spec",
        agent_name="test",
        file_context=Files(spec=document),
        tools=[StructuredTool.from_function(lookup)],
        max_tool_rounds=10,
    )
    assert result.content == "done"
    assert len(seen) == 9
    assert all(document in first for first in seen)


@pytest.mark.parametrize("name", list(DEFAULTS))
def test_limits_are_operator_settings_delivered_to_app_and_worker(name):
    spec = env_registry.env_spec_map()[name]
    assert spec.operator_settable is True
    assert env_registry.registry_default_value(name) == DEFAULTS[name]
    assert name in _RUNTIME_ENV
    for filename in (
        "docker-compose.yaml",
        "docker-compose.prod.yaml",
        "docker-compose.compact.yaml",
    ):
        data = yaml.safe_load((ROOT / "infra/docker" / filename).read_text())
        section = (
            "x-compact-runtime-environment" if "compact" in filename else "x-app-worker-environment"
        )
        assert data[section][name] == "${" + name + ":-" + DEFAULTS[name] + "}"
