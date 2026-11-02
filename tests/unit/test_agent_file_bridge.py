"""Agent-visible file tools use only invocation-authorized handles."""

import base64

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from core.application.orchestration.agents.common import invoke_agent

_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
_PNG_BYTES = base64.b64decode(_PNG_BASE64)
_OFFLINE_API_KEY = (
    "not-real"  # pragma: allowlist secret - synthetic offline SDK fixture, never a credential
)


class FileContext:
    def list_files(self):
        return [
            {
                "name": "file_1",
                "filename": "notes.md",
                "content_type": "text/markdown",
                "text_readable": True,
                "size_bytes": 6,
            }
        ]

    def read_text(self, name, offset, max_chars):
        assert name == "file_1"
        return {"text": "hello!"[offset : offset + max_chars], "next_offset": 6, "eof": True}


def test_agent_reads_authorized_text_through_tool(monkeypatch):
    from core.application.orchestration.agents import common

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        def bind_tools(self, tools):
            self.tools = tools
            return self

        def invoke(self, messages, **kwargs):
            if len(messages) == 1:
                assert "file_1" in messages[0].content
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_agent_file",
                            "args": {"name": "file_1", "offset": 0},
                            "id": "call_1",
                        }
                    ],
                )
            assert "hello!" in messages[-1].content
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
        task_input="Read it",
        agent_name="test",
        file_context=FileContext(),
    )
    assert result.content == "done"


def test_unsupported_media_is_rejected_before_model_call(monkeypatch):
    from core.application.orchestration.agents import common

    class MediaContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "file_1",
                    "filename": "clip.mp4",
                    "content_type": "video/mp4",
                    "size_bytes": 10,
                }
            ]

    class Model:
        def invoke(self, *args, **kwargs):
            pytest.fail("unsupported media must be rejected before provider invocation")

    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: Model())
    with pytest.raises(ValueError, match=r"(?i)(video|media).*(unsupported|tool)"):
        invoke_agent(
            state={"project_config": {}},
            persona={"system_prompt": ""},
            platform_config={},
            task_input="Describe",
            agent_name="test",
            file_context=MediaContext(),
        )


def test_known_openai_image_profile_produces_native_block():
    from langchain_openai import ChatOpenAI

    from core.application.orchestration.agents.agent_files import prepare_agent_files

    class ImageContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "image",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": len(_PNG_BYTES),
                }
            ]

        def read_bytes(self, name, *, max_size_bytes):
            assert name == "image"
            return _PNG_BYTES

    model = ChatOpenAI(model="gpt-4o", api_key=_OFFLINE_API_KEY)
    _, _, blocks = prepare_agent_files(ImageContext(), model, "openai")
    assert blocks == [
        {"type": "text", "text": "Attached file: image"},
        {"type": "image", "base64": _PNG_BASE64, "mime_type": "image/png"},
    ]


def test_unknown_or_compatible_model_profile_does_not_authorize_media():
    from langchain_openai import ChatOpenAI

    from core.application.orchestration.agents.agent_files import prepare_agent_files

    class ImageContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "image",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": 3,
                }
            ]

    model = ChatOpenAI(model="gpt-4o", api_key=_OFFLINE_API_KEY)
    with pytest.raises(ValueError, match="unsupported"):
        prepare_agent_files(ImageContext(), model, "openai_compatible")
    unknown = ChatOpenAI(model="not-in-sdk-profile", api_key=_OFFLINE_API_KEY)
    with pytest.raises(ValueError, match="unsupported"):
        prepare_agent_files(ImageContext(), unknown, "openai")


def test_openai_audio_profile_produces_native_block():
    from langchain_openai import ChatOpenAI

    from core.application.orchestration.agents.agent_files import prepare_agent_files

    class AudioContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "sound",
                    "filename": "sound.wav",
                    "content_type": "audio/wav",
                    "size_bytes": 3,
                }
            ]

        def read_bytes(self, name, *, max_size_bytes):
            return b"abc"

    model = ChatOpenAI(model="test", api_key=_OFFLINE_API_KEY, profile={"audio_inputs": True})
    _, _, blocks = prepare_agent_files(AudioContext(), model, "openai")
    assert blocks == [
        {"type": "text", "text": "Attached file: sound"},
        {"type": "audio", "base64": "YWJj", "mime_type": "audio/wav"},
    ]


