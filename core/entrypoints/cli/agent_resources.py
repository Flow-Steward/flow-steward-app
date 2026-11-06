"""Developer CLI for journal-owned temporary resources and automatic lifecycle cleanup."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from core.application.tooling.resource_cleanup.lifecycle import run_owned
from core.application.tooling.resource_cleanup.models import KINDS
from core.application.tooling.resource_cleanup.service import ResourceCleanup
from core.infrastructure.agent_resources.gateway import NativeResources
from core.infrastructure.agent_resources.journal import ResourceJournal


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--registry",
        required=True,
        type=Path,
        help="Retained primary checkout logs/resource-ownership/v1 directory",
    )
    result.add_argument("--owner", required=True, help="Exact owning task/chat ID")
    commands = result.add_subparsers(dest="action", required=True)
    commands.add_parser("init")
    directory = commands.add_parser("mkdir")
    directory.add_argument("name")
    track = commands.add_parser("track")
    track.add_argument("kind", choices=sorted(KINDS))
    track.add_argument("identity")
    track.add_argument("--borrowed", action="store_true")
    commands.add_parser("private-builder")
    release = commands.add_parser("release")
    release.add_argument("resource_id")
    release.add_argument("--consumer")
    retain = commands.add_parser("retain")
    retain.add_argument("resource_id")
    for field in ["consumer", "reason", "release-event", "review-at"]:
        retain.add_argument("--" + field, required=True)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Default is preview; only released resources can be removed",
    )
    run = commands.add_parser("run")
    run.add_argument("command", nargs=argparse.REMAINDER)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    service = ResourceCleanup(ResourceJournal(arguments.registry), NativeResources())
    owner = arguments.owner
    try:
        if arguments.action == "init":
            payload = service.initialize(owner).to_dict()
        elif arguments.action == "mkdir":
            payload = asdict(service.create_directory(owner, arguments.name))
        elif arguments.action == "track":
            payload = asdict(
                service.track(
                    owner, arguments.kind, arguments.identity, borrowed=arguments.borrowed
                )
            )
        elif arguments.action == "private-builder":
            payload = asdict(service.create_builder(owner))
        elif arguments.action == "release":
            payload = asdict(
                service.release(owner, arguments.resource_id, consumer=arguments.consumer)
            )
        elif arguments.action == "retain":
            service.retain(
                owner,
                arguments.resource_id,
                consumer=arguments.consumer,
                reason=arguments.reason,
                release_event=arguments.release_event,
                review_at=arguments.review_at,
            )
            payload = {"state": "retained"}
        elif arguments.action == "cleanup":
            receipt = service.cleanup(owner, apply=arguments.apply)
            print(json.dumps(asdict(receipt), indent=2))
            return 2 if receipt.blocked else 0
        else:
            command = arguments.command
            if command[:1] == ["--"]:
                command = command[1:]
            outcome = run_owned(service, owner, command)
            print(json.dumps(asdict(outcome), indent=2))
            return outcome.exit_code
        print(json.dumps(payload, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
