"""Unit tests for HTTP request step auth secret resolution."""

from __future__ import annotations

import gzip
import json
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Thread
from unittest.mock import patch

import httpx
import pytest


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_test_mode_http_writes_reach_a_real_local_recorder(method: str):
    """Characterize effects, not SSRF: only this test's loopback URL is admitted."""
    from core.application.orchestration.http_request_step_runtime import (
        execute_http_request_step,
    )
    from core.infrastructure.runtime.test_mode import (
        reset_runtime_test_mode,
        set_runtime_test_mode,
    )

    recorded: Queue[tuple[str, str, object]] = Queue()

    class Recorder(BaseHTTPRequestHandler):
        def record(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            recorded.put((self.command, self.path, json.loads(body)))
            response = b'{"recorded":true}'
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_POST = record
        do_PATCH = record

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/synthetic-write"

    def allow_recorder_only(candidate):
        if candidate != url:
            raise AssertionError("Only the disposable recorder URL is permitted")
        return candidate

    token = set_runtime_test_mode(True)
    try:
        with (
            patch(
                "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
                side_effect=allow_recorder_only,
            ),
            patch(
                "core.application.orchestration.http_request_step_runtime._SSRFGuardedHTTPTransport",
                httpx.HTTPTransport,
            ),
            patch(
                "core.application.extensions.lifecycle._is_blocked_ip",
                side_effect=lambda address: address != "127.0.0.1",
            ),
        ):
            result = execute_http_request_step(
                config={
                    "method": method,
                    "url": url,
                    "body_template": {"source": "state.inputs.payload"},
                    "expected_statuses": [201],
                    "timeout_seconds": 2,
                },
                state={"test_mode": True, "inputs": {"payload": {"message": "synthetic"}}},
            )
        assert recorded.get(timeout=2) == (
            method,
            "/synthetic-write",
            {"message": "synthetic"},
        )
        assert recorded.empty()
        assert result["ok"] is True
        assert result["status_code"] == 201
        assert result["response_body"] == {"recorded": True}
        assert result["external_effect_status"] == "succeeded"
    finally:
        reset_runtime_test_mode(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/{{ state.rows[abc].sku | default:'x' }}",
        "https://example.test/{{ rows[abc].sku | default:'x' }}",
    ],
)
def test_malformed_template_selector_fails_before_http_client_is_created(url: str):
    from core.application.orchestration.http_request_step_runtime import (
        execute_http_request_step,
    )

    with patch(
        "core.application.orchestration.http_request_step_runtime.httpx.Client",
        side_effect=AssertionError("network client must not be created"),
    ) as client:
        result = execute_http_request_step(
            config={
                "method": "GET",
                "url": url,
                "response_mode": "inline",
                "response_target": "state.response",
                "timeout_seconds": 5,
                "expected_statuses": [200],
            },
            state={"rows": [{"sku": "A1"}]},
        )

    assert result["ok"] is False
    assert result["failure_reason"] == "invalid_input_selector"
    assert result["selector"] == "state.rows[abc].sku"
    assert result["path"] == "override.http_request.url"
    client.assert_not_called()


def test_render_value_resolves_state_source_reference_objects():
    from core.application.orchestration.http_request_step_runtime import _render_value

    state = {
        "inputs": {
            "payload": {"title": "Hello", "tags": ["seo", "cms"]},
            "method": "patch",
        }
    }

    assert _render_value({"source": "state.inputs.payload"}, state) == {
        "title": "Hello",
        "tags": ["seo", "cms"],
    }
    assert _render_value({"body": {"source": "state.inputs.payload"}}, state) == {
        "body": {"title": "Hello", "tags": ["seo", "cms"]},
    }


def test_bearer_auth_uses_secret_resolver_before_env():
    from core.application.orchestration.http_request_step_runtime import (
        _resolve_auth_headers_and_object,
    )

    with patch(
        "core.application.orchestration.http_request_step_runtime._resolve_secret_ref_value",
        return_value="token-from-secret",
    ) as resolve_ref:
        headers, auth_obj = _resolve_auth_headers_and_object(
            auth_mode="bearer",
            auth_ref="project_api_token",
            state={"project_config": {"project_id": 7}},
        )

    assert headers["Authorization"] == "Bearer token-from-secret"
    assert auth_obj is None
    resolve_ref.assert_called_once_with("project_api_token", {"project_config": {"project_id": 7}})


