"""Evidence-backed adapter for destructive Clone restore phase effects."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from core.application.clone.planning import CloneTargetShell, PreparedCloneRestore
from core.application.lifecycle.errors import LifecycleError

RestorePhaseHandler = Callable[[PreparedCloneRestore], None]
RestorePhasePostcondition = Callable[[PreparedCloneRestore], bool]


class CloneRestoreGateway:
    """Dispatch lower-level restore effects and persist plan-bound phase evidence."""

    def __init__(
        self,
        *,
        operation_root: Path,
        target_shell: Callable[[], CloneTargetShell],
        phase_handlers: Mapping[str, RestorePhaseHandler],
        phase_postconditions: Mapping[str, RestorePhasePostcondition] | None = None,
    ) -> None:
        self._operation_root = Path(operation_root)
        self._target_shell = target_shell
        self._phase_handlers = dict(phase_handlers)
        self._phase_postconditions = dict(phase_postconditions or {})

    def current_target_shell(self) -> CloneTargetShell:
        return self._target_shell()

    def execute_phase(self, prepared: PreparedCloneRestore, phase: str) -> None:
        handler = self._phase_handlers.get(phase)
        if handler is None:
            raise RuntimeError(f"Clone restore phase is not configured: {phase}")
        if phase != "persist_plan" and not Path(prepared.staged_root).is_dir():
            raise LifecycleError.recovery_required(
                code="clone_restore_staging_rebootstrap_required",
                cause="Private Clone staging is missing before an unproven restore phase.",
                remediation="Re-authenticate the archive, then resume with the same confirmed plan.",
                step=phase,
                recovery_command="flow-steward clone restore",
            )
        handler(prepared)
        self._write_evidence(prepared, phase)

    def postcondition_holds(self, prepared: PreparedCloneRestore, phase: str) -> bool:
        path = self._evidence_path(prepared, phase)
        if path.is_symlink() or not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        if payload != {"phase": phase, "plan_digest": prepared.plan.plan_digest}:
            return False
        verifier = self._phase_postconditions.get(phase)
        return verifier(prepared) if verifier is not None else True

    def cleanup(self, prepared: PreparedCloneRestore) -> None:
        staged = Path(prepared.staged_root)
        shutil.rmtree(staged)
        if staged.exists() or staged.is_symlink():
            raise RuntimeError("Clone plaintext staging cleanup did not complete")

    def _write_evidence(self, prepared: PreparedCloneRestore, phase: str) -> None:
        root = self._operation_root / prepared.plan.plan_digest
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = self._evidence_path(prepared, phase)
        temporary = path.with_suffix(".partial")
        temporary.unlink(missing_ok=True)
        with temporary.open("x", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(
                {"phase": phase, "plan_digest": prepared.plan.plan_digest},
                output,
                sort_keys=True,
                separators=(",", ":"),
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def _evidence_path(self, prepared: PreparedCloneRestore, phase: str) -> Path:
        if not phase or not phase.replace("_", "").isalnum():
            raise ValueError("Clone restore phase identifier is invalid")
        return self._operation_root / prepared.plan.plan_digest / f"{phase}.json"


__all__ = [
    "CloneRestoreGateway",
    "RestorePhaseHandler",
    "RestorePhasePostcondition",
]
