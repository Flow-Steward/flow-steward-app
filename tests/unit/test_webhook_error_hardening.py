from __future__ import annotations

import json

from core.entrypoints.api.provider_webhooks import _default_api_error_response


def test_provider_webhook_default_error_response_does_not_disclose_exception() -> None:
    response = _default_api_error_response(RuntimeError("secret database DSN leaked"))

    assert response.status_code == 500
    assert json.loads(response.body) == {"error": "Request failed"}
