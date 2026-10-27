"""Private controller of `./flow-steward doctor`, run in ./install.sh's management runtime.

One implementation for both profiles (the doctor task). A plain `doctor` only reads:
it takes no lock, writes nothing to the installation (its log goes to a temporary
file), and offers no repair while an operation runs. `doctor --fix [ids]` owns the
installation the way ./install.sh does, diagnoses afresh, shows the plan, and after
confirmation repairs what is safe; the summary is a new diagnosis. The configuration
checks the launcher ran before (the earlier doctor) arrive as a report and are shown as
their own section.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.application.doctor.checks import p1_checks
from core.application.doctor.contracts import DoctorFinding, DoctorReport, FixResult
from core.application.doctor.hints import (
    hint_checks,
)
from core.application.doctor.repairs import repair_checks
from core.application.doctor.service import DoctorService, UnknownCheckError
from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.infrastructure.config.installer_config import installer_state_root
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.installer import host_diagnostics
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.installation_doctor import (
    InstallationDoctorProbe,
    TemporaryDoctorLog,
    _env_value,
)
from core.infrastructure.installer.state_store import InstallerStateStore

_ROOT = Path(
    str(runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or Path(__file__).resolve().parents[3])
)
_EXIT_CLEAN, _EXIT_USAGE, _EXIT_FINDINGS, _EXIT_BUSY = 0, 2, 4, 6
_EXIT_NOT_CHECKED = 5  # D4: nothing could be checked here
_DOCTOR_SCHEMA = "flow-steward.doctor.v1"
# D4: the Compact image run by a platform (Railway) -- built without the Docker CLI, and
# with no ./install.sh behind it. Every check reads, every repair changes, through Docker.
_NO_DOCKER = (
    "Docker is not available here: this is the Compact container itself, as a platform "
    "such as Railway runs it"
)
_NO_DOCKER_NEXT = (
    "Run ./flow-steward doctor on the Docker host of this installation; on a platform, "
    "the service's logs and restart are the platform's."
)
_BUSY_CODES = frozenset({"installer_operation_active", "lifecycle_operation_active"})


# -- the configuration section: the earlier doctor's report, run by the launcher --------


def _configuration_section() -> dict[str, Any] | None:
    raw = str(runtime_env.get("FS_DOCTOR_CONFIGURATION_REPORT") or "").strip()
    if not raw:
        return None
    try:
        report = json.loads(raw)
    except ValueError:
        return {"error": "The configuration checks returned a report that is not JSON."}
    return report if isinstance(report, dict) else None


def _configuration_findings(section: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not section:
        return []
    details = (
        (section.get("error") or {}).get("details")
        if isinstance(section.get("error"), dict)
        else None
    )
    details = details or section.get("data") or section.get("details") or {}
    findings = details.get("findings") if isinstance(details, dict) else None
    return [
        finding
        for finding in findings or ()
        if isinstance(finding, dict) and str(finding.get("status")) in {"failed", "warning"}
    ]


# -- output --------------------------------------------------------------------------


def _finding_json(finding: DoctorFinding) -> dict[str, Any]:
    return {
        "check_id": finding.check_id,
        "status": finding.status,
        "cause": finding.cause,
        "next_step": finding.next_step,
        "repairable": finding.repairable,
    }


def _problems(report: DoctorReport, configuration: list[dict[str, Any]]) -> list[str]:
    return [finding.check_id for finding in report.failing] + [
        str(item.get("check_id") or "configuration") for item in configuration
    ]


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _render_text(
    report: DoctorReport,
    configuration: list[dict[str, Any]],
    section: dict[str, Any] | None,
    *,
    running: str,
    profile: str,
) -> None:
    out = sys.stdout
    out.write(f"Flow Steward doctor -- {profile}\n")
    if running:
        out.write(
            f"An operation is running: {running}. What follows may be its work in progress;"
            " no fixes are offered until it has finished.\n"
        )
    passed = [finding for finding in report.findings if finding.status == "ok"]
    shown = [finding for finding in report.findings if finding.status != "ok"]
    out.write(f"\nInstallation: {len(passed)} of {len(report.findings)} checks are fine.\n")
    for finding in shown:  # failures, warnings, and what could not be checked
        out.write(f"  {finding.status.upper():5} {finding.check_id}: {finding.cause}\n")
        if finding.next_step:
            out.write(f"        -> {finding.next_step}\n")
    _render_configuration(configuration, section)
    problems = _problems(report, configuration)
    repairable = [f.check_id for f in report.failing if f.repairable and not running]
    if not problems:
        out.write("\nNo problems found.\n")
        return
    out.write(f"\n{_count(len(problems), 'problem')}: {', '.join(problems)}.")
    if repairable:
        out.write(f" Fix with ./flow-steward doctor --fix {' '.join(repairable)}")
    out.write("\n")


def _render_configuration(
    configuration: list[dict[str, Any]], section: dict[str, Any] | None
) -> None:
    out = sys.stdout
    if section is None:
        out.write("\nConfiguration: not checked in this run.\n")
        return
    out.write(f"\nConfiguration: {_count(len(configuration), 'problem')}.\n")
    for item in configuration:
        out.write(
            f"  {str(item.get('status')).upper():5} {item.get('title')}: {item.get('detail')}\n"
        )
        if item.get("remediation"):
            out.write(f"        -> {item.get('remediation')}\n")


def _render_fix_text(result: FixResult) -> None:
    out = sys.stdout
    if not result.plan:
        out.write("Nothing doctor --fix can repair was found.\n")
    elif not result.confirmed:
        out.write("Nothing was changed.\n")
    for outcome in result.outcomes:
        out.write(f"  {outcome.kind:14} {outcome.check_id}: {outcome.detail}\n")
    if result.final is not None:
        left = result.final.failing
        out.write(
            ("\nAfter the repairs: " if result.outcomes else "\nStill to do: ")
            + (
                "no problems.\n"
                if not left
                else f"{len(left)} left: {', '.join(f.check_id for f in left)}.\n"
            )
        )
        for finding in left:
            out.write(f"  {finding.status.upper():5} {finding.check_id}: {finding.cause}\n")
            if finding.next_step:
                out.write(f"        -> {finding.next_step}\n")


def _ask(plan: tuple[DoctorFinding, ...], *, confirmed: bool, as_json: bool) -> bool:
    if not as_json:
        sys.stdout.write("doctor --fix will repair:\n")
        for finding in plan:
            sys.stdout.write(f"  {finding.check_id}: {finding.cause}\n")
    if confirmed:
        return True
    if not sys.stdin.isatty():
        if not as_json:
            sys.stdout.write("Rerun with --confirm to repair without a question.\n")
        return False
    sys.stdout.write("Repair these now? [y/N] ")
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow-steward doctor", add_help=True)
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    for ignored in ("--plain", "--tui", "--no-color", "--verbose", "--non-interactive"):
        parser.add_argument(ignored, action="store_true")
    parser.add_argument("ids", nargs="*")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "doctor" in arguments:  # the launcher passes its command line through
        arguments.remove("doctor")
    args = _parser().parse_args(arguments)
    if args.ids and not args.fix:
        sys.stderr.write("Check ids are for --fix: ./flow-steward doctor --fix <ids>\n")
        return _EXIT_USAGE
    if _docker_unavailable_here():
        return _without_docker(args)
    state = InstallerStateStore(
        installer_state_root(_ROOT), lifecycle_state_root=_ROOT / ".flow-steward"
    )
    journal = state.load_journal() if state.journal_path.exists() else None
    if journal is None:
        sys.stderr.write("Flow Steward is not installed here. Run ./install.sh.\n")
        return _EXIT_FINDINGS
    gateway = _installation_gateway(state, journal.project_identity)
    if gateway is None:
        sys.stderr.write("This installation records no Compose project; run ./install.sh.\n")
        return _EXIT_FINDINGS
    config = gateway.installed_config(journal.config)
    service = DoctorService(
        (*p1_checks(), *hint_checks(), *repair_checks()), profile=config.deployment_profile
    )
    if not args.fix:
        probe = InstallationDoctorProbe(
            root=_ROOT, gateway=gateway, state=state, config=config, log=TemporaryDoctorLog()
        )
        return _diagnose(service, probe, as_json=args.json)
    return _fix(service, gateway, state, config, args)


def _docker_unavailable_here() -> bool:
    """D4: inside the Compact image, which is built without the Docker CLI."""
    profile = str(runtime_env.get("FS_DEPLOYMENT_PROFILE") or "").strip().lower()
    return profile == "compact" and not host_diagnostics.docker_cli_available()


def without_docker(argv: Sequence[str]) -> int:
    """The provider console's doctor (`platform_runtime doctor`), where there is never
    Docker: the configuration section it was handed, and D4 for the installation."""
    args = _parser().parse_args(list(argv))
    if args.ids and not args.fix:
        sys.stderr.write("Check ids are for --fix: platform_runtime doctor --fix <ids>\n")
        return _EXIT_USAGE
    return _without_docker(args)


def _without_docker(args: argparse.Namespace) -> int:
    """Name each check and repair doctor cannot run here, and why; read and change
    nothing. Not "not installed" (nothing is installed here by ./install.sh), and not
    "no problems" (nothing was looked at)."""
    section = _configuration_section()
    configuration = _configuration_findings(section)
    problems = [str(item.get("check_id") or "configuration") for item in configuration]
    code = _EXIT_FINDINGS if problems else _EXIT_NOT_CHECKED
    service = DoctorService((*p1_checks(), *hint_checks(), *repair_checks()), profile="compact")
    try:
        repairs = service.repairs(args.ids) if args.fix else ()
    except UnknownCheckError as exc:
        sys.stderr.write(f"{exc}\n")
        return _EXIT_USAGE
    checks = (
        [
            (c.check_id, f"{c.title} -- it reads the installation through Docker.")
            for c in service.checks
        ]
        if not args.fix
        else []
    )
    unavailable = [
        (c.check_id, f"{c.title} -- it changes the installation through Docker.") for c in repairs
    ]
    if args.json:
        json.dump(
            {
                "schema": _DOCTOR_SCHEMA,
                "profile": "compact",
                "mode": "no_docker",
                "reason": f"{_NO_DOCKER}.",
                "next_step": _NO_DOCKER_NEXT,
                "installation": [
                    {"check_id": check_id, "status": "not_checked", "cause": cause}
                    for check_id, cause in checks
                ],
                "unavailable_repairs": [
                    {"check_id": check_id, "cause": cause} for check_id, cause in unavailable
                ],
                "configuration": section,
                "problems": problems,
                "exit_code": code,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return code
    out = sys.stdout
    out.write(f"Flow Steward doctor -- compact\n{_NO_DOCKER}.\n\nInstallation:\n")
    for label, rows in (("NOT CHECKED", checks), ("UNAVAILABLE", unavailable)):
        for check_id, cause in rows:
            out.write(f"  {label} {check_id}: {cause}\n")
    out.write(f"  {'Nothing was changed.' if args.fix else 'Nothing was checked.'}\n")
    out.write(f"  -> {_NO_DOCKER_NEXT}\n")
    if not args.fix:
        _render_configuration(configuration, section)
    if problems:
        out.write(f"\n{_count(len(problems), 'problem')}: {', '.join(problems)}.\n")
    return code


def _installation_gateway(
    state: InstallerStateStore, project_identity: str
) -> DockerInstallerGateway | None:
    """The gateway of this installation's own Compose project, as its journal records it
    -- never the default name, which is another installation's on a shared host (found
    live: without it doctor read the owner's "flow-steward" stand)."""
    project = project_identity or _env_value(state.root / "compose.env", "FS_COMPOSE_PROJECT_NAME")
    if not project:
        return None
    return DockerInstallerGateway(
        repo_root=_ROOT,
        state_store=state,
        environment={**runtime_env, "FS_COMPOSE_PROJECT_NAME": project},
    )


