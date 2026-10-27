from __future__ import annotations

import ast
import inspect
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.application.extensions.binding_service import ExtensionBindingService
from core.application.extensions.managed_release_service import ManagedReleaseService
from core.application.extensions.uninstall_cleanup_queries import (
    ExtensionUninstallCleanupQueries,
)
from core.application.extensions.uninstall_inventory_repository import (
    ExtensionUninstallInventoryRepository,
    _connection_rows,
)
from core.application.extensions.uninstall_models import RemovalTransactionState
from core.application.extensions.uninstall_persistence_service import (
    ExtensionUninstallPersistenceService,
)
from core.application.extensions.uninstall_service import ExtensionUninstallService
from core.application.extensions.uninstall_target_removal import remove_resolved_uninstall_target
from core.application.management.errors import (
    ServiceConflictError,
    ServicePreconditionError,
    ServiceValidationError,
)

_EXTENSIONS = Path(__file__).parents[2] / "core" / "application" / "extensions"


def test_binding_service_constructs_explicit_uninstall_components() -> None:
    source = inspect.getsource(ExtensionBindingService.__init__)

    assert "ExtensionUninstallInventoryRepository(" in source
    assert "inventory=self._uninstall_inventory_repository" in source
    assert "ExtensionUninstallPersistenceService(" in source
    assert "persistence=self._uninstall_persistence_service" in source


def test_uninstall_components_expose_typed_narrow_entrypoints() -> None:
    inventory_signature = inspect.signature(ExtensionUninstallInventoryRepository.collect)
    persistence_signature = inspect.signature(
        ExtensionUninstallPersistenceService.remove_installed_target
    )

    assert inventory_signature.return_annotation != inspect.Signature.empty
    assert persistence_signature.return_annotation != inspect.Signature.empty


@pytest.mark.parametrize("claimed_origin", ["archive", "system", "official"])
def test_read_only_archive_root_blocks_every_extension_before_removal_mutations(
    claimed_origin: str,
) -> None:
    """No origin earns a disable-in-place bypass: every extension uninstalls the same way."""
    context = SimpleNamespace(extension_id="acme.mail", account_id="account-1")
    target = SimpleNamespace(extension={"origin": claimed_origin}, install_id="install-acme")
    persistence = MagicMock()
    reconcile = MagicMock()
    delete_runtime_records = MagicMock()
    install_store = MagicMock()
    plan = MagicMock()
    unavailable = ServicePreconditionError("archive root is read-only")
    require_archive_mutation_available = MagicMock(side_effect=unavailable)

    with pytest.raises(ServicePreconditionError) as captured:
        remove_resolved_uninstall_target(
            context,
            target,
            persistence=persistence,
            reconcile_committed_bundle_cleanup=reconcile,
            delete_runtime_install_records=delete_runtime_records,
            install_store=install_store,
            plan=plan,
            require_archive_mutation_available=require_archive_mutation_available,
        )

    assert captured.value is unavailable
    require_archive_mutation_available.assert_called_once_with()
    persistence.remove_installed_target.assert_not_called()
    reconcile.assert_not_called()
    delete_runtime_records.assert_not_called()
    install_store.uninstall.assert_not_called()
    plan.apply_confirmed_uninstall_affected_state.assert_not_called()


def test_uninstall_allows_only_preserved_workflow_dependents() -> None:
    workflow = {
        "kind": "workflow",
        "id": "fixture-document-sync",
        "action": "preserve_and_disable",
    }

    ExtensionUninstallService._reject_required_dependents_in_plan(
        {"affected": [workflow]},
        extension_id="fixture.extension-oauth",
    )

    with pytest.raises(ServiceValidationError, match="required dependents"):
        ExtensionUninstallService._reject_required_dependents_in_plan(
            {
                "affected": [
                    workflow,
                    {
                        "kind": "dataset",
                        "id": "fixture-owned-dataset",
                        "action": "preserve_and_disable",
                    },
                ]
            },
            extension_id="fixture.extension-oauth",
        )