def test_native_media_size_rejected_before_read():
    from langchain_openai import ChatOpenAI

    from core.application.orchestration.agents.agent_files import prepare_agent_files

    class OversizeContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "image",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": 6 * 1024 * 1024,
                }
            ]

        def read_bytes(self, name, *, max_size_bytes):
            pytest.fail("oversized input must be rejected before reading")

    model = ChatOpenAI(model="gpt-4o", api_key=_OFFLINE_API_KEY)
    with pytest.raises(ValueError, match="size limit"):
        prepare_agent_files(OversizeContext(), model, "openai")


def test_native_media_reaches_model_as_content_block(monkeypatch):
    from langchain_openai import ChatOpenAI

    from core.application.orchestration.agents import common

    class ImageContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "image",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": len(_PNG_BYTES),
                }
            ]

        def read_bytes(self, name, *, max_size_bytes):
            return _PNG_BYTES

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    model = ChatOpenAI(
        model="gpt-4o",
        api_key=_OFFLINE_API_KEY,
        metadata={"flow_steward_provider_policy": "openai"},
    )
    seen = []

    def fake_invoke(self, messages, **kwargs):
        seen.extend(messages)
        return AIMessage(content="done")

    monkeypatch.setattr(ChatOpenAI, "invoke", fake_invoke)
    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        common, "_finalize_response_with_current_runtime_helpers", lambda **kw: kw["draft_response"]
    )
    result = invoke_agent(
        state={"project_config": {}},
        persona={"system_prompt": ""},
        platform_config={},
        task_input="Describe",
        agent_name="test",
        file_context=ImageContext(),
    )
    assert result.content == "done"
    assert seen[0].content[-1] == {"type": "image", "base64": _PNG_BASE64, "mime_type": "image/png"}


def test_native_media_aggregate_limit_is_checked_before_read():
    from langchain_openai import ChatOpenAI

    from core.application.orchestration.agents.agent_files import prepare_agent_files

    class OversizeContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": f"image_{i}",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": 4 * 1024 * 1024,
                }
                for i in range(3)
            ]

        def read_bytes(self, name, *, max_size_bytes):
            pytest.fail("aggregate limit must be checked before any read")

    model = ChatOpenAI(model="gpt-4o", api_key=_OFFLINE_API_KEY)
    with pytest.raises(ValueError, match="total native input size"):
        prepare_agent_files(OversizeContext(), model, "openai")


def test_agent_model_receives_all_three_text_pages_without_clipping(monkeypatch):
    from core.application.orchestration.agents import common

    content = "x" * 9000 + "END"

    class LongContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "file_1",
                    "filename": "notes.md",
                    "content_type": "text/markdown",
                    "text_readable": True,
                    "size_bytes": len(content),
                }
            ]

        def read_text(self, name, offset, max_chars):
            end = min(offset + max_chars, len(content))
            return {"text": content[offset:end], "next_offset": end, "eof": end == len(content)}

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            pages = [msg.content for msg in messages if getattr(msg, "type", "") == "tool"]
            if len(pages) == 3:
                assert "next_offset: 4000" in pages[0] and "eof: False" in pages[0]
                assert "next_offset: 8000" in pages[1] and "eof: False" in pages[1]
                assert "next_offset: 9003" in pages[2] and "eof: True" in pages[2]
                assert pages[2].endswith("END")
                assert all("truncated" not in page for page in pages)
                return AIMessage(content="done")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_agent_file",
                        "args": {"name": "file_1", "offset": len(pages) * 4000},
                        "id": f"call_{len(pages)}",
                    }
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
        file_context=LongContext(),
    )
    assert result.content == "done"


