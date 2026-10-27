from __future__ import annotations

from pathlib import Path

import pytest

from core.application.clone.planning import (
    CloneReplacementPlan,
    CloneTargetShell,
    PreparedCloneRestore,
)
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.clone.restore_gateway import CloneRestoreGateway


def _prepared(tmp_path: Path) -> PreparedCloneRestore:
    plan = CloneReplacementPlan.model_construct(plan_digest="a" * 64)
    return PreparedCloneRestore.model_construct(
        staged_root=str(tmp_path / "staged"),
        plan=plan,
    )


def test_restore_gateway_persists_plan_bound_phase_evidence(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    Path(prepared.staged_root).mkdir()
    calls: list[str] = []
    gateway = CloneRestoreGateway(
        operation_root=tmp_path / "operations",
        target_shell=lambda: CloneTargetShell.model_construct(),
        phase_handlers={"restore_database": lambda _prepared: calls.append("database")},
    )

    gateway.execute_phase(prepared, "restore_database")

    assert calls == ["database"]
    assert gateway.postcondition_holds(prepared, "restore_database") is True


def test_restore_gateway_requires_rebootstrap_when_staging_is_missing(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    gateway = CloneRestoreGateway(
        operation_root=tmp_path / "operations",
        target_shell=lambda: CloneTargetShell.model_construct(),
        phase_handlers={"restore_database": lambda _prepared: None},
    )

    with pytest.raises(LifecycleError) as raised:
        gateway.execute_phase(prepared, "restore_database")

    assert raised.value.exit_code.name == "RECOVERY_REQUIRED"
    assert "Re-authenticate" in raised.value.remediation


def test_restore_gateway_cleanup_fails_if_plaintext_staging_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(tmp_path)
    staged = Path(prepared.staged_root)
    staged.mkdir()
    gateway = CloneRestoreGateway(
        operation_root=tmp_path / "operations",
        target_shell=lambda: CloneTargetShell.model_construct(),
        phase_handlers={},
    )
    monkeypatch.setattr(
        "core.infrastructure.clone.restore_gateway.shutil.rmtree",
        lambda _path, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="plaintext"):
        gateway.cleanup(prepared)