def test_basic_auth_uses_secret_resolver_for_user_and_password():
    from core.application.orchestration.http_request_step_runtime import (
        _resolve_auth_headers_and_object,
    )

    def _side_effect(ref: str, _state: dict):
        return {"api_user_ref": "alice", "api_pass_ref": "secret"}.get(ref, "")

    with patch(
        "core.application.orchestration.http_request_step_runtime._resolve_secret_ref_value",
        side_effect=_side_effect,
    ):
        headers, auth_obj = _resolve_auth_headers_and_object(
            auth_mode="basic",
            auth_ref="api_user_ref:api_pass_ref",
            state={},
        )

    assert headers == {}
    assert isinstance(auth_obj, httpx.BasicAuth)


def test_secret_ref_value_reads_project_secret_before_env():
    from core.application.orchestration.http_request_step_runtime import _resolve_secret_ref_value

    class _Logger:
        def get_project_secret(self, *, project_id: str, secret_key: str):
            if project_id == "11" and secret_key == "auth_ref":
                return "secret-from-db"
            return ""

    with (
        patch(
            "core.application.orchestration.http_request_step_runtime._get_shared_logger",
            return_value=_Logger(),
        ),
        patch.dict("os.environ", {"auth_ref": "secret-from-env"}, clear=False),
    ):
        value = _resolve_secret_ref_value("auth_ref", {"project_config": {"project_id": 11}})

    assert value == "secret-from-db"


def test_secret_ref_value_rejects_explicit_env_reference():
    from core.application.orchestration.http_request_step_runtime import _resolve_secret_ref_value

    with patch.dict("os.environ", {"FS_TEST_VALIDATION_SECRET": "proof-secret"}, clear=False):
        try:
            _resolve_secret_ref_value(
                "env:FS_TEST_VALIDATION_SECRET", {"project_config": {"project_id": 11}}
            )
        except ValueError as exc:
            assert "env:" in str(exc)
        else:
            raise AssertionError("env auth references must be rejected")


def test_secret_ref_value_does_not_fallback_to_process_env():
    from core.application.orchestration.http_request_step_runtime import _resolve_secret_ref_value

    class _Logger:
        def get_project_secret(self, *, project_id: str, secret_key: str):
            assert project_id == "11"
            assert secret_key == "auth_ref"
            return ""

    with (
        patch(
            "core.application.orchestration.http_request_step_runtime._get_shared_logger",
            return_value=_Logger(),
        ),
        patch.dict("os.environ", {"auth_ref": "secret-from-env"}, clear=False),
    ):
        value = _resolve_secret_ref_value("auth_ref", {"project_config": {"project_id": 11}})

    assert value == ""


def test_execute_http_request_step_rejects_env_auth_before_network():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client") as client,
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
        patch.dict("os.environ", {"FS_TEST_VALIDATION_SECRET": "proof-secret"}, clear=False),
    ):
        try:
            execute_http_request_step(
                config={
                    "url": "https://api.example.test/resource",
                    "auth_mode": "bearer",
                    "auth_ref": "env:FS_TEST_VALIDATION_SECRET",
                },
                state={"project_config": {"project_id": 7}},
            )
        except ValueError as exc:
            assert "env:" in str(exc)
        else:
            raise AssertionError("env auth references must be rejected")

    client.assert_not_called()


def test_http_request_step_rejects_disallowed_response_peer():
    import httpx

    from core.application.orchestration.http_request_step_runtime import (
        _reject_blocked_response_peer,
    )

    class _Stream:
        def get_extra_info(self, key):
            return ("169.254.169.254", 80) if key == "peername" else None

    request = httpx.Request("GET", "http://example.com/")
    response = httpx.Response(200, request=request, extensions={"network_stream": _Stream()})

    with pytest.raises(httpx.ConnectError):
        _reject_blocked_response_peer(response)