def test_openai_sdk_serializes_native_media_without_provider_call():
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="test", api_key=_OFFLINE_API_KEY, use_responses_api=False)
    message = HumanMessage(
        content=[
            {"type": "text", "text": "Attached file: picture"},
            {"type": "image", "base64": _PNG_BASE64, "mime_type": "image/png"},
            {"type": "text", "text": "Attached file: recording"},
            {"type": "audio", "base64": "YWJj", "mime_type": "audio/wav"},
        ]
    )
    payload = model._get_request_payload([message])
    content = payload["messages"][0]["content"]
    assert content[0]["text"] == "Attached file: picture"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{_PNG_BASE64}"
    assert content[2]["text"] == "Attached file: recording"
    assert content[3]["input_audio"] == {"data": "YWJj", "format": "wav"}


def test_pinned_google_video_profile_and_sdk_converter_without_provider_call():
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_google_genai.chat_models import _convert_to_parts

    from core.application.orchestration.agents.agent_files import prepare_agent_files
    from core.application.orchestration.model_provider import _build_pinned_google_chat_model

    class VideoContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "clip",
                    "filename": "clip.mp4",
                    "content_type": "video/mp4",
                    "size_bytes": 3,
                }
            ]

        def read_bytes(self, name, *, max_size_bytes):
            return b"abc"

    model = _build_pinned_google_chat_model(
        ChatGoogleGenerativeAI,
        model="gemini-2.5-pro",
        google_api_key=_OFFLINE_API_KEY,
    )
    _, _, blocks = prepare_agent_files(VideoContext(), model, "google")
    assert blocks[0] == {"type": "text", "text": "Attached file: clip"}
    parts = _convert_to_parts(blocks, model="gemini-2.5-pro")
    assert parts[1].inline_data.mime_type == "video/mp4"
    assert parts[1].inline_data.data == b"abc"


def test_pinned_google_model_receives_all_media_types(monkeypatch):
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_google_genai.chat_models import _convert_to_parts

    from core.application.orchestration.agents import common
    from core.application.orchestration.model_provider import _build_pinned_google_chat_model

    class MediaContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": name,
                    "filename": name,
                    "content_type": mime,
                    "size_bytes": len(_PNG_BYTES),
                }
                for name, mime in (
                    ("picture", "image/png"),
                    ("sound", "audio/wav"),
                    ("clip", "video/mp4"),
                )
            ]

        def read_bytes(self, name, *, max_size_bytes):
            return _PNG_BYTES

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    model = _build_pinned_google_chat_model(
        ChatGoogleGenerativeAI,
        model="gemini-2.5-pro",
        google_api_key=_OFFLINE_API_KEY,
        metadata={"flow_steward_provider_policy": "google"},
    )
    seen = []

    def fake_invoke(self, messages, **kwargs):
        seen.extend(messages)
        return AIMessage(content="done")

    monkeypatch.setattr(type(model), "invoke", fake_invoke)
    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        common, "_finalize_response_with_current_runtime_helpers", lambda **kw: kw["draft_response"]
    )
    result = invoke_agent(
        state={"project_config": {}},
        persona={"system_prompt": ""},
        platform_config={},
        task_input="Describe",
        agent_name="test",
        file_context=MediaContext(),
    )
    assert result.content == "done"
    parts = _convert_to_parts(seen[0].content, model="gemini-2.5-pro")
    assert [part.inline_data.mime_type for part in parts if part.inline_data] == [
        "image/png",
        "audio/wav",
        "video/mp4",
    ]


def test_file_read_budget_exhaustion_fails_without_forced_final_response(monkeypatch):
    from core.application.orchestration.agents import common

    class LongContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "file_1",
                    "filename": "long.txt",
                    "content_type": "text/plain",
                    "text_readable": True,
                    "size_bytes": 100000,
                }
            ]

        def read_text(self, name, offset, max_chars):
            return {"text": "x" * max_chars, "next_offset": offset + max_chars, "eof": False}

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            self.calls += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_agent_file",
                        "args": {"name": "file_1", "offset": (self.calls - 1) * 4000},
                        "id": f"call_{self.calls}",
                    }
                ],
            )

    model = Model()
    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="file_read_budget_exceeded"):
        invoke_agent(
            state={"project_config": {}},
            persona={"system_prompt": ""},
            platform_config={},
            task_input="Read",
            agent_name="test",
            file_context=LongContext(),
        )
    assert model.calls == 20


