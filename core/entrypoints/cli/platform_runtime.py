"""Restricted one-shot dispatcher for Compact platform maintenance operations."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import sys
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer import host_diagnostics

_OPERATIONS = (
    "bootstrap",
    "candidate-preflight",
    "status",
    "doctor",
    "proxy-fragment",
    "database-status",
    "database-upgrade",
    "database-restore-parity-capture",
    "database-restore-parity-verify",
    "maintenance-status",
    "maintenance-enable",
    "maintenance-disable",
    "release-info",
    "release-check",
    "release-upgrade",
    "backup-create",
    "backup-list",
    "backup-verify",
    "clone-create",
    "clone-restore",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flow Steward Compact platform operations")
    parser.add_argument("operation", choices=_OPERATIONS)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="")
    parser.add_argument("--author")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-version", default="")
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--passphrase-stdin", action="store_true")
    # doctor only: D4 names each repair doctor --fix could not make here, and why
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("ids", nargs="*")
    return parser


def _canonical_argv(args: argparse.Namespace) -> list[str]:
    mapping = {
        "status": ["status"],
        "doctor": ["doctor"],
        "proxy-fragment": ["proxy-fragment"],
        "database-status": ["database", "status"],
        "maintenance-status": ["maintenance", "status"],
        "maintenance-enable": ["maintenance", "enable"],
        "maintenance-disable": ["maintenance", "disable"],
        "backup-create": ["backup", "create"],
        "backup-list": ["backup", "list"],
        "release-info": ["release", "info"],
        "release-check": ["release", "check"],
    }
    specified = {
        "backup": args.backup is not None,
        "archive": args.archive is not None,
        "baseline": args.baseline is not None,
        "output": args.output is not None,
        "label": bool(args.label),
        "author": args.author is not None,
        "maintenance_confirmed": args.maintenance_confirmed,
        "confirm": args.confirm,
        "confirmation": bool(args.confirmation),
        "operation_id": bool(args.operation_id),
        "dry_run": args.dry_run,
        "target_version": bool(args.target_version),
        "timeout_seconds": args.timeout_seconds is not None,
        "passphrase_stdin": args.passphrase_stdin,
    }
    allowed = {
        "database-upgrade": {"author", "maintenance_confirmed", "confirm"},
        "maintenance-enable": {"confirm"},
        "maintenance-disable": {"confirm"},
        "database-restore-parity-capture": {"output", "label", "confirm"},
        "database-restore-parity-verify": {"baseline", "output", "label", "confirm"},
        "backup-verify": {"backup"},
        "release-upgrade": {"target_version", "timeout_seconds", "dry_run", "confirm"},
        "clone-create": {"output", "passphrase_stdin", "confirm"},
        "clone-restore": {
            "archive",
            "passphrase_stdin",
            "confirmation",
            "operation_id",
        },
    }.get(args.operation, set())
    irrelevant = sorted(
        name for name, present in specified.items() if present and name not in allowed
    )
    if irrelevant:
        rendered = ", ".join("--" + name.replace("_", "-") for name in irrelevant)
        raise ValueError(f"{args.operation} does not accept {rendered}")
    if args.operation == "database-upgrade":
        if not args.maintenance_confirmed or not args.confirm:
            raise ValueError("database-upgrade requires --maintenance-confirmed and --confirm")
        command = [
            "database",
            "upgrade",
            "--author",
            args.author or "platform-operator",
            "--maintenance-confirmed",
            "--confirm",
        ]
    elif args.operation in {"maintenance-enable", "maintenance-disable"}:
        if not args.confirm:
            raise ValueError(f"{args.operation} requires --confirm")
        command = [*mapping[args.operation], "--confirm"]
    elif args.operation == "database-restore-parity-capture":
        if args.output is None or not args.confirm:
            raise ValueError("database-restore-parity-capture requires --output and --confirm")
        command = [
            "database",
            "restore-parity-capture",
            "--label",
            args.label or "baseline",
            "--output",
            str(args.output),
            "--confirm",
        ]
    elif args.operation == "database-restore-parity-verify":
        if args.baseline is None or not args.confirm:
            raise ValueError("database-restore-parity-verify requires --baseline and --confirm")
        command = [
            "database",
            "restore-parity-verify",
            "--label",
            args.label or "restored",
            "--baseline",
            str(args.baseline),
        ]
        if args.output is not None:
            command.extend(("--output", str(args.output)))
        command.append("--confirm")
    elif args.operation == "backup-verify":
        if args.backup is None:
            raise ValueError("backup-verify requires --backup")
        command = ["backup", "verify", str(args.backup)]
    elif args.operation == "release-upgrade":
        command = [
            "release",
            "upgrade",
            "--timeout-seconds",
            str(args.timeout_seconds or 60),
        ]
        if args.target_version:
            command.extend(("--target-version", args.target_version))
        if args.dry_run:
            command.append("--dry-run")
        if args.confirm:
            command.append("--confirm")
    elif args.operation == "clone-create":
        command = ["clone", "create"]
        if args.output is not None:
            command.extend(("--output-directory", str(args.output)))
        if args.passphrase_stdin:
            command.append("--passphrase-stdin")
        if args.confirm:
            command.append("--confirm")
    elif args.operation == "clone-restore":
        if args.archive is None:
            raise ValueError("clone-restore requires --archive")
        command = ["clone", "restore", str(args.archive)]
        if args.passphrase_stdin:
            command.append("--passphrase-stdin")
        if args.confirmation:
            command.extend(("--confirm", args.confirmation))
        if args.operation_id:
            command.extend(("--operation-id", args.operation_id))
    else:
        command = mapping[args.operation]
    if args.json:
        command.append("--json")
    if args.plain:
        command.append("--plain")
    if args.tui:
        command.append("--tui")
    if args.no_color:
        command.append("--no-color")
    if args.non_interactive:
        command.append("--non-interactive")
    if args.verbose:
        command.append("--verbose")
    return command


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    operation = args.operation
    if (args.fix or args.ids) and operation != "doctor":
        _parser().error("--fix and check ids are for doctor")
    if operation == "candidate-preflight":
        from core.application.lifecycle.stack_service import StackGatewayError
        from core.entrypoints.cli.lifecycle_runtime import LifecycleCLIRuntime

        runtime = LifecycleCLIRuntime()
        config = runtime.configuration_service.read_active(required=False)
        if config is None:
            print("Installed configuration is unavailable.", file=sys.stderr)
            return 2
        try:
            target = runtime.configuration_service.derive_target(config)
            output = runtime.docker_lifecycle_gateway.compact_candidate_preflight(target)
        except (StackGatewayError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(output, end="")
        return 0
    if operation == "bootstrap":
        from core.entrypoints.cli.compact_runtime import _bootstrap
        from core.infrastructure.config.runtime_env import runtime_env

        _bootstrap(Path(str(runtime_env.get("FS_COMPACT_DATA_ROOT") or "/data")))
        return 0

    root_variable = "FS_DOCKER_HOST_PROJECT_ROOT"
    inherited_root = _runtime_env.get(root_variable)
    if inherited_root is None:
        _runtime_env[root_variable] = "/data/state"
    try:
        from click.exceptions import Exit

        from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime

        click_module = importlib.import_module("core.entrypoints.cli.lifecycle_click_app")
        click_module._runtime._runtime = CompactPlatformRuntime()

        if operation == "doctor":
            return _doctor(click_module, args)

        try:
            result = click_module.click_app.main(
                args=_canonical_argv(args),
                prog_name="flow-steward-platform",
                standalone_mode=False,
            )
        except Exit as exc:
            return int(exc.exit_code)
        except ValueError as exc:
            _parser().error(str(exc))
        return int(result) if isinstance(result, int) else 0
    finally:
        if inherited_root is None:
            _runtime_env.pop(root_variable, None)
        else:
            _runtime_env[root_variable] = inherited_root


def _doctor(click_module, args: argparse.Namespace) -> int:
    """Use host management tooling when present; embedded runtimes report its absence."""
    from click.exceptions import Exit

    from core.infrastructure.config.runtime_env import runtime_env

    captured = io.StringIO()
    # Its findings are in the report; the exit code is the installation doctor's to give.
    with contextlib.redirect_stdout(captured):
        try:
            result = click_module.click_app.main(
                args=["doctor", "--json"], prog_name="flow-steward-platform", standalone_mode=False
            )
        except Exit as exc:
            result = exc.exit_code
    if runtime_env.get("FS_DOCTOR_SECTION") == "configuration":
        # The launcher owns the outer installation diagnosis; this inner run
        # contributes only the canonical configuration report and its exit code.
        print(captured.getvalue(), end="")
        return int(result) if isinstance(result, int) else 0

    from core.entrypoints.cli import doctor as installation_doctor

    runtime_env["FS_DOCTOR_CONFIGURATION_REPORT"] = captured.getvalue().strip()
    doctor = (
        installation_doctor.main
        if host_diagnostics.docker_cli_available()
        else installation_doctor.without_docker
    )
    return doctor([*(["--json"] if args.json else []), *(["--fix", *args.ids] if args.fix else [])])


if __name__ == "__main__":
    raise SystemExit(main())
