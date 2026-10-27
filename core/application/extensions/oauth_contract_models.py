"""Immutable extension OAuth contract models and canonical fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

DEFAULT_PKCE_MODE = "s256"


#: How a provider joins the scopes of a connection type into one parameter.
#: RFC 6749 says space; some providers answer only to comma.
SCOPE_SEPARATORS = {"space": " ", "comma": ","}


DEFAULT_SCOPE_SEPARATOR = "space"


ORIGIN_LABEL_CHARSETS = {
    "lowercase_alnum_hyphen": re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"),
    "lowercase_alnum": re.compile(r"^[a-z0-9]+$"),
}


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OAuthCredentialField:
    field_id: str
    value_type: str
    required: bool
    secret: bool
    change_policy: str
    label: str
    help_text: str
    placeholder: str
    autocomplete: str
    validation: dict[str, Any]
    browser_exposable: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "field_id": self.field_id,
            "value_type": self.value_type,
            "required": self.required,
            "secret": self.secret,
            "browser_exposable": self.browser_exposable,
            "change_policy": self.change_policy,
            "label": self.label,
            "help_text": self.help_text,
            "validation": dict(self.validation),
        }
        if self.placeholder:
            result["placeholder"] = self.placeholder
        if self.autocomplete:
            result["autocomplete"] = self.autocomplete
        return result

    def security_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "value_type": self.value_type,
            "required": self.required,
            "secret": self.secret,
            "browser_exposable": self.browser_exposable,
            "change_policy": self.change_policy,
            "validation": dict(self.validation),
        }


@dataclass(frozen=True)
class OAuthOriginBinding:
    """One authorization input promoted to the origin the endpoints are built on.

    The provider names the input, the fixed host suffix, and a bounded alphabet
    for the label. Core never sees a template and never evaluates an expression:
    it checks the label against the declared alphabet and joins it to the declared
    suffix. A provider cannot reach a host it did not already name.
    """

    binding_id: str
    input_id: str
    host_suffix: str
    charset: str
    min_length: int
    max_length: int

    def normalize(self, value: str) -> str:
        """The canonical host for one raw input value, or "" when it is not usable.

        A bare label and a value already carrying the suffix both normalize to the
        same host, so an operator may type either.
        """
        candidate = value.strip().lower().rstrip(".")
        if not candidate or len(candidate) > 255:
            return ""
        if candidate.endswith(self.host_suffix):
            label = candidate[: -len(self.host_suffix)]
        else:
            label = candidate
        if not self.min_length <= len(label) <= self.max_length:
            return ""
        if not ORIGIN_LABEL_CHARSETS[self.charset].match(label):
            return ""
        return f"{label}{self.host_suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "input_id": self.input_id,
            "host_suffix": self.host_suffix,
            "label": {
                "charset": self.charset,
                "min_length": self.min_length,
                "max_length": self.max_length,
            },
        }


@dataclass(frozen=True)
class OAuthCodeExchangeProfile:
    """Which fields the authorization-code exchange body carries.

    RFC 6749 requires `grant_type` and `redirect_uri`; some authorization servers
    reject both and take the code with the client credentials alone. The provider
    says which, and may add a closed set of static values. It cannot name a field
    whose value comes from anywhere but this declaration.
    """

    include_grant_type: bool = True
    include_redirect_uri: bool = True
    static_parameters: dict[str, str] = dataclass_field(default_factory=dict)
    #: What an omitted `token_type` means for this provider. Empty keeps the
    #: field required, which is the right default for a provider that sends it.
    implicit_token_type: str = ""
    #: Fields whose absence makes the response malformed rather than partial.
    required_response_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "include_grant_type": self.include_grant_type,
            "include_redirect_uri": self.include_redirect_uri,
            "static_parameters": dict(self.static_parameters),
            "implicit_token_type": self.implicit_token_type,
            "required_response_fields": list(self.required_response_fields),
        }


@dataclass(frozen=True)
class OAuthCallbackVerification:
    """How a signed callback proves it came from the provider.

    Every field is drawn from a closed vocabulary, so the policy is data rather
    than a hook: core knows how to compute one canonical message and one digest,
    and the provider says which parameter carries the signature and which carries
    the subject the connection will be bound to.
    """

    signature_parameter: str
    algorithm: str
    digest: str
    message: str
    secret_source: str
    subject_parameter: str
    subject_binding: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature_parameter": self.signature_parameter,
            "algorithm": self.algorithm,
            "digest": self.digest,
            "message": self.message,
            "secret_source": self.secret_source,
            "subject_parameter": self.subject_parameter,
            "subject_binding": self.subject_binding,
        }


@dataclass(frozen=True)
class OAuthEndpointPermission:
    """One endpoint, addressed one of exactly two ways.

    Either a fixed absolute HTTPS `url`, or an `origin_binding` naming a declared
    binding plus a fixed absolute `path`. There is no third form, so an endpoint
    can never be assembled from workflow input, callback input, or a header.
    """

    url: str = ""
    client_auth_method: str = ""
    origin_binding: str = ""
    path: str = ""

    @property
    def is_bound(self) -> bool:
        return bool(self.origin_binding)

    def resolve(self, origin: str) -> str:
        """The absolute URL for a verified origin.

        A fixed endpoint ignores the origin entirely; a bound one refuses to build
        anything without one, so a missing origin fails closed rather than
        silently addressing the wrong host.
        """
        if not self.is_bound:
            return self.url
        host = origin.strip().lower()
        if not host:
            return ""
        return f"https://{host}{self.path}"

    def bind(self, origin: str) -> OAuthEndpointPermission:
        """This endpoint as a fixed one, addressed at a verified origin.

        The transport only knows how to send to a URL, so a bound endpoint is
        resolved once, here, against an origin that has already been verified.
        Binding to nothing yields an endpoint with no URL, which every caller
        treats as unavailable rather than as a default.
        """
        if not self.is_bound:
            return self
        return OAuthEndpointPermission(
            url=self.resolve(origin), client_auth_method=self.client_auth_method
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.is_bound:
            result["origin_binding"] = self.origin_binding
            result["path"] = self.path
        else:
            result["url"] = self.url
        if self.client_auth_method:
            result["client_auth_method"] = self.client_auth_method
        return result


@dataclass(frozen=True)
class OAuthAuthorizationIntent:
    intent_id: str
    static_parameters: dict[str, Any]
    user_inputs: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "static_parameters": self.static_parameters,
            "user_inputs": [dict(row) for row in self.user_inputs],
        }


@dataclass(frozen=True)
class OAuthProviderContract:
    provider_id: str
    display_name: str
    description: str
    setup_page_id: str
    help_page_id: str
    error_help_anchors: dict[str, str]
    credential_fields: tuple[OAuthCredentialField, ...]
    authorization_endpoint: OAuthEndpointPermission
    token_endpoint: OAuthEndpointPermission
    revocation_endpoint: OAuthEndpointPermission | None
    callback_id: str
    authorization_intents: tuple[OAuthAuthorizationIntent, ...]
    post_connect_action_id: str | None
    pkce: str = DEFAULT_PKCE_MODE
    scope_separator: str = DEFAULT_SCOPE_SEPARATOR
    origin_binding: OAuthOriginBinding | None = None
    code_exchange: OAuthCodeExchangeProfile = dataclass_field(
        default_factory=OAuthCodeExchangeProfile
    )
    callback_verification: OAuthCallbackVerification | None = None
    unique_subject_per_configuration: bool = False

    @property
    def token_auth_method(self) -> str:
        return self.token_endpoint.client_auth_method

    @property
    def uses_pkce(self) -> bool:
        return self.pkce != "none"

    @property
    def scope_delimiter(self) -> str:
        return SCOPE_SEPARATORS[self.scope_separator]

    def join_scopes(self, scopes: Sequence[str]) -> str:
        """The scope parameter exactly as this provider expects to receive it."""
        return self.scope_delimiter.join(scopes)

    def split_scopes(self, granted: str) -> tuple[str, ...]:
        """Granted scopes read back from a token response.

        Split on the declared separator, but tolerate the other one too: a
        provider that documents commas has been known to answer with spaces, and
        mis-reading the grant would strand a working connection.
        """
        raw = granted.replace(",", " ") if self.scope_separator == "comma" else granted
        return tuple(item for item in raw.replace(",", " ").split() if item)

    def flow_dict(self) -> dict[str, Any]:
        return {"type": "authorization_code", "pkce": self.pkce}

    @property
    def intents(self) -> dict[str, OAuthAuthorizationIntent]:
        return {row.intent_id: row for row in self.authorization_intents}

    def _security_payload(self) -> dict[str, Any]:
        security_intents = []
        for intent in sorted(self.authorization_intents, key=lambda row: row.intent_id):
            intent_payload = intent.to_dict()
            intent_payload["user_inputs"] = sorted(
                intent_payload["user_inputs"], key=lambda row: str(row.get("input_id") or "")
            )
            security_intents.append(intent_payload)
        result: dict[str, Any] = {
            "provider_id": self.provider_id,
            "flow": self.flow_dict(),
            "scope_separator": self.scope_separator,
            "unique_subject_per_configuration": self.unique_subject_per_configuration,
            "code_exchange": self.code_exchange.to_dict(),
            "origin_binding": self.origin_binding.to_dict() if self.origin_binding else None,
            "callback_verification": (
                self.callback_verification.to_dict() if self.callback_verification else None
            ),
            "credential_fields": [
                row.security_dict()
                for row in sorted(self.credential_fields, key=lambda item: item.field_id)
                if row.change_policy != "cosmetic"
            ],
            "authorization_endpoint": self.authorization_endpoint.to_dict(),
            "token_endpoint": self.token_endpoint.to_dict(),
            "callback_id": self.callback_id,
            "authorization_intents": security_intents,
        }
        if self.post_connect_action_id is not None:
            result["post_connect_action_id"] = self.post_connect_action_id
        return result

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "description": self.description,
            "setup_page_id": self.setup_page_id,
            "help_page_id": self.help_page_id,
            "flow": self.flow_dict(),
            "credential_fields": [row.to_dict() for row in self.credential_fields],
            "authorization_endpoint": self.authorization_endpoint.to_dict(),
            "token_endpoint": self.token_endpoint.to_dict(),
            "callback_id": self.callback_id,
            "authorization_intents": [row.to_dict() for row in self.authorization_intents],
            "security_fingerprint": provider_security_fingerprint(self),
            "revocation_fingerprint": provider_revocation_fingerprint(self),
        }
        if self.revocation_endpoint:
            result["revocation_endpoint"] = self.revocation_endpoint.to_dict()
        if self.error_help_anchors:
            result["error_help_anchors"] = dict(sorted(self.error_help_anchors.items()))
        if self.post_connect_action_id is not None:
            result["post_connect_action_id"] = self.post_connect_action_id
        return result


def provider_security_fingerprint(provider: OAuthProviderContract) -> str:
    return _fingerprint(provider._security_payload())


def provider_revocation_fingerprint(provider: OAuthProviderContract) -> str:
    return _fingerprint(
        provider.revocation_endpoint.to_dict() if provider.revocation_endpoint else None
    )