def test_agent_can_request_later_page_after_history_pruning(monkeypatch):
    from core.application.orchestration.agents import common

    content = "x" * 32000 + "END"

    class LongContext(FileContext):
        def list_files(self):
            return [
                {
                    "name": "file_1",
                    "filename": "long.txt",
                    "content_type": "text/plain",
                    "text_readable": True,
                    "size_bytes": len(content),
                }
            ]

        def read_text(self, name, offset, max_chars):
            end = min(offset + max_chars, len(content))
            return {"text": content[offset:end], "next_offset": end, "eof": end == len(content)}

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 9:
                pages = [msg.content for msg in messages if getattr(msg, "type", "") == "tool"]
                assert len(pages) < 8
                assert "next_offset: 32000" in pages[-1]
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_agent_file",
                            "args": {"name": "file_1", "offset": 32000},
                            "id": "last",
                        }
                    ],
                )
            if self.calls == 10:
                assert messages[-1].content.endswith("END")
                assert "eof: True" in messages[-1].content
                return AIMessage(content="done")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_agent_file",
                        "args": {"name": "file_1", "offset": (self.calls - 1) * 4000},
                        "id": f"call_{self.calls}",
                    }
                ],
            )

    model = Model()
    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: model)
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
        file_context=LongContext(),
    )
    assert result.content == "done"


@pytest.mark.parametrize("scenario", ["media", "complete_text", "incomplete_text"])
def test_budget_finalization_allows_completed_or_partial_work(monkeypatch, scenario):
    from langchain_core.tools import StructuredTool

    from core.application.orchestration.agents import agent_files, common

    class Context(FileContext):
        def list_files(self):
            if scenario == "media":
                return [
                    {
                        "name": "file_1",
                        "filename": "image.png",
                        "content_type": "image/png",
                        "size_bytes": len(_PNG_BYTES),
                    }
                ]
            return super().list_files()

        def read_bytes(self, name, *, max_size_bytes):
            return _PNG_BYTES

        def read_text(self, name, offset, max_chars):
            return {"text": "hello", "next_offset": 5, "eof": scenario == "complete_text"}

    class Prompt:
        def format_messages(self, **kwargs):
            return [HumanMessage(content=kwargs["task_input"])]

    class Model:
        calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            self.calls += 1
            if scenario != "media" and self.calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_agent_file",
                            "args": {"name": "file_1", "offset": 0},
                            "id": "read",
                        }
                    ],
                )
            action_call = 1 if scenario == "media" else 2
            if self.calls == action_call:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "write_report", "args": {"text": "done"}, "id": "action"}],
                )
            return AIMessage(content="final report")

    def write_report(text: str) -> str:
        """Write the report."""
        return text

    model = Model()
    monkeypatch.setattr(agent_files, "_native_kind", lambda *args: "image")
    monkeypatch.setattr(common, "build_context_block", lambda *a, **kw: "")
    monkeypatch.setattr(common, "build_langchain_prompt", lambda *a, **kw: Prompt())
    monkeypatch.setattr(common, "get_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(common, "emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        common, "_finalize_response_with_current_runtime_helpers", lambda **kw: kw["draft_response"]
    )
    kwargs = {
        "state": {"project_config": {}},
        "persona": {"system_prompt": ""},
        "platform_config": {},
        "task_input": "Work",
        "agent_name": "test",
        "file_context": Context(),
        "tools": [StructuredTool.from_function(write_report)],
        "max_tool_rounds": 1,
    }
    result = invoke_agent(**kwargs)
    assert result.content == "final report"
    assert model.calls == (2 if scenario == "media" else 3)
