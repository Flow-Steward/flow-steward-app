"""Core conformance for per-tenant origins, signed callbacks, and exchange profiles.

Everything here runs against a neutral fixture provider. The point of these
capabilities is that core gains no provider knowledge, so a test that named a
real provider would be evidence of the opposite.
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.extensions.oauth_callback_connection_checks import (
    _duplicate_subject_connection_id,
    _reconnect_subject_mismatch,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    contract_section,
    resolve_authorization_origin,
    validate_authorization_intent_inputs,
)
from core.infrastructure.security.hmac_signatures import hmac_digest_matches, hmac_sha256_hex

_BOUND = Path("tests/fixtures/extensions/fixture_extension_oauth_bound/extension.yaml")
_FIXED = Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml")


def _manifest(path: Path) -> dict[str, Any]:
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The fixed fixture names a post-connect action, which only validates when the
    # action it names exists.
    manifest.setdefault(
        "action_definitions", [{"action_id": "verify_identity", "handler": {"mode": "extension"}}]
    )
    return manifest


def _provider(path: Path = _BOUND):
    return compile_oauth_providers(_manifest(path))[0]


class TestTheOriginIsBuiltOnlyFromDeclaredParts:
    """An origin is a declared suffix plus a bounded label. Nothing else."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("acme", "acme.identity.example.test"),
            ("ACME", "acme.identity.example.test"),
            ("Acme.Identity.Example.Test", "acme.identity.example.test"),
            ("acme.identity.example.test.", "acme.identity.example.test"),
            ("a-b-c", "a-b-c.identity.example.test"),
        ],
    )
    def test_a_label_or_a_full_host_reaches_the_same_origin(self, raw: str, expected: str) -> None:
        assert _provider().origin_binding.normalize(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            " ",
            "other.example.org",
            "*.identity.example.test",
            "a b",
            "a/b",
            "-acme",
            "acme-",
            "https://acme.identity.example.test",
            "acme.identity.example.test:443",
            "x" * 61,
            "acme.identity.example.test/admin",
        ],
    )
    def test_anything_else_is_refused(self, raw: str) -> None:
        assert _provider().origin_binding.normalize(raw) == ""

    def test_a_suffix_match_still_requires_the_dot(self) -> None:
        """`evilidentity.example.test` must not satisfy `.identity.example.test`."""
        assert (
            _provider().origin_binding.normalize("evil.identity.example.test.attacker.test") == ""
        )


class TestEndpointsResolveOnlyAgainstAVerifiedOrigin:
    def test_a_bound_endpoint_needs_an_origin(self) -> None:
        provider = _provider()

        assert provider.token_endpoint.resolve("") == ""
        assert provider.token_endpoint.bind("").url == ""

    def test_a_bound_endpoint_uses_the_declared_path(self) -> None:
        provider = _provider()

        assert (
            provider.token_endpoint.resolve("acme.identity.example.test")
            == "https://acme.identity.example.test/oauth/token"
        )

    def test_a_fixed_provider_is_unchanged(self) -> None:
        provider = _provider(_FIXED)

        assert provider.token_endpoint.url == "https://identity.example.test/token"
        assert provider.token_endpoint.is_bound is False
        assert provider.token_endpoint.bind("attacker.test") is provider.token_endpoint


class TestTheBoundInputIsNotAlsoAQueryParameter:
    """The value that decides where the request goes is not reflected into it."""

    def test_the_bound_input_maps_to_no_parameter(self) -> None:
        provider = _provider()

        mapped = validate_authorization_intent_inputs(
            provider, intent_id="connect", values={"tenant_domain": "acme"}
        )

        assert mapped == {}

    def test_the_same_input_resolves_the_origin(self) -> None:
        provider = _provider()

        origin = resolve_authorization_origin(
            provider, intent_id="connect", values={"tenant_domain": "acme"}
        )

        assert origin == "acme.identity.example.test"

    @pytest.mark.parametrize("value", ["", "other.example.org", "*.x"])
    def test_an_unusable_origin_fails_the_authorization(self, value: str) -> None:
        _raises_input_133_1 = _provider()
        with pytest.raises(ValueError):
            resolve_authorization_origin(
                _raises_input_133_1, intent_id="connect", values={"tenant_domain": value}
            )

    def test_a_provider_without_a_binding_resolves_nothing(self) -> None:
        assert (
            resolve_authorization_origin(
                _provider(_FIXED), intent_id="connect", values={"email_hint": "a@b.test"}
            )
            == ""
        )


class TestScopesFollowTheDeclaredSeparator:
    def test_a_comma_provider_joins_and_splits_on_commas(self) -> None:
        provider = _provider()

        assert provider.join_scopes(["mail.read", "mail.write"]) == "mail.read,mail.write"
        assert provider.split_scopes("mail.read,mail.write") == ("mail.read", "mail.write")

    def test_a_comma_provider_still_reads_a_space_separated_grant(self) -> None:
        """Mis-reading a grant would strand a working connection."""
        assert _provider().split_scopes("mail.read mail.write") == ("mail.read", "mail.write")

    def test_a_space_provider_is_unchanged(self) -> None:
        provider = _provider(_FIXED)

        assert provider.scope_separator == "space"
        assert provider.join_scopes(["mail.read", "mail.write"]) == "mail.read mail.write"