def test_execute_http_request_step_renders_method_template_and_source_body():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    captured: dict[str, object] = {}

    class _Response:
        status_code = 201
        headers = {"content-type": "application/json"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b'{"ok": true, "id": "post-1"}'

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **kwargs):
            captured.update(kwargs)
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "method": "{{ state.inputs.method | default('POST') }}",
                "url": "https://example.test/posts",
                "headers": {"Content-Type": "application/json"},
                "body_template": {"source": "state.inputs.payload"},
                "expected_statuses": [200, 201],
                "response_target": "state.external_response",
            },
            state={
                "inputs": {
                    "method": "patch",
                    "payload": {"title": "Hello", "tags": ["seo", "cms"]},
                }
            },
        )

    assert captured["method"] == "PATCH"
    assert captured["json"] == {"title": "Hello", "tags": ["seo", "cms"]}
    assert result["ok"] is True
    assert result["status_code"] == 201


def test_request_body_template_reads_only_canonical_key() -> None:
    from core.application.orchestration.http_request_step_runtime import (
        _request_body_template,
    )

    assert _request_body_template({"body_template": {"canonical": True}}) == {"canonical": True}
    assert _request_body_template({"body": "legacy", "json": {"legacy": True}}) is None


def test_execute_http_request_step_rejects_localhost_before_resolving_auth_secret():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    with (
        patch(
            "core.application.orchestration.http_request_step_runtime._resolve_secret_ref_value",
            return_value="should-not-resolve",
        ) as resolve_secret,
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client") as client,
    ):
        try:
            execute_http_request_step(
                config={
                    "url": "http://127.0.0.1:8080/internal",
                    "auth_mode": "bearer",
                    "auth_ref": "api_token",
                },
                state={"project_config": {"project_id": 7}},
            )
        except ValueError as exc:
            assert "http_request step url" in str(exc)
        else:
            raise AssertionError("localhost URL should be rejected")

    resolve_secret.assert_not_called()
    client.assert_not_called()


def test_execute_http_request_step_redacts_authorization_from_result_and_disables_redirects():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    captured: dict[str, object] = {}
    client_options: dict[str, object] = {}

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b'{"ok": true}'

    class _Client:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **kwargs):
            captured.update(kwargs)
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
        patch(
            "core.application.orchestration.http_request_step_runtime._resolve_secret_ref_value",
            return_value="secret-token",
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://api.example.test/resource",
                "auth_mode": "bearer",
                "auth_ref": "api_token",
            },
            state={"project_config": {"project_id": 7}},
        )

    assert client_options["follow_redirects"] is False
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert result["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert "secret-token" not in str(result["request"])


def test_execute_http_request_step_inline_overflow_returns_response_too_large():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 200
        headers = {"content-type": "text/plain"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b"abc"
            yield b"def"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/big",
                "max_response_bytes": 5,
                "expected_statuses": [200],
            },
            state={},
        )

    assert result["ok"] is False
    assert result["failure_reason"] == "response_too_large"
    assert result["response_body"] == ""


def test_execute_http_request_step_artifact_streams_decoded_body_without_get():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class _NoGetStorage(InMemoryObjectStorage):
        def get(self, key: str):
            if str(key).endswith("/meta.json"):
                return super().get(key)
            raise AssertionError("artifact body must not be read back")

    class _Response:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b"\x00\x01"
            yield b"file"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    plane = ArtifactDatasetPlane(storage=_NoGetStorage())
    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/download.bin",
                "response_mode": "artifact",
                "expected_statuses": [200],
            },
            state={"_artifact_plane": plane, "project_id": 7, "job_id": "job_1"},
        )

    assert result["ok"] is True
    assert result["artifact_handle"].startswith("artifact:art_")
    assert result["artifact_id"].startswith("art_")
    assert result["filename"] == "download.bin"
    assert result["size_bytes"] == 6
    assert "response_body" not in result


def test_execute_http_request_step_gzip_expansion_is_bounded_before_large_decoded_chunk():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    compressed = gzip.compress(b"x" * (128 * 1024))
    observed_raw_chunk_sizes: list[int] = []

    class _Response:
        status_code = 200
        headers = {"content-type": "text/plain", "content-encoding": "gzip"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            step = int(chunk_size or len(compressed))
            for idx in range(0, len(compressed), step):
                chunk = compressed[idx : idx + step]
                observed_raw_chunk_sizes.append(len(chunk))
                yield chunk

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/compressed",
                "max_response_bytes": 64 * 1024,
                "expected_statuses": [200],
            },
            state={},
        )

    assert result["failure_reason"] == "response_too_large"
    assert max(observed_raw_chunk_sizes) <= 64 * 1024


