"""Installation-level policy for agent-readable text artifacts."""

from __future__ import annotations

import re

from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.config.runtime_env import runtime_env

_ENV_NAME = "FS_AGENT_TEXT_FILE_EXTENSIONS"
# The most text one read may return: the ceiling of both operator limits below.
AGENT_TEXT_READ_MAX_CHARS = 2_000_000
_SUFFIX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_YAML_EXTENSIONS = frozenset({".yaml", ".yml"})
_MIME_EXTENSIONS = {
    "text/markdown": frozenset({".md", ".markdown"}),
    "application/json": frozenset({".json"}),
    "application/xml": frozenset({".xml"}),
    "text/xml": frozenset({".xml"}),
    "text/csv": frozenset({".csv"}),
    "text/tab-separated-values": frozenset({".tsv"}),
    "application/yaml": _YAML_EXTENSIONS,
    "application/x-yaml": _YAML_EXTENSIONS,
    "text/yaml": _YAML_EXTENSIONS,
    "application/rtf": frozenset({".rtf"}),
    "text/rtf": frozenset({".rtf"}),
}
_EXTENSIONLESS = {**_MIME_EXTENSIONS, "text/plain": frozenset({".txt"})}
_BINARY_MIMES = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-bzip",
        "application/x-bzip2",
        "application/bzip2",
        "application/x-xz",
        "application/xz",
        "application/zstd",
        "application/x-zstd",
        "application/x-sqlite3",
        "application/vnd.sqlite3",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/vnd.rar",
        "application/x-rar-compressed",
        "application/x-executable",
        "application/x-elf",
        "application/x-msdownload",
        "application/java-archive",
        "application/wasm",
        "application/vnd.ms-excel",
        "application/msword",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
)
_BINARY_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/vnd.openxmlformats-officedocument.",
)


def get_agent_text_file_extensions() -> tuple[str, ...]:
    """Return the effective ordered, validated dotted extensions."""
    raw = runtime_env.get(_ENV_NAME)
    if raw is None or not raw.strip():
        raw = registry_default_value(_ENV_NAME)
    entries = raw.split(",")
    if len(entries) > 64:
        raise ValueError("Too many agent text file extensions")
    result = []
    for entry in entries:
        suffix = entry.strip().lower().removeprefix(".")
        if not _SUFFIX.fullmatch(suffix):
            raise ValueError("Invalid agent text file extension")
        normalized = f".{suffix}"
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def is_agent_text_file(filename: str, content_type: str) -> bool:
    """Classify metadata without allowing extensions to override binary MIME."""
    extensions = frozenset(get_agent_text_file_extensions())
    name = str(filename or "").strip().lower()
    mime = str(content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if (
        "/" in name
        or "\\" in name
        or "\x00" in name
        or re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime) is None
        or mime in _BINARY_MIMES
        or mime.startswith(_BINARY_PREFIXES)
    ):
        return False
    suffix = f".{name.rsplit('.', 1)[1]}" if "." in name and not name.endswith(".") else ""
    if not suffix:
        return bool(extensions.intersection(_EXTENSIONLESS.get(mime, frozenset())))
    if suffix not in extensions:
        return False
    recognized = _MIME_EXTENSIONS.get(mime)
    if recognized is not None:
        return suffix in recognized
    # Unknown MIME is not authority by itself. The configured suffix selects a
    # bounded read, whose strict UTF-8 and binary checks still decide exposure.
    return True


def _bounded_chars(name: str, *, fallback: int, minimum: int, maximum: int) -> int:
    try:
        default = int(registry_default_value(name))
    except (KeyError, TypeError, ValueError):
        default = fallback
    try:
        value = int(str(runtime_env.get(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def agent_file_inline_max_chars() -> int:
    """Text an agent receives in full in its task message, summed over its files.

    Files that fit are attached whole, so the model sees all of them on every
    round; zero sends every text file through ``read_agent_file`` instead.
    """
    return _bounded_chars(
        "FS_AGENT_FILE_INLINE_MAX_CHARS",
        fallback=AGENT_TEXT_READ_MAX_CHARS,
        minimum=0,
        maximum=AGENT_TEXT_READ_MAX_CHARS,
    )


def agent_file_page_chars() -> int:
    """Characters one ``read_agent_file`` call returns for a file not attached in full."""
    return _bounded_chars(
        "FS_AGENT_FILE_PAGE_CHARS", fallback=20_000, minimum=1_000, maximum=200_000
    )