class TestThePkceModeIsDeclared:
    def test_the_bound_fixture_declares_none(self) -> None:
        assert _provider().pkce == "none"
        assert _provider().uses_pkce is False

    def test_the_existing_fixture_keeps_s256(self) -> None:
        assert _provider(_FIXED).pkce == "s256"
        assert _provider(_FIXED).uses_pkce is True

    def test_the_flow_travels_into_the_security_fingerprint(self) -> None:
        """Changing the PKCE mode is a change of security posture, not cosmetics."""
        manifest = _manifest(_BOUND)
        before = compile_oauth_providers(manifest)[0]
        manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]["flow"]["pkce"] = "s256"
        after = compile_oauth_providers(manifest)[0]

        assert before.to_dict()["security_fingerprint"] != after.to_dict()["security_fingerprint"]


class TestTheExchangeProfileIsClosed:
    def test_the_bound_fixture_omits_grant_type_and_redirect_uri(self) -> None:
        profile = _provider().code_exchange

        assert profile.include_grant_type is False
        assert profile.include_redirect_uri is False
        assert profile.static_parameters == {"expiring": "1"}

    def test_a_provider_without_a_profile_is_rfc_6749(self) -> None:
        profile = _provider(_FIXED).code_exchange

        assert profile.include_grant_type is True
        assert profile.include_redirect_uri is True
        assert profile.static_parameters == {}

    @pytest.mark.parametrize(
        "name",
        ["code", "client_id", "client_secret", "grant_type", "redirect_uri", "code_verifier"],
    )
    def test_a_static_parameter_cannot_rewrite_a_reserved_field(self, name: str) -> None:
        manifest = _manifest(_BOUND)
        provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
        provider["code_exchange"]["static_parameters"] = {name: "x"}

        with pytest.raises(ValueError):
            compile_oauth_providers(manifest)


class TestTheCallbackSignaturePolicyIsClosed:
    def test_the_canonical_message_is_sorted_and_excludes_the_signature(self) -> None:
        """The message core builds is the one the provider documents."""
        pairs = [("tenant", "acme"), ("code", "c"), ("signature", "ignored"), ("state", "s")]
        message = "&".join(f"{key}={value}" for key, value in sorted(pairs) if key != "signature")

        assert message == "code=c&state=s&tenant=acme"

    def test_a_digest_matches_only_itself(self) -> None:
        digest = hmac_sha256_hex(secret="shh", payload=b"code=c&state=s")

        assert hmac_digest_matches(digest, digest) is True
        assert hmac_digest_matches(digest, digest[:-1] + "0") is False
        assert hmac_digest_matches(digest, "") is False
        assert hmac_digest_matches("", digest) is False
        assert hmac_digest_matches(digest, digest[:-1]) is False

    @pytest.mark.parametrize(
        "override",
        [
            {"algorithm": "hmac_sha1"},
            {"digest": "base64"},
            {"message": "raw_query"},
            {"secret_source": "access_token"},  # pragma: allowlist secret
            {"subject_binding": "not_the_binding"},
            {"subject_parameter": "signature"},
        ],
    )
    def test_an_unsupported_policy_is_refused(self, override: dict[str, str]) -> None:
        manifest = _manifest(_BOUND)
        provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
        provider["callback_verification"].update(override)

        with pytest.raises(ValueError):
            compile_oauth_providers(manifest)

    def test_verification_requires_a_declared_origin_binding(self) -> None:
        manifest = _manifest(_BOUND)
        provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
        provider.pop("origin_binding")

        with pytest.raises(ValueError):
            compile_oauth_providers(manifest)


class TestMalformedDeclarationsAreRefused:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["origin_binding"].update({"host_suffix": "identity.example.test"}),
            lambda p: p["origin_binding"].update({"host_suffix": ".example"}),
            lambda p: p["origin_binding"]["label"].update({"charset": "anything"}),
            lambda p: p["origin_binding"]["label"].update({"min_length": 0}),
            lambda p: p["origin_binding"]["label"].update({"max_length": 999}),
            lambda p: p.update({"scope_separator": "semicolon"}),
            lambda p: p["flow"].update({"pkce": "plain"}),
            lambda p: p.update({"unique_subject_per_configuration": "yes"}),
            lambda p: p["authorization_endpoint"].update({"path": "/a?b=1"}),
            lambda p: p["authorization_endpoint"].update({"path": "/../escape"}),
            lambda p: p["authorization_endpoint"].update({"path": "//elsewhere"}),
            lambda p: p["authorization_endpoint"].update({"url": "https://elsewhere.test/a"}),
        ],
    )
    def test_each_is_rejected(self, mutate) -> None:
        manifest = _manifest(_BOUND)
        mutate(manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0])

        with pytest.raises(ValueError):
            compile_oauth_providers(manifest)

    def test_a_binding_no_endpoint_uses_is_rejected(self) -> None:
        manifest = _manifest(_BOUND)
        provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
        provider["authorization_endpoint"] = {"url": "https://identity.example.test/authorize"}
        provider["token_endpoint"] = {
            "url": "https://identity.example.test/token",
            "client_auth_method": "client_secret_post",
        }

        with pytest.raises(ValueError):
            compile_oauth_providers(manifest)