def test_decoded_gzip_chunks_do_not_exceed_fixed_chunk_size():
    from core.application.orchestration.http_response_decoding import (
        DECODED_RESPONSE_CHUNK_BYTES,
        _decoded_response_chunks,
    )

    payload = b"x" * (128 * 1024)
    compressed = gzip.compress(payload)

    class _Response:
        headers = {"content-encoding": "gzip"}  # noqa: RUF012

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield compressed

    chunks = list(_decoded_response_chunks(_Response(), max_size=len(payload)))

    assert b"".join(chunks) == payload
    assert max(len(chunk) for chunk in chunks) <= DECODED_RESPONSE_CHUNK_BYTES


def test_decoded_gzip_accepts_exact_limit_and_rejects_corrupt_or_concatenated_streams():
    from core.application.orchestration.http_response_decoding import _decoded_response_chunks

    payload = b"x" * (64 * 1024)

    class _Response:
        headers = {"content-encoding": "gzip"}  # noqa: RUF012

        def __init__(self, body: bytes):
            self._body = body

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield self._body

    assert (
        b"".join(_decoded_response_chunks(_Response(gzip.compress(payload)), max_size=len(payload)))
        == payload
    )

    _raises_input_579_1 = _decoded_response_chunks(
        _Response(gzip.compress(b"truncated")[:-4]), max_size=100
    )
    with pytest.raises(httpx.DecodingError, match="Incomplete gzip response"):
        list(_raises_input_579_1)

    _raises_input_582_1 = _decoded_response_chunks(
        _Response(gzip.compress(b"first") + gzip.compress(b"second")), max_size=100
    )
    with pytest.raises(httpx.DecodingError, match="Concatenated gzip response"):
        list(_raises_input_582_1)


def test_decoded_deflate_accepts_zlib_wrapped_and_raw_streams():
    from core.application.orchestration.http_response_decoding import _decoded_response_chunks

    payload = b"deflate body" * 100
    raw_encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw_deflate = raw_encoder.compress(payload) + raw_encoder.flush()

    class _Response:
        headers = {"content-encoding": "deflate"}  # noqa: RUF012

        def __init__(self, body: bytes):
            self._body = body

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield self._body

    assert (
        b"".join(_decoded_response_chunks(_Response(zlib.compress(payload)), max_size=4096))
        == payload
    )
    assert b"".join(_decoded_response_chunks(_Response(raw_deflate), max_size=4096)) == payload


def test_execute_http_request_step_artifact_midstream_timeout_is_retryable_timeout():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b"partial"
            raise httpx.ReadTimeout("slow body")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/file.bin",
                "response_mode": "artifact",
                "expected_statuses": [200],
            },
            state={"_artifact_plane": ArtifactDatasetPlane()},
        )

    assert result["ok"] is False
    assert result["failure_reason"] == "timeout"
    assert result["retryable"] is True


def test_execute_http_request_step_empty_artifact_is_valid():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            if False:
                yield b""

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/empty.bin",
                "response_mode": "artifact",
                "expected_statuses": [200],
            },
            state={"_artifact_plane": ArtifactDatasetPlane()},
        )

    assert result["ok"] is True
    assert result["size_bytes"] == 0
    assert result["sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_execute_http_request_step_content_disposition_rfc5987_filename():
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 200
        headers = {  # noqa: RUF012
            "content-type": "text/csv",
            "content-disposition": "attachment; filename*=UTF-8''caf%C3%A9.csv",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b"id\n1\n"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/download",
                "response_mode": "artifact",
                "expected_statuses": [200],
            },
            state={"_artifact_plane": ArtifactDatasetPlane()},
        )

    assert result["ok"] is True
    assert result["filename"] == "café.csv"


def test_artifact_filename_prefers_strict_rfc5987_over_plain_filename():
    from core.application.orchestration.http_response_decoding import (
        _filename_from_content_disposition,
    )

    filename = _filename_from_content_disposition(
        "attachment; filename=plain.txt; filename*=UTF-8''unicode%C3%A9.txt"
    )

    assert filename == "unicodeé.txt"


