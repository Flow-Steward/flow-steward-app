#!/usr/bin/env python3
"""Generate secure Flow Steward environment secrets."""

from __future__ import annotations

import argparse
import re
import secrets
import shlex
import sys
from pathlib import Path

DEFAULT_ENV_NAME = "ENCRYPTION_KEY"
DEFAULT_SECRET_BYTES = 32
DEFAULT_SECRET_FORMAT = "hex"  # pragma: allowlist secret
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def generate_secret_key(
    *, byte_count: int = DEFAULT_SECRET_BYTES, output_format: str = DEFAULT_SECRET_FORMAT
) -> str:
    """Return a cryptographically strong secret suitable for env files."""
    if byte_count < DEFAULT_SECRET_BYTES:
        raise ValueError("secret keys must use at least 32 random bytes")
    if output_format == "hex":
        return secrets.token_hex(byte_count)
    if output_format == "urlsafe":
        return secrets.token_urlsafe(byte_count)
    raise ValueError(f"unsupported secret format: {output_format}")


def write_env_value(path: Path, name: str, value: str, *, force: bool = False) -> str:
    """Create or update ``name`` in an env file, preserving unrelated content."""
    normalized_name = _validate_env_name(name)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    status = "created"
    replaced = False
    for line in existing_lines:
        key = _line_env_key(line)
        if key != normalized_name:
            output.append(line)
            continue
        current_value = line.split("=", 1)[1].strip().strip('"').strip("'") if "=" in line else ""
        if current_value and not force:
            return "exists"
        output.append(f"{normalized_name}={shlex.quote(value)}")
        status = "updated"
        replaced = True
    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{normalized_name}={shlex.quote(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a 256-bit Flow Steward secret. By default this emits an "
            "ENCRYPTION_KEY env assignment."
        )
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_ENV_NAME,
        help="Environment variable name to emit or update.",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=DEFAULT_SECRET_BYTES,
        help="Random bytes to generate. Minimum: 32.",
    )
    parser.add_argument(
        "--format",
        choices=("hex", "urlsafe"),
        default=DEFAULT_SECRET_FORMAT,
        help="Secret encoding format. hex is shell- and env-file-friendly.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Write the value into this env file instead of printing it.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Replace an existing env-file value. Rotating ENCRYPTION_KEY requires "
            "re-encrypting stored secrets."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print only the secret value when not writing an env file.",
    )
    args = parser.parse_args(argv)

    try:
        name = _validate_env_name(str(args.name or ""))
        value = generate_secret_key(
            byte_count=int(args.bytes),
            output_format=str(args.format or DEFAULT_SECRET_FORMAT),
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    env_file = args.env_file
    if env_file is not None:
        status = write_env_value(env_file, name, value, force=bool(args.force))
        if status == "exists":
            print(f"{name} already exists in {env_file}; use --force to rotate it.")
            return 0
        print(f"{status.capitalize()} {name} in {env_file}.")
        return 0

    if bool(args.raw):
        print(value)
    else:
        print(f"{name}={value}")
    return 0


def _validate_env_name(name: str) -> str:
    normalized = str(name or "").strip().upper()
    if not _ENV_NAME_RE.fullmatch(normalized):
        raise ValueError("env var names must match [A-Z][A-Z0-9_]*")
    return normalized


def _line_env_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    return stripped.split("=", 1)[0].strip()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
