"""`python -m core.entrypoints.cli` support for the schema-driven public CLI."""

from __future__ import annotations

import sys

from core.entrypoints.cli.click_app import click_app


def main(argv: list[str] | None = None) -> int:
    """Run the generated Click tree with the public ``flow-steward`` program name."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        click_app.main(args=args, prog_name="flow-steward", standalone_mode=True)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
