"""Human layout of command results, shared by the coloured and the plain renderer.

A command result is turned into a short list of blocks (a headline, aligned
key/value pairs, tables, notes). The coloured renderer and the plain renderer
print the same blocks, so both read the same and no command falls back to raw
JSON. Only ``--json`` prints JSON.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import click

from core.application.management.command_specs.contracts import (
    CLIWarning,
    CommandSpec,
    OperationType,
)
from core.entrypoints.cli.platform.command_names import command_line, with_invocation_prefix

Tone = Literal["plain", "success", "error", "warning", "muted", "heading", "label"]


@dataclass(frozen=True)
class Line:
    """One line of text with a meaning that the coloured renderer turns into a colour."""

    text: str
    tone: Tone = "plain"
    indent: int = 0


@dataclass(frozen=True)
class Pairs:
    """Aligned ``label  value`` rows."""

    rows: tuple[tuple[str, str], ...]
    indent: int = 0


@dataclass(frozen=True)
class Grid:
    """A borderless table: a header row and aligned columns."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    indent: int = 0


Block = Line | Pairs | Grid

SUCCESS_MARK = "✓"
FAILURE_MARK = "✗"
WARNING_MARK = "!"

_MAX_GRID_COLUMNS = 7
_INLINE_LIST_WIDTH = 80
_HIDDEN_KEYS = frozenset({"command", "schema", "ok", "operation"})
_HEADLINE_KEYS = frozenset({"status"})
_NEXT_KEYS = ("next_steps", "next_step", "next_commands")
_FAILED_STATUSES = frozenset(
    {"failed", "failure", "error", "blocked", "invalid", "unhealthy", "not_ready", "refused"}
)
_WARNING_STATUSES = frozenset(
    {"warning", "warnings", "degraded", "partial", "pending", "skipped", "stale", "unknown"}
)
_ACRONYMS = {
    "id": "ID",
    "ids": "IDs",
    "url": "URL",
    "urls": "URLs",
    "api": "API",
    "dsn": "DSN",
    "sha": "SHA",
    "ttl": "TTL",
    "tls": "TLS",
    "http": "HTTP",
    "https": "HTTPS",
    "mcp": "MCP",
    "llm": "LLM",
    "ui": "UI",
    "db": "DB",
    "pid": "PID",
    "cpu": "CPU",
    "smtp": "SMTP",
    "json": "JSON",
}
# Labels that read better than the field name.
_LABELS = {
    "is_active": "Active",
    "must_change_password": "Password reset",  # pragma: allowlist secret - a label
    "display_name": "Name",
    "account_id": "Account",
    "size_bytes": "Size",
    "verification_status": "Verified",
}
# What a change did, said as a sentence; commands not listed say "<subject>: <status>".
_MESSAGES = {
    "backup.create": "Backup created and verified",
    "backup.verify": "Backup verified",
    "clone.create": "Clone archive created",
    "clone.restore": "Clone restored",
    "config.set": "Setting recorded; apply it with flow-steward config apply",
    "config.apply": "Configuration applied",
    "users.create": "User created",
    "users.set-password": "Password changed",  # pragma: allowlist secret - a message
    "users.set-roles": "Roles changed",
    "users.set-active": "User updated",
    "users.unlock": "User unlocked",
    "users.force-reset": "Password reset required at next sign-in",
    "extensions.install": "Extension installed",
    "extensions.uninstall": "Extension uninstalled",
    "extensions.enable": "Extension enabled",
    "extensions.disable": "Extension disabled",
    "jobs.cancel": "Job cancelled",
    "jobs.restart": "Job restarted",
    "jobs.approve": "Approved",
    "jobs.reject": "Rejected",
    "release.upgrade": "Release upgraded",
    "database.upgrade": "Database upgraded",
}
# The columns worth a table cell, per command and list field; other lists pick their own.
_TABLE_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "backup.list": {"backups": ("path", "size_bytes", "created_at", "verification_status")},
    "users.list": {
        "users": (
            "id",
            "email",
            "display_name",
            "roles",
            "is_active",
            "must_change_password",
        )
    },
}
_USER_FACTS = ("email", "roles", "is_active", "must_change_password", "id")
# The facts worth showing of a nested record, per command; other records show everything.
_RECORD_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    command: {"user": _USER_FACTS}
    for command in (
        "users.create",
        "users.set-password",
        "users.set-roles",
        "users.set-active",
        "users.unlock",
        "users.force-reset",
    )
}
_ISO_DATETIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]00:?00)?([+-]\d{2}:?\d{2})?$"
)


