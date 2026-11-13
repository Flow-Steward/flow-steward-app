"""Tab completion: generated from the command tree, served without Docker."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.entrypoints.cli.platform.completion import (
    COMPLETION_DIR,
    PROGRAM,
    SHELLS,
    command_tree,
    render,
)

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "core/entrypoints/cli/scripts/flow_steward_launcher.sh"


@pytest.mark.parametrize("shell", SHELLS)
def test_committed_scripts_match_the_command_tree(shell: str) -> None:
    committed = (COMPLETION_DIR / f"{PROGRAM}.{shell}").read_text(encoding="utf-8")

    assert committed == render(shell), (
        "Regenerate: python -m core.entrypoints.cli.platform.completion write"
    )


def test_the_tree_holds_commands_options_and_choices() -> None:
    tree = command_tree()
    users = next(child for child in tree.children if child.name == "users")
    create = next(child for child in users.children if child.name == "create")
    role = next(option for option in create.options if "--role" in option.names)

    assert {"backup", "maintenance", "status", "users"} <= {c.name for c in tree.children}
    assert set(role.choices) == {"admin", "operator", "viewer"}
    assert "--tui" not in render("bash")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_bash_completes_the_next_word() -> None:
    script = COMPLETION_DIR / f"{PROGRAM}.bash"
    probe = (
        f"source {script}; "
        't() { COMP_WORDS=("$@"); COMP_CWORD=$((${#COMP_WORDS[@]} - 1)); '
        'COMPREPLY=(); _flow_steward_complete; echo "${COMPREPLY[*]}"; }; '
        "t ./flow-steward us; t ./flow-steward users cr; t ./flow-steward users create --role ''"
    )
    result = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["users", "create", "admin operator viewer"]


def test_launcher_prints_a_script_without_docker(tmp_path: Path) -> None:
    environment = {**os.environ, "PATH": "/usr/bin:/bin"}
    printed = subprocess.run(
        ["sh", str(LAUNCHER), "completion", "zsh"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    refused = subprocess.run(
        ["sh", str(LAUNCHER), "completion", "powershell"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert printed.returncode == 0, printed.stderr
    assert printed.stdout == (COMPLETION_DIR / f"{PROGRAM}.zsh").read_text(encoding="utf-8")
    assert "preparing management runtime" not in printed.stderr
    assert refused.returncode == 2
    assert "completion bash|zsh|fish" in refused.stderr


def test_the_interactive_shell_defines_every_command() -> None:
    rc = (COMPLETION_DIR / "flow-steward-shell.bash").read_text(encoding="utf-8")

    assert "_FLOW_STEWARD_COMMANDS" in rc
    assert "FS_SHELL_PROGRAM" in rc
    assert "HISTFILE" in rc
