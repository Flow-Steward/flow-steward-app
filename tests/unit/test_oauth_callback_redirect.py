from core.entrypoints.api.integrations_request_helpers import _oauth_redirect_url


def test_oauth_redirect_inserts_callback_result_before_fragment() -> None:
    assert _oauth_redirect_url(
        "/extensions/fixture.extension-oauth?project_id=project-1#oauth",
        outcome="error",
        reason="missing_required_scope",
    ) == (
        "/extensions/fixture.extension-oauth"
        "?project_id=project-1&oauth=error&reason=missing_required_scope#oauth"
    )


def test_oauth_redirect_removes_untrusted_sync_marker_without_backend_outcome() -> None:
    assert (
        _oauth_redirect_url(
            "/registry/mcp/mcp_alpha?project_id=project-1&sync=success",
            outcome="success",
        )
        == "/registry/mcp/mcp_alpha?project_id=project-1&oauth=success"
    )
