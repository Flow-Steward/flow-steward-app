from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.extensions.provider_install_binding import (
    resolve_install_bound_provider_extension,
)


def _store(*, connection: dict, resolved: dict) -> MagicMock:
    store = MagicMock()
    store.get_project_connection.return_value = connection
    store.get_extension_by_install_id.return_value = resolved
    return store


@pytest.mark.parametrize(
    ("connection_updates", "resolved_updates", "message"),
    [
        ({"project_id": "project-other"}, {}, "project scope mismatch"),
        ({"account_id": "account-other"}, {}, "account scope mismatch"),
        ({"extension_id": "extension.other"}, {}, "extension scope mismatch"),
        ({}, {"install_id": "install-other"}, "install identity mismatch"),
        ({}, {"extension_key": "extension.other"}, "extension identity mismatch"),
    ],
)
def test_install_bound_provider_resolution_rejects_scope_mismatches(
    connection_updates, resolved_updates, message
) -> None:
    connection = {
        "connection_id": "connection-notifier",
        "account_id": "account-1",
        "project_id": "project-9",
        "extension_id": "acme.notification-provider",
        "extension_install_id": "install-notifier",
        **connection_updates,
    }
    resolved = {
        "extension_key": "acme.notification-provider",
        "install_id": "install-notifier",
        **resolved_updates,
    }

    _raises_input_46_1 = _store(connection=connection, resolved=resolved)
    with pytest.raises(RuntimeError, match=message):
        resolve_install_bound_provider_extension(
            _raises_input_46_1,
            extension={"extension_key": "acme.notification-provider"},
            connection_id="connection-notifier",
            project_id="project-9",
            account_id="account-1",
        )
