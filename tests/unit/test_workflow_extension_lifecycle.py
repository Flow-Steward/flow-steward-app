from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.application.extensions.workflow_install_contract import (
    resolve_workflow_extension_install_contract,
)


def test_disabled_extension_install_contract_remains_resolvable_for_saved_workflow_step() -> None:
    install = SimpleNamespace(
        install_id="install-fixture",
        release_id="release-fixture",
        extension_id="fixture.extension-oauth",
        account_id="account-1",
        visibility="account",
        state="disabled",
    )
    release = SimpleNamespace(
        extension_id="fixture.extension-oauth",
        descriptor_sha256="a" * 64,
        descriptor={
            "extension_contract_v2_normalized": {"operations": []},
            "action_definitions": [{"action_id": "search_records"}],
        },
    )
    install_store = MagicMock()
    install_store.list_for_account.return_value = [install]
    runtime_store = SimpleNamespace(get_release=lambda _release_id: release)

    with (
        patch(
            "core.application.extensions.install.DBExtensionInstallStore",
            return_value=install_store,
        ) as install_store_factory,
        patch(
            "core.application.extensions.runtime_store_db.DBExtensionRuntimeStore",
            return_value=runtime_store,
        ),
    ):
        contract = resolve_workflow_extension_install_contract(
            object(),
            install_id="install-fixture",
            account_id="account-1",
            default_account_id="account-default",
        )

    assert contract is not None
    assert contract["install_state"] == "disabled"
    assert contract["release_id"] == "release-fixture"
    assert contract["descriptor_sha256"] == "a" * 64
    assert contract["action_definitions"] == [{"action_id": "search_records"}]
    assert (
        install_store_factory.return_value.list_for_account.call_args.kwargs[
            "include_inactive_owned"
        ]
        is True
    )