def humanize_key(key: str) -> str:
    """``files_created`` -> ``Files created``; ``base_url`` -> ``Base URL``."""
    if key in _LABELS:
        return _LABELS[key]
    words = [word for word in re.split(r"[_\-\s.]+", str(key)) if word]
    if not words:
        return str(key)
    rendered = [_ACRONYMS.get(word.lower(), word.lower()) for word in words]
    first = rendered[0]
    rendered[0] = first if first in _ACRONYMS.values() else first[:1].upper() + first[1:]
    return " ".join(rendered)


_PYDANTIC_PREFIXES = ("Value error, ", "Assertion failed, ")


def readable_issue(message: str) -> str:
    """``Value error, Invalid email address`` -> ``invalid email address``."""
    text = str(message).strip()
    for prefix in _PYDANTIC_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def humanize_value(value: str) -> str:
    """``already_enabled`` -> ``already enabled``; other text is kept as written."""
    text = str(value)
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)+", text):
        return text.replace("_", " ")
    return text


def format_bytes(value: int) -> str:
    """``699923`` -> ``684 KiB``."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" or size >= 10 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def format_field(key: str, value: Any) -> str:
    """A scalar formatted with what its field name says about it."""
    if isinstance(value, int) and not isinstance(value, bool) and str(key).endswith("bytes"):
        return format_bytes(value)
    return format_scalar(value)


def format_scalar(value: Any) -> str:
    """Render one scalar the way a person reads it."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "none"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    match = _ISO_DATETIME.match(text)
    if match:
        date, clock, utc, offset = match.groups()
        zone = " UTC" if utc else f" {offset}" if offset else ""
        return f"{date} {clock}{zone}"
    return text


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == ()


def _compact(value: Any) -> str:
    """A nested value squeezed into one table cell."""
    if _is_scalar(value):
        return format_scalar(value)
    if isinstance(value, Mapping):
        return ", ".join(f"{humanize_key(k)}: {_compact(v)}" for k, v in value.items()) or "none"
    if isinstance(value, Sequence):
        return ", ".join(_compact(item) for item in value) or "none"
    return str(value)


def _status_tone(status: str) -> Tone:
    normalized = status.strip().lower()
    if normalized in _FAILED_STATUSES:
        return "error"
    if normalized in _WARNING_STATUSES:
        return "warning"
    return "success"


def _mark(tone: Tone) -> str:
    return {"error": FAILURE_MARK, "warning": WARNING_MARK}.get(tone, SUCCESS_MARK)


def _command_words(spec: CommandSpec) -> str:
    return " ".join(spec.path)


def _grid_columns(rows: Sequence[Mapping[str, Any]], preferred: Sequence[str] = ()) -> list[str]:
    if preferred:
        return [column for column in preferred if any(column in row for row in rows)]
    columns: list[str] = []
    for row in rows:
        for key, value in row.items():
            if key in columns or key in _HIDDEN_KEYS:
                continue
            if _is_scalar(value) or (
                isinstance(value, Sequence) and all(_is_scalar(item) for item in value)
            ):
                columns.append(key)
    return columns[:_MAX_GRID_COLUMNS]


