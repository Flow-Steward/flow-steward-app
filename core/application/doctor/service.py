"""Diagnose, plan and repair -- one run, nothing carried between runs.

`doctor --fix` in one run: a fresh diagnosis; a plan made from it alone; confirmation;
before each repair its check runs again (a problem already gone is left alone); after it
the check runs again to verify; and the summary is one more diagnosis. There is no
journal and no resume point: interrupted anywhere, the next `doctor` sees what is left,
because every repair is idempotent and leaves a state its own check recognises.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from core.application.doctor.contracts import (
    CheckBlocked,
    DoctorCheck,
    DoctorFinding,
    DoctorReport,
    FixResult,
    RepairOutcome,
)


class UnknownCheckError(ValueError):
    """An id given to `doctor --fix` that no check has."""


class DoctorService:
    def __init__(self, checks: Sequence[DoctorCheck], *, profile: str) -> None:
        self._checks = tuple(check for check in checks if profile in check.profiles)
        self._by_id = {check.check_id: check for check in self._checks}
        self.profile = profile

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    @property
    def checks(self) -> tuple[DoctorCheck, ...]:
        return self._checks

    def repairs(self, only: Iterable[str] = ()) -> tuple[DoctorCheck, ...]:
        """The checks that have a repair, of those named (all of them when none is)."""
        wanted = self._known(only)
        return tuple(
            check
            for check in self._checks
            if check.repair is not None and (not wanted or check.check_id in wanted)
        )

    def _known(self, only: Iterable[str]) -> tuple[str, ...]:
        wanted = tuple(dict.fromkeys(only))
        unknown = [check_id for check_id in wanted if check_id not in self._by_id]
        if unknown:
            raise UnknownCheckError(
                f"Unknown check id: {', '.join(unknown)}. Valid ids: {', '.join(self._by_id)}."
            )
        return wanted

    def diagnose(self, context: Any) -> DoctorReport:
        """Every check, read-only, from the state as it is now."""
        return DoctorReport(tuple(self._run(check, context) for check in self._checks))

    def fix(
        self,
        context: Any,
        *,
        only: Iterable[str] = (),
        confirm: Callable[[tuple[DoctorFinding, ...]], bool],
    ) -> FixResult:
        wanted = self._known(only)
        diagnosis = self.diagnose(context)
        plan = tuple(
            finding
            for finding in diagnosis.failing
            if finding.repairable
            and self._by_id[finding.check_id].repair is not None
            and (not wanted or finding.check_id in wanted)
        )
        result = FixResult(plan=plan)
        if not plan or not confirm(plan):
            result.final = diagnosis
            return result
        result.confirmed = True
        for planned in plan:
            result.outcomes.append(self._repair_one(self._by_id[planned.check_id], context))
        result.final = self.diagnose(context)
        return result

    def _repair_one(self, check: DoctorCheck, context: Any) -> RepairOutcome:
        before = self._run(check, context)
        if not before.needs_attention:
            return RepairOutcome(check.check_id, "already_fine", before.cause)
        if not before.repairable:
            return RepairOutcome(check.check_id, "refused", before.cause)
        unavailable = check.unavailable(context) if check.unavailable is not None else ""
        if unavailable:
            return RepairOutcome(check.check_id, "refused", unavailable)
        assert check.repair is not None
        try:
            done = check.repair(context, before)
        except Exception as exc:  # the next check says what is left
            return RepairOutcome(check.check_id, "still_failing", f"{type(exc).__name__}: {exc}")
        after = self._run(check, context)
        if after.needs_attention:
            return RepairOutcome(check.check_id, "still_failing", after.cause)
        return RepairOutcome(check.check_id, "repaired", done)

    @staticmethod
    def _run(check: DoctorCheck, context: Any) -> DoctorFinding:
        try:
            return check.diagnose(context)
        except CheckBlocked as blocked:
            return DoctorFinding(
                check.check_id,
                "info",
                cause=f"Not checked: {blocked.reason}.",
                next_step=blocked.next_step,
            )
        except Exception as exc:
            return DoctorFinding(
                check.check_id,
                "fail",
                cause=f"The check could not run: {type(exc).__name__}: {exc}",
                next_step="Rerun ./flow-steward doctor; if it repeats, report it with this line.",
            )
