"""`python -m core.entrypoints.cli` support for the schema-driven public CLI."""

from __future__ import annotations

# Must stay the first import: fixes the OTEL_* environment before libraries read it.
import core.infrastructure.observability.otel_bootstrap  # noqa: F401

# isort: split
import sys

from core.entrypoints.cli.click_app import click_app
from core.infrastructure.observability.otel_runtime import shutdown_telemetry

# A command must not wait on an unreachable collector for long before it exits.
_FINAL_FLUSH_SECONDS = 2.0


def main(argv: list[str] | None = None) -> None:
    """Run the generated Click tree with the public ``flow-steward`` program name.

    Click's standalone mode ends the process with the command's exit status.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        click_app.main(args=args, prog_name="flow-steward", standalone_mode=True)
    finally:
        # The CLI starts no exporter of its own; this flushes one a command started.
        shutdown_telemetry(_FINAL_FLUSH_SECONDS)


if __name__ == "__main__":
    main()