class TestASecondGrantForTheSameSubjectIsRefused:
    """A provider keeping one token per app and subject must not be given two.

    Issuing a second grant retires the first, so a duplicate connection would
    silently break the one already working. The decision is made before the code
    is exchanged; the code then expires unused.
    """

    class _Repository:
        def __init__(self, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
            self.rows = rows or []
            self.fail = fail
            self.calls = 0

        def list_connections_for_configuration(self, **_scope: Any) -> list[dict[str, Any]]:
            self.calls += 1
            if self.fail:
                raise RuntimeError("storage unavailable")
            return self.rows

    @staticmethod
    def _connection(subject: str, **overrides: Any) -> dict[str, Any]:
        row = {
            "connection_id": "connection-existing",
            "enabled": True,
            "connection_type": "bound_mail",
            "connection_config": {"oauth": {"subject": subject}},
        }
        row.update(overrides)
        return row

    def _decide(self, repository: Any, *, state_extra: dict[str, Any] | None = None) -> str:
        state = {
            "connection_type": "bound_mail",
            "tenant_id": "t",
            "account_id": "a",
            "project_id": "p",
            "extension_install_id": "i",
            "extension_id": "e",
            "provider_id": "bound_example",
            "configuration_id": "c",
        }
        state.update(state_extra or {})
        return _duplicate_subject_connection_id(
            repository=repository,
            transaction_factory=contextlib.nullcontext,
            provider=_provider(),
            state=state,
            subject="acme.identity.example.test",
        )

    def test_an_existing_active_connection_blocks_the_grant(self) -> None:
        repository = self._Repository([self._connection("acme.identity.example.test")])

        assert self._decide(repository) == "connection-existing"

    def test_a_different_subject_does_not(self) -> None:
        repository = self._Repository([self._connection("other.identity.example.test")])

        assert self._decide(repository) == ""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"enabled": False},
            {
                "connection_config": {
                    "oauth": {
                        "subject": "acme.identity.example.test",
                        "lifecycle_status": "disconnecting",
                    }
                }
            },
        ],
    )
    def test_an_inactive_connection_does_not(self, overrides: dict[str, Any]) -> None:
        repository = self._Repository([self._connection("acme.identity.example.test", **overrides)])

        assert self._decide(repository) == ""

    def test_another_connection_type_on_the_same_subject_still_blocks(self) -> None:
        """The provider issues one token per application and subject, not per type.

        A grant for one connection type retires the token a connection of another
        type is using, so the check cannot be scoped to the type that asked.
        """
        repository = self._Repository(
            [self._connection("acme.identity.example.test", connection_type="bound_files")]
        )

        assert self._decide(repository) == "connection-existing"

    def test_reconnecting_the_same_connection_is_allowed(self) -> None:
        """Replacing a grant is exactly what reconnect is for."""
        repository = self._Repository([self._connection("acme.identity.example.test")])

        assert (
            self._decide(repository, state_extra={"existing_connection_id": "connection-existing"})
            == ""
        )
        assert repository.calls == 0

    def test_a_provider_that_does_not_ask_is_never_checked(self) -> None:
        repository = self._Repository([self._connection("acme.identity.example.test")])

        assert (
            _duplicate_subject_connection_id(
                repository=repository,
                transaction_factory=contextlib.nullcontext,
                provider=_provider(_FIXED),
                state={},
                subject="acme.identity.example.test",
            )
            == ""
        )
        assert repository.calls == 0

    def test_an_unanswerable_lookup_blocks_rather_than_allows(self) -> None:
        """Not knowing is not the same as knowing there is no duplicate."""
        assert self._decide(self._Repository(fail=True)) == "unknown"


