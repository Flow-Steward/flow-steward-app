"""AC17-R123: what authorizes an extension to fetch an artifact the host must serve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.infrastructure.security.artifact_access_tokens import (
    ArtifactAccessTokenError,
    issue_artifact_access_token,
    verify_artifact_access_token,
)

_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "a-deployment-secret-for-tests")


def _token(**overrides: object) -> str:
    fields: dict = {
        "object_id": "art_123",
        "kind": "artifact",
        "method": "GET",
        "now": _NOW,
    }
    fields.update(overrides)
    return issue_artifact_access_token(**fields)  # type: ignore[arg-type]


def test_a_token_authorizes_exactly_what_it_was_issued_for() -> None:
    claims = verify_artifact_access_token(
        _token(), object_id="art_123", kind="artifact", method="GET", now=_NOW
    )

    assert claims["object_id"] == "art_123"
    assert claims["method"] == "GET"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_id", "art_other"),
        ("kind", "dataset"),
        ("method", "PUT"),
    ],
)
def test_a_token_does_not_authorize_anything_else(field: str, value: str) -> None:
    """One object, one kind, one method. A read grant that could write is not a grant."""
    request = {"object_id": "art_123", "kind": "artifact", "method": "GET"}
    request[field] = value
    token = _token()

    with pytest.raises(ArtifactAccessTokenError, match="not for this request"):
        verify_artifact_access_token(token, now=_NOW, **request)


def test_an_expired_token_is_refused() -> None:
    token = _token(ttl_seconds=60)

    verify_artifact_access_token(
        token, object_id="art_123", kind="artifact", method="GET", now=_NOW
    )
    one_second_past_the_ttl = _NOW + timedelta(seconds=61)

    with pytest.raises(ArtifactAccessTokenError, match="expired"):
        verify_artifact_access_token(
            token,
            object_id="art_123",
            kind="artifact",
            method="GET",
            now=one_second_past_the_ttl,
        )


def test_a_tampered_payload_is_refused_rather_than_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claims are only trusted after the signature is, never before."""
    import base64
    import json

    token = _token()
    payload, _, signature = token.partition(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["object_id"] = "art_someone_elses"
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )

    with pytest.raises(ArtifactAccessTokenError, match="not valid"):
        verify_artifact_access_token(
            f"{forged}.{signature}",
            object_id="art_someone_elses",
            kind="artifact",
            method="GET",
            now=_NOW,
        )


def test_a_token_from_another_deployment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _token()
    monkeypatch.setenv("FS_SECRET_KEY", "a-different-deployments-secret")

    with pytest.raises(ArtifactAccessTokenError, match="not valid"):
        verify_artifact_access_token(
            token, object_id="art_123", kind="artifact", method="GET", now=_NOW
        )


def test_the_platform_grant_signature_cannot_be_presented_as_an_access_token() -> None:
    """Both derive from FS_SECRET_KEY, and their contexts keep them apart.

    Without distinct context strings a value minted to prove "the platform issued
    this descriptor" would also open the door it describes.
    """
    from core.infrastructure.security import artifact_access_tokens, artifact_grant_signing

    assert artifact_access_tokens._CONTEXT != artifact_grant_signing._CONTEXT


@pytest.mark.parametrize("malformed", ["", "not-a-token", "onlyonepart", "a.b", "."])
def test_malformed_tokens_are_refused_without_interpretation(malformed: str) -> None:
    with pytest.raises(ArtifactAccessTokenError):
        verify_artifact_access_token(
            malformed, object_id="art_123", kind="artifact", method="GET", now=_NOW
        )


def test_no_deployment_secret_means_no_tokens_rather_than_unsigned_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issuing returns nothing and verifying refuses; neither pretends to succeed."""
    monkeypatch.delenv("FS_SECRET_KEY", raising=False)

    assert issue_artifact_access_token(object_id="art_123", kind="artifact", method="GET") == ""
    with pytest.raises(ArtifactAccessTokenError, match="unavailable"):
        verify_artifact_access_token(
            "anything", object_id="art_123", kind="artifact", method="GET"
        )


def test_the_refusal_never_says_what_the_token_was_for() -> None:
    """An endpoint that names the expected artifact answers an unasked question."""
    token = _token()

    with pytest.raises(ArtifactAccessTokenError) as raised:
        verify_artifact_access_token(
            token, object_id="art_other", kind="artifact", method="GET", now=_NOW
        )

    assert "art_123" not in str(raised.value)
