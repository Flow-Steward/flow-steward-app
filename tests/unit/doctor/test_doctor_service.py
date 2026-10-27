"""The doctor's contract, before any real check (docs/tasks/2026-09-23-doctor-...md).

Principle 0: nothing is carried from logs, journals or an earlier run. `--fix` diagnoses
afresh, plans from that alone, re-checks each problem just before and after repairing
it, and ends with one more diagnosis. No journal: interrupted, the next run sees what
is left.
"""

from __future__ import annotations

import pytest

from core.application.doctor.contracts import DoctorCheck, DoctorFinding
from core.application.doctor.service import DoctorService, UnknownCheckError


class _World:
    """The installation's state; checks read it, repairs change it."""

    def __init__(self, **broken: bool) -> None:
        self.broken = dict(broken)
        self.repairs: list[str] = []


def _check(check_id: str, *, repairable: bool = True, repair_fails: bool = False) -> DoctorCheck:
    def diagnose(world: _World) -> DoctorFinding:
        if world.broken.get(check_id):
            return DoctorFinding(
                check_id, "fail", cause=f"{check_id} is broken", repairable=repairable
            )
        return DoctorFinding(check_id, "ok")

    def repair(world: _World, finding: DoctorFinding) -> str:
        world.repairs.append(check_id)
        if repair_fails:
            raise RuntimeError("repair broke")
        world.broken[check_id] = False
        return f"repaired {check_id}"

    return DoctorCheck(check_id, check_id, diagnose, repair if repairable else None)


def _service(*checks: DoctorCheck) -> DoctorService:
    return DoctorService(checks, profile="scalable")


def test_the_plan_is_what_this_run_found_and_can_repair() -> None:
    world = _World(stack_stopped=True, schema_state=True)
    service = _service(_check("stack_stopped"), _check("schema_state", repairable=False))
    seen: list[tuple[str, ...]] = []

    result = service.fix(
        world, confirm=lambda plan: seen.append(tuple(f.check_id for f in plan)) or True
    )

    assert seen == [("stack_stopped",)]  # a hint-only finding is never in the plan
    assert [outcome.kind for outcome in result.outcomes] == ["repaired"]
    assert result.final is not None
    assert not result.final.findings[0].needs_attention


def test_nothing_changes_without_confirmation() -> None:
    world = _World(stack_stopped=True)

    result = _service(_check("stack_stopped")).fix(world, confirm=lambda _plan: False)

    assert world.repairs == []
    assert result.confirmed is False


def test_a_problem_gone_before_its_repair_is_left_alone() -> None:
    """The check runs again just before the repair: nothing trusted from the plan."""
    world = _World(stack_stopped=True)

    def confirm_and_meanwhile_fix(_plan) -> bool:
        world.broken["stack_stopped"] = False  # someone else fixed it meanwhile
        return True

    result = _service(_check("stack_stopped")).fix(world, confirm=confirm_and_meanwhile_fix)

    assert world.repairs == []
    assert result.outcomes[0].kind == "already_fine"


def test_a_repair_that_does_not_hold_is_reported_and_the_rest_go_on() -> None:
    world = _World(first=True, second=True)
    service = _service(_check("first", repair_fails=True), _check("second"))

    result = service.fix(world, confirm=lambda _plan: True)

    assert [(o.check_id, o.kind) for o in result.outcomes] == [
        ("first", "still_failing"),
        ("second", "repaired"),
    ]
    assert "repair broke" in result.outcomes[0].detail
    assert result.final is not None
    assert not result.final.clean  # the new diagnosis says so


def test_selected_ids_are_the_only_ones_repaired() -> None:
    world = _World(first=True, second=True)

    _service(_check("first"), _check("second")).fix(
        world, only=["second"], confirm=lambda _plan: True
    )

    assert world.repairs == ["second"]


def test_an_unknown_id_is_refused_with_the_valid_ones() -> None:
    service = _service(_check("stack_stopped"))
    world = _World()
    with pytest.raises(
        UnknownCheckError, match=r"Unknown check id: stack_stoped\. Valid ids: stack_stopped"
    ):
        service.fix(world, only=["stack_stoped"], confirm=lambda _p: True)


def test_a_check_that_cannot_run_is_a_finding_and_the_others_still_run() -> None:
    """The task's condition 2: doctor works exactly when things are broken."""

    def down(_world):
        raise ConnectionError("database is down")

    service = _service(DoctorCheck("schema_state", "Schema", down), _check("stack_stopped"))

    report = service.diagnose(_World(stack_stopped=True))

    by_id = {finding.check_id: finding for finding in report.findings}
    assert by_id["schema_state"].status == "fail"
    assert "database is down" in by_id["schema_state"].cause
    assert by_id["stack_stopped"].cause == "stack_stopped is broken"


def test_an_interrupted_fix_leaves_what_the_next_diagnosis_names() -> None:
    """No resume point: the second of two repairs never ran; the next doctor names it."""
    world = _World(first=True, second=True)
    service = _service(_check("first"), _check("second"))

    def interrupted_after_first(check_id: str):
        original = service._by_id[check_id].repair

        def repair(context, finding):
            if check_id == "second":
                raise KeyboardInterrupt
            return original(context, finding)

        return repair

    for check_id in ("first", "second"):
        object.__setattr__(service._by_id[check_id], "repair", interrupted_after_first(check_id))
    with pytest.raises(KeyboardInterrupt):
        service.fix(world, confirm=lambda _plan: True)

    remaining = [f.check_id for f in service.diagnose(world).failing]
    assert remaining == ["second"]


def test_a_check_that_needs_a_stopped_service_is_not_a_failure_and_names_the_fix_first() -> None:
    """Live on a stopped Compact stand: three checks that read the runtime or the
    database each came out FAIL "could not run ... report it", so one problem looked like
    four and three next steps were wrong. Not checked is what they are."""
    from core.application.doctor.contracts import CheckBlocked

    def needs_the_database(_world: _World) -> DoctorFinding:
        raise CheckBlocked("pg-fs is not running", "./flow-steward doctor --fix stack_stopped")

    service = DoctorService(
        [_check("stack_stopped"), DoctorCheck("graph_catalog", "Graphs", needs_the_database)],
        profile="compact",
    )

    report = service.diagnose(_World(stack_stopped=True))

    blocked = next(f for f in report.findings if f.check_id == "graph_catalog")
    assert blocked.status == "info"
    assert not blocked.needs_attention
    assert blocked.cause == "Not checked: pg-fs is not running."
    assert blocked.next_step == "./flow-steward doctor --fix stack_stopped"
    assert [f.check_id for f in report.failing] == ["stack_stopped"]  # one problem, one fix