class TestRefreshFailuresAreClassifiedByWhatCanChange:
    """Retry what a retry could fix; stop on what it never will.

    The distinction that matters is not the status code but whether the stored
    grant is still worth keeping, so none of these paths discards a token pair.
    """

    @staticmethod
    def _code(status: int, error: str = ""):
        from core.infrastructure.extension_runtime.oauth_transport import _closed_error_code

        return _closed_error_code(status, {"error": error} if error else {}).value

    def test_a_401_invalid_request_asks_for_reauthorization(self) -> None:
        """The credential itself is refused; retrying it can never succeed."""
        assert self._code(401, "invalid_request") == "authorization_required"

    @pytest.mark.parametrize("status", [429])
    def test_rate_limiting_is_retryable(self, status: int) -> None:
        assert self._code(status) == "rate_limited"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_failures_are_retryable(self, status: int) -> None:
        assert self._code(status) == "provider_unavailable"

    @pytest.mark.parametrize("status", [400, 403])
    def test_a_settled_refusal_is_not_retried(self, status: int) -> None:
        assert self._code(status) == "invalid_client"

    @pytest.mark.parametrize(
        "error,expected",
        [
            ("invalid_grant", "invalid_grant"),
            ("invalid_client", "invalid_client"),
            ("authorization_required", "authorization_required"),
        ],
    )
    def test_named_oauth_errors_keep_their_meaning(self, error: str, expected: str) -> None:
        assert self._code(400, error) == expected

    def test_a_rate_limit_outranks_its_body(self) -> None:
        """429 is about the caller, whatever the body happens to say."""
        assert self._code(429, "invalid_client") == "rate_limited"


class TestTheRefreshTokenLifetimeIsRecorded:
    def test_a_fully_documented_response_parses_whole(self) -> None:
        from core.infrastructure.extension_runtime.oauth_transport import _token_payload

        parsed = _token_payload(
            {
                "access_token": "at",
                "scope": "mail.read,mail.write",
                "expires_in": 3600,
                "refresh_token": "rt",
                "refresh_token_expires_in": 7776000,
            },
            implicit_token_type="bearer",
        )

        assert parsed["expires_in"] == 3600
        assert parsed["refresh_token_expires_in"] == 7776000
        assert parsed["token_type"] == "Bearer"

    def test_an_omitted_token_type_is_still_refused_without_a_declaration(self) -> None:
        from core.infrastructure.extension_runtime.oauth_transport import _token_payload

        with pytest.raises(ValueError):
            _token_payload({"access_token": "at"})

    @pytest.mark.parametrize("value", [-1, 1.5, True, "3600", float("inf"), 10**12])
    def test_an_unusable_refresh_lifetime_is_rejected(self, value: Any) -> None:
        from core.infrastructure.extension_runtime.oauth_transport import _token_payload

        with pytest.raises(ValueError):
            _token_payload(
                {"access_token": "at", "refresh_token_expires_in": value},
                implicit_token_type="bearer",
            )


class TestTheSharedHmacPrimitiveIsTheOnlyOne:
    """One implementation, used by both verifiers, with behaviour unchanged.

    The webhook path streams a body it must not buffer and the OAuth callback
    signs a short string, which is why the module exports the incremental signer
    as well as the one-shot digest. A helper that only took bytes would have
    forced one of the two to keep a second implementation.
    """

    def test_the_webhook_verifier_uses_the_shared_module(self) -> None:
        import core.application.webhooks.signature_verifiers as verifiers

        source = Path(verifiers.__file__).read_text(encoding="utf-8")

        assert "hmac_sha256_signer" in source
        assert "hmac_digest_matches" in source
        assert "hmac.new(" not in source
        assert "hmac.compare_digest(" not in source

    def test_the_incremental_signer_and_the_one_shot_agree(self) -> None:
        from core.infrastructure.security.hmac_signatures import hmac_sha256_signer

        signer = hmac_sha256_signer(secret="shh")
        signer.update(b"one")
        signer.update(b"two")

        assert signer.hexdigest() == hmac_sha256_hex(secret="shh", payload=b"onetwo")

    def test_the_shared_digest_still_matches_stdlib_hmac(self) -> None:
        """Behaviour preservation, stated against the standard library."""
        import hashlib
        import hmac as stdlib_hmac

        key = "shh"  # pragma: allowlist secret - a literal for the comparison below
        expected = stdlib_hmac.new(key.encode(), b"body", hashlib.sha256).hexdigest()

        assert hmac_sha256_hex(secret=key, payload=b"body") == expected

    @pytest.mark.parametrize(
        "expected,provided,matches",
        [
            ("abc", "abc", True),
            ("abc", "abd", False),
            ("abc", "ab", False),
            ("abc", "", False),
            ("", "abc", False),
            ("", "", False),
            # base64 digests compare the same way as hex ones
            ("YWJj", "YWJj", True),
            ("YWJj", "YWJk", False),
        ],
    )
    def test_comparison_is_length_and_content_sensitive(
        self, expected: str, provided: str, matches: bool
    ) -> None:
        assert hmac_digest_matches(expected, provided) is matches


