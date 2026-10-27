import json
import os
import shutil
from pathlib import Path

import pytest

from core.infrastructure.extension_runtime.bundle_cleanup import (
    prepare_cleanup_intent,
    reconcile_cleanup_journal,
)


def _intent(root: Path, bundle: Path) -> Path:
    result = prepare_cleanup_intent(
        extensions_root=root,
        extension_id="acme.demo",
        account_id="account-default",
        plan_revision="revision-1",
        bundle_roots=[bundle],
    )
    assert result is not None
    return result


def _write_extension_release(path: Path, *, version: str) -> None:
    path.mkdir(parents=True)
    (path / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": "acme.demo",
                "kind": "tool_provider",
                "version": version,
                "display_name": "Demo",
                "description": "Restart recovery fixture.",
                "vendor": {"name": "Acme"},
                "entrypoint": {"mode": "subprocess", "command": ["python3", "main.py"]},
                "health": {"command": ["python3", "-c", "print('ok')"]},
                "ui_manifest": "ui/ui_manifest.yaml",
                "features": ["tool"],
            }
        ),
        encoding="utf-8",
    )
    (path / "ui").mkdir()
    (path / "ui" / "ui_manifest.yaml").write_text(
        "version: '1'\npages: []\n",
        encoding="utf-8",
    )


def test_cleanup_reconciles_crash_after_database_commit(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)

    result = reconcile_cleanup_journal(
        extensions_root=root, is_installed=lambda _extension, _account: False
    )

    assert result.completed == 1
    assert not bundle.exists()
    assert not intent.exists()


def test_startup_safe_cleanup_discards_intent_when_source_survived(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
        discovered_extension_ids={"acme.demo"},
    )

    assert result.discarded == 1
    assert bundle.exists()
    assert not intent.exists()


