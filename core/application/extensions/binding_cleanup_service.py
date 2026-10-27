from __future__ import annotations

import contextlib
import shutil
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import ScopeContext

from .binding_constants import (
    MAX_EXTENSION_ARCHIVE_BYTES,
    MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
    archive_too_large_message,
)
from .binding_context import ExtensionLifecycleContext
from .lifecycle_ports import service_scope
from .managed_release_service import ManagedReleaseService

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES
_archive_too_large_message = archive_too_large_message


class ExtensionCleanupService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        managed_release: ManagedReleaseService,
    ) -> None:
        self._context = context
        self._managed_release = managed_release

    def extension_bundle_quarantine(
        self, bundle_root: Path | None
    ) -> contextlib.AbstractContextManager[bool]:
        return self._extension_bundle_quarantine(bundle_root)

    def remove_extension_bundle_after_commit(self, bundle_root: Path | None) -> bool:
        return self._remove_extension_bundle_after_commit(bundle_root)

    def prepare_extension_cleanup(
        self,
        *,
        extension_id: str,
        account_id: str,
        plan_revision: str,
        bundle_roots: Iterable[Path],
    ) -> Path | None:
        return self._prepare_extension_cleanup(
            extension_id=extension_id,
            account_id=account_id,
            plan_revision=plan_revision,
            bundle_roots=bundle_roots,
        )

    def reconcile_extension_cleanup(
        self,
        *,
        bl: Any,
        intent_path: Path | None = None,
        committed_extension_id: str = "",
        discard_installed_intents: bool = False,
        discovered_extension_ids: Iterable[str] = (),
    ) -> Any:
        return self._reconcile_extension_cleanup(
            bl=bl,
            intent_path=intent_path,
            committed_extension_id=committed_extension_id,
            discard_installed_intents=discard_installed_intents,
            discovered_extension_ids=discovered_extension_ids,
        )

    def rollback_failed_new_extension_lifecycle(
        self,
        *,
        bl: Any,
        extension_id: str,
        scope: ScopeContext | None,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> None:
        self._rollback_failed_new_extension_lifecycle(
            bl=bl,
            extension_id=extension_id,
            scope=scope,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    def cleanup_failed_simple_update_state(
        self,
        *,
        bl: Any,
        extension_id: str,
        failed_version: str,
    ) -> None:
        self._cleanup_failed_simple_update_state(
            bl=bl,
            extension_id=extension_id,
            failed_version=failed_version,
        )

    def purge_simple_extension_scan_state(self, *, bl: Any, extension_id: str) -> None:
        self._purge_simple_extension_scan_state(bl=bl, extension_id=extension_id)

    def has_simple_extension_artifact(self, *, bl: Any, extension_id: str) -> bool:
        return self._has_simple_extension_artifact(bl=bl, extension_id=extension_id)

    @staticmethod
    @contextlib.contextmanager
    def _extension_bundle_quarantine(bundle_root: Path | None) -> Iterator[bool]:
        quarantined: Path | None = None
        if bundle_root is not None and (bundle_root.exists() or bundle_root.is_symlink()):
            quarantined = (
                bundle_root.parent / f".{bundle_root.name}.uninstall-{uuid.uuid4().hex[:8]}"
            )
            shutil.move(str(bundle_root), str(quarantined))
        try:
            yield quarantined is not None
        except Exception:
            if (
                quarantined is not None
                and (quarantined.exists() or quarantined.is_symlink())
                and bundle_root is not None
            ):
                shutil.move(str(quarantined), str(bundle_root))
            raise
        else:
            if quarantined is not None and (quarantined.exists() or quarantined.is_symlink()):
                if quarantined.is_symlink():
                    quarantined.unlink()
                else:
                    shutil.rmtree(quarantined, ignore_errors=True)

    @staticmethod
    def _remove_extension_bundle_after_commit(bundle_root: Path | None) -> bool:
        """Remove an uninstalled bundle only after its database commit is durable.

        Rename first so a crash during recursive deletion cannot expose a partial
        package. A leftover ``.uninstall-*`` directory is safe garbage: unlike the
        old pre-commit quarantine path, it must never be restored as an install.
        """

        if bundle_root is None or not (bundle_root.exists() or bundle_root.is_symlink()):
            return False
        quarantined = bundle_root.parent / f".{bundle_root.name}.uninstall-{uuid.uuid4().hex[:8]}"
        shutil.move(str(bundle_root), str(quarantined))
        if quarantined.is_symlink():
            quarantined.unlink()
        else:
            shutil.rmtree(quarantined)
        return True

    def _extension_cleanup_is_installed(
        self, *, bl: Any, extension_id: str, account_id: str
    ) -> bool:
        try:
            install = self._context.install_store(bl).get(
                extension_id=extension_id,
                account_id=account_id,
            )
            if install is not None:
                return True
        except (AttributeError, TypeError):
            pass
        row = bl.get_extension(extension_id) if hasattr(bl, "get_extension") else None
        return bool(row)

    def _prepare_extension_cleanup(
        self,
        *,
        extension_id: str,
        account_id: str,
        plan_revision: str,
        bundle_roots: Iterable[Path],
    ) -> Path | None:
        from core.infrastructure.extension_runtime.bundle_cleanup import prepare_cleanup_intent
        from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root

        managed_root = Path(default_extensions_root()).resolve()
        managed_bundle_roots = [
            Path(item)
            for item in bundle_roots
            if Path(item).absolute() != managed_root
            and managed_root in Path(item).absolute().parents
        ]
        if not managed_bundle_roots:
            return None
        return prepare_cleanup_intent(
            extensions_root=managed_root,
            extension_id=extension_id,
            account_id=account_id,
            plan_revision=plan_revision,
            bundle_roots=managed_bundle_roots,
        )

    def _reconcile_extension_cleanup(
        self,
        *,
        bl: Any,
        intent_path: Path | None = None,
        committed_extension_id: str = "",
        discard_installed_intents: bool = False,
        discovered_extension_ids: Iterable[str] = (),
    ) -> Any:
        from core.infrastructure.extension_runtime.bundle_cleanup import reconcile_cleanup_journal
        from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root

        return reconcile_cleanup_journal(
            extensions_root=Path(default_extensions_root()),
            intent_path=intent_path,
            discard_installed_intents=discard_installed_intents,
            discovered_extension_ids=discovered_extension_ids,
            is_installed=lambda extension_id, account_id: (
                False
                if committed_extension_id and extension_id == committed_extension_id
                else self._extension_cleanup_is_installed(
                    bl=bl,
                    extension_id=extension_id,
                    account_id=account_id,
                )
            ),
        )

    def _rollback_failed_new_extension_lifecycle(
        self,
        *,
        bl: Any,
        extension_id: str,
        scope: ScopeContext | None,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
    ) -> None:
        resolved_scope = service_scope(scope)
        account_id = resolved_scope.account_id or DEFAULT_ACCOUNT_ID
        resolved_install_store = (
            install_store if install_store is not None else self._context.install_store(bl)
        )
        resolved_runtime_store = (
            runtime_store if runtime_store is not None else self._context.runtime_store(bl)
        )
        command = self._context.command_service(
            bl,
            install_store=resolved_install_store,
            runtime_store=resolved_runtime_store,
        )
        existing = (
            resolved_install_store.get(extension_id=extension_id, account_id=account_id)
            if hasattr(resolved_install_store, "get")
            else None
        )
        if existing is not None:
            command.rollback_install_release_failure(
                install_store=resolved_install_store,
                runtime_store=resolved_runtime_store,
                extension_id=extension_id,
                account_id=account_id,
                failed_install_id=str(getattr(existing, "install_id", "") or ""),
                previous=None,
            )
        if hasattr(resolved_runtime_store, "list_releases"):
            for release in resolved_runtime_store.list_releases(
                extension_id=extension_id, account_id=account_id
            ):
                command.cleanup_release_artifacts(release.release_id)

    def _cleanup_failed_simple_update_state(
        self,
        *,
        bl: Any,
        extension_id: str,
        failed_version: str,
    ) -> None:
        conn = getattr(bl, "conn", None)
        if conn is None:
            return
        normalized_version = str(failed_version or "").strip()
        if not normalized_version:
            return
        with contextlib.suppress(Exception), conn.cursor() as cur:
            cur.execute(
                """
                SELECT eb.id AS bundle_id, ev.id AS version_id
                FROM extension_bundles eb
                JOIN extension_versions ev ON ev.bundle_id = eb.id
                WHERE eb.extension_key = %s
                  AND ev.version = %s
                LIMIT 1
                """,
                (extension_id, normalized_version),
            )
            version_row = cur.fetchone()
            if isinstance(version_row, dict):
                version_id = str(version_row.get("version_id") or "").strip()
                bundle_id = str(version_row.get("bundle_id") or "").strip()
            elif isinstance(version_row, tuple):
                bundle_id = str(version_row[0] or "").strip()
                version_id = str(version_row[1] or "").strip()
            else:
                version_id = ""
                bundle_id = ""
            if version_id:
                cur.execute(
                    "DELETE FROM extension_releases WHERE release_id = %s",
                    (version_id,),
                )
                cur.execute(
                    "DELETE FROM extension_versions WHERE id = %s",
                    (version_id,),
                )
            if bundle_id:
                cur.execute(
                    """
                    UPDATE enabled_extensions
                    SET updated_at = now()
                    WHERE bundle_id = %s
                    """,
                    (bundle_id,),
                )

    def _purge_simple_extension_scan_state(self, *, bl: Any, extension_id: str) -> None:
        conn = getattr(bl, "conn", None)
        if conn is None:
            return
        with conn.cursor() as cur:
            if getattr(bl, "_table_exists", lambda *_args: False)("project_connections"):
                cur.execute(
                    """
                    DELETE FROM project_connections
                    WHERE extension_id = %s
                       OR extension_install_id IN (
                            SELECT install_id
                            FROM extension_installs
                            WHERE extension_id = %s
                       )
                    """,
                    (extension_id, extension_id),
                )
            if getattr(bl, "_table_exists", lambda *_args: False)(
                "extension_callback_registrations"
            ):
                cur.execute(
                    "DELETE FROM extension_callback_registrations WHERE extension_id = %s",
                    (extension_id,),
                )
            if getattr(bl, "_table_exists", lambda *_args: False)("llm_provider_sources"):
                cur.execute(
                    "DELETE FROM llm_provider_sources WHERE extension_id = %s", (extension_id,)
                )
            if getattr(bl, "_table_exists", lambda *_args: False)("extension_installs"):
                cur.execute(
                    "DELETE FROM extension_installs WHERE extension_id = %s", (extension_id,)
                )
            if getattr(bl, "_table_exists", lambda *_args: False)("extension_releases"):
                cur.execute(
                    "DELETE FROM extension_releases WHERE extension_id = %s AND source_kind = %s",
                    (extension_id, "extension_scan"),
                )
            cur.execute(
                "SELECT id FROM extension_bundles WHERE extension_key = %s LIMIT 1",
                (extension_id,),
            )
            row = cur.fetchone()
            if isinstance(row, dict):
                bundle_id = str(row.get("id") or "").strip()
            elif isinstance(row, tuple):
                bundle_id = str(row[0] or "").strip()
            else:
                bundle_id = ""
            if not bundle_id:
                return
            cur.execute("DELETE FROM enabled_extensions WHERE bundle_id = %s", (bundle_id,))
            cur.execute("DELETE FROM extension_versions WHERE bundle_id = %s", (bundle_id,))
            cur.execute("DELETE FROM extension_bundles WHERE id = %s", (bundle_id,))

    def _has_simple_extension_artifact(self, *, bl: Any, extension_id: str) -> bool:
        with contextlib.suppress(Exception):
            if self._managed_release.managed_simple_extension_root(extension_id).exists():
                return True
        conn = getattr(bl, "conn", None)
        if conn is None:
            return False
        with contextlib.suppress(Exception), conn.cursor() as cur:
            if getattr(bl, "_table_exists", lambda *_args: False)("extension_bundles"):
                cur.execute(
                    "SELECT 1 FROM extension_bundles WHERE extension_key = %s LIMIT 1",
                    (extension_id,),
                )
                if cur.fetchone():
                    return True
            if getattr(bl, "_table_exists", lambda *_args: False)("extension_releases"):
                cur.execute(
                    """
                    SELECT 1
                    FROM extension_releases
                    WHERE extension_id = %s
                      AND source_kind = %s
                    LIMIT 1
                    """,
                    (extension_id, "extension_scan"),
                )
                if cur.fetchone():
                    return True
        return False


__all__ = ["ExtensionCleanupService"]