def _list_blocks(
    key: str, items: Sequence[Any], indent: int, preferred: Sequence[str] = ()
) -> list[Block]:
    label = humanize_key(key)
    if not items:
        return [Pairs(((label, "none"),), indent)]
    if all(_is_scalar(item) for item in items):
        joined = ", ".join(format_scalar(item) for item in items)
        if len(joined) <= _INLINE_LIST_WIDTH and "\n" not in joined:
            return [Pairs(((label, joined),), indent)]
        return [
            Line(label, "heading", indent),
            *(Line(f"• {format_scalar(item)}", "plain", indent + 2) for item in items),
        ]
    if all(isinstance(item, Mapping) for item in items):
        columns = _grid_columns(items, preferred)
        if columns:
            rows = tuple(
                tuple(
                    ""
                    if row.get(column) is None
                    else format_field(column, row[column])
                    if _is_scalar(row[column])
                    else _compact(row.get(column, ""))
                    for column in columns
                )
                for row in items
            )
            return [
                Line(f"{label} ({len(items)})", "heading", indent),
                Grid(tuple(humanize_key(column) for column in columns), rows, indent + 2),
            ]
    blocks: list[Block] = [Line(f"{label} ({len(items)})", "heading", indent)]
    for item in items:
        if isinstance(item, Mapping):
            blocks.extend(payload_blocks(item, indent=indent + 2, bullet=True))
        else:
            blocks.append(Line(f"• {_compact(item)}", "plain", indent + 2))
    return blocks


def payload_blocks(
    payload: Mapping[str, Any],
    *,
    indent: int = 0,
    skip: Iterable[str] = (),
    bullet: bool = False,
    columns: Mapping[str, Sequence[str]] | None = None,
    fields: Mapping[str, Sequence[str]] | None = None,
) -> list[Block]:
    """Lay out a result mapping as pairs, sections and tables, in the model's field order."""
    skipped = set(skip) | _HIDDEN_KEYS
    blocks: list[Block] = []
    pending: list[tuple[str, str]] = []

    def flush() -> None:
        if pending:
            blocks.append(Pairs(tuple(pending), indent))
            pending.clear()

    for key, value in payload.items():
        if key in skipped or _is_empty(value):
            continue
        if isinstance(value, str) and "\n" in value.strip():
            flush()
            blocks.append(Line(humanize_key(key), "heading", indent))
            blocks.extend(Line(line, "plain", indent + 2) for line in value.rstrip().splitlines())
        elif _is_scalar(value):
            pending.append((humanize_key(key), format_field(key, value)))
        elif isinstance(value, Mapping):
            flush()
            chosen = (fields or {}).get(key)
            if chosen:
                value = {name: value[name] for name in chosen if name in value}
            blocks.append(Line(humanize_key(key), "heading", indent))
            blocks.extend(payload_blocks(value, indent=indent + 2))
        elif isinstance(value, Sequence):
            flush()
            blocks.extend(_list_blocks(key, value, indent, (columns or {}).get(key, ())))
        else:
            pending.append((humanize_key(key), str(value)))
    flush()
    if bullet and blocks:
        first = blocks[0]
        if isinstance(first, Pairs) and first.rows:
            (label, value), *rest = first.rows
            blocks[0] = Line(f"• {label}: {value}", "plain", indent - 2 if indent >= 2 else 0)
            if rest:
                blocks.insert(1, Pairs(tuple(rest), indent))
    return blocks


def _next_lines(payload: Mapping[str, Any]) -> list[Block]:
    steps: list[str] = []
    for key in _NEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            steps.append(value.strip())
        elif isinstance(value, Sequence) and not isinstance(value, str):
            steps.extend(str(item).strip() for item in value if str(item).strip())
    if not steps:
        return []
    if len(steps) == 1:
        return [Line(f"Next: {with_invocation_prefix(steps[0])}", "muted")]
    return [
        Line("Next:", "muted"),
        *(Line(with_invocation_prefix(step), "muted", 2) for step in steps),
    ]


