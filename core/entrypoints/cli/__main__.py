"""`python -m core.entrypoints.cli` support for the schema-driven public CLI."""

from __future__ import annotations

import sys

from core.entrypoints.cli.click_app import click_app


def main(argv: list[str] | None = None) -> None:
    """Run the generated Click tree with the public ``flow-steward`` program name.

    Click's standalone mode ends the process with the command's exit status.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    click_app.main(args=args, prog_name="flow-steward", standalone_mode=True)


if __name__ == "__main__":
    main()
