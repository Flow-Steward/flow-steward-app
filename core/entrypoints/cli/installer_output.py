"""Terminal output of the installer command: the closing result and its renderer.

Presentation only: these helpers print an already computed ``InstallRunResult``
and pick the progress renderer for the requested output mode.
"""

from __future__ import annotations

from typing import Any

import click

from core.application.installer import InstallRunResult
from core.entrypoints.cli.platform.renderers import (
    JSONRenderer,
    PlainRenderer,
    RichRenderer,
)


def _render_installed(result: InstallRunResult) -> None:
    """The closing block: what happened, where it is, what to look at."""
    updated = bool(result.updated_to_revision)
    click.echo()
    click.secho(
        "✓ Flow Steward is updated." if updated else "✓ Flow Steward is installed.",
        fg="green",
        bold=True,
    )
    if result.product_url:
        click.echo(f"  Website: {result.product_url}")
    # The update sentence travels in warnings for the JSON result; on screen it is the
    # update note, said once, not a warning.
    update_note = ""
    for warning in result.warnings:
        if updated and warning.startswith(("Updated from ", "Updated to ")):
            update_note = warning
            continue
        click.secho(f"  Warning: {warning}", fg="yellow")
    if updated:
        _announce_update(result, note=update_note)
    if result.log_path:
        click.secho(f"  Log: {result.log_path}", dim=True)


def _announce_update(result: InstallRunResult, *, note: str = "") -> None:
    """Say in a sentence that the running version changed, and from what to what.

    AC17-R141: the defect was an update reported and not applied with nothing on the
    screen to say otherwise, so the opposite has to be said plainly too. The fields
    are in the JSON result for machines; this is the line an operator reads.
    """
    if not result.updated_to_revision:
        return
    if note:
        click.echo(f"  {note}")
        return
    if not result.updated_from_revision:
        click.echo(
            f"Updated to {result.updated_to_revision}; the runtime was not running, so the "
            "revision it replaced is not known."
        )
        return
    click.echo(f"Updated from {result.updated_from_revision} to {result.updated_to_revision}.")


def _announce_changes(result: InstallRunResult) -> None:
    """R157/R159 acceptance: what the run changed was written to the log only."""
    for change in result.changes:
        click.echo(change)


def _announce_setup_link(result: InstallRunResult) -> None:
    """Put the one-time setup link on stderr, never in the serialized result.

    The operator needs a link that works by following installer output alone, but
    `./install.sh --json > result.json` must not capture a live setup token. stderr
    reaches the terminal in both output modes and is not captured by that redirect.
    """
    if not result.bootstrap_setup_url:
        return
    click.echo(
        "\n".join(
            (
                "",
                "=" * 60,
                "NEXT STEP: CREATE THE FIRST ADMINISTRATOR",
                "",
                "Open this link in your browser:",
                "",
                result.bootstrap_setup_url,
                "",
                "This one-time link contains a secret setup token.",
                "Do not share it.",
                "=" * 60,
                "",
            )
        ),
        err=True,
    )


def _render_already_installed(result: InstallRunResult) -> None:
    click.echo("Flow Steward is already installed.")
    click.echo()
    # AC17-R162: "the running images were built from another revision, and left in
    # place" reached the JSON only, and a rollback that did nothing read as a success.
    for warning in result.warnings:
        click.echo(f"Warning: {warning}")
    if result.warnings:
        click.echo()
    if result.bootstrap_instruction:
        click.echo(result.bootstrap_instruction)
        # AC17-R93: the serialized result carried bootstrap_url while the human-readable
        # render printed only the prose, so an instruction ending "open the resulting URL"
        # never showed which URL. The token-bearing link is announced separately on
        # stderr; this bare URL holds no secret. Printing both would be redundant.
        if result.bootstrap_url and not result.bootstrap_setup_url:
            click.echo(f"  {result.bootstrap_url}")
        click.echo()
    click.echo("Manage this installation with:")
    click.echo("  ./flow-steward status")
    click.echo("  ./flow-steward doctor")
    if result.log_path:
        click.echo(f"Log: {result.log_path}")


def _installer_renderer(
    *, json_requested: bool, plain_requested: bool, color_enabled: bool, animation_enabled: bool
) -> Any:
    if json_requested:
        return JSONRenderer()
    if plain_requested:
        return PlainRenderer()
    return RichRenderer(color_enabled=color_enabled, animation_enabled=animation_enabled)
