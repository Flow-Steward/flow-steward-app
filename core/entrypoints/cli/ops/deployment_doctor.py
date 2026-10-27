#!/usr/bin/env python3
"""CLI entrypoint for Flow Steward deployment doctor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.application.tooling.ops.deployment_doctor import (
    DEFAULT_COMPOSE_PROBE_SERVICE,
    DoctorOptions,
    run_doctor,
)
from core.entrypoints.cli.ops.deployment_doctor_output import render_human
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OSS deployment readiness checks.")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[4]),
        help="Repository root to inspect.",
    )
    parser.add_argument(
        "--env-file",
        default=".env.prod",
        help="Production environment file to validate.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Run only repository/env/compose static checks; skip live deployment probes.",
    )
    parser.add_argument(
        "--live-only",
        action="store_true",
        help="Run live deployment probes only, reading runtime config from process environment.",
    )
    parser.add_argument(
        "--probe-mode",
        choices=("compose", "host"),
        default="compose",
        help=(
            "Live probe boundary. 'compose' delegates internal probes to fs-app; "
            "'host' runs from the current process."
        ),
    )
    parser.add_argument(
        "--compose-service",
        default=DEFAULT_COMPOSE_PROBE_SERVICE,
        help="Compose service used for internal live probes.",
    )
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Compose file to validate. May be supplied more than once.",
    )
    parser.add_argument(
        "--compose-profile",
        action="append",
        default=[],
        help="Compose profile for selected optional services. May be supplied more than once.",
    )
    parser.add_argument(
        "--required-service",
        action="append",
        default=[],
        help="Compose service expected in the selected stack. May be supplied more than once.",
    )
    parser.add_argument(
        "--runtime-service",
        action="append",
        default=[],
        help="Runtime service expected to be running. May be supplied more than once.",
    )
    parser.add_argument(
        "--stack-environment",
        choices=("production", "local-development", "local_development"),
        default="",
        help="Validation mode for the selected stack.",
    )
    parser.add_argument(
        "--container-base-url",
        default="",
        help="Internal app URL used when delegating live probes into Compose.",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="App base URL for host-mode live probes.",
    )
    parser.add_argument(
        "--mcp-url",
        default="",
        help="MCP URL for current-process live probes. Defaults to MCP_URL.",
    )
    parser.add_argument(
        "--bearer-token",
        default="",
        help="Bearer token for MCP auth probe. Defaults to MCP_BEARER_TOKEN/FS_SESSION_TOKEN.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="Per-probe timeout for live checks.",
    )
    parser.add_argument(
        "--skip-provider-cli",
        action="store_true",
        help="Skip CLI-provider auth checks.",
    )
    parser.add_argument(
        "--run-compose-config",
        action="store_true",
        help="Run `docker compose config --quiet` when Docker Compose is available.",
    )
    parser.add_argument("--skip-git", action="store_true", help="Skip tracked OSS path guard.")
    parser.add_argument(
        "--allow-lifecycle-mutation",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--allow-release-transition",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    repo_root = Path(str(args.repo_root)).expanduser()
    payload = run_doctor(
        DoctorOptions(
            repo_root=repo_root,
            env_file=Path(str(args.env_file)).expanduser(),
            strict=bool(args.strict),
            run_compose_config=bool(args.run_compose_config),
            skip_git=bool(args.skip_git),
            static_only=bool(args.static_only),
            base_url=str(args.base_url or ""),
            mcp_url=str(args.mcp_url or ""),
            bearer_token=str(args.bearer_token or ""),
            timeout_seconds=float(args.timeout_seconds or 5.0),
            skip_provider_cli=bool(args.skip_provider_cli),
            live_only=bool(args.live_only),
            probe_mode=str(args.probe_mode or "compose"),
            compose_service=str(args.compose_service or DEFAULT_COMPOSE_PROBE_SERVICE),
            container_base_url=str(args.container_base_url or ""),
            compose_files=tuple(Path(str(item)).expanduser() for item in args.compose_file),
            compose_profiles=tuple(str(item) for item in args.compose_profile),
            required_compose_services=tuple(str(item) for item in args.required_service),
            runtime_compose_services=tuple(str(item) for item in args.runtime_service),
            stack_environment=str(
                args.stack_environment or _runtime_env.get("FS_STACK_ENVIRONMENT") or "production"
            ),
            allow_lifecycle_mutation=bool(args.allow_lifecycle_mutation),
            allow_release_transition=bool(args.allow_release_transition),
        )
    )
    if bool(args.json):
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))
    else:
        print(render_human(payload))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
