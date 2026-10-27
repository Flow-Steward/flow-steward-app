"""Control-flow failures remain owned until every compensation has been attempted."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.application.extensions.archive_update_models import UpdateRollbackState
from core.application.extensions.command_install_release import ExtensionReleaseInstallWorkflow
from core.application.extensions.update_failure_reporting import raise_update_failure


class FatalCompensationSignal(BaseException):
    """An extension cancellation signal outside the ordinary Exception hierarchy."""


FATAL_TYPES = [KeyboardInterrupt, SystemExit, FatalCompensationSignal]


def _install_workflow(
    monkeypatch, *, original: BaseException | None, cleanup: BaseException | None
):
    workflow = object.__new__(ExtensionReleaseInstallWorkflow)
    prepared, record, result = object(), object(), object()
    rollback = Mock(side_effect=cleanup)
    monkeypatch.setattr(workflow, "_prepare", Mock(return_value=prepared))
    monkeypatch.setattr(workflow, "_persist_install", Mock(return_value=record))
    monkeypatch.setattr(workflow, "_publish_install", Mock(side_effect=original))
    monkeypatch.setattr(workflow, "_rollback", rollback)
    monkeypatch.setattr(workflow, "_result", Mock(return_value=result))
    return workflow, prepared, record, result, rollback


@pytest.mark.parametrize("fatal_type", FATAL_TYPES)
def test_update_runs_remaining_compensations_and_reraises_exact_failures(fatal_type):
    original = RuntimeError("mutation failed")
    fatal = fatal_type("cleanup interrupted")
    ordinary = ValueError("second cleanup failed")
    lifecycle, managed_release, state, cleanup = Mock(), Mock(), Mock(), Mock()
    managed_release.remove_managed_simple_extension_active_root.side_effect = fatal
    state.scan_extensions.side_effect = ordinary
    prepared = SimpleNamespace(
        request=SimpleNamespace(
            extensions_root="/extensions",
            scope=None,
            bl=object(),
            install_store=object(),
            runtime_store=object(),
            current_install=SimpleNamespace(install_id="install-1"),
        ),
        extension_id="fixture",
        target_dir="/extensions/fixture",
        previous_active_release_dir=None,
        previous_version="1.0.0",
        previous_enabled=True,
        previous_config={},
        candidate_version="2.0.0",
        lifecycle_command=lifecycle,
        previous_install_payload={},
        previous_release=None,
        previous_entitlement=None,
        previous_projections=[],
        candidate_release_id="release-2",
        previous_release_id="release-1",
    )
    rollback = UpdateRollbackState(
        prepared=prepared,
        context=Mock(),
        managed_release=managed_release,
        cleanup=cleanup,
        state=state,
        published_target=True,
    )
    failures = rollback.rollback_update()

    assert failures == [fatal, ordinary]
    state.set_enabled_state_locked.assert_called_once()
    cleanup.cleanup_failed_simple_update_state.assert_called_once()
    lifecycle.rollback_install_release_failure.assert_called_once()
    lifecycle.cleanup_release_if_unreferenced.assert_called_once()
    assert "remove candidate activation" in fatal.__notes__[0]
    assert "rescan previous bundle" in ordinary.__notes__[0]
    with pytest.raises(BaseExceptionGroup) as caught:
        raise_update_failure(original, failures)
    assert caught.value.exceptions[0] is original
    assert caught.value.exceptions[1] is fatal
    assert caught.value.exceptions[2] is ordinary


@pytest.mark.parametrize("fatal_type", FATAL_TYPES)
def test_update_original_cancellation_is_reraised_unchanged(fatal_type):
    original = fatal_type("mutation interrupted")
    with pytest.raises(fatal_type) as caught:
        raise_update_failure(original, [])
    assert caught.value is original


@pytest.mark.parametrize("fatal_type", FATAL_TYPES)
def test_install_original_cancellation_rolls_back_and_is_reraised(monkeypatch, fatal_type):
    original = fatal_type("mutation interrupted")
    workflow, prepared, record, _, rollback = _install_workflow(
        monkeypatch, original=original, cleanup=None
    )
    install_request = SimpleNamespace()
    with pytest.raises(fatal_type) as caught:
        workflow.install(install_request)
    assert caught.value is original
    rollback.assert_called_once_with(prepared, install_record=record)


@pytest.mark.parametrize("fatal_type", FATAL_TYPES)
def test_install_keeps_original_and_fatal_cleanup_identity(monkeypatch, fatal_type):
    original = RuntimeError("mutation failed")
    cleanup = fatal_type("cleanup interrupted")
    workflow, prepared, record, _, rollback = _install_workflow(
        monkeypatch, original=original, cleanup=cleanup
    )
    install_request = SimpleNamespace()
    with pytest.raises(BaseExceptionGroup) as caught:
        workflow.install(install_request)
    assert caught.value.exceptions[0] is original
    assert caught.value.exceptions[1] is cleanup
    assert "compensation failed" in cleanup.__notes__[0]
    rollback.assert_called_once_with(prepared, install_record=record)


def test_successful_install_does_not_compensate(monkeypatch):
    workflow, _, _, result, rollback = _install_workflow(monkeypatch, original=None, cleanup=None)
    assert workflow.install(SimpleNamespace()) is result
    rollback.assert_not_called()


@pytest.mark.parametrize("fatal_type", FATAL_TYPES)
def test_update_cancellation_and_ordinary_cleanup_failure_both_survive(fatal_type):
    original = fatal_type("mutation interrupted")
    cleanup = RuntimeError("cleanup failed")
    with pytest.raises(BaseExceptionGroup) as caught:
        raise_update_failure(original, [cleanup])
    assert caught.value.exceptions[0] is original
    assert caught.value.exceptions[1] is cleanup


@pytest.mark.parametrize("fatal_type", FATAL_TYPES)
def test_install_cancellation_and_ordinary_cleanup_failure_both_survive(monkeypatch, fatal_type):
    original = fatal_type("mutation interrupted")
    cleanup = RuntimeError("cleanup failed")
    workflow, prepared, record, _, rollback = _install_workflow(
        monkeypatch, original=original, cleanup=cleanup
    )
    install_request = SimpleNamespace()
    with pytest.raises(BaseExceptionGroup) as caught:
        workflow.install(install_request)
    assert caught.value.exceptions[0] is original
    assert caught.value.exceptions[1] is cleanup
    rollback.assert_called_once_with(prepared, install_record=record)
