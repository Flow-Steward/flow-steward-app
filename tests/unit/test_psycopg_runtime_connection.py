from unittest.mock import MagicMock, patch

from core.infrastructure.config.session_identity import session_application_name


def test_psycopg_runtime_disables_server_side_prepare_by_default():
    from core.infrastructure.db.psycopg_runtime import _connection

    raw_conn = MagicMock()
    with patch.object(_connection.psycopg, "connect", return_value=raw_conn) as mock_connect:
        adapter = _connection.connect("postgresql://example")

    assert adapter._conn is raw_conn
    mock_connect.assert_called_once_with(
        "postgresql://example",
        prepare_threshold=None,
        application_name=session_application_name(),
    )


def test_psycopg_runtime_preserves_explicit_prepare_threshold():
    from core.infrastructure.db.psycopg_runtime import _connection

    with patch.object(_connection.psycopg, "connect", return_value=MagicMock()) as mock_connect:
        _connection.connect("postgresql://example", prepare_threshold=10)

    mock_connect.assert_called_once_with(
        "postgresql://example",
        prepare_threshold=10,
        application_name=session_application_name(),
    )
