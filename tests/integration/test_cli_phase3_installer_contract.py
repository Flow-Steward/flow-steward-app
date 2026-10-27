from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import click

from core.application.installer import InstallConfig
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.entrypoints.cli import installer

ROOT = Path(__file__).resolve().parents[2]


def test_phase3_has_one_private_no_subcommand_installer_controller() -> None:
    assert isinstance(installer.installer_command, click.Command)
    assert not isinstance(installer.installer_command, click.Group)
    assert installer.installer_command.name == "install.sh"
    assert importlib.util.find_spec("core.entrypoints.cli.ops.stack_installer") is None


def test_install_script_rejects_post_install_management_domains() -> None:
    for domain in ("backup", "config", "environment", "release", "stack"):
        completed = subprocess.run(
            ["bash", str(ROOT / "install.sh"), domain],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "FS_INSTALLER_USE_HOST_PYTHON": "1",
                "PYTHON_BIN": sys.executable,
            },
        )

        assert completed.returncode == 2, domain
        assert "./flow-steward for management" in completed.stderr, domain


def test_phase3_public_tree_adds_only_core_readiness_and_bootstrap_token() -> None:
    phase3_paths = {
        spec.path
        for spec in PUBLIC_COMMAND_SPECS
        if spec.path in {("status",), ("doctor",), ("users", "issue-bootstrap-token")}
    }

    assert phase3_paths == {
        ("status",),
        ("doctor",),
        ("users", "issue-bootstrap-token"),
    }


def test_installer_contract_requires_no_ai_or_extension_input() -> None:
    fields = set(InstallConfig.model_fields)

    assert not fields.intersection(
        {
            "openai_api_key",
            "anthropic_api_key",
            "embedding_provider",
            "extension_id",
        }
    )