def _diagnose(service: DoctorService, probe: InstallationDoctorProbe, *, as_json: bool) -> int:
    report = service.diagnose(probe)
    try:
        running = probe.operation_running()
    except Exception:
        running = "unknown (the lock could not be read)"
    section = _configuration_section()
    configuration = _configuration_findings(section)
    problems = _problems(report, configuration)
    code = _EXIT_FINDINGS if problems else _EXIT_CLEAN
    if as_json:
        json.dump(
            {
                "schema": _DOCTOR_SCHEMA,
                "profile": probe.profile,
                "operation_running": running,
                "installation": [_finding_json(finding) for finding in report.findings],
                "configuration": section,
                "problems": problems,
                "fix_command": (
                    "./flow-steward doctor --fix "
                    + " ".join(f.check_id for f in report.failing if f.repairable)
                )
                if any(f.repairable for f in report.failing) and not running
                else "",
                "exit_code": code,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        _render_text(report, configuration, section, running=running, profile=probe.profile)
    return code


def _fix(
    service: DoctorService,
    gateway: DockerInstallerGateway,
    state: InstallerStateStore,
    config: InstallConfig,
    args: argparse.Namespace,
) -> int:
    log = state.open_log(f"doctor-{uuid4().hex}")
    probe = InstallationDoctorProbe(
        root=_ROOT, gateway=gateway, state=state, config=config, log=log
    )
    try:
        with state.operation_lock():
            probe.holding = True
            result = service.fix(
                probe,
                only=args.ids,
                confirm=lambda plan: _ask(plan, confirmed=args.confirm, as_json=args.json),
            )
    except UnknownCheckError as exc:
        sys.stderr.write(f"{exc}\n")
        return _EXIT_USAGE
    except InstallerFailure as exc:
        # Taking the installation takes the lifecycle lock too: an install, another
        # doctor --fix, or a config/stack/maintenance/backup/database operation of either
        # profile holds one of them, and the failure names which.
        if exc.code not in _BUSY_CODES:
            raise
        sys.stderr.write(
            f"{exc.cause} Rerun ./flow-steward doctor --fix when it has finished. "
            "Nothing was changed.\n"
        )
        return _EXIT_BUSY
    finally:
        log.close()
    final = result.final
    code = _EXIT_FINDINGS if final is not None and final.failing else _EXIT_CLEAN
    if args.json:
        json.dump(
            {
                "schema": _DOCTOR_SCHEMA,
                "profile": config.deployment_profile,
                "plan": [_finding_json(finding) for finding in result.plan],
                "confirmed": result.confirmed,
                "outcomes": [
                    {"check_id": o.check_id, "kind": o.kind, "detail": o.detail}
                    for o in result.outcomes
                ],
                "final": [_finding_json(f) for f in (final.findings if final else ())],
                "log_path": str(log.path),
                "exit_code": code,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        _render_fix_text(result)
        sys.stdout.write(f"Log: {log.path}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