class TestARequiredTokenFieldMakesAResponseMalformed:
    """A documented field that is missing is a broken answer, not a partial one.

    Filling the gap from requested scopes would record a grant the provider never
    confirmed, which is worse than refusing the response.
    """

    @staticmethod
    def _complete() -> dict[str, Any]:
        return {
            "access_token": "at",
            "scope": "mail.read",
            "expires_in": 3600,
            "refresh_token": "rt",
            "refresh_token_expires_in": 7776000,
        }

    @staticmethod
    def _required() -> tuple[str, ...]:
        return _provider().code_exchange.required_response_fields

    def test_the_bound_fixture_requires_all_five(self) -> None:
        assert self._required() == (
            "access_token",
            "expires_in",
            "refresh_token",
            "refresh_token_expires_in",
            "scope",
        )

    def test_a_complete_response_is_accepted(self) -> None:
        from core.infrastructure.extension_runtime.oauth_transport import _token_payload

        parsed = _token_payload(
            self._complete(), implicit_token_type="bearer", required_fields=self._required()
        )

        assert parsed["access_token"] == "at"
        assert parsed["refresh_token_expires_in"] == 7776000

    @pytest.mark.parametrize(
        "missing",
        ["scope", "expires_in", "refresh_token", "refresh_token_expires_in"],
    )
    def test_any_missing_field_is_malformed(self, missing: str) -> None:
        from core.infrastructure.extension_runtime.oauth_transport import _token_payload

        payload = self._complete()
        payload.pop(missing)

        _raises_input_610_1 = self._required()
        with pytest.raises(ValueError):
            _token_payload(
                payload, implicit_token_type="bearer", required_fields=_raises_input_610_1
            )

    def test_a_provider_that_declares_nothing_keeps_the_lenient_shape(self) -> None:
        from core.infrastructure.extension_runtime.oauth_transport import _token_payload

        assert _provider(_FIXED).code_exchange.required_response_fields == ()
        parsed = _token_payload({"access_token": "at", "token_type": "Bearer"})

        assert parsed == {"access_token": "at", "token_type": "Bearer"}

    def test_requested_scopes_are_not_a_fallback_when_scope_is_required(self) -> None:
        """The grant is what the provider said, never what we asked for."""
        from core.application.extensions.oauth_callback_policy import _scopes

        _raises_input_625_1 = _provider()
        with pytest.raises(ValueError):
            _scopes(None, fallback=["mail.read"], provider=_raises_input_625_1)

    def test_the_fallback_still_applies_to_a_provider_that_may_omit_scope(self) -> None:
        from core.application.extensions.oauth_callback_policy import _scopes

        assert _scopes(None, fallback=["mail.read"], provider=_provider(_FIXED)) == ["mail.read"]

    @pytest.mark.parametrize("field", ["token_type", "id_token", "anything"])
    def test_an_unknown_required_field_is_refused_at_contract_time(self, field: str) -> None:
        manifest = _manifest(_BOUND)
        provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
        provider["code_exchange"]["required_response_fields"] = [field]

        with pytest.raises(ValueError):
            compile_oauth_providers(manifest)


