"""Attach ownership to the canonical local quality gate without changing its source files."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from core.application.tooling.resource_cleanup.models import (
    DISPOSABLE_LABEL,
    OWNER_LABEL,
    TOKEN_LABEL,
    Ledger,
)
from core.infrastructure.agent_resources.journal import write_text


def prepare(
    ledger: Ledger,
    command: list[str],
    environment: dict[str, str],
    scratch: str,
    cache: str,
    *,
    select_image: Callable[[Path], str | None] | None = None,
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
        overlay = Path(scratch) / "owned-quality.yaml"
        if any(character.isspace() for character in str(base) + str(overlay)):
            raise ValueError(
                "canonical Compose file selection does not support whitespace in paths"
            )
        borrowed_image = select_image(script) if select_image else None
        tools = Path(cache) / "tools"
        tools.mkdir(mode=0o777)
        tools.chmod(0o777)  # nosec B103 -- only beneath the verified 0700 task cache (ADR 0060).
        labels = {OWNER_LABEL: ledger.owner, TOKEN_LABEL: ledger.token, DISPOSABLE_LABEL: "true"}
        audit_service = {
            "labels": labels,
            "volumes": [{"type": "bind", "source": str(tools), "target": "/app/logs/tool-cache"}],
        }
        if borrowed_image:
            audit_service.update(image=borrowed_image, pull_policy="never", build=None)
        else:
            audit_service["build"] = {"labels": labels}
        payload = {
            "services": {
                "fs-audit": audit_service,
                "pg-audit": {"labels": labels},
                "rabbitmq-audit": {"labels": labels},
            },
            "networks": {"default": {"labels": labels}},
        }
        # JSON is YAML-compatible; only our controlled build-null field needs a
        # Compose reset tag. Avoid requiring a YAML package on the Docker host.
        serialized = json.dumps(payload, indent=2)
        if borrowed_image:
            serialized = serialized.replace('"build": null', '"build": !reset null', 1)
        write_text(overlay, serialized + "\n")
        # quality-cache is intentionally not labelled or copied. All build exports live
        # in the marked cache directory, rather than each source copy's .flow-steward.
        environment = {k: v for k, v in environment.items() if k != "FS_BORROWED_AUDIT_IMAGE"}
        if borrowed_image:
            environment["FS_BORROWED_AUDIT_IMAGE"] = borrowed_image
            environment.pop("BUILDX_BUILDER", None)
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
