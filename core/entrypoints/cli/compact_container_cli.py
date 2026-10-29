"""Management CLI for an operator already inside a Compact runtime container."""

from __future__ import annotations

import json
import sys
from collections.abc import MutableMapping
from pathlib import Path

import click

from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.runtime.compact.runtime_secrets import self_minted_secret_names

_HOST_ONLY_DOMAINS = frozenset({"config", "environment", "quality", "release", "stack", "testing"})
#: An operator who types `config set NAME=VALUE` on Compact out of habit learns the path.
_COMPACT_HOST_SETTINGS_PATH = (
    "Host settings on Compact: ./install.sh --setting NAME=VALUE. "
    "Settings: https://docs.flowsteward.org/docs/reference/host-settings"
)
_UNAVAILABLE_COMMANDS = {
    "extensions": frozenset({"install", "uninstall"}),
    "users": frozenset({"issue-bootstrap-token"}),
}
_GLOBAL_FLAGS = frozenset({"--json", "--plain", "--no-color", "--verbose", "--non-interactive"})


def restore_runtime_secrets(state_path: Path, environment: MutableMapping[str, str]) -> None:
    """Give this one operator process the secrets PID 1 generated for the app."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Compact runtime secret state is unavailable") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError("Compact runtime secret state has an unsupported format")
    minted = state.get("minted")
    if not isinstance(minted, dict):
        raise RuntimeError("Compact runtime secret state has an unsupported format")

    for name in self_minted_secret_names():
        if environment.get(name):
            continue
        value = minted.get(name)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Compact runtime secret state is missing {name}")
        environment[name] = value


def _command_path(arguments: list[str]) -> tuple[str, str]:
    words = [argument for argument in arguments if argument not in _GLOBAL_FLAGS]
    if not words or words[0].startswith("-"):
        return "", ""
    return words[0], words[1] if len(words) > 1 and not words[1].startswith("-") else ""


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if runtime_env.get("FS_DEPLOYMENT_PROFILE") != "compact":
        print("This management command requires the Compact runtime container.", file=sys.stderr)
        return 2

    top, subcommand = _command_path(arguments)
    if top in _HOST_ONLY_DOMAINS:
        print(
            f"{top} is not available in Compact container: it requires host Docker "
            "or installer tooling. Use the host ./flow-steward on a VPS.",
            file=sys.stderr,
        )
        if top == "config":
            print(_COMPACT_HOST_SETTINGS_PATH, file=sys.stderr)
        return 2
    if subcommand in _UNAVAILABLE_COMMANDS.get(top, ()):
        print(
            f"{top} {subcommand} is not available in Compact container; "
            "use the installation procedure for this operation.",
            file=sys.stderr,
        )
        return 2

    data_root = Path(runtime_env.get("FS_COMPACT_DATA_ROOT") or "/data")
    try:
        restore_runtime_secrets(data_root / "state" / "runtime-secrets.json", runtime_env)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    runtime_env.setdefault("FS_DOCKER_HOST_PROJECT_ROOT", str(data_root / "state"))
    from core.entrypoints.cli.click_app import _runtime, click_app
    from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime
    from core.entrypoints.cli.runtime import CLIRuntime

    class CompactManagementRuntime(CompactPlatformRuntime, CLIRuntime):
        """Product CLI services with Compact-native lifecycle adapters."""

    for domain in _HOST_ONLY_DOMAINS:
        click_app.commands.pop(domain, None)
    for domain, commands in _UNAVAILABLE_COMMANDS.items():
        group = click_app.commands.get(domain)
        if isinstance(group, click.Group):
            for command in commands:
                group.commands.pop(command, None)
    _runtime._runtime = CompactManagementRuntime()
    click_app.main(args=arguments or ["--help"], prog_name="flow-steward")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
