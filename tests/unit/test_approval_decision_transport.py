from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.application.management.errors import ServiceValidationError
from core.entrypoints.api import approval_decision_transport


class _FakeRequest:
    def __init__(
        self,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        chunk_size: int = 0,
    ) -> None:
        self.headers = headers or {"content-type": "application/json"}
        self.state = SimpleNamespace(auth_source="", auth_user=None)
        self._raw_body = raw_body if raw_body is not None else json.dumps(payload).encode()
        self._chunk_size = max(0, int(chunk_size))

    async def stream(self):
        chunk_size = self._chunk_size or len(self._raw_body) or 1
        for offset in range(0, len(self._raw_body), chunk_size):
            yield self._raw_body[offset : offset + chunk_size]


def _parse(request: _FakeRequest):
    return asyncio.run(approval_decision_transport.approval_decision_from_request(request))


def test_transport_parses_bounded_json_into_strict_decision() -> None:
    decision = _parse(_FakeRequest({"approval_id": "gate-1", "approved": False}))

    assert decision.approval_id == "gate-1"
    assert decision.approved is False


def test_transport_rejects_chunked_overflow_with_stable_413() -> None:
    limit = approval_decision_transport.MAX_APPROVAL_DECISION_BODY_BYTES
    request = _FakeRequest(None, raw_body=b"x" * (limit + 1), chunk_size=4096)

    with pytest.raises(ServiceValidationError) as captured:
        _parse(request)

    assert captured.value.status_code == 413
    assert captured.value.payload["code"] == "approval_payload_too_large"
