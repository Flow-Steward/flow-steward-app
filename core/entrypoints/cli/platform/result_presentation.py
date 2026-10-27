"""Plain and Rich presentation of CLI operation results and progress."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import click
from pydantic import BaseModel

from core.application.management.command_specs.contracts import (
    ExecutionResult,
    OperationProgressEvent,
)


def _data(value: ExecutionResult | BaseModel | None) -> dict[str, Any]:
    if isinstance(value, ExecutionResult):
        if value.public_result is not None:
            return dict(value.public_result)
        value = value.result
    return value.model_dump(mode="json") if value is not None else {}


def _render_users_list_plain(payload: dict[str, Any]) -> None:
    users = payload.get("users") or []
    if not users:
        click.echo("No users found.")
        return
    for row in users:
        if hasattr(row, "model_dump"):
            row = row.model_dump(mode="json")
        if not isinstance(row, dict):
            continue
        roles = ",".join(str(role) for role in row.get("roles", []) if str(role))
        email = str(row.get("email") or row.get("username") or "")
        click.echo(
            " ".join(
                (
                    f"id={row.get('id', '')}",
                    f"email={email}",
                    f"roles={roles}",
                    f"active={str(bool(row.get('is_active', False))).lower()}",
                    f"reset={str(bool(row.get('must_change_password', False))).lower()}",
                )
            )
        )


def _readiness_rows(payload: dict[str, Any], area: str) -> list[dict[str, Any]]:
    return [
        row
        for row in payload.get("findings", ())
        if isinstance(row, dict) and str(row.get("area") or "") == area
    ]


def _error_findings(details: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in details.get("findings", ()) if isinstance(row, dict)]


def _render_error_findings_plain(details: dict[str, Any]) -> None:
    for row in _error_findings(details):
        click.echo(
            f"{row.get('status', 'failed')}: {row.get('check_id', '')} — {row.get('title', '')}"
        )
        if row.get("detail"):
            click.echo(f"  {row['detail']}")
        if row.get("remediation"):
            click.echo(f"  Next: {row['remediation']}")


def _render_error_findings_rich(console: Any, details: dict[str, Any]) -> None:
    from rich.markup import escape

    for row in _error_findings(details):
        status = escape(str(row.get("status") or "failed"))
        check_id = escape(str(row.get("check_id") or ""))
        title = escape(str(row.get("title") or ""))
        console.print(f"[red]{status}[/red]: {check_id} — {title}")
        if row.get("detail"):
            console.print(f"  {escape(str(row['detail']))}")
        if row.get("remediation"):
            console.print(f"  [cyan]Next[/cyan]: {escape(str(row['remediation']))}")


def _status_summary(payload: dict[str, Any]) -> str:
    return (
        f"Core: {payload.get('core_passed', 0)} passed, "
        f"{payload.get('core_failed', 0)} failed; "
        f"optional warnings: {payload.get('optional_warnings', 0)}; "
        f"recommendations: {payload.get('recommendations', 0)}"
    )


def _status_notices(payload: dict[str, Any]) -> list[str]:
    """What `status` keeps beside its blockers: an operation running on the stack now."""
    return [
        f"{row.get('title', '')}: {row.get('detail', '')}".strip(": ")
        for row in payload.get("findings") or []
        if isinstance(row, dict) and row.get("status") == "info"
    ]


def _render_status_plain(payload: dict[str, Any]) -> None:
    click.echo(f"Status: {payload.get('status', 'unknown')}")
    click.echo(_status_summary(payload))
    for notice in _status_notices(payload):
        click.echo(f"Note: {notice}")


def _render_status_rich(console: Any, payload: dict[str, Any]) -> None:
    from rich.markup import escape

    status = str(payload.get("status") or "unknown")
    style = "green" if status == "ready" else "red"
    console.print(f"[bold {style}]{escape(status.upper())}[/bold {style}]")
    console.print(escape(_status_summary(payload)))
    for notice in _status_notices(payload):
        console.print(f"[yellow]Note[/yellow]: {escape(notice)}")


def _render_readiness_plain(payload: dict[str, Any]) -> None:
    sections = (
        ("Core readiness", "core"),
        ("Optional features", "optional"),
        ("Recommendations", "recommendation"),
    )
    for label, area in sections:
        rows = _readiness_rows(payload, area)
        click.echo(f"{label} ({len(rows)})")
        if not rows:
            click.echo("  none")
            continue
        for row in rows:
            click.echo(f"  {row.get('status', 'unknown')}: {row.get('title', '')}")
            if row.get("detail"):
                click.echo(f"    {row['detail']}")
            if row.get("remediation"):
                click.echo(f"    Next: {row['remediation']}")


def _render_readiness_rich(console: Any, payload: dict[str, Any]) -> None:
    from rich.markup import escape

    styles = {"core": "cyan", "optional": "yellow", "recommendation": "blue"}
    for label, area in (
        ("Core readiness", "core"),
        ("Optional features", "optional"),
        ("Recommendations", "recommendation"),
    ):
        rows = _readiness_rows(payload, area)
        console.print(f"[bold {styles[area]}]{label}[/bold {styles[area]}] ({len(rows)})")
        for row in rows:
            status = str(row.get("status") or "unknown")
            console.print(f"  {escape(status)}: {escape(str(row.get('title') or ''))}")
            if row.get("remediation"):
                console.print(f"    [dim]Next: {escape(str(row['remediation']))}[/dim]")


def _render_extensions_create_plain(payload: dict[str, Any]) -> None:
    click.echo(
        " ".join(
            (
                f"status={payload.get('status', '')}",
                f"extension_id={payload.get('extension_id', '')}",
                f"path={payload.get('path', '')}",
                f"files_created={payload.get('files_created', 0)}",
            )
        )
    )


def _render_users_list_rich(console: Any, payload: dict[str, Any]) -> None:
    from rich.table import Table
    from rich.text import Text

    users = payload.get("users") or []
    if not users:
        console.print("[dim]No users found.[/dim]")
        return
    table = Table(title="Users", show_lines=False)
    for column in ("ID", "Email", "Name", "Roles", "Active", "Reset", "Account"):
        table.add_column(column)
    for row in users:
        if hasattr(row, "model_dump"):
            row = row.model_dump(mode="json")
        if not isinstance(row, dict):
            continue
        roles = ", ".join(str(role) for role in row.get("roles", []) if str(role))
        email = str(row.get("email") or row.get("username") or "")
        table.add_row(
            Text(str(row.get("id") or "")),
            Text(email),
            Text(str(row.get("display_name") or "")),
            Text(roles),
            Text("yes" if bool(row.get("is_active", False)) else "no"),
            Text("yes" if bool(row.get("must_change_password", False)) else "no"),
            Text(str(row.get("account_id") or "")),
        )
    console.print(table)
    total = int(payload.get("total") or len(users))
    limit = int(payload.get("limit") or len(users) or 1)
    offset = int(payload.get("offset") or 0)
    shown = len(users)
    console.print(f"Showing {shown} of {total} users (offset {offset}, limit {limit}).")
    if bool(payload.get("has_more")):
        console.print(
            f"Next page: flow-steward users list --offset {offset + shown} --limit {limit}"
        )


def _render_rich_payload(
    console: Any, payload: dict[str, Any], *, escape: Callable[[str], str]
) -> None:
    operation_payload = bool(payload.get("operation"))
    for key, value in payload.items():
        if key == "command" or (operation_payload and key in {"operation", "status"}):
            continue
        if value in (None, "", [], {}):
            continue
        escaped_key = escape(str(key))
        if key == "stdout":
            console.print("[bold]stdout[/bold]")
            console.print(escape(str(value)), highlight=False, soft_wrap=True)
        elif key == "stderr":
            console.print("[bold yellow]stderr[/bold yellow]")
            console.print(escape(str(value)), highlight=False, soft_wrap=True)
        elif key == "artifacts" and isinstance(value, list):
            console.print("[bold]artifacts[/bold]")
            for artifact in value:
                console.print(f"- {escape(str(artifact))}")
        elif isinstance(value, (dict, list)):
            console.print(f"[bold]{escaped_key}[/bold]")
            console.print(
                escape(
                    json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, indent=2)
                ),
                highlight=False,
                soft_wrap=True,
            )
        else:
            console.print(
                f"[cyan]{escaped_key}[/cyan]: {escape(str(value))}",
                highlight=False,
                soft_wrap=True,
            )


def _progress_line(event: OperationProgressEvent) -> str:
    status = event.status.value
    total = (
        f" ({event.completed}/{event.total})"
        if event.completed is not None and event.total is not None
        else ""
    )
    detail = f": {event.detail}" if event.detail else ""
    return f"{status}: {event.label}{total}{detail}"