def test_uninstall_inventory_reads_exact_connections_from_production_store_shape() -> None:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [
        ("fixture-invoices", "account-1", "project-1", "Fixture invoices", {"refs": {}}),
        ("fixture-legacy", "account-1", "project-1", "Fixture legacy", {"refs": {}}),
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    store = SimpleNamespace(
        conn=connection,
        _table_exists=lambda table: table == "project_connections",
    )

    rows = _connection_rows(
        store,
        account_id="account-1",
        extension_id="fixture.extension-oauth",
        install_id="install-fixture",
        include_consumer_accounts=False,
    )

    assert [row["connection_id"] for row in rows] == ["fixture-invoices", "fixture-legacy"]
    sql = " ".join(str(cursor.execute.call_args.args[0]).split())
    assert "extension_install_id = %s" in sql
    assert "extension_install_id IS NULL" in sql
    assert cursor.execute.call_args.args[1] == (
        "account-1",
        "install-fixture",
        "install-fixture",
        "fixture.extension-oauth",
    )


def test_uninstall_workflow_modules_and_functions_stay_reviewable() -> None:
    module_names = (
        "uninstall_inventory_repository.py",
        "uninstall_persistence_service.py",
        "uninstall_plan_service.py",
        "uninstall_service.py",
    )

    for module_name in module_names:
        source = (_EXTENSIONS / module_name).read_text(encoding="utf-8")
        assert len(source.splitlines()) <= 500, module_name
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno is not None
                length = node.end_lineno - node.lineno + 1
                assert length <= 80, f"{module_name}:{node.name} is {length} lines"


def test_uninstall_inventory_lists_owning_account_legacy_extension_connection() -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.statement = ""
            self.description: tuple[()] = ()

        def execute(self, statement: str, _params: tuple[str, ...]) -> None:
            self.statement = statement

        def fetchall(self) -> list[dict[str, object]]:
            if "extension_id = %s" not in self.statement:
                return []
            return [
                {
                    "connection_id": "conn_d8a1d58a13394087",
                    "account_id": "account-default",
                    "project_id": "project-browserqa",
                    "display_name": "Browser review mailbox",
                    "secret_refs": {
                        "refs": {
                            "password": "project_connection:conn_d8a1d58a13394087:password"  # pragma: allowlist secret
                        }  # pragma: allowlist secret
                    },
                }
            ]

    class _Store:
        def __init__(self) -> None:
            self.conn = object()
            self.cursor_instance = _Cursor()

        def _table_exists(self, table_name: str) -> bool:
            return table_name == "project_connections"

        @contextmanager
        def cursor(self):
            yield self.cursor_instance

    remove = ExtensionUninstallInventoryRepository()._collect_removals(
        bl=_Store(),
        extension_id="acme.mailbox",
        install_id="pi_imap_mailbox",
        release_id="pr_imap_mailbox",
        account_id="account-default",
        include_consumer_accounts=False,
        projections=[],
    )

    assert {
        "kind": "project_connection",
        "id": "conn_d8a1d58a13394087",
        "display_name": "Browser review mailbox",
        "project_id": "project-browserqa",
        "count": 1,
    } in remove
    assert {
        "kind": "connection_credential_refs",
        "id": "conn_d8a1d58a13394087:credentials",
        "display_name": "Browser review mailbox",
        "count": 1,
    } in remove


def test_global_uninstall_inventory_includes_consumers_and_owner_legacy_connection() -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.statement = ""
            self.params: tuple[str, ...] = ()
            self.description: tuple[()] = ()

        def execute(self, statement: str, params: tuple[str, ...]) -> None:
            self.statement = statement
            self.params = params

        def fetchall(self) -> list[dict[str, object]]:
            if "extension_install_id IS NULL" not in self.statement:
                return []
            return [
                {
                    "connection_id": "conn_consumer",
                    "account_id": "account-consumer",
                    "project_id": "project-consumer",
                    "display_name": "Consumer mailbox",
                    "secret_refs": {
                        "refs": {
                            "password": "project_connection:conn_consumer:password"  # pragma: allowlist secret
                        }  # pragma: allowlist secret
                    },
                },
                {
                    "connection_id": "conn_owner_legacy",
                    "account_id": "account-owner",
                    "project_id": "project-owner",
                    "display_name": "Owner mailbox",
                    "secret_refs": {
                        "refs": {
                            "password": "project_connection:conn_owner_legacy:password"  # pragma: allowlist secret
                        }  # pragma: allowlist secret
                    },
                },
            ]

    class _Store:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def _table_exists(self, table_name: str) -> bool:
            return table_name == "project_connections"

        @contextmanager
        def cursor(self):
            yield self.cursor_instance

    store = _Store()
    remove = ExtensionUninstallInventoryRepository()._collect_removals(
        bl=store,
        extension_id="acme.mailbox",
        install_id="pi_imap_mailbox",
        release_id="pr_imap_mailbox",
        account_id="account-owner",
        include_consumer_accounts=True,
        projections=[],
    )

    assert store.cursor_instance.params == (
        "pi_imap_mailbox",
        "account-owner",
        "acme.mailbox",
    )
    assert [item for item in remove if item["kind"] == "consumer_runtime_connections"] == [
        {
            "kind": "consumer_runtime_connections",
            "id": "pi_imap_mailbox",
            "display_name": "Consumer project connections",
            "count": 2,
        }
    ]
    assert [item for item in remove if item["kind"] == "consumer_connection_credential_refs"] == [
        {
            "kind": "consumer_connection_credential_refs",
            "id": "pi_imap_mailbox",
            "display_name": "Consumer connection credentials",
            "count": 2,
        }
    ]


def test_uninstall_inventory_lists_exact_saved_workflow_dependents() -> None:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [
        {
            "account_id": "account-1",
            "workflow_id": "fixture_document_sync",
            "display_name": "Fixture document sync",
            "dependency_ref": "search_documents",
        }
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    store = SimpleNamespace(
        conn=connection,
        _table_exists=lambda table: (
            table in {"workflow_runtime_dependencies", "registry_workflows"}
        ),
    )

    affected = ExtensionUninstallInventoryRepository().collect_affected(
        bl=store,
        extension_id="fixture.extension-oauth",
        install_id="install-fixture",
        account_id="account-1",
        include_consumer_accounts=False,
        runtime_store=SimpleNamespace(list_projections=lambda **_kwargs: []),
        install_store=MagicMock(),
    )

    assert affected == [
        {
            "kind": "workflow",
            "id": "fixture_document_sync",
            "display_name": "Fixture document sync",
            "owner_install_id": "",
            "owner_account_id": "account-1",
            "action": "preserve_and_disable",
            "reason": (
                "Uses fixture.extension-oauth action 'search_documents'; this workflow will stop "
                "until the extension is reinstalled or the step is replaced."
            ),
        }
    ]
    sql = " ".join(str(cursor.execute.call_args.args[0]).split())
    assert "workflow_runtime_dependencies" in sql
    assert "d.install_id = %s" in sql
    assert cursor.execute.call_args.args[1] == (
        False,
        "account-1",
        "install-fixture",
    )


def test_uninstall_inventory_recovers_legacy_step_through_exact_connection() -> None:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.side_effect = [
        [],
        [
            {
                "account_id": "account-1",
                "workflow_id": "legacy_stock_drive_sync",
                "display_name": "Legacy Stock Drive sync",
                "dependency_ref": "search_drive_files",
            }
        ],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    store = SimpleNamespace(
        conn=connection,
        _table_exists=lambda table: (
            table
            in {
                "workflow_runtime_dependencies",
                "registry_workflows",
                "registry_workflow_steps",
                "project_connections",
            }
        ),
    )

    affected = ExtensionUninstallInventoryRepository().collect_affected(
        bl=store,
        extension_id="fixture.extension-oauth",
        install_id="install-fixture",
        account_id="account-1",
        include_consumer_accounts=False,
        runtime_store=SimpleNamespace(list_projections=lambda **_kwargs: []),
        install_store=MagicMock(),
    )

    assert [item["id"] for item in affected] == ["legacy_stock_drive_sync"]
    legacy_sql = " ".join(str(cursor.execute.call_args.args[0]).split())
    assert "s.connector_id = %s" in legacy_sql
    assert "FROM registry_workflow_steps s" in legacy_sql
    assert "JOIN registry_workflows w ON w.workflow_id = s.owner_workflow_id" in legacy_sql
    assert "d.step_id = s.step_id" in legacy_sql
    assert "phase" not in legacy_sql
    assert "c.extension_install_id = %s" in legacy_sql
    assert "c.extension_id = %s" in legacy_sql
    assert cursor.execute.call_args.args[1] == (
        "extension.fixture.extension-oauth",
        False,
        "account-1",
        "install-fixture",
        "fixture.extension-oauth",
    )


def test_last_install_removes_every_unreferenced_release_for_extension(monkeypatch) -> None:
    queries = ExtensionUninstallCleanupQueries()
    context = MagicMock()
    context.extension_id = "acme.demo"
    context.bl._table_exists.return_value = True
    state = RemovalTransactionState(
        install_id="install-current",
        release_id="release-current",
        remove_shared_runtime_package=True,
        affected_state_persisted=False,
    )
    cursor = MagicMock()
    cursor.rowcount = 2
    monkeypatch.setattr(queries, "_cleanup_install_record", lambda *_args: True)
    monkeypatch.setattr(queries, "_cleanup_shared_runtime_rows", lambda *_args: None)
    removed_data: list[str] = []

    queries.cleanup_runtime_records(
        context=context,
        state=state,
        cur=cursor,
        removed_data=removed_data,
    )

    sql, params = cursor.execute.call_args.args
    assert "er.extension_id = %s" in sql
    assert params == ("acme.demo",)
    assert removed_data == ["release_runtime_records"]


def test_extension_uninstall_blocks_live_projected_workflow(monkeypatch) -> None:
    queries = ExtensionUninstallCleanupQueries()
    context = MagicMock()
    context.account_id = "account-default"
    context.bl._table_exists.return_value = True
    cursor = MagicMock()
    cursor.fetchall.return_value = [{"workflow_id": "extension.acme.demo.workflow"}]
    conflict = ServiceConflictError("Workflow has non-terminal dependent jobs")
    guard = MagicMock(side_effect=conflict)
    monkeypatch.setattr(
        "core.application.workflows.workflow_deletion_guard."
        "assert_workflow_has_no_nonterminal_jobs",
        guard,
    )

    with pytest.raises(ServiceConflictError):
        queries._cleanup_workflow_projections(context, "install-1", cursor, [])

    guard.assert_called_once_with(
        context.bl,
        workflow_id="extension.acme.demo.workflow",
        account_id="account-default",
        cursor=cursor,
    )
    assert not any(
        "DELETE FROM registry_workflows" in call.args[0] for call in cursor.execute.call_args_list
    )


def test_last_install_cleanup_includes_all_managed_release_directories(tmp_path: Path) -> None:
    active = tmp_path / "extensions" / "acme_demo"
    releases = tmp_path / "extensions" / ".releases" / "acme_demo"
    old_release = releases / "1.0.0-old"
    current_release = releases / "2.0.0-current"
    old_release.mkdir(parents=True)
    current_release.mkdir()
    active.symlink_to(current_release, target_is_directory=True)
    cleanup = MagicMock()
    managed_release = MagicMock()
    managed_release.managed_simple_extension_root.return_value = active
    managed_release.validated_managed_simple_extension_cleanup_directory.side_effect = (
        lambda *, extension_id, bundle_root: bundle_root
    )
    managed_release.validated_managed_simple_extension_release_directory.side_effect = (
        lambda *, extension_id, release_root: release_root
    )
    service = ExtensionUninstallPersistenceService(
        context=MagicMock(),
        cleanup=cleanup,
        managed_release=managed_release,
        plan=MagicMock(),
    )
    context = MagicMock(
        extension_id="acme.demo",
        account_id="account-default",
        plan_revision="revision-1",
    )

    service._prepare_bundle_cleanup(context, current_release, True)

    roots = cleanup.prepare_extension_cleanup.call_args.kwargs["bundle_roots"]
    assert roots == [active, old_release, current_release]


def _bundle_cleanup_service(active: Path) -> tuple[ExtensionUninstallPersistenceService, Any]:
    cleanup = MagicMock()
    managed_release = MagicMock()
    managed_release.managed_simple_extension_root.return_value = active
    managed_release.validated_managed_simple_extension_release_directory.side_effect = (
        lambda *, extension_id, release_root: release_root
    )
    service = ExtensionUninstallPersistenceService(
        context=MagicMock(),
        cleanup=cleanup,
        managed_release=managed_release,
        plan=MagicMock(),
    )
    return service, cleanup


_CLEANUP_CONTEXT = {
    "extension_id": "acme.demo",
    "account_id": "account-default",
    "plan_revision": "revision-1",
}


def test_plain_folder_left_by_an_older_build_is_removed_with_the_install(tmp_path: Path) -> None:
    """Left in place, the next scan registered the folder again (seen with Ollama)."""
    active = tmp_path / "extensions" / "acme_demo"
    sibling = tmp_path / "extensions" / "other_extension"
    active.mkdir(parents=True)
    (active / ".fs-python" / "3.12").mkdir(parents=True)
    sibling.mkdir()
    service, cleanup = _bundle_cleanup_service(active)

    intent, notes = service._prepare_bundle_cleanup(MagicMock(**_CLEANUP_CONTEXT), active, True)

    assert cleanup.prepare_extension_cleanup.call_args.kwargs["bundle_roots"] == [active]
    assert intent is cleanup.prepare_extension_cleanup.return_value
    assert notes == ()


def test_uninstall_does_not_need_the_extension_files(tmp_path: Path) -> None:
    """A lost volume or a manual cleanup must not trap the install record."""
    active = tmp_path / "extensions" / "acme_demo"
    active.parent.mkdir(parents=True)
    service, cleanup = _bundle_cleanup_service(active)

    _intent, notes = service._prepare_bundle_cleanup(
        MagicMock(**_CLEANUP_CONTEXT), active / "missing-release", True
    )

    assert cleanup.prepare_extension_cleanup.call_args.kwargs["bundle_roots"] == []
    assert notes == ("extension_files_already_missing",)


def test_a_dangling_release_link_is_removed_without_following_it(tmp_path: Path) -> None:
    active = tmp_path / "extensions" / "acme_demo"
    active.parent.mkdir(parents=True)
    active.symlink_to(tmp_path / "extensions" / ".releases" / "acme_demo" / "gone")
    service, cleanup = _bundle_cleanup_service(active)

    _intent, notes = service._prepare_bundle_cleanup(MagicMock(**_CLEANUP_CONTEXT), None, True)

    assert cleanup.prepare_extension_cleanup.call_args.kwargs["bundle_roots"] == [active]
    assert notes == ()


def test_a_git_checkout_in_the_extensions_root_is_kept(tmp_path: Path) -> None:
    active = tmp_path / "extensions" / "acme_demo"
    (active / ".git").mkdir(parents=True)
    service, cleanup = _bundle_cleanup_service(active)

    _intent, notes = service._prepare_bundle_cleanup(MagicMock(**_CLEANUP_CONTEXT), active, True)

    assert cleanup.prepare_extension_cleanup.call_args.kwargs["bundle_roots"] == []
    assert notes == ("extension_folder_kept_git_checkout",)


def test_shared_runtime_package_keeps_its_files(tmp_path: Path) -> None:
    active = tmp_path / "extensions" / "acme_demo"
    active.mkdir(parents=True)
    service, cleanup = _bundle_cleanup_service(active)

    assert service._prepare_bundle_cleanup(MagicMock(**_CLEANUP_CONTEXT), active, False) == (
        None,
        (),
    )
    cleanup.prepare_extension_cleanup.assert_not_called()


def test_failed_remote_revoke_cannot_undo_committed_uninstall_cleanup() -> None:
    revocations = [
        {
            "endpoint": "https://oauth.example/revoke",
            "data": {"token": "synthetic-token", "client_id": "synthetic-client"},
        }
    ]

    with patch(
        "core.application.auth.oauth_client.execute_prepared_oauth_revocation",
        side_effect=RuntimeError("provider unavailable"),
    ) as execute_revoke:
        ExtensionUninstallPersistenceService._execute_oauth_revocations(revocations)

    execute_revoke.assert_called_once_with(revocations[0])


def test_callback_cleanup_inventories_global_consumer_registrations_and_owned_secrets() -> None:
    service = ExtensionUninstallPersistenceService(
        context=MagicMock(),
        cleanup=MagicMock(),
        managed_release=MagicMock(),
        plan=MagicMock(),
    )
    context = MagicMock(extension_id="acme.oauth", account_id="owner-account")
    context.bl._table_exists.side_effect = lambda table: (
        table
        in {
            "extension_callback_registrations",
            "project_secrets",
        }
    )
    target = MagicMock(include_consumer_accounts=True)
    state = MagicMock(install_id="install-1")
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("11111111-1111-4111-8111-111111111111", "consumer-project"),
        ("22222222-2222-4222-8222-222222222222", "owner-project"),
    ]
    removed_data: list[str] = []

    service._queries._cleanup_callbacks(context, target, state, cursor, removed_data)

    calls = cursor.execute.call_args_list
    select_sql, select_params = calls[0].args
    assert "SELECT c.id, c.project_id" in select_sql
    assert "surviving_install.account_id = p.account_id" in select_sql
    assert "owned_connection.extension_install_id = %s" in select_sql
    assert select_params == ("acme.oauth", "install-1", "install-1")
    secret_calls = [call for call in calls if "DELETE FROM project_secrets" in call.args[0]]
    assert len(secret_calls) == 2
    assert secret_calls[0].args[1] == (
        "consumer-project",
        [
            "extension_callback:11111111-1111-4111-8111-111111111111:capability_token",
            "extension_callback:11111111-1111-4111-8111-111111111111:shared_secret",
        ],
    )
    registration_delete = next(
        call for call in calls if "DELETE FROM extension_callback_registrations" in call.args[0]
    )
    assert registration_delete.args[1] == (
        [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ],
        "acme.oauth",
    )


def test_cleanup_validator_accepts_exact_physical_direct_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    active = tmp_path / "extensions" / "acme_demo"
    active.mkdir(parents=True)
    sibling = active.parent / "other_extension"
    sibling.mkdir()
    service = ManagedReleaseService(context=MagicMock())
    monkeypatch.setattr(service, "_managed_simple_extension_root", lambda _extension_id: active)

    assert (
        service.validated_managed_simple_extension_cleanup_directory(
            extension_id="acme.demo",
            bundle_root=active,
        )
        == active
    )
    with pytest.raises(ServiceValidationError, match="active release path is invalid"):
        service.validated_managed_simple_extension_cleanup_directory(
            extension_id="acme.demo",
            bundle_root=sibling,
        )
