"""The installation checks of `./flow-steward doctor`, phase P1 (the doctor task).

Each check reads the state through a DoctorProbe -- plain data, fetched fresh on every
call -- and names what is wrong and the command that fixes it. A repair is offered only
where it is safe and idempotent; the rest are hints. The probe is the Docker side
(core/entrypoints/cli/doctor.py wires it to the installer gateway).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.application.doctor.contracts import DoctorCheck, DoctorFinding

_RERUN = "./flow-steward doctor --fix {check_id}"


@dataclass(frozen=True)
class InterruptedOperation:
    operation_id: str
    kind: str
    status: str
    step: str
    recovery_command: str
    continuable: bool  # doctor --fix can continue it the way the installer would


@dataclass(frozen=True)
class MaintenanceFacts:
    phase: str
    owner_kind: str
    owner_id: str
    since: str


class DoctorProbe(Protocol):
    """What the checks read and do; every read is of the state as it is now."""

    profile: str

    def operation_running(self) -> str: ...  # "" when none, else what runs

    def stopped_services(self) -> list[str]: ...

    def start_stopped_services(self) -> str: ...

    # "" when these sources built the installation, else the two revisions in words
    def sources_differ_from_installation(self) -> str: ...

    def missing_images(self) -> list[tuple[str, str]]: ...  # recorded, not on this host

    # (container id, its name, a stopped replaced container still holding it)
    def temporary_names(self) -> list[tuple[str, str, str]]: ...

    def interrupted_operations(self) -> list[InterruptedOperation]: ...

    def continue_operation(self, operation: InterruptedOperation) -> str: ...

    def maintenance(self) -> MaintenanceFacts | None: ...

    def disable_maintenance(self) -> str: ...

    def shared_tree_problems(self) -> list[str] | None: ...  # None: no shared model here

    def normalize_shared_trees(self) -> str: ...

    def wide_secrets(self) -> list[str]: ...

    def narrow_secrets(self) -> str: ...

    def stale_graphs(self) -> list[str]: ...

    def repair_graph_catalog(self) -> str: ...


def _fail(check_id: str, cause: str, next_step: str = "", *, repairable: bool = True):
    return DoctorFinding(
        check_id,
        "fail",
        cause=cause,
        next_step=next_step or _RERUN.format(check_id=check_id),
        repairable=repairable,
    )


def _stack_stopped(probe: DoctorProbe) -> DoctorFinding:
    stopped = probe.stopped_services()
    renames = probe.temporary_names()
    if not stopped:
        return DoctorFinding("stack_stopped", "ok", cause=_running_words(renames))
    blocked = _start_blocked(probe, stopped, renames)
    if blocked is not None:
        return blocked
    return _fail(
        "stack_stopped",
        "Not running: " + ", ".join(stopped) + ".",
        _RERUN.format(check_id="stack_stopped") + " (or ./install.sh)",
    )


def _running_words(renames: list[tuple[str, str, str]]) -> str:
    if not renames:
        return "Every service is running."
    names = ", ".join(name for _container_id, name, _replaced in renames)
    return f"Every service is running ({names} runs under a temporary name: see container_names)."


def _start_blocked(
    probe: DoctorProbe, stopped: list[str], renames: list[tuple[str, str, str]]
) -> DoctorFinding | None:
    """What a plain start cannot put right, in the order it has to be put right."""
    down = "Not running: " + ", ".join(stopped)
    missing = probe.missing_images()
    if missing:
        # A start cannot create a container without its image (live, AC17-R177); what
        # brings each one back -- a rebuild or its owner -- recorded_images_present says.
        names = ", ".join(name for name, _value in missing)
        verb = "image %s is" if len(missing) == 1 else "images %s are"
        return _fail(
            "stack_stopped",
            f"{down}; the {verb % names} not on this host.",
            "Bring back the images as recorded_images_present says, then rerun "
            "./flow-steward doctor.",
            repairable=False,
        )
    interrupted = probe.interrupted_operations()
    if interrupted:
        # F1 (live on fs-acc): an update killed after Compose recreate. A start cannot
        # finish it; what continues the operation also starts the stack.
        what = "an update" if interrupted[0].kind == "update" else "an operation"
        return _fail(
            "stack_stopped",
            f"{down}; {what} was interrupted, so doctor does not start it on its own.",
            interrupted[0].recovery_command
            + " (it finishes the interrupted operation, which also starts the stack)",
            repairable=False,
        )
    if renames:
        return _fail(
            "stack_stopped",
            f"{down}; a recreate was left half-way (see container_names).",
            _RERUN.format(check_id="container_names"),
            repairable=False,
        )
    moved = probe.sources_differ_from_installation()
    if moved:
        # Starting from other sources is an update: Compose recreates what these compose
        # files define differently, from images the installation may not have (live,
        # 2026-09-25). ./install.sh updates and then starts the stack.
        return _fail(
            "stack_stopped",
            f"{down}; {moved}, so doctor does not start it.",
            "./install.sh (it updates the installation to these sources, then starts it)",
            repairable=False,
        )
    return None


def _describe(op: InterruptedOperation) -> str:
    if op.kind == "update":
        return f"An update was interrupted: {op.step}."
    return (
        f"Unfinished, and nobody is running it: {op.kind.replace('_', ' ')} {op.operation_id}"
        f" stopped at {op.step or 'its start'} ({op.status})."
    )


def _lifecycle_interrupted(probe: DoctorProbe) -> DoctorFinding:
    left = probe.interrupted_operations()
    if not left:
        return DoctorFinding(
            "lifecycle_interrupted", "ok", cause="No lifecycle operation was left unfinished."
        )
    continuable = all(op.continuable for op in left)
    return _fail(
        "lifecycle_interrupted",
        " ".join(_describe(op) for op in left),
        _RERUN.format(check_id="lifecycle_interrupted")
        if continuable
        else " / ".join(dict.fromkeys(op.recovery_command for op in left)),
        repairable=continuable,
    )


def _continue_interrupted(probe: DoctorProbe, _finding: DoctorFinding) -> str:
    return " ".join(probe.continue_operation(op) for op in probe.interrupted_operations())


def _maintenance_left_enabled(probe: DoctorProbe) -> DoctorFinding:
    facts = probe.maintenance()
    if facts is None:
        return DoctorFinding("maintenance_left_enabled", "ok", cause="Maintenance mode is off.")
    running = probe.operation_running()
    if running:
        return DoctorFinding(
            "maintenance_left_enabled",
            "info",
            cause=f"Maintenance mode is on while an operation runs ({running}).",
        )
    return _fail(
        "maintenance_left_enabled",
        f"Maintenance mode has been on since {facts.since} ({facts.phase}, set by "
        f"{facts.owner_kind} {facts.owner_id}), and no operation is running: the "
        "installation refuses work.",
        _RERUN.format(check_id="maintenance_left_enabled")
        + " (or ./flow-steward maintenance disable)",
    )


def _shared_dirs_modes(probe: DoctorProbe) -> DoctorFinding:
    problems = probe.shared_tree_problems()
    if problems is None:
        return DoctorFinding(
            "shared_dirs_modes",
            "ok",
            cause="Not used here: this installation's trees belong to its own user.",
        )
    if not problems:
        return DoctorFinding(
            "shared_dirs_modes", "ok", cause="The shared trees have their groups and modes."
        )
    return _fail("shared_dirs_modes", "Not in the shared model: " + "; ".join(problems) + ".")


def _secrets_modes(probe: DoctorProbe) -> DoctorFinding:
    wide = probe.wide_secrets()
    if not wide:
        return DoctorFinding(
            "secrets_modes", "ok", cause="Secrets and state are readable by their owner only."
        )
    shown = "; ".join(wide[:5]) + (f"; and {len(wide) - 5} more" if len(wide) > 5 else "")
    return _fail("secrets_modes", f"Readable beyond their owner: {shown}.")


def _graph_catalog(probe: DoctorProbe) -> DoctorFinding:
    stale = probe.stale_graphs()
    if not stale:
        return DoctorFinding("graph_catalog", "ok", cause="Graph ids match their namespaces.")
    return _fail(
        "graph_catalog",
        "Graph ids no longer match their namespaces, so graph search is broken: "
        + ", ".join(stale)
        + ". A database restore did not finish its graph catalogue repair.",
    )


def p1_checks() -> tuple[DoctorCheck, ...]:
    return (
        DoctorCheck(
            "lifecycle_interrupted",
            "Unfinished lifecycle operations",
            _lifecycle_interrupted,
            _continue_interrupted,
        ),
        DoctorCheck(
            "stack_stopped",
            "Services running",
            _stack_stopped,
            lambda probe, _finding: probe.start_stopped_services(),
        ),
        DoctorCheck(
            "maintenance_left_enabled",
            "Maintenance mode",
            _maintenance_left_enabled,
            lambda probe, _finding: probe.disable_maintenance(),
        ),
        DoctorCheck(
            "shared_dirs_modes",
            "Shared tree groups and modes",
            _shared_dirs_modes,
            lambda probe, _finding: probe.normalize_shared_trees(),
        ),
        DoctorCheck(
            "secrets_modes",
            "Secret and state file modes",
            _secrets_modes,
            lambda probe, _finding: probe.narrow_secrets(),
        ),
        DoctorCheck(
            "graph_catalog",
            "Graph catalogue identity",
            _graph_catalog,
            lambda probe, _finding: probe.repair_graph_catalog(),
        ),
    )