def test_artifact_filename_accepts_basic_rfc5987_language_tag():
    from core.application.orchestration.http_response_decoding import (
        _filename_from_content_disposition,
    )

    filename = _filename_from_content_disposition("attachment; filename*=UTF-8'en-US'caf%C3%A9.csv")

    assert filename == "café.csv"


def test_artifact_filename_rejects_invalid_rfc5987_data():
    from core.application.orchestration.http_response_decoding import (
        _filename_from_content_disposition,
    )

    invalid_headers = [
        "attachment; filename*=UTF-8''bad%ZZ.csv",
        "attachment; filename*=UTF-8''bad%FF.csv",
        "attachment; filename*=X-UNKNOWN-CHARSET''file.csv",
        "attachment; filename*=UTF-8''café.csv",
        "attachment; filename*=UTF-8''a b.csv",
        "attachment; filename*=UTF-8''foo'bar.csv",
        "attachment; filename*=\"UTF-8''caf%C3%A9.csv\"",
        "attachment; filename*=UTF 8''caf%C3%A9.csv",
        "attachment; filename*=UTF@8''caf%C3%A9.csv",
        "attachment; filename*=UTF/8''caf%C3%A9.csv",
        "attachment; filename*=UTF:8''caf%C3%A9.csv",
        "attachment; filename*=UTF(8)''caf%C3%A9.csv",
        "attachment; filename*=UTF\t8''caf%C3%A9.csv",
        "attachment; filename*=UTF-8'en_US'caf%C3%A9.csv",
    ]

    for header in invalid_headers:
        with pytest.raises(ValueError, match="artifact filename"):
            _filename_from_content_disposition(header)


def test_artifact_filename_falls_back_to_plain_content_disposition_filename():
    from core.application.orchestration.http_response_decoding import (
        _filename_from_content_disposition,
    )

    assert _filename_from_content_disposition('attachment; filename="plain.csv"') == "plain.csv"


def test_artifact_filename_from_url_path_uses_strict_utf8_percent_decoding():
    from core.application.orchestration.http_response_decoding import _filename_from_url_path

    assert _filename_from_url_path("https://example.test/files/caf%C3%A9.csv") == "café.csv"

    for url in (
        "https://example.test/files/bad%ZZ.csv",
        "https://example.test/files/bad%FF.csv",
    ):
        with pytest.raises(ValueError, match="artifact filename"):
            _filename_from_url_path(url)


def test_execute_http_request_step_inline_respects_text_charset_and_json_bom():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    responses = [
        (
            {"content-type": "text/plain; charset=iso-8859-1"},
            "café".encode("iso-8859-1"),
            "café",
        ),
        (
            {"content-type": "application/json"},
            '{"name":"café"}'.encode("utf-16"),
            {"name": "café"},
        ),
    ]

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            headers, body, _expected = responses.pop(0)

            class _Response:
                status_code = 200

                def __init__(self):
                    self.headers = headers

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def iter_raw(self, chunk_size=None):
                    _ = chunk_size
                    yield body

            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        text_result = execute_http_request_step(
            config={"url": "https://example.test/text", "expected_statuses": [200]},
            state={},
        )
        json_result = execute_http_request_step(
            config={"url": "https://example.test/json", "expected_statuses": [200]},
            state={},
        )

    assert text_result["response_body"] == "café"
    assert json_result["response_body"] == {"name": "café"}


def test_execute_http_request_step_large_500_keeps_unexpected_status_retryable():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 500
        headers = {"content-type": "text/plain", "content-length": str(10 * 1024 * 1024)}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b"server exploded"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={"url": "https://example.test/error", "expected_statuses": [200]},
            state={},
        )

    assert result["failure_reason"] == "unexpected_status"
    assert result["retryable"] is True
    assert "server exploded" in result["response_excerpt"]


def test_execute_http_request_step_unsupported_encoded_404_keeps_unexpected_status():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 404
        headers = {"content-type": "text/plain", "content-encoding": "zstd"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b"not a supported response encoding"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={"url": "https://example.test/missing", "expected_statuses": [200]},
            state={},
        )

    assert result["ok"] is False
    assert result["status_code"] == 404
    assert result["failure_reason"] == "unexpected_status"
    assert "Unsupported content-encoding" in result["response_excerpt"]