_PAGING_KEYS = (
    "total",
    "limit",
    "offset",
    "has_more",
    "page",
    "per_page",
    "total_pages",
    "count",
)


def _only_list(payload: Mapping[str, Any]) -> tuple[str, list[Any]] | None:
    """The one list a listing command returns, if it returns exactly one."""
    lists = [(key, value) for key, value in payload.items() if isinstance(value, list)]
    return lists[0] if len(lists) == 1 else None


def _option(spec: CommandSpec, key: str) -> str | None:
    field = next((field for field in spec.fields if field.key == key), None)
    if field is None or field.positional:
        return None
    return field.option_name or "--" + key.replace("_", "-")


def _paging_lines(spec: CommandSpec, payload: Mapping[str, Any]) -> list[Block]:
    """``Showing 20 of 135`` and the command for the next page, instead of raw paging fields."""
    keys = payload.keys()
    if not ({"has_more", "total_pages"} & keys or {"total", "limit"} <= keys):
        return []
    listed = _only_list(payload)
    shown = len(listed[1]) if listed else 0
    total = payload.get("total")
    page, pages = payload.get("page"), payload.get("total_pages")
    summary = f"Showing {shown} of {total}" if isinstance(total, int) else f"Showing {shown}"
    if isinstance(page, int) and isinstance(pages, int):
        summary += f" (page {page} of {pages})"
    lines: list[Block] = [Line(summary + ".", "muted")]
    more = bool(payload.get("has_more")) or (
        isinstance(page, int) and isinstance(pages, int) and page < pages
    )
    offset_option, limit_option = _option(spec, "offset"), _option(spec, "limit")
    if more and offset_option:
        words = [*spec.path, offset_option, str(int(payload.get("offset") or 0) + shown)]
        limit = payload.get("limit", payload.get("per_page"))
        if limit_option and isinstance(limit, int):
            words += [limit_option, str(limit)]
        lines.append(Line(f"Next page: {command_line(*words)}", "muted"))
    return lines


def _empty_listing(spec: CommandSpec, payload: Mapping[str, Any]) -> list[Block]:
    """``No jobs found.`` for a read command whose only list is empty."""
    if spec.operation_type is not OperationType.READ:
        return []
    listed = _only_list(payload)
    if listed is None or listed[1]:
        return []
    return [Line(f"No {humanize_key(listed[0]).lower()} found.", "muted", 2)]


def _payload_warnings(payload: Mapping[str, Any]) -> list[str]:
    value = payload.get("warnings")
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [str(item) for item in value if _is_scalar(item) and str(item).strip()]
    return []


def result_blocks(
    spec: CommandSpec,
    payload: Mapping[str, Any],
    warnings: Sequence[CLIWarning] = (),
    *,
    log_path: str | None = None,
) -> list[Block]:
    """The whole human answer of a successful command."""
    status = payload.get("status")
    status_text = str(status).strip() if _is_scalar(status) and status is not None else ""
    blocks: list[Block] = []
    skip: set[str] = {*_NEXT_KEYS}
    if log_path and payload.get("log_path") == log_path:
        skip.add("log_path")  # the renderer prints "Log:" once
    if spec.operation_type is OperationType.READ:
        blocks.append(Line(humanize_key(_command_words(spec)), "heading"))
    else:
        tone = _status_tone(status_text) if status_text else "success"
        message = _MESSAGES.get(spec.command_id)
        if message and tone == "success":
            headline = with_invocation_prefix(message)
        elif status_text:
            headline = f"{humanize_key(spec.path[0])}: {humanize_value(status_text)}"
        else:
            headline = f"Done: {_command_words(spec)}"
        blocks.append(Line(f"{_mark(tone)} {headline}", tone))
        skip |= _HEADLINE_KEYS
    empty = _empty_listing(spec, payload)
    paging = [] if empty else _paging_lines(spec, payload)
    if paging or empty:
        skip.update(_PAGING_KEYS)
    payload_warnings = _payload_warnings(payload)
    if payload_warnings:
        skip.add("warnings")
    blocks.extend(
        payload_blocks(
            payload,
            indent=2,
            skip=skip,
            columns=_TABLE_COLUMNS.get(spec.command_id),
            fields=_RECORD_FIELDS.get(spec.command_id),
        )
    )
    blocks.extend(empty or paging)
    for message in (*payload_warnings, *(warning.message for warning in warnings)):
        blocks.append(Line(f"{WARNING_MARK} {with_invocation_prefix(message)}", "warning"))
    blocks.extend(_next_lines(payload))
    return blocks


