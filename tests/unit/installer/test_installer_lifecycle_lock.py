"""AC17-R162: ./install.sh and the lifecycle commands never run at the same time.

The installer took only its own install.lock, so `config apply` or `stack stop` could
run underneath it -- each recreating containers the other was verifying. Now the
installer also holds the lifecycle lock for its whole run, and each side refuses the
other by name. On Compact the lifecycle commands keep their journals in the data
volume, which the installer cannot see, so they also lock the host's copy of the
installer's lock, mounted read-only into the runtime image.
"""

from __future__ import annotations

import ast
import re
from contextlib import ExitStack
from pathlib import Path

import pytest
import yaml

from core.application.installer.errors import InstallerFailure
from core.application.lifecycle.contracts import (
    LifecycleOperationJournal,
    LifecycleOperationKind,
)
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

ROOT = Path(__file__).resolve().parents[3]


def _apply_journal(store: LifecycleStateStore, operation_id: str) -> None:
    store.save_journal(
        LifecycleOperationJournal(
            operation_id=operation_id,
            kind=LifecycleOperationKind.CONFIG_APPLY,
            current_step="runtime",
            recovery_command="./flow-steward config apply --confirm",
        )
    )


def test_install_is_refused_while_config_apply_runs(tmp_path: Path) -> None:
    lifecycle = LifecycleStateStore(tmp_path)
    installer = InstallerStateStore(tmp_path / ".flow-steward")
    _apply_journal(lifecycle, "apply-1")

    with (
        lifecycle.operation_lock("apply-1", mutating=True),
        pytest.raises(InstallerFailure) as refused,
        installer.operation_lock(),
    ):
        pass

    assert refused.value.code == "lifecycle_operation_active"
    assert refused.value.cause == "Operation apply-1 is running: config apply, step runtime."
    assert refused.value.exit_code == 6


def test_config_and_stack_are_refused_while_install_runs(tmp_path: Path) -> None:
    lifecycle = LifecycleStateStore(tmp_path)
    installer = InstallerStateStore(tmp_path / ".flow-steward")

    with (
        installer.operation_lock(),
        pytest.raises(LifecycleError) as refused,
        lifecycle.operation_lock("stop-1", mutating=True),
    ):
        pass

    assert refused.value.code == "lifecycle_operation_active"
    assert refused.value.cause.startswith("./install.sh is running (operation install-")


def test_compact_lifecycle_commands_are_refused_while_install_runs(tmp_path: Path) -> None:
    host_state = tmp_path / "host" / ".flow-steward"
    installer = InstallerStateStore(host_state)
    compact = LifecycleStateStore(
        tmp_path / "volume" / "state", installation_lock_path=installer.lifecycle_lock_path
    )

    with (
        installer.operation_lock(),
        pytest.raises(LifecycleError) as refused,
        compact.operation_lock("maintenance-1", mutating=True),
    ):
        pass

    assert "./install.sh is running" in refused.value.cause
    # the refused operation left its own volume lock free again
    with compact.operation_lock("maintenance-2", mutating=True):
        pass


def test_install_is_refused_while_a_compact_lifecycle_command_runs(tmp_path: Path) -> None:
    host_state = tmp_path / "host" / ".flow-steward"
    installer = InstallerStateStore(host_state)
    with installer.operation_lock():
        pass  # the installer of this release has run: its host lock file exists
    installer.lifecycle_lock_path.chmod(0o400)  # the runtime image sees it read-only
    compact = LifecycleStateStore(
        tmp_path / "volume" / "state", installation_lock_path=installer.lifecycle_lock_path
    )

    with compact.operation_lock("maintenance-1", mutating=True):
        installer.lifecycle_lock_path.chmod(0o600)
        with pytest.raises(InstallerFailure) as refused, installer.operation_lock():
            pass  # SIM117: the chmod must happen while the Compact command holds the lock

    assert refused.value.code == "lifecycle_operation_active"
    assert "maintenance" in refused.value.cause


def test_neither_side_is_refused_when_the_other_is_not_running(tmp_path: Path) -> None:
    """The green direction, in both orders and on both profiles."""
    lifecycle = LifecycleStateStore(tmp_path)
    installer = InstallerStateStore(tmp_path / ".flow-steward")
    compact = LifecycleStateStore(
        tmp_path / "volume", installation_lock_path=installer.lifecycle_lock_path
    )
    missing = LifecycleStateStore(
        tmp_path / "other", installation_lock_path=tmp_path / "absent" / "active.lock"
    )

    with ExitStack() as held:
        held.enter_context(installer.operation_lock())
    for store, operation_id in ((lifecycle, "a-1"), (compact, "c-1"), (missing, "m-1")):
        with store.operation_lock(operation_id, mutating=True):
            pass
    with installer.operation_lock():
        pass
    # read-only commands never take the lock, the installer running or not
    with installer.operation_lock(), lifecycle.operation_lock("status-1", mutating=False):
        pass


def test_every_compact_lifecycle_store_holds_the_installation_lock() -> None:
    """Derived from the source: the runtime builds its stores through one method, and
    Compact overrides it with the host lock that its compose file mounts."""
    source = (ROOT / "core/entrypoints/cli/lifecycle_runtime.py").read_text(encoding="utf-8")
    runtime = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "LifecycleCLIRuntime"
    )
    builders = [
        function.name
        for function in ast.walk(runtime)
        if isinstance(function, ast.FunctionDef)
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "LifecycleStateStore"
    ]
    assert builders == ["_lifecycle_state"]
    consumers = {
        function.name
        for function in runtime.body
        if isinstance(function, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr == "_lifecycle_state"
            for call in ast.walk(function)
        )
    }
    assert consumers == {
        "configuration_service",
        "stack_service",
        "backup_command_service",
        "maintenance_service",
        "environment_service",
    }

    from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime

    lock = CompactPlatformRuntime()._lifecycle_state().installation_lock_path
    assert lock is not None
    compose = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.compact.yaml").read_text(encoding="utf-8")
    )
    mounts = compose["services"]["fs-cli"]["volumes"]
    mounted = next(
        re.search(r"\}:(/[^:]+)", str(volume)).group(1)
        for volume in mounts
        if str(volume).startswith("${FS_INSTALLER_STATE_DIR")
    )
    assert lock == Path(mounted) / "operations" / "active.lock"


def test_a_moved_installer_state_directory_still_meets_scalable_and_compact(
    tmp_path: Path,
) -> None:
    """FS_INSTALLER_STATE_DIR moves the installer's state, not the checkout's
    .flow-steward, which is where Scalable's fs-cli takes the lock; Compact takes the
    mounted state directory's. The installer holds both."""
    checkout_state = tmp_path / "checkout" / ".flow-steward"
    installer = InstallerStateStore(tmp_path / "moved-state", lifecycle_state_root=checkout_state)
    scalable = LifecycleStateStore(checkout_state)
    compact = LifecycleStateStore(
        tmp_path / "volume", installation_lock_path=installer.lifecycle_lock_path
    )

    with installer.operation_lock():
        for store in (scalable, compact):
            with (
                pytest.raises(LifecycleError, match=r"install\.sh is running"),
                store.operation_lock("stop-1", mutating=True),
            ):
                pass
    _apply_journal(scalable, "apply-2")
    with (
        scalable.operation_lock("apply-2", mutating=True),
        pytest.raises(InstallerFailure, match="config apply"),
        installer.operation_lock(),
    ):
        pass
    assert len(set(installer.lifecycle_lock_paths)) == 2  # non-vacuity: two files