def test_cleanup_rejects_symlinked_ancestor_without_touching_external_victim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    journal_root = root / ".fs-uninstall-cleanup"
    journal_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    victim = outside / "victim"
    victim.mkdir(parents=True)
    sentinel = victim / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    intent = journal_root / "hostile.json"
    intent.write_text(
        json.dumps(
            {
                "version": 1,
                "extension_id": "acme.demo",
                "account_id": "account-default",
                "plan_revision": "hostile",
                "roots": [
                    {
                        "source": str(root / "escape" / "victim"),
                        "quarantine": str(root / "escape" / ".victim.uninstall-hostile"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
    )

    assert result.pending == 1
    assert "symlinked ancestor" in result.errors[0]
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert victim.is_dir()
    assert intent.exists()
    assert not (outside / ".victim.uninstall-hostile").exists()


def test_descriptor_walk_rejects_symlink_even_without_preflight_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.extension_runtime import bundle_cleanup

    root = tmp_path / "extensions"
    journal_root = root / ".fs-uninstall-cleanup"
    journal_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    victim = outside / "victim"
    victim.mkdir(parents=True)
    sentinel = victim / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    intent = journal_root / "hostile.json"
    intent.write_text(
        json.dumps(
            {
                "extension_id": "acme.demo",
                "account_id": "account-default",
                "roots": [
                    {
                        "source": str(root / "escape" / "victim"),
                        "quarantine": str(root / "escape" / ".victim.uninstall-hostile"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def lexical_containment(check_root: Path, candidate: Path) -> Path:
        absolute_root = check_root.resolve()
        absolute = Path(os.path.abspath(candidate))
        if absolute == absolute_root or absolute_root not in absolute.parents:
            raise ValueError("escape")
        return absolute

    monkeypatch.setattr(bundle_cleanup, "_contained", lexical_containment)

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
    )

    assert result.pending == 1
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert victim.is_dir()
    assert intent.exists()


def test_startup_safe_cleanup_preserves_only_discovered_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)
    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
        discovered_extension_ids={"acme.demo"},
    )

    assert result.discarded == 1
    assert bundle.is_dir()
    assert not intent.exists()


def test_startup_safe_cleanup_removes_undiscovered_dangling_final_symlink(
    tmp_path: Path,
) -> None:
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    root = tmp_path / "extensions"
    root.mkdir()
    bundle = root / "acme_demo"
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    sentinel = outside_parent / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    outside_target = outside_parent / "missing"
    bundle.symlink_to(outside_target, target_is_directory=True)
    intent = _intent(root, bundle)

    scanned = scan_extensions(str(root))
    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
        discovered_extension_ids={str(item.get("extension_key") or "").strip() for item in scanned},
    )

    assert scanned == []
    assert result.completed == 1
    assert not bundle.exists()
    assert not bundle.is_symlink()
    assert outside_parent.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not intent.exists()


def test_same_process_cleanup_unlinks_final_symlink_without_touching_external_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    outside_target = tmp_path / "outside"
    outside_target.mkdir()
    sentinel = outside_target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    bundle = root / "acme_demo"
    bundle.symlink_to(outside_target, target_is_directory=True)
    intent = _intent(root, bundle)

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
    )

    assert result.completed == 1
    assert not bundle.exists()
    assert not bundle.is_symlink()
    assert outside_target.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not intent.exists()


def test_prepare_cleanup_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    outside = tmp_path / "outside"
    victim = outside / "victim"
    victim.mkdir(parents=True)
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked ancestor"):
        prepare_cleanup_intent(
            extensions_root=root,
            extension_id="acme.demo",
            account_id="account-default",
            plan_revision="hostile",
            bundle_roots=[root / "escape" / "victim"],
        )


def test_cleanup_uses_descriptor_relative_rename_and_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.extension_runtime import bundle_cleanup

    root = tmp_path / "extensions"
    bundle = root / "nested" / "acme_demo"
    bundle.mkdir(parents=True)
    (bundle / "payload.txt").write_text("payload", encoding="utf-8")
    intent = _intent(root, bundle)
    real_replace = os.replace
    replace_calls: list[tuple[object, object, int | None, int | None]] = []

    def descriptor_replace(
        source,
        destination,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
    ):
        replace_calls.append((source, destination, src_dir_fd, dst_dir_fd))
        assert not Path(source).is_absolute()
        assert not Path(destination).is_absolute()
        assert src_dir_fd is not None
        assert dst_dir_fd is not None
        return real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(bundle_cleanup.os, "replace", descriptor_replace)
    monkeypatch.setattr(
        shutil,
        "rmtree",
        lambda *_args, **_kwargs: pytest.fail("path-based shutil.rmtree must not be used"),
    )

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: False,
    )

    assert result.completed == 1
    assert len(replace_calls) == 1
    assert not bundle.exists()
    assert not intent.exists()


def test_cleanup_retry_finishes_existing_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)
    quarantine = next(root.glob(".acme_demo.uninstall-*"), None)
    assert quarantine is None
    import json

    payload = json.loads(intent.read_text(encoding="utf-8"))
    quarantine = Path(payload["roots"][0]["quarantine"])
    bundle.replace(quarantine)

    result = reconcile_cleanup_journal(
        extensions_root=root, is_installed=lambda _extension, _account: False
    )

    assert result.completed == 1
    assert not quarantine.exists()
    assert not intent.exists()


def test_cleanup_keeps_installed_intent_pending_for_inflight_uninstall(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)

    result = reconcile_cleanup_journal(
        extensions_root=root, is_installed=lambda _extension, _account: True
    )

    assert result.pending == 1
    assert "still installed or lifecycle in progress" in result.errors[0]
    assert bundle.exists()
    assert intent.exists()


def test_restart_after_cleanup_intent_before_commit_discards_it_for_lifecycle_lock_owner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=lambda _extension, _account: True,
        discard_installed_intents=True,
    )

    assert result.discarded == 1
    assert bundle.exists()
    assert not intent.exists()


def test_cleanup_failure_retains_intent_for_retry(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "extensions"
    bundle = root / "acme_demo"
    bundle.mkdir(parents=True)
    intent = _intent(root, bundle)

    def fail_replace(_source, _target, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.bundle_cleanup.os.replace", fail_replace
    )
    result = reconcile_cleanup_journal(
        extensions_root=root, is_installed=lambda _extension, _account: False
    )

    assert result.pending == 1
    assert "disk unavailable" in result.errors[0]
    assert intent.exists()
    assert bundle.exists()


def test_restart_recovers_committed_release_publication_before_discovery(
    tmp_path: Path,
) -> None:
    from core.infrastructure.extension_runtime.publication_recovery import (
        recover_managed_release_publications,
    )

    root = tmp_path / "extensions"
    stale_release = root / ".releases" / "acme_demo" / "1.0.0"
    committed_release = root / ".releases" / "acme_demo" / "2.0.0"
    _write_extension_release(stale_release, version="1.0.0")
    _write_extension_release(committed_release, version="2.0.0")
    active = root / "acme_demo"
    active.symlink_to(stale_release, target_is_directory=True)

    recovered = recover_managed_release_publications(
        extensions_root=root,
        extensions=[{"extension_key": "acme.demo", "bundle_root": str(committed_release)}],
    )

    assert recovered == 1
    assert active.is_symlink()
    assert active.resolve() == committed_release

    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    scanned = scan_extensions(str(root))
    assert [(row["extension_key"], row["version"]) for row in scanned] == [("acme.demo", "2.0.0")]
    assert (
        recover_managed_release_publications(
            extensions_root=root,
            extensions=[{"extension_key": "acme.demo", "bundle_root": str(committed_release)}],
        )
        == 0
    )


def test_restart_rejects_committed_release_beneath_symlinked_managed_ancestor(
    tmp_path: Path,
) -> None:
    import pytest

    from core.infrastructure.extension_runtime.publication_recovery import (
        recover_managed_release_publications,
    )

    root = tmp_path / "extensions"
    outside_parent = tmp_path / "outside" / "acme_demo"
    outside_release = outside_parent / "2.0.0"
    _write_extension_release(outside_release, version="2.0.0")
    (root / ".releases").mkdir(parents=True)
    (root / ".releases" / "acme_demo").symlink_to(
        outside_parent,
        target_is_directory=True,
    )
    committed_release = root / ".releases" / "acme_demo" / "2.0.0"

    with pytest.raises(ValueError, match="symlinked ancestors"):
        recover_managed_release_publications(
            extensions_root=root,
            extensions=[{"extension_key": "acme.demo", "bundle_root": str(committed_release)}],
        )

    active = root / "acme_demo"
    assert not active.exists()
    assert not active.is_symlink()


def test_same_process_post_commit_cleanup_finishes_before_rescan(tmp_path: Path) -> None:
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    root = tmp_path / "extensions"
    release = root / ".releases" / "acme_demo" / "2.0.0"
    _write_extension_release(release, version="2.0.0")
    active = root / "acme_demo"
    active.symlink_to(release, target_is_directory=True)
    intent = prepare_cleanup_intent(
        extensions_root=root,
        extension_id="acme.demo",
        account_id="account-default",
        plan_revision="committed-uninstall",
        bundle_roots=[active, release],
    )
    assert intent is not None
    install_checks: list[tuple[str, str]] = []

    def is_installed(extension_id: str, account_id: str) -> bool:
        install_checks.append((extension_id, account_id))
        return False

    result = reconcile_cleanup_journal(
        extensions_root=root,
        is_installed=is_installed,
    )

    assert result.completed == 1
    assert install_checks == [("acme.demo", "account-default")]
    assert not active.exists()
    assert not active.is_symlink()
    assert not release.exists()
    assert scan_extensions(str(root)) == []
    assert (
        reconcile_cleanup_journal(
            extensions_root=root,
            is_installed=is_installed,
        )
        == type(result)()
    )


def test_restart_fails_closed_when_committed_managed_release_is_missing(
    tmp_path: Path,
) -> None:
    from core.infrastructure.extension_runtime.publication_recovery import (
        recover_managed_release_publications,
    )

    root = tmp_path / "extensions"
    missing_release = root / ".releases" / "acme_demo" / "2.0.0"

    with pytest.raises(FileNotFoundError, match="Committed extension release"):
        recover_managed_release_publications(
            extensions_root=root,
            extensions=[{"extension_key": "acme.demo", "bundle_root": str(missing_release)}],
        )


def test_an_unreadable_cleanup_journal_is_pending_work_not_a_failed_scan(
    tmp_path: Path, monkeypatch
) -> None:
    """Seen on Compact: the API process could not open the installer's journal.

    The scan answered 500 with "Permission denied: '.fs-uninstall-cleanup'".
    The scan now runs on the installer; a process that still cannot read the
    journal reports the cleanup as pending instead of failing.
    """
    from core.infrastructure.extension_runtime import bundle_cleanup

    def denied(_root):
        raise PermissionError(13, "Permission denied", ".fs-uninstall-cleanup")

    monkeypatch.setattr(bundle_cleanup, "_journal_paths", denied)

    result = bundle_cleanup.reconcile_cleanup_journal(
        extensions_root=tmp_path, is_installed=lambda *_a: False
    )

    assert result.pending == 1
    assert result.completed == 0
    assert result.errors == (".fs-uninstall-cleanup: Permission denied",)