def plan_blocks(spec: CommandSpec, plan: Mapping[str, Any]) -> list[Block]:
    """The impact plan shown before a confirmation, as readable sections."""
    blocks: list[Block] = [Line(f"Impact plan: {_command_words(spec)}", "heading")]
    sections: tuple[tuple[str, Tone], ...] = (
        ("remove", "error"),
        ("affected", "warning"),
        ("retained", "success"),
    )
    section_keys = {key for key, _ in sections}
    blocks.extend(payload_blocks(plan, indent=2, skip=section_keys))
    for key, tone in sections:
        rows = plan.get(key)
        if not isinstance(rows, list):
            continue
        blocks.append(Line(f"{humanize_key(key)} ({len(rows)})", tone, 2))
        if not rows:
            blocks.append(Line("none", "muted", 4))
        for row in rows:
            if isinstance(row, Mapping):
                blocks.extend(payload_blocks(row, indent=6, bullet=True))
            else:
                blocks.append(Line(f"• {_compact(row)}", "plain", 4))
    return blocks


# --- printing -------------------------------------------------------------------------------


def _grid_lines(grid: Grid) -> list[str]:
    widths = [len(column) for column in grid.columns]
    for row in grid.rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row, strict=True)]
    pad = " " * grid.indent

    def line(cells: Sequence[str]) -> str:
        return pad + "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))

    return [line(grid.columns).rstrip(), *(line(row).rstrip() for row in grid.rows)]


def _pair_lines(pairs: Pairs) -> list[tuple[str, str]]:
    width = max((len(label) for label, _ in pairs.rows), default=0)
    pad = " " * pairs.indent
    return [
        (f"{pad}{label}:".ljust(width + pairs.indent + 2), value) for label, value in pairs.rows
    ]


def emit_plain(blocks: Iterable[Block], *, err: bool = False) -> None:
    """Print blocks as plain text: same layout as the coloured form, no escape codes."""
    for block in blocks:
        if isinstance(block, Line):
            click.echo(" " * block.indent + block.text, err=err)
        elif isinstance(block, Pairs):
            for label, value in _pair_lines(block):
                click.echo(f"{label}{value}", err=err)
        else:
            for text in _grid_lines(block):
                click.echo(text, err=err)


_STYLES: dict[Tone, str] = {
    "plain": "",
    "success": "bold green",
    "error": "bold red",
    "warning": "yellow",
    "muted": "dim",
    "heading": "bold",
    "label": "cyan",
}


def emit_rich(console: Any, blocks: Iterable[Block]) -> None:
    """Print blocks through a Rich console with colour carrying the meaning."""
    from rich.text import Text

    for block in blocks:
        if isinstance(block, Line):
            console.print(
                Text(" " * block.indent + block.text, style=_STYLES[block.tone]),
                soft_wrap=True,
            )
        elif isinstance(block, Pairs):
            for label, value in _pair_lines(block):
                # A base style would colour the value too; only the label is styled.
                text = Text()
                text.append(label, style=_STYLES["label"])
                text.append(value)
                console.print(text, soft_wrap=True)
        else:
            header, *rows = _grid_lines(block)
            console.print(Text(header, style="bold"), soft_wrap=True)
            for row in rows:
                console.print(Text(row), soft_wrap=True)