def test_execute_http_request_step_head_ignores_large_content_length_body_cap():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 200

        def __init__(self):
            self.headers = {
                "content-type": "application/octet-stream",
                "content-length": str(10 * 1024 * 1024),
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            if False:
                yield b""

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "method": "HEAD",
                "url": "https://example.test/file",
                "max_response_bytes": 5,
                "expected_statuses": [200],
            },
            state={},
        )

    assert result["ok"] is True
    assert result["response_body"] == ""


def test_execute_http_request_step_304_ignores_representation_content_length():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 304

        def __init__(self):
            self.headers = {
                "content-type": "application/json",
                "content-length": str(10 * 1024 * 1024),
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            raise AssertionError("304 response body must not be read")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "url": "https://example.test/cacheable",
                "max_response_bytes": 5,
                "expected_statuses": [304],
            },
            state={},
        )

    assert result["ok"] is True
    assert result["status_code"] == 304
    assert result["response_body"] == ""


def test_execute_http_request_step_rejects_invalid_response_mode_before_network():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client") as client,
        pytest.raises(ValueError, match="response_mode"),
    ):
        execute_http_request_step(
            config={"url": "https://example.test/file", "response_mode": "inlineish"},
            state={},
        )

    client.assert_not_called()


def test_execute_http_request_step_rejects_non_integral_runtime_limits():
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    for value in (True, 1.5, "5"):
        with pytest.raises(ValueError, match="max_response_bytes"):
            execute_http_request_step(
                config={"url": "https://example.test/file", "max_response_bytes": value},
                state={},
            )


def test_safe_read_429_projects_provider_neutral_retry_facts() -> None:
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Response:
        status_code = 429
        headers = {  # noqa: RUF012
            "content-type": "application/json",
            "Retry-After": "7",
            "X-Secret": "must-not-be-projected",  # pragma: allowlist secret - redaction fixture
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            yield b'{"private":"must-not-enter-retry-signal"}'

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            return _Response()

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={"method": "GET", "url": "https://example.test/items"}, state={}
        )

    assert result["failure_class"] == "provider"
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 7.0
    assert result["request_semantics"] == "safe_read"
    assert result["external_effect_status"] == "failed"
    assert result["definitely_no_external_effect"] is True
    assert result["http_status"] == 429
    retry_projection = {
        key: result[key]
        for key in (
            "failure_class",
            "retryable",
            "retry_after_seconds",
            "request_semantics",
            "external_effect_status",
            "definitely_no_external_effect",
            "http_status",
        )
    }
    assert "must-not" not in repr(retry_projection)


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_mutating_transport_timeout_is_one_shot_timeout_unknown(method: str) -> None:
    from core.application.orchestration.http_request_step_runtime import execute_http_request_step

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **_kwargs):
            raise httpx.ReadTimeout("ambiguous after request bytes")

    with (
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={"method": method, "url": "https://example.test/mutation"}, state={}
        )

    assert result["request_semantics"] == "mutation"
    assert result["failure_class"] == "transient"
    assert result["retryable"] is False
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False


def test_http_timeout_is_capped_by_remaining_operation_deadline() -> None:
    from core.application.orchestration.http_request_step_runtime import (
        execute_http_request_step,
    )

    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        def __init__(self):
            self.headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_raw(self, chunk_size=None):
            _ = chunk_size
            return iter(())

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **kwargs):
            captured.update(kwargs)
            return _Response()

    with (
        patch(
            "core.application.orchestration.http_request_step_runtime.time.time",
            return_value=90.0,
        ),
        patch("core.application.orchestration.http_request_step_runtime.httpx.Client", _Client),
        patch(
            "core.application.orchestration.http_request_step_runtime._validate_http_request_url",
            side_effect=lambda url: url,
        ),
    ):
        result = execute_http_request_step(
            config={
                "method": "GET",
                "url": "https://example.test/items",
                "timeout_seconds": 30,
            },
            state={"_operation_deadline_epoch_seconds": 95.0},
        )

    assert result["ok"] is True
    assert captured["timeout"] == 5.0