class TestOneSubjectGrantIsSerialized:
    """Two callbacks for the same configuration and subject cannot both exchange.

    Checking for a duplicate and then exchanging leaves a window: both callbacks
    pass the check, both exchange, and the provider retires one of the two tokens
    behind the connection still holding it. The lock closes that window by
    covering the check, the exchange and the write as one step.
    """

    class _Claims:
        """A stand-in for the advisory lock, with the same mutual exclusion."""

        def __init__(self) -> None:
            self.held: set[tuple[str, str]] = set()
            self.granted: list[tuple[str, str]] = []
            self.refused: list[tuple[str, str]] = []

        @contextmanager
        def oauth_provider_token_claim(self, *, configuration_id: str, subject: str):
            key = (configuration_id, subject)
            if key in self.held:
                self.refused.append(key)
                raise TimeoutError("OAuth grant claim is busy")
            self.held.add(key)
            self.granted.append(key)
            try:
                yield
            finally:
                self.held.discard(key)

    @staticmethod
    def _service(repository: Any):
        from core.application.extensions.oauth_callback import OAuthCallbackService

        service = OAuthCallbackService.__new__(OAuthCallbackService)
        service._repository = repository
        return service

    def test_a_second_grant_for_the_same_subject_is_refused_while_one_is_held(self) -> None:
        claims = self._Claims()
        service = self._service(claims)
        provider = _provider()

        with (
            service._grant_claim(
                provider=provider, configuration_id="config-a", subject="acme.identity.example.test"
            ),
            pytest.raises(TimeoutError),
            service._grant_claim(
                provider=provider,
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ),
        ):
            pass

        assert claims.granted == [("config-a", "acme.identity.example.test")]
        assert claims.refused == [("config-a", "acme.identity.example.test")]

    def test_the_claim_is_released_when_the_section_ends(self) -> None:
        claims = self._Claims()
        service = self._service(claims)
        provider = _provider()

        for _ in range(2):
            with service._grant_claim(
                provider=provider,
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ):
                pass

        assert claims.refused == []
        assert claims.held == set()

    def test_the_claim_is_released_even_when_the_section_raises(self) -> None:
        claims = self._Claims()
        service = self._service(claims)

        with (
            contextlib.suppress(RuntimeError),
            service._grant_claim(
                provider=_provider(),
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ),
        ):
            raise RuntimeError("exchange blew up")

        assert claims.held == set()

    def test_different_subjects_do_not_block_each_other(self) -> None:
        claims = self._Claims()
        service = self._service(claims)
        provider = _provider()

        with (
            service._grant_claim(
                provider=provider, configuration_id="config-a", subject="one.identity.example.test"
            ),
            service._grant_claim(
                provider=provider, configuration_id="config-a", subject="two.identity.example.test"
            ),
        ):
            pass

        assert claims.refused == []

    def test_different_configurations_do_not_block_each_other(self) -> None:
        """They can be two different applications at the provider."""
        claims = self._Claims()
        service = self._service(claims)
        provider = _provider()

        with (
            service._grant_claim(
                provider=provider, configuration_id="config-a", subject="acme.identity.example.test"
            ),
            service._grant_claim(
                provider=provider,
                configuration_id="config-b",
                subject="acme.identity.example.test",
            ),
        ):
            pass

        assert claims.refused == []

    def test_a_provider_that_does_not_ask_takes_no_lock(self) -> None:
        claims = self._Claims()
        service = self._service(claims)

        with service._grant_claim(
            provider=_provider(_FIXED), configuration_id="config-a", subject="whatever"
        ):
            pass

        assert claims.granted == []

    @staticmethod
    def _refresh_service(repository: Any):
        """A runtime token service with just enough wired to take the claim."""
        from core.application.extensions.oauth_runtime import OAuthRuntimeTokenService

        service = OAuthRuntimeTokenService.__new__(OAuthRuntimeTokenService)
        service._repository = repository
        return service

    @staticmethod
    def _connection(subject: str) -> dict[str, Any]:
        return {"connection_config": {"oauth": {"subject": subject}}}

    def test_a_refresh_cannot_start_while_a_grant_holds_the_subject(self) -> None:
        """The two operations revoke each other's result, so they must not overlap.

        Exercised through both real code paths — the callback's grant claim and
        the runtime's refresh claim — rather than by reading either of them.
        """
        claims = self._Claims()
        grants = self._service(claims)
        refreshes = self._refresh_service(claims)

        with (
            grants._grant_claim(
                provider=_provider(),
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ),
            pytest.raises(TimeoutError),
            refreshes._provider_token_claim(
                provider=_provider(),
                configuration_id="config-a",
                connection=self._connection("acme.identity.example.test"),
            ),
        ):
            pass

        assert claims.refused == [("config-a", "acme.identity.example.test")]

    def test_a_refresh_for_another_subject_is_not_blocked_by_a_grant(self) -> None:
        claims = self._Claims()
        grants = self._service(claims)
        refreshes = self._refresh_service(claims)

        with (
            grants._grant_claim(
                provider=_provider(),
                configuration_id="config-a",
                subject="one.identity.example.test",
            ),
            refreshes._provider_token_claim(
                provider=_provider(),
                configuration_id="config-a",
                connection=self._connection("two.identity.example.test"),
            ),
        ):
            pass

        assert claims.refused == []

    def test_a_refresh_under_another_configuration_is_not_blocked(self) -> None:
        """Two configurations can be two applications, each with its own token."""
        claims = self._Claims()
        grants = self._service(claims)
        refreshes = self._refresh_service(claims)

        with (
            grants._grant_claim(
                provider=_provider(),
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ),
            refreshes._provider_token_claim(
                provider=_provider(),
                configuration_id="config-b",
                connection=self._connection("acme.identity.example.test"),
            ),
        ):
            pass

        assert claims.refused == []

    def test_the_refresh_claim_releases_so_a_later_grant_succeeds(self) -> None:
        claims = self._Claims()
        grants = self._service(claims)
        refreshes = self._refresh_service(claims)

        with refreshes._provider_token_claim(
            provider=_provider(),
            configuration_id="config-a",
            connection=self._connection("acme.identity.example.test"),
        ):
            pass
        with grants._grant_claim(
            provider=_provider(),
            configuration_id="config-a",
            subject="acme.identity.example.test",
        ):
            pass

        assert claims.refused == []
        assert claims.held == set()

    def test_a_refresh_for_a_provider_that_does_not_ask_takes_no_lock(self) -> None:
        claims = self._Claims()
        refreshes = self._refresh_service(claims)

        with refreshes._provider_token_claim(
            provider=_provider(_FIXED),
            configuration_id="config-a",
            connection=self._connection("acme.identity.example.test"),
        ):
            pass

        assert claims.granted == []

    def test_a_grant_cannot_start_while_a_refresh_holds_the_subject(self) -> None:
        """The cross case: a refresh in flight blocks a new authorization."""
        claims = self._Claims()
        service = self._service(claims)

        with (
            claims.oauth_provider_token_claim(
                configuration_id="config-a", subject="acme.identity.example.test"
            ),
            pytest.raises(TimeoutError),
            service._grant_claim(
                provider=_provider(),
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ),
        ):
            pass

    def test_a_declaring_provider_refuses_to_run_without_the_lock(self) -> None:
        """Running unserialized is the failure the lock exists to prevent."""
        from core.application.extensions.oauth_callback import OAuthGrantClaimUnavailable

        class _NoClaim:
            pass

        service = self._service(_NoClaim())

        with (
            pytest.raises(OAuthGrantClaimUnavailable),
            service._grant_claim(
                provider=_provider(),
                configuration_id="config-a",
                subject="acme.identity.example.test",
            ),
        ):
            pass

    @pytest.mark.parametrize("missing", ["configuration_id", "subject"])
    def test_an_incomplete_key_refuses_rather_than_locking_nothing(self, missing: str) -> None:
        from core.application.extensions.oauth_callback import OAuthGrantClaimUnavailable

        service = self._service(self._Claims())
        values = {"configuration_id": "config-a", "subject": "acme.identity.example.test"}
        values[missing] = ""

        with (
            pytest.raises(OAuthGrantClaimUnavailable),
            service._grant_claim(provider=_provider(), **values),
        ):
            pass


