"""Configurable text formats share one strict policy at the agent boundary."""

import pytest

from core.application.orchestration.agent_file_policy import (
    get_agent_text_file_extensions,
    is_agent_text_file,
)
from core.infrastructure.config.runtime_env import runtime_env

ENV = "FS_AGENT_TEXT_FILE_EXTENSIONS"


def test_default_and_custom_extensions_normalize_and_blank_resets_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert get_agent_text_file_extensions() == (
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".xml",
        ".csv",
        ".tsv",
        ".yaml",
        ".yml",
        ".rtf",
    )
    monkeypatch.setitem(runtime_env, ENV, " TXT, .XML,foo, .txt ")
    assert get_agent_text_file_extensions() == (".txt", ".xml", ".foo")
    monkeypatch.setitem(runtime_env, ENV, "")
    assert get_agent_text_file_extensions() == (
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".xml",
        ".csv",
        ".tsv",
        ".yaml",
        ".yml",
        ".rtf",
    )
    assert is_agent_text_file("note.txt", "text/plain")


@pytest.mark.parametrize(
    "value", ["../txt", "txt,*", "txt,,md", "a" * 33, ",".join(f"a{i}" for i in range(65))]
)
def test_invalid_suffix_configuration_fails_closed(monkeypatch, value):
    monkeypatch.setitem(runtime_env, ENV, value)
    with pytest.raises(ValueError):
        get_agent_text_file_extensions()


@pytest.mark.parametrize(
    "name,mime",
    [
        ("report.xml", "application/xml"),
        ("notes.rtf", "application/rtf"),
        ("data.csv", "text/csv"),
        ("data.tsv", "text/tab-separated-values"),
        ("settings.yaml", "application/yaml"),
        ("note.markdown", "text/markdown"),
        ("no_extension", "text/plain"),
        ("no_extension", "application/json"),
    ],
)
def test_default_text_formats_and_legacy_extensionless_are_recognized(monkeypatch, name, mime):
    monkeypatch.delenv(ENV, raising=False)
    assert is_agent_text_file(name, mime)


def test_custom_suffix_cannot_override_media_or_binary_mime(monkeypatch):
    monkeypatch.setitem(runtime_env, ENV, "foo,png")
    assert is_agent_text_file("report.foo", "application/octet-stream")
    assert not is_agent_text_file("image.png", "image/png")
    assert not is_agent_text_file("fake.foo", "application/pdf")
    assert not is_agent_text_file("fake.foo", "video/mp4")
    assert not is_agent_text_file("document.xml", "application/xml")


def test_runtime_reads_raw_rtf_and_custom_text_without_conversion(monkeypatch):
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "rtf,foo")
    for name, mime, body in [
        ("document.rtf", "application/rtf", b"{\\rtf1 hello}"),
        ("document.foo", "application/octet-stream", b"line one\nline two"),
    ]:
        plane = ArtifactPlane(body, content_type=mime)
        plane.meta["filename"] = name
        files = context(plane)
        assert files.list_files()[0]["text_readable"] is True
        assert files.read_text("document", 0, 100)["text"] == body.decode("utf-8")


@pytest.mark.parametrize(
    "body",
    [
        b"bad\x00data",
        b"bad\x01data",
        b"\xff",
        b"%PDF-1.7",
        b"PK\x03\x04binary",
        b"{\\rtf1\\bin12 payload}",
    ],
)
def test_reader_rejects_binary_or_invalid_utf8(monkeypatch, body):
    from core.application.orchestration.agent_file_runtime import AgentFileAccessError
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "txt")
    plane = ArtifactPlane(body, content_type="application/octet-stream")
    plane.meta["filename"] = "document.txt"
    files = context(plane)
    with pytest.raises(AgentFileAccessError):
        files.read_text("document", 0, 100)


def test_model_bridge_uses_runtime_classification_for_custom_text(monkeypatch):
    from core.application.orchestration.agents.agent_files import prepare_agent_files
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "foo")
    plane = ArtifactPlane(b"hello", content_type="application/octet-stream")
    plane.meta["filename"] = "document.foo"
    listing, tools, blocks = prepare_agent_files(context(plane), model=object(), provider="")
    assert '"text_readable": true' in listing
    assert [tool.name for tool in tools] == ["read_agent_file"]
    assert blocks == []


