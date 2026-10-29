"""Structured credential redaction shared across application surfaces."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.application.management.command_specs.contracts import CommandSpec

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|dsn|passphrase|password|private[_-]?key|secret|session|token)",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>[a-z][a-z0-9+.-]*://[^:/@\s]+:)(?P<secret>[^@\s/]+)(?P<suffix>@)", re.IGNORECASE
)
_AUTHORIZATION_BEARER = re.compile(
    r"(?i)(?P<label>\bauthorization\s*[:=]\s*bearer)(?P<separator>\s+)(?P<value>[^\s,;]+)"
)
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)(?P<label>\bauthorization\s*[:=]\s*(?:[a-z][a-z0-9_-]*\s+)?)(?P<value>[^\s,;]+)"
)
_SPACE_DELIMITED_CREDENTIAL = re.compile(
    r"(?i)(?P<label>\b(?:bearer|token))(?P<separator>\s+)(?P<value>[^\s,;]+)"
)
_ASSIGNED_CREDENTIAL = re.compile(
    r"(?i)(?P<label>\b[a-z0-9_-]*(?:password|passphrase|api(?:[\s_-]?key)|bearer|cookie|credential|"
    r"session|token|secret|private(?:[\s_-]?key))[a-z0-9_-]*\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_QUOTED_MAPPING_ENTRY = re.compile(
    r"(?P<prefix>(?P<key_quote>['\"])(?P<key>(?:\\.|(?!(?P=key_quote))[^\\\r\n]){1,128})"
    r"(?P=key_quote)\s*:\s*)"
    r"(?P<value_quote>['\"])(?P<value>(?:\\.|[^\\\r\n])*?)(?P=value_quote)"
)
_OPAQUE_SECRET_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])("
    r"gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r")(?![A-Za-z0-9_])"
)


@dataclass(frozen=True)
class RedactionContext:
    """Known command secrets used to scrub values outside named secret fields."""

    secret_fields: frozenset[str] = frozenset()
    known_secrets: tuple[str, ...] = ()

    @classmethod
    def from_command_spec(cls, spec: CommandSpec, values: Mapping[str, object]) -> RedactionContext:
        secret_fields = frozenset(field.key for field in spec.fields if field.secret)
        known_secrets = tuple(
            str(values[field]).strip()
            for field in secret_fields
            if values.get(field) is not None and str(values[field]).strip()
        )
        return cls(secret_fields=secret_fields, known_secrets=known_secrets)


def redact(value: object, *, context: RedactionContext | None = None) -> object:
    """Return a deep redacted copy while leaving the supplied value untouched."""
    context = context or RedactionContext()
    if isinstance(value, BaseException):
        return redact_text(str(value), context=context)
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_secret_key(str(key), context) else redact(item, context=context)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, context=context) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, context=context) for item in value)
    if isinstance(value, set):
        return {redact(item, context=context) for item in value}
    if isinstance(value, str):
        return redact_text(value, context=context)
    return value


def redact_output(
    value: object,
    *,
    context: RedactionContext | None = None,
    secret_fields: frozenset[str] | None = None,
    public_usage_counter_paths: frozenset[tuple[str, ...]] = frozenset(),
    server_redacted_document_paths: frozenset[tuple[str, ...]] = frozenset(),
) -> object:
    """Redact output, preserving only schema-declared integer usage counters.

    A schema-declared server-redacted document (an export the server already
    masked by key) is scrubbed with `redact_document_text`, not word heuristics.
    """
    context = context or RedactionContext()
    explicit_secret_fields = frozenset(secret_fields or frozenset()) | context.secret_fields
    output_context = RedactionContext(
        secret_fields=explicit_secret_fields,
        known_secrets=context.known_secrets,
    )
    if isinstance(value, Mapping):
        return {
            key: _redact_output_member(
                str(key),
                item,
                output_context,
                public_usage_counter_paths,
                server_redacted_document_paths,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_output(item, context=context, secret_fields=explicit_secret_fields)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_output(item, context=context, secret_fields=explicit_secret_fields)
            for item in value
        )
    if isinstance(value, set):
        return {
            redact_output(item, context=context, secret_fields=explicit_secret_fields)
            for item in value
        }
    if isinstance(value, str):
        return redact_text(value, context=context)
    return value


def _redact_output_member(
    key: str,
    value: object,
    context: RedactionContext,
    paths: frozenset[tuple[str, ...]],
    document_paths: frozenset[tuple[str, ...]] = frozenset(),
) -> object:
    if _is_public_usage_counter(key, value, context, paths):
        return value
    if _is_secret_key(key, context):
        return _REDACTED
    if (key,) in document_paths and isinstance(value, str):
        return redact_document_text(value, context=context)
    child_paths = frozenset(path[1:] for path in paths if path and path[0] == key)
    child_document_paths = frozenset(
        path[1:] for path in document_paths if len(path) > 1 and path[0] == key
    )
    return redact_output(
        value,
        context=context,
        public_usage_counter_paths=child_paths,
        server_redacted_document_paths=child_document_paths,
    )


def _is_public_usage_counter(
    key: str,
    value: object,
    context: RedactionContext,
    paths: frozenset[tuple[str, ...]],
) -> bool:
    return (
        (key,) in paths
        and key not in context.secret_fields
        and type(value) is int
        and value >= 0
        and str(value) not in context.known_secrets
    )


def redact_document_text(text: str, *, context: RedactionContext | None = None) -> str:
    """Scrub a document whose secret-keyed values the server already masked.

    Known command secrets, URL credentials, opaque provider tokens and
    authorization headers are still removed. The word heuristics of
    `redact_text` are not applied: in prose and templates "token" and
    "password" are ordinary words, and replacing what follows them corrupts
    the document (`for token in words` would lose its `in`).
    """
    context = context or RedactionContext()
    sanitized = _URL_CREDENTIALS.sub(r"\g<prefix>" + _REDACTED + r"\g<suffix>", str(text))
    for secret in sorted(set(context.known_secrets), key=len, reverse=True):
        if secret:
            sanitized = sanitized.replace(secret, _REDACTED)
    sanitized = _OPAQUE_SECRET_TOKEN.sub(_REDACTED, sanitized)
    sanitized = _AUTHORIZATION_HEADER.sub(_replace_credential_value, sanitized)
    return _AUTHORIZATION_BEARER.sub(_replace_credential_value, sanitized)


def redact_text(text: str, *, context: RedactionContext | None = None) -> str:
    """Scrub known values and URL credentials from unstructured diagnostic text."""
    context = context or RedactionContext()
    sanitized = _URL_CREDENTIALS.sub(r"\g<prefix>" + _REDACTED + r"\g<suffix>", str(text))
    for secret in sorted(set(context.known_secrets), key=len, reverse=True):
        if secret:
            sanitized = sanitized.replace(secret, _REDACTED)
    sanitized = _OPAQUE_SECRET_TOKEN.sub(_REDACTED, sanitized)
    sanitized = _QUOTED_MAPPING_ENTRY.sub(
        lambda match: _replace_quoted_mapping_value(match, context=context),
        sanitized,
    )
    sanitized = _AUTHORIZATION_HEADER.sub(_replace_credential_value, sanitized)
    sanitized = _AUTHORIZATION_BEARER.sub(_replace_credential_value, sanitized)
    sanitized = _SPACE_DELIMITED_CREDENTIAL.sub(_replace_credential_value, sanitized)
    return _ASSIGNED_CREDENTIAL.sub(_replace_credential_value, sanitized)


def _replace_quoted_mapping_value(
    match: re.Match[str],
    *,
    context: RedactionContext,
) -> str:
    raw_key = match.group("key")
    key = _decode_quoted_mapping_key(raw_key, quote=match.group("key_quote"))
    if key is not None and not _is_secret_key(key, context):
        return match.group(0)
    if key is None and "\\" not in raw_key:
        return match.group(0)
    quote = match.group("value_quote")
    return f"{match.group('prefix')}{quote}{_REDACTED}{quote}"


def _decode_quoted_mapping_key(raw_key: str, *, quote: str) -> str | None:
    """Decode serializer escapes before deciding whether a mapping key is sensitive."""
    try:
        decoded = json.loads(f'"{raw_key}"') if quote == '"' else ast.literal_eval(f"'{raw_key}'")
    except (SyntaxError, ValueError):
        return None
    return decoded if isinstance(decoded, str) else None


def _replace_credential_value(match: re.Match[str]) -> str:
    return f"{match.group('label')}{match.group('separator') if 'separator' in match.groupdict() else ''}{_REDACTED}"


def _is_secret_key(key: str, context: RedactionContext) -> bool:
    if key in context.secret_fields:
        return True
    normalized = key.lower().replace("-", "_")
    if normalized in {"bearer_env_var", "token_env_var", "token_env_state"}:
        return False
    if normalized.endswith(("_ref", "_reference", "_id")):
        return False
    if normalized in {"must_change_password", "revoked_sessions"} or normalized.endswith(
        ("_session_count", "_session_total")
    ):
        return False
    return bool(_SENSITIVE_KEY.search(key))
