"""Synthetic, network-free contracts for opaque HTTP input references."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from core.application.orchestration import http_request_step_runtime as runtime
from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
from core.infrastructure.security.prompt_safety import REDACTED_CREDENTIAL_MARKER
from core.infrastructure.security.secret_reference import (
    SecretReference,
    inline_secret_ref_fingerprint,
)

_FIXTURE_VALUE = "fixture-credential-for-http"
_FIXTURE_KEY = "fixture-http-reference"
_FIXTURE_ENVELOPE = "fixture-encrypted-envelope"


def _state(reference):
    fingerprint = (
        inline_secret_ref_fingerprint(reference)
        if reference.is_inline
        else secret_ref_fingerprint(reference.ref_key)
    )
    return {
        "inputs": {"login": reference},
        "runtime_context": {"account_id": "fixture-account"},
        "_workflow_secret_ref_fingerprints": [fingerprint],
    }


def _config(**changes):
    return {
        "method": "POST",
        "url": "https://example.test/fixture",
        "headers": {"X-Login": "Bearer {{ state.inputs.login }}", "X-Ordinary": "kept"},
        "query_params": {"login": {"source": "state.inputs.login"}},
        "body_template": {"login": {"source": "state.inputs.login"}, "count": 0},
        **changes,
    }


@pytest.fixture
def boundary_io(monkeypatch):
    captured = []
    response = SimpleNamespace(
        status_code=200,
        headers={"content-type": "application/json"},
        chunks=[b'{"ordinary":true}'],
        error=None,
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @property
        def status_code(self):
            return response.status_code

        @property
        def headers(self):
            return response.headers

        def iter_raw(self, chunk_size=None):
            if response.error is not None:
                raise response.error
            yield from response.chunks

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, **kwargs):
            captured.append(kwargs)
            return Response()

    read = Mock(
        side_effect=lambda _conn, *, account_id, ref_key: (
            _FIXTURE_ENVELOPE
            if account_id == "fixture-account" and ref_key == _FIXTURE_KEY
            else None
        )
    )
    decrypt = Mock(return_value=_FIXTURE_VALUE)
    monkeypatch.setattr(runtime.httpx, "Client", Client)
    monkeypatch.setattr(runtime, "_SSRFGuardedHTTPTransport", lambda: object())
    monkeypatch.setattr(runtime, "_validate_http_request_url", lambda url: url)
    monkeypatch.setattr(runtime, "_get_shared_logger", lambda: SimpleNamespace(conn=object()))
    monkeypatch.setattr(
        "core.infrastructure.db.model_provider_store.get_secret_ref_ciphertext", read
    )
    monkeypatch.setattr("core.infrastructure.security.secret_codec.decrypt_secret", decrypt)
    return SimpleNamespace(captured=captured, response=response, read=read, decrypt=decrypt)


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("inline", [False, True])
def test_opaque_inputs_resolve_only_for_dispatch_and_metadata_stays_safe(boundary_io, wire, inline):
    reference = SecretReference(
        name="login",
        ref_key="" if inline else _FIXTURE_KEY,
        ciphertext=_FIXTURE_ENVELOPE if inline else "",
    )
    state = _state(reference)
    if wire:
        state["inputs"]["login"] = reference.to_wire()
    before = copy.deepcopy(state)
    config = _config()
    config_before = copy.deepcopy(config)

    result = runtime.execute_http_request_step(config=config, state=state)

    sent = boundary_io.captured[0]
    assert sent["headers"]["X-Login"] == f"Bearer {_FIXTURE_VALUE}"
    assert sent["params"] == {"login": _FIXTURE_VALUE}
    assert sent["json"] == {"login": _FIXTURE_VALUE, "count": 0}
    assert result["ok"] is True
    assert result["request"]["headers"]["X-Login"] == REDACTED_CREDENTIAL_MARKER
    assert result["request"]["headers"]["X-Ordinary"] == "kept"
    assert result["request"]["query_params"]["login"] == REDACTED_CREDENTIAL_MARKER
    assert result["request"]["body"]["login"] == REDACTED_CREDENTIAL_MARKER
    for forbidden in (
        _FIXTURE_VALUE,
        _FIXTURE_KEY,
        _FIXTURE_ENVELOPE,
        "flow-steward.secret-ref.v1",
    ):
        assert forbidden not in json.dumps(result)
    assert state == before
    assert config == config_before
    if inline:
        boundary_io.read.assert_not_called()
    else:
        assert boundary_io.read.call_args.kwargs == {
            "account_id": "fixture-account",
            "ref_key": _FIXTURE_KEY,
        }


@pytest.mark.parametrize("failure", ["no-grant", "wrong-account", "missing-inline-grant"])
def test_unavailable_authority_fails_closed_before_network(boundary_io, failure):
    reference = SecretReference(name="login", ref_key=_FIXTURE_KEY)
    if failure == "missing-inline-grant":
        reference = SecretReference(name="login", ciphertext=_FIXTURE_ENVELOPE)
    state = _state(reference)
    if failure == "wrong-account":
        state["runtime_context"]["account_id"] = "another-fixture-account"
    else:
        state["_workflow_secret_ref_fingerprints"] = []

    result = runtime.execute_http_request_step(config=_config(), state=state)

    assert result["ok"] is False
    assert result["failure_reason"] == "unresolved_secret_input"
    assert boundary_io.captured == []
    boundary_io.decrypt.assert_not_called()
    assert _FIXTURE_KEY not in json.dumps(result)
    assert _FIXTURE_ENVELOPE not in json.dumps(result)


@pytest.mark.parametrize("field", ["method", "url", "artifact_filename"])
def test_reference_cannot_be_used_as_public_request_control(boundary_io, field):
    reference = SecretReference(name="login", ref_key=_FIXTURE_KEY)
    config = _config(**{field: "{{ state.inputs.login }}"})
    if field == "artifact_filename":
        config["response_mode"] = "artifact"
    result = runtime.execute_http_request_step(config=config, state=_state(reference))
    assert result["ok"] is False
    assert boundary_io.captured == []
    boundary_io.read.assert_not_called()
    boundary_io.decrypt.assert_not_called()


@pytest.mark.parametrize("url", ["http://example.test/fixture", "https://{{ state.host }}/fixture"])
def test_opaque_request_requires_existing_static_https_origin_policy(boundary_io, url):
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    state["host"] = "example.test"
    result = runtime.execute_http_request_step(config=_config(url=url), state=state)
    assert result["ok"] is False
    assert boundary_io.captured == []
    boundary_io.read.assert_not_called()
    boundary_io.decrypt.assert_not_called()


@pytest.mark.parametrize("surface", ["body", "status-error", "exception", "header", "authority"])
def test_reflection_is_blocked_in_result_and_exception_surfaces(boundary_io, surface):
    response = boundary_io.response
    if surface in {"body", "status-error", "authority"}:
        reflected = _FIXTURE_KEY if surface == "authority" else _FIXTURE_VALUE
        response.chunks = [json.dumps({"echo": reflected}).encode()]
        if surface == "status-error":
            response.status_code = 400
    elif surface == "exception":
        response.error = httpx.ReadError(f"fixture failure {_FIXTURE_VALUE}")
    else:
        response.headers["content-type"] = f"text/plain; reflected={_FIXTURE_VALUE}"

    result = runtime.execute_http_request_step(
        config=_config(),
        state=_state(SecretReference(name="login", ref_key=_FIXTURE_KEY)),
    )
    assert result["ok"] is False
    assert result["failure_reason"] == "secret_exposure_blocked"
    assert _FIXTURE_VALUE not in json.dumps(result)
    assert _FIXTURE_KEY not in json.dumps(result)


@pytest.mark.parametrize("reflected", [False, True])
def test_artifact_stream_retains_safe_bytes_but_never_persists_reflection(boundary_io, reflected):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    plane = ArtifactDatasetPlane()
    stored = []
    original = plane.create_artifact_from_stream

    def create(**kwargs):
        chunks = kwargs.pop("chunks")

        def record():
            for chunk in chunks:
                stored.append(chunk)
                yield chunk

        return original(chunks=record(), **kwargs)

    plane.create_artifact_from_stream = create
    boundary_io.response.headers = {"content-type": "application/octet-stream"}
    boundary_io.response.chunks = (
        [b"prefix fixture-cred", b"ential-for-http suffix"]
        if reflected
        else [b"ordinary ", b"download"]
    )
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    state["_artifact_plane"] = plane
    result = runtime.execute_http_request_step(
        config=_config(response_mode="artifact", artifact_filename="fixture.bin"),
        state=state,
    )
    if reflected:
        assert result["ok"] is False
        assert result["failure_reason"] == "secret_exposure_blocked"
        assert _FIXTURE_VALUE.encode() not in b"".join(stored)
        assert not plane._artifacts
    else:
        assert result["ok"] is True
        assert result["artifact_handle"].startswith("artifact:")
        assert b"".join(stored) == b"ordinary download"


def test_nested_template_preserves_json_rendering_and_reference_provenance(boundary_io):
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    state["payload"] = {"login": state["inputs"]["login"], "count": 0}
    result = runtime.execute_http_request_step(
        config=_config(body_template="payload={{ state.payload }}"),
        state=state,
    )
    assert boundary_io.captured[0]["content"].decode() == (
        "payload=" + json.dumps({"login": _FIXTURE_VALUE, "count": 0})
    )
    assert result["ok"] is True
    assert _FIXTURE_KEY not in json.dumps(result)
    assert _FIXTURE_VALUE not in json.dumps(result)


def test_no_reference_request_keeps_existing_rendering_and_metadata(boundary_io):
    state = {"inputs": {"login": "ordinary"}, "rows": [{"label": "first"}]}
    result = runtime.execute_http_request_step(
        config=_config(
            body_template={
                "label": "{{ state.rows[0].label }}",
                "fallback": "{{ state.absent | default:'fallback' }}",
                "zero": 0,
                "enabled": False,
            }
        ),
        state=state,
    )
    assert result["ok"] is True
    assert result["request"] == {
        "method": "POST",
        "url": "https://example.test/fixture",
        "query_params": {"login": "ordinary"},
        "headers": {
            "X-Login": "Bearer ordinary",
            "X-Ordinary": "kept",
            "Accept-Encoding": "gzip, deflate",
        },
        "body": {"label": "first", "fallback": "fallback", "zero": 0, "enabled": False},
    }
    assert boundary_io.captured[0]["json"] == result["request"]["body"]
    boundary_io.read.assert_not_called()
    boundary_io.decrypt.assert_not_called()


def test_blocked_destination_fails_before_any_reference_resolution(boundary_io, monkeypatch):
    def deny(_url):
        raise ValueError("fixture destination denied")

    monkeypatch.setattr(runtime, "_validate_http_request_url", deny)
    config = _config()
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    with pytest.raises(ValueError, match="fixture destination denied"):
        runtime.execute_http_request_step(config=config, state=state)
    assert boundary_io.captured == []
    boundary_io.read.assert_not_called()
    boundary_io.decrypt.assert_not_called()


@pytest.mark.parametrize("field", ["filename", "content-type"])
def test_artifact_metadata_reflection_is_blocked_before_creation(boundary_io, field):
    create = Mock(side_effect=AssertionError("artifact creation must not start"))
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    state["_artifact_plane"] = SimpleNamespace(create_artifact_from_stream=create)
    if field == "filename":
        boundary_io.response.headers["content-disposition"] = (
            f'attachment; filename="{_FIXTURE_VALUE}"'
        )
    else:
        boundary_io.response.headers["content-type"] = _FIXTURE_VALUE
    result = runtime.execute_http_request_step(
        config=_config(response_mode="artifact"), state=state
    )
    assert result["ok"] is False
    assert result["failure_reason"] == "secret_exposure_blocked"
    create.assert_not_called()


def test_reflection_budget_exhaustion_fails_before_network(boundary_io):
    boundary_io.decrypt.return_value = "x" * 65_537
    result = runtime.execute_http_request_step(
        config=_config(),
        state=_state(SecretReference(name="login", ref_key=_FIXTURE_KEY)),
    )
    assert result["ok"] is False
    assert result["failure_reason"] == "secret_exposure_blocked"
    assert boundary_io.captured == []


@pytest.mark.parametrize("reflected", [False, True])
def test_artifact_utf16_odd_chunks_keep_decoder_alignment(boundary_io, reflected):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    plane = ArtifactDatasetPlane()
    stored = []
    original = plane.create_artifact_from_stream

    def create(**kwargs):
        chunks = kwargs.pop("chunks")

        def record():
            for chunk in chunks:
                stored.append(chunk)
                yield chunk

        return original(chunks=record(), **kwargs)

    plane.create_artifact_from_stream = create
    text = "ordinary-prefix " * 20 + (_FIXTURE_VALUE if reflected else "ordinary download")
    payload = text.encode("utf-16-le")
    boundary_io.response.headers = {"content-type": "text/plain; charset=utf-16-le"}
    boundary_io.response.chunks = [
        payload[offset : offset + 3] for offset in range(0, len(payload), 3)
    ]
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    state["_artifact_plane"] = plane
    result = runtime.execute_http_request_step(
        config=_config(response_mode="artifact", artifact_filename="fixture.bin"),
        state=state,
    )
    if reflected:
        assert result["ok"] is False
        assert result["failure_reason"] == "secret_exposure_blocked"
        assert _FIXTURE_VALUE.encode("utf-16-le") not in b"".join(stored)
        assert not plane._artifacts
    else:
        assert result["ok"] is True
        assert b"".join(stored) == payload


@pytest.mark.parametrize("reflected", [True, False])
def test_artifact_raw_utf8_reflection_is_blocked_despite_declared_charset(boundary_io, reflected):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    plane = ArtifactDatasetPlane()
    stored = []
    original = plane.create_artifact_from_stream

    def create(**kwargs):
        chunks = kwargs.pop("chunks")

        def record():
            for chunk in chunks:
                stored.append(chunk)
                yield chunk

        return original(chunks=record(), **kwargs)

    plane.create_artifact_from_stream = create
    middle = _FIXTURE_VALUE.encode() if reflected else bytes(range(256))
    payload = b"ordinary prefix " * 20 + middle + b" ordinary suffix" * 20
    boundary_io.response.headers = {"content-type": "application/octet-stream; charset=utf-16-le"}
    boundary_io.response.chunks = [
        payload[offset : offset + 3] for offset in range(0, len(payload), 3)
    ]
    state = _state(SecretReference(name="login", ref_key=_FIXTURE_KEY))
    state["_artifact_plane"] = plane
    result = runtime.execute_http_request_step(
        config=_config(response_mode="artifact", artifact_filename="fixture.bin"),
        state=state,
    )
    if reflected:
        assert result["ok"] is False
        assert result["failure_reason"] == "secret_exposure_blocked"
        assert _FIXTURE_VALUE.encode() not in b"".join(stored)
        assert not plane._artifacts
    else:
        assert result["ok"] is True
        assert b"".join(stored) == payload