@pytest.mark.parametrize("mime", ["text/x-custom", "application/x-custom"])
def test_custom_suffix_accepts_unknown_mime_but_still_checks_bytes(monkeypatch, mime):
    from core.application.orchestration.agent_file_runtime import AgentFileAccessError
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "foo")
    assert is_agent_text_file("report.foo", mime)
    plane = ArtifactPlane(b"ordinary text", content_type=mime)
    plane.meta["filename"] = "report.foo"
    assert context(plane).read_text("document", 0, 100)["text"] == "ordinary text"
    plane = ArtifactPlane(b"bad\x00body", content_type=mime)
    plane.meta["filename"] = "report.foo"
    with pytest.raises(AgentFileAccessError):
        context(plane).read_text("document", 0, 100)


@pytest.mark.parametrize(
    "suffix,mime",
    [
        ("xml", "application/xml"),
        ("xml", "text/xml"),
        ("rtf", "application/rtf"),
        ("rtf", "text/rtf"),
        ("yaml", "application/yaml"),
        ("yml", "text/yaml"),
    ],
)
def test_extensionless_registered_text_mime_requires_enabled_format(monkeypatch, suffix, mime):
    monkeypatch.setitem(runtime_env, ENV, suffix)
    assert is_agent_text_file("document", mime)
    monkeypatch.setitem(runtime_env, ENV, "foo")
    assert not is_agent_text_file("document", mime)


def test_known_binary_mime_is_denied_even_with_custom_suffix(monkeypatch):
    monkeypatch.setitem(runtime_env, ENV, "foo")
    for mime in ("application/pdf", "application/zip", "image/png", "audio/wav", "video/mp4"):
        assert not is_agent_text_file("document.foo", mime)


@pytest.mark.parametrize(
    "suffix,mime,body",
    [
        ("xml", "application/xml", b"<document>ok</document>"),
        ("rtf", "application/rtf", b"{\\rtf1 plain}"),
        ("yaml", "application/yaml", b"key: value\n"),
    ],
)
def test_extensionless_text_reaches_bounded_reader(monkeypatch, suffix, mime, body):
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, suffix)
    plane = ArtifactPlane(body, content_type=mime)
    plane.meta["filename"] = "document"
    assert context(plane).read_text("document", 0, 100)["text"] == body.decode("utf-8")


def test_base64_text_remains_literal_text(monkeypatch):
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "foo")
    body = b"iVBORw0KGgoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    plane = ArtifactPlane(body, content_type="application/x-custom")
    plane.meta["filename"] = "source.foo"
    assert context(plane).read_text("document", 0, 100)["text"] == body.decode("ascii")


@pytest.mark.parametrize(
    "mime",
    [
        "application/x-bzip",
        "application/x-bzip2",
        "application/bzip2",
        "application/x-xz",
        "application/xz",
        "application/zstd",
        "application/x-zstd",
        "application/x-sqlite3",
        "application/vnd.sqlite3",
    ],
)
def test_known_binary_metadata_blocks_even_utf8_body(monkeypatch, mime):
    from core.application.orchestration.agent_file_runtime import AgentFileAccessError
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "txt")
    assert not is_agent_text_file("document.txt", mime)
    plane = ArtifactPlane(b"hello", content_type=mime)
    plane.meta["filename"] = "document.txt"
    with pytest.raises(AgentFileAccessError):
        context(plane).read_text("document", 0, 10)


@pytest.mark.parametrize(
    "body,accepted",
    [
        (b"a\tb\nc\rd", True),
        (b"a\x00b", False),
        (b"a\x1fb", False),
        (b"a\x7fb", False),
        ("a\u0085b".encode(), False),
        ("a\u00a0b".encode(), True),
    ],
)
def test_text_control_guard_preserves_boundary_semantics(monkeypatch, body, accepted):
    from core.application.orchestration.agent_file_runtime import AgentFileAccessError
    from tests.unit.test_agent_file_runtime import ArtifactPlane, context

    monkeypatch.setitem(runtime_env, ENV, "txt")
    plane = ArtifactPlane(body, content_type="text/plain")
    plane.meta["filename"] = "document.txt"
    if accepted:
        assert context(plane).read_text("document", 0, 100)["text"] == body.decode()
    else:
        with pytest.raises(AgentFileAccessError):
            context(plane).read_text("document", 0, 100)