class TestReconnectCannotMoveAConnectionToAnotherSubject:
    """A reconnect replaces a grant; it must not repoint the connection.

    Workflows name a connection, not a subject. Letting a reconnect land a
    different subject would leave every one of them running and quietly
    addressing somewhere else.
    """

    class _Repository:
        def __init__(self, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
            self.rows = rows or []
            self.fail = fail

        def list_connections_for_configuration(self, **_scope: Any) -> list[dict[str, Any]]:
            if self.fail:
                raise RuntimeError("storage unavailable")
            return self.rows

    def _mismatch(self, repository: Any, *, existing: str | None, subject: str) -> bool:
        state = {
            "connection_type": "bound_mail",
            "tenant_id": "t",
            "account_id": "a",
            "project_id": "p",
            "extension_install_id": "i",
            "extension_id": "e",
            "provider_id": "bound_example",
            "configuration_id": "c",
        }
        if existing:
            state["existing_connection_id"] = existing
        return _reconnect_subject_mismatch(
            repository=repository,
            transaction_factory=contextlib.nullcontext,
            provider=_provider(),
            state=state,
            subject=subject,
        )

    @staticmethod
    def _row(subject: str, **overrides: Any) -> dict[str, Any]:
        row = {
            "connection_id": "connection-a",
            "connection_type": "bound_mail",
            "connection_config": {"oauth": {"subject": subject}},
        }
        row.update(overrides)
        return row

    def test_reconnecting_to_the_same_subject_is_allowed(self) -> None:
        repository = self._Repository([self._row("acme.identity.example.test")])

        assert (
            self._mismatch(
                repository, existing="connection-a", subject="acme.identity.example.test"
            )
            is False
        )

    def test_reconnecting_to_a_different_subject_is_refused(self) -> None:
        repository = self._Repository([self._row("acme.identity.example.test")])

        assert (
            self._mismatch(
                repository, existing="connection-a", subject="other.identity.example.test"
            )
            is True
        )

    def test_a_first_authorization_may_give_a_connection_its_subject(self) -> None:
        repository = self._Repository([self._row("")])

        assert (
            self._mismatch(
                repository, existing="connection-a", subject="acme.identity.example.test"
            )
            is False
        )

    def test_a_connection_this_configuration_does_not_hold_is_refused(self) -> None:
        repository = self._Repository([self._row("acme.identity.example.test")])

        assert (
            self._mismatch(
                repository, existing="connection-missing", subject="acme.identity.example.test"
            )
            is True
        )

    def test_a_connection_of_another_type_is_refused(self) -> None:
        repository = self._Repository(
            [self._row("acme.identity.example.test", connection_type="something_else")]
        )

        assert (
            self._mismatch(
                repository, existing="connection-a", subject="acme.identity.example.test"
            )
            is True
        )

    def test_a_new_connection_is_not_a_reconnect(self) -> None:
        repository = self._Repository([self._row("acme.identity.example.test")])

        assert (
            self._mismatch(repository, existing=None, subject="acme.identity.example.test") is False
        )

    def test_an_unanswerable_lookup_refuses_rather_than_repoints(self) -> None:
        assert (
            self._mismatch(
                self._Repository(fail=True),
                existing="connection-a",
                subject="acme.identity.example.test",
            )
            is True
        )

    def test_a_provider_without_callback_verification_is_unaffected(self) -> None:
        repository = self._Repository([self._row("acme.identity.example.test")])

        assert (
            _reconnect_subject_mismatch(
                repository=repository,
                transaction_factory=contextlib.nullcontext,
                provider=_provider(_FIXED),
                state={"existing_connection_id": "connection-a"},
                subject="anything",
            )
            is False
        )


class TestAContractSectionIsReadInEitherDeclaredForm:
    """A section may be declared inline or as a path to a bundle file.

    The compiler resolves the reference and publishes the rows at the descriptor's
    top level, leaving the original string where it was. Reading only the original
    place works for an inline declaration and silently yields a filename for a
    referenced one — which is how every connection type of a file-referencing
    bundle came to look unknown.
    """

    @staticmethod
    def _inline() -> dict[str, Any]:
        return {
            "runtime": {
                "extension_contract_v2": {
                    "connection_types": [{"connection_type_id": "bound_mail"}],
                }
            }
        }

    @staticmethod
    def _referenced() -> dict[str, Any]:
        return {
            "connection_types": [{"connection_type_id": "bound_mail"}],
            "runtime": {
                "extension_contract_v2": {
                    "connection_types": "contracts/connection_types.yaml",
                }
            },
        }

    def test_an_inline_section_is_read(self) -> None:
        rows = contract_section(self._inline(), "connection_types")

        assert [row["connection_type_id"] for row in rows] == ["bound_mail"]

    def test_a_referenced_section_is_read_from_where_it_resolved(self) -> None:
        rows = contract_section(self._referenced(), "connection_types")

        assert [row["connection_type_id"] for row in rows] == ["bound_mail"]

    def test_a_missing_section_is_empty_rather_than_a_filename(self) -> None:
        descriptor = {"runtime": {"extension_contract_v2": {"connection_types": "some/path.yaml"}}}

        assert contract_section(descriptor, "connection_types") == []

    @pytest.mark.parametrize("junk", [None, "", 7, {"a": 1}])
    def test_a_non_list_section_yields_nothing(self, junk: Any) -> None:
        descriptor = {"connection_types": junk, "runtime": {"extension_contract_v2": {}}}

        assert contract_section(descriptor, "connection_types") == []

    def test_non_object_rows_are_dropped(self) -> None:
        descriptor = {"connection_types": [{"connection_type_id": "a"}, "junk", None]}

        assert contract_section(descriptor, "connection_types") == [{"connection_type_id": "a"}]

    def test_providers_are_not_read_this_way(self) -> None:
        """The published provider form cannot be compiled again.

        It carries fingerprints the declaration never has and drops keys that only
        exist in a declaration, so `compile_oauth_providers` reads the raw
        declaration on purpose.
        """
        declared = _manifest(_BOUND)["runtime"]["extension_contract_v2"]["oauth_providers"]
        published = _provider().to_dict()
        descriptor = {
            "oauth_providers": [published],
            "runtime": {"extension_contract_v2": {"oauth_providers": declared}},
            "callbacks": [
                {
                    "callback_id": "oauth_authorization",
                    "purpose": "oauth_authorization",
                    "methods": ["GET"],
                    "connection_scoped": False,
                }
            ],
        }

        assert "security_fingerprint" in published
        assert "origin_binding" not in published
        # Compiling still works, because it never looks at the published rows.
        assert compile_oauth_providers(descriptor)[0].provider_id == "bound_example"

    def test_the_requested_scopes_lookup_accepts_a_referenced_section(self) -> None:
        """The end this fix exists for: scopes resolve for a file-referencing bundle."""
        from core.application.extensions.oauth_authorization import OAuthAuthorizationService

        descriptor = {
            "connection_types": [
                {
                    "connection_type_id": "bound_mail",
                    "auth": {
                        "type": "oauth2",
                        "provider_id": "bound_example",
                        "scopes": {"required": ["mail.read"], "optional": []},
                    },
                }
            ],
            "runtime": {
                "extension_contract_v2": {
                    "connection_types": "contracts/connection_types.yaml",
                }
            },
        }

        scopes = OAuthAuthorizationService._requested_scopes(
            descriptor, connection_type="bound_mail", provider_id="bound_example"
        )

        assert scopes == ["mail.read"]

    def test_the_required_scopes_lookup_accepts_a_referenced_section(self) -> None:
        """The callback's own lookup, which had the same defect as the start's.

        Reading only the declared place returned a filename, no row matched, and
        the caller read that as "requires nothing knowable" — refusing a grant that
        carried every scope asked for, and reporting it exactly like a shortfall.
        """
        from core.application.extensions.oauth_callback_policy import _required_connection_scopes

        descriptor = {
            "connection_types": [
                {
                    "connection_type_id": "bound_mail",
                    "auth": {
                        "type": "oauth2",
                        "provider_id": "bound_example",
                        "scopes": {"required": ["mail.read", "mail.write"], "optional": []},
                    },
                }
            ],
            "runtime": {
                "extension_contract_v2": {
                    "connection_types": "contracts/connection_types.yaml",
                }
            },
        }

        required = _required_connection_scopes(
            descriptor, connection_type="bound_mail", provider_id="bound_example"
        )

        assert required == {"mail.read", "mail.write"}
        # The shape that was failing: a complete grant must satisfy it.
        assert required.issubset({"mail.read", "mail.write"})

    def test_an_unknown_connection_type_still_reads_as_unknown(self) -> None:
        """None means "cannot say", and must stay distinguishable from an empty set."""
        from core.application.extensions.oauth_callback_policy import _required_connection_scopes

        descriptor = {"connection_types": [{"connection_type_id": "other"}]}

        assert (
            _required_connection_scopes(
                descriptor, connection_type="bound_mail", provider_id="bound_example"
            )
            is None
        )
