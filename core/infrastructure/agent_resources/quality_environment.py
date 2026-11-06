"""Attach ownership to the canonical local quality gate without changing its source files."""

from __future__ import annotations

from pathlib import Path

from core.application.tooling.resource_cleanup.models import (
    DISPOSABLE_LABEL,
    OWNER_LABEL,
    TOKEN_LABEL,
    Ledger,
)
from core.infrastructure.agent_resources.journal import write_json


def prepare(
    ledger: Ledger, command: list[str], environment: dict[str, str], scratch: str, cache: str
) -> dict[str, str]:
    for argument in command:
        script = Path(argument).absolute()
        if tuple(script.parts[-5:]) != ("core", "entrypoints", "cli", "scripts", "lint.sh"):
            continue
        if not script.is_file():
            raise ValueError("canonical quality script does not exist")
        if (
            any(arg.startswith("--sonar") or arg == "--with-sonar" for arg in command)
            or environment.get("SONAR_ENABLED") == "1"
        ):
            raise ValueError(
                "shared SonarQube must be coordinated separately from managed quality cleanup"
            )
        checkout = script.parents[4]
        base = checkout / "infra/docker/docker-compose.yaml"
        if not base.is_file() or not (checkout / ".env").is_file():
            raise ValueError(
                "quality checkout needs its Compose file and local .env before managed execution"
            )
        overlay = Path(scratch) / "owned-quality.json"
        if any(character.isspace() for character in str(base) + str(overlay)):
            raise ValueError(
                "canonical Compose file selection does not support whitespace in paths"
            )
        tools = Path(cache) / "tools"
        tools.mkdir(mode=0o777)
        tools.chmod(0o777)  # nosec B103 -- only beneath the verified 0700 task cache (ADR 0060).
        labels = {OWNER_LABEL: ledger.owner, TOKEN_LABEL: ledger.token, DISPOSABLE_LABEL: "true"}
        write_json(
            overlay,
            {
                "services": {
                    "fs-audit": {
                        "labels": labels,
                        "build": {"labels": labels},
                        "volumes": [
                            {"type": "bind", "source": str(tools), "target": "/app/logs/tool-cache"}
                        ],
                    },
                    "pg-audit": {"labels": labels},
                    "rabbitmq-audit": {"labels": labels},
                },
                "networks": {"default": {"labels": labels}},
            },
        )
        # quality-cache is intentionally not labelled or copied. All build exports live
        # in the marked cache directory, rather than each source copy's .flow-steward.
        return {
            **environment,
            "FS_OWNED_QUALITY": ledger.token,
            "FS_STACK_ENV_FILE": "/dev/null",
            "FS_STACK_ENVIRONMENT": "development",
            "ENVIRONMENT": "development",
            "FS_COMPOSE_PROJECT_NAME": "fs-owned-" + ledger.token,
            "FS_COMPOSE_FILES": str(base) + " " + str(overlay),
            "FS_INSTALLER_STATE_DIR": str(Path(cache) / "installer-state"),
        }
    return environment
