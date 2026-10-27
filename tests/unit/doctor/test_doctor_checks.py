"""The installation checks of `./flow-steward doctor`, each from both sides: the broken
state named with the command that fixes it, and the healthy one passing (the doctor
task: red first, then green). The probe is plain data here; its Docker side has its
own tests (test_doctor_probes.py)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.application.doctor import checks
from core.application.doctor.checks import InterruptedOperation, MaintenanceFacts
from core.application.doctor.contracts import DoctorFinding
from core.application.doctor.service import DoctorService


@dataclass
class _Probe:
    profile: str = "scalable"
    running: str = ""
    stopped: list[str] = field(default_factory=list)
    interrupted: list[InterruptedOperation] = field(default_factory=list)
    maintenance_facts: MaintenanceFacts | None = None
    tree_problems: list[str] | None = None
    wide: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    moved: str = ""
    images: list[tuple[str, str]] = field(default_factory=list)
    renames: list[tuple[str, str, str]] = field(default_factory=list)

    def operation_running(self) -> str:
        return self.running

    def stopped_services(self) -> list[str]:
        return self.stopped

    def sources_differ_from_installation(self) -> str:
        return self.moved

    def missing_images(self) -> list[tuple[str, str]]:
        return self.images

    def temporary_names(self) -> list[tuple[str, str, str]]:
        return self.renames

    def interrupted_operations(self) -> list[InterruptedOperation]:
        return self.interrupted

    def maintenance(self) -> MaintenanceFacts | None:
        return self.maintenance_facts

    def shared_tree_problems(self) -> list[str] | None:
        return self.tree_problems

    def wide_secrets(self) -> list[str]:
        return self.wide

    def stale_graphs(self) -> list[str]:
        return self.stale


def _diagnose(check_id: str, probe: _Probe) -> DoctorFinding:
    (check,) = [check for check in checks.p1_checks() if check.check_id == check_id]
    return check.diagnose(probe)


def _operation(*, continuable: bool) -> InterruptedOperation:
    return InterruptedOperation(
        operation_id="op-1",
        kind="config_apply",
        status="failed",
        step="apply_runtime",
        recovery_command="./flow-steward config apply --confirm",
        continuable=continuable,
    )


def test_every_check_id_is_unique_and_names_its_own_repair_command() -> None:
    ids = [check.check_id for check in checks.p1_checks()]
    assert len(ids) == len(set(ids)) == 6  # non-vacuity: the P1 set
    # F1 (live on fs-acc): an unfinished operation is what explains a stopped stack, so
    # it is reported, and fixed, before it.
    assert ids[:2] == ["lifecycle_interrupted", "stack_stopped"]


# Docker container IDs of the fs-acc recreate: hex, not secrets.
_REPLACEMENT_ID = "2d589e15b668"  # pragma: allowlist secret
_REPLACED_ID = "9c7b7a7eead0"  # pragma: allowlist secret
_TEMPORARY_ID = "5f3e2d1c0b9a"  # pragma: allowlist secret

_UPDATE = InterruptedOperation(
    operation_id="",
    kind="update",
    status="interrupted",
    step="compact-runtime is not on the image .env.prod records",
    recovery_command="./install.sh",
    continuable=False,
)


def test_a_stopped_stack_an_interrupted_update_left_is_finished_by_the_update() -> None:
    """Live on fs-acc (F1): an update killed after Compose recreate; stack_stopped offered
    --fix, and the start could not bring the runtime back."""
    finding = _diagnose("stack_stopped", _Probe(stopped=["compact-runtime"], interrupted=[_UPDATE]))

    assert not finding.repairable
    assert finding.next_step.startswith("./install.sh")
    assert "an update was interrupted" in finding.cause


def test_a_stopped_stack_with_a_recreate_left_half_way_sends_to_container_names() -> None:
    renames = [(_REPLACEMENT_ID, "fs-acc-compact-runtime-1", _REPLACED_ID)]

    finding = _diagnose("stack_stopped", _Probe(stopped=["compact-runtime"], renames=renames))

    assert not finding.repairable
    assert finding.next_step == "./flow-steward doctor --fix container_names"


def test_an_interrupted_update_is_named_with_the_command_that_finishes_it() -> None:
    finding = _diagnose("lifecycle_interrupted", _Probe(interrupted=[_UPDATE]))

    assert finding.status == "fail"
    assert not finding.repairable
    assert "An update was interrupted: compact-runtime is not on the image" in finding.cause
    assert finding.next_step == "./install.sh"


def test_a_running_service_under_a_temporary_name_is_said_by_stack_stopped() -> None:
    renames = [(_TEMPORARY_ID, "fs-acc-compact-runtime-1", "")]

    finding = _diagnose("stack_stopped", _Probe(renames=renames))

    assert finding.status == "ok"  # it runs; container_names is the problem to fix
    assert "fs-acc-compact-runtime-1 runs under a temporary name" in finding.cause


def test_stopped_services_are_named_with_the_fix() -> None:
    finding = _diagnose("stack_stopped", _Probe(stopped=["fs-app", "fs-worker"]))

    assert finding.status == "fail"
    assert finding.repairable
    assert "fs-app, fs-worker" in finding.cause
    assert finding.next_step.startswith("./flow-steward doctor --fix stack_stopped")


@pytest.mark.parametrize("profile", ["compact", "scalable"])
def test_fix_refuses_a_stack_blocked_after_confirmation(profile: str) -> None:
    class Probe(_Probe):
        def start_stopped_services(self) -> str:
            self.stopped.clear()
            return "started"

    probe = Probe(profile=profile, stopped=["pg-fs"])
    check = next(check for check in checks.p1_checks() if check.check_id == "stack_stopped")
    service = DoctorService([check], profile=profile)

    def confirm(_plan) -> bool:
        probe.interrupted.append(_UPDATE)
        return True

    result = service.fix(probe, confirm=confirm)

    assert probe.stopped == ["pg-fs"]
    assert result.outcomes[0].kind == "refused"
    assert "an update was interrupted" in result.outcomes[0].detail
    assert result.final is not None
    assert not result.final.clean
    assert result.final.findings[0].next_step.startswith("./install.sh")


def test_a_stopped_stack_under_other_sources_is_started_by_the_update_not_by_doctor() -> None:
    """Live (2026-09-25): sources newer than the installation, and --fix started the
    stack through the update's start with the new compose files -- Compose recreated
    the containers from images the installation does not have. Starting from these
    sources is an update, and ./install.sh is what does one."""
    finding = _diagnose(
        "stack_stopped",
        _Probe(
            stopped=["compact-runtime"], moved="these sources are at aaaa, the installation at bbbb"
        ),
    )

    assert finding.status == "fail"
    assert not finding.repairable
    assert "these sources are at aaaa, the installation at bbbb" in finding.cause
    assert finding.next_step.startswith("./install.sh")


def test_a_stopped_stack_whose_images_are_gone_sends_to_the_images_first() -> None:
    """Live on fs-ac19-bench (2026-09-25): stack_stopped advised ./install.sh, which
    fails on the pruned image recorded_images_present names in the same report."""
    finding = _diagnose(
        "stack_stopped",
        _Probe(
            stopped=["pg-fs"],
            moved="these sources are at aaaa, the installation at bbbb",
            images=[("POSTGRES_IMAGE", "sha256:" + "9" * 64)],
        ),
    )

    assert not finding.repairable
    assert finding.next_step.startswith("Bring back the images as recorded_images_present says")
    assert "POSTGRES_IMAGE" in finding.cause


def test_a_running_stack_passes() -> None:
    assert _diagnose("stack_stopped", _Probe()).status == "ok"


def test_an_interrupted_operation_doctor_can_continue_is_offered_as_a_fix() -> None:
    finding = _diagnose("lifecycle_interrupted", _Probe(interrupted=[_operation(continuable=True)]))

    assert finding.status == "fail"
    assert finding.repairable
    assert "config apply op-1 stopped at apply_runtime (failed)" in finding.cause
    assert finding.next_step == "./flow-steward doctor --fix lifecycle_interrupted"


def test_an_interrupted_operation_doctor_cannot_continue_names_its_own_recovery() -> None:
    finding = _diagnose(
        "lifecycle_interrupted", _Probe(interrupted=[_operation(continuable=False)])
    )

    assert finding.status == "fail"
    assert not finding.repairable
    assert finding.next_step == "./flow-steward config apply --confirm"


def test_no_interrupted_operation_passes() -> None:
    assert _diagnose("lifecycle_interrupted", _Probe()).status == "ok"


_FACTS = MaintenanceFacts(
    phase="active", owner_kind="operation", owner_id="op-9", since="2026-09-23T10:00:00Z"
)


def test_maintenance_left_on_with_nothing_running_is_a_fix() -> None:
    finding = _diagnose("maintenance_left_enabled", _Probe(maintenance_facts=_FACTS))

    assert finding.status == "fail"
    assert finding.repairable
    assert "since 2026-09-23T10:00:00Z" in finding.cause
    assert "op-9" in finding.cause


def test_maintenance_during_a_running_operation_is_only_information() -> None:
    finding = _diagnose(
        "maintenance_left_enabled", _Probe(maintenance_facts=_FACTS, running="lifecycle op-9")
    )

    assert finding.status == "info"
    assert not finding.needs_attention  # its operation turns it off


def test_maintenance_off_passes() -> None:
    assert _diagnose("maintenance_left_enabled", _Probe()).status == "ok"


@pytest.mark.parametrize(
    ("problems", "status"),
    [(None, "ok"), ([], "ok"), (["/data/extensions has 3 entries outside the model"], "fail")],
)
def test_shared_trees(problems: list[str] | None, status: str) -> None:
    finding = _diagnose("shared_dirs_modes", _Probe(tree_problems=problems))

    assert finding.status == status
    if problems:
        assert problems[0] in finding.cause


def test_wide_secrets_are_named_and_a_long_list_is_cut_short() -> None:
    wide = [f"/srv/fs/.flow-steward/f{index} is 0644, wanted 0600" for index in range(7)]

    finding = _diagnose("secrets_modes", _Probe(wide=wide))

    assert finding.status == "fail"
    assert wide[4] in finding.cause
    assert wide[5] not in finding.cause
    assert "and 2 more" in finding.cause


def test_owner_only_secrets_pass() -> None:
    assert _diagnose("secrets_modes", _Probe()).status == "ok"


def test_stale_graphs_are_named() -> None:
    finding = _diagnose("graph_catalog", _Probe(stale=["knowledge"]))

    assert finding.status == "fail"
    assert "knowledge" in finding.cause


def test_matching_graphs_pass() -> None:
    assert _diagnose("graph_catalog", _Probe()).status == "ok"
