"""Keep HTTP input references opaque until an authorized outgoing-copy boundary."""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.application.extensions.secret_reflection import (
    collect_secret_values,
    value_exposes_secret,
)
from core.application.orchestration.http_response_decoding import _charset_from_content_type
from core.application.orchestration.step_templating import (
    _TEMPLATE_RE,
    _is_source_reference,
    _render_template_string,
    _resolve_path,
    _split_default_filter,
)
from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
from core.infrastructure.security.prompt_safety import (
    REDACTED_CREDENTIAL_MARKER,
    redact_sensitive_structure,
)
from core.infrastructure.security.secret_reference import (
    as_secret_reference,
    resolve_secret_reference,
)


class HttpSecretInputError(ValueError):
    """A reference cannot be used at this HTTP boundary."""


class HttpSecretExposureError(ValueError):
    """Protected material cannot leave the outgoing request copy."""


@dataclass(frozen=True, repr=False)
class _TemplateValue:
    value: Any


@dataclass(frozen=True, repr=False)
class _SecretTemplate:
    parts: tuple[str | _TemplateValue, ...]


def has_http_secret(value: Any) -> bool:
    if as_secret_reference(value) is not None or isinstance(value, _SecretTemplate):
        return True
    if isinstance(value, dict):
        return any(has_http_secret(nested) for nested in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_http_secret(nested) for nested in value)
    return False


def _render_http_template(template: str, state: dict[str, Any]) -> Any:
    parts: list[str | _TemplateValue] = []
    end = 0
    secret = False
    for match in _TEMPLATE_RE.finditer(template):
        parts.append(template[end : match.start()])
        selector, default, has_default = _split_default_filter(match.group(1).strip())
        value = _resolve_path(state, selector)
        if value in (None, "") and has_default:
            value = default
        secret = secret or has_http_secret(value)
        parts.append(_TemplateValue(value))
        end = match.end()
    if not secret:
        return _render_template_string(template, state)
    parts.append(template[end:])
    return _SecretTemplate(tuple(parts))


def render_http_value(value: Any, state: dict[str, Any]) -> Any:
    """Match deterministic templating without stringifying a secret reference."""
    if as_secret_reference(value) is not None:
        return value
    if _is_source_reference(value):
        return _resolve_path(state, str(value.get("source") or "").strip())
    if isinstance(value, str):
        return _render_http_template(value, state)
    if isinstance(value, list):
        return [render_http_value(item, state) for item in value]
    if isinstance(value, dict):
        return {str(key): render_http_value(item, state) for key, item in value.items()}
    return value


def _public_copy(value: Any) -> Any:
    if isinstance(value, _SecretTemplate):
        return REDACTED_CREDENTIAL_MARKER
    if isinstance(value, dict):
        if as_secret_reference(value) is not None:
            return REDACTED_CREDENTIAL_MARKER
        return {key: _public_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_copy(item) for item in value]
    return redact_sensitive_structure(value)


class HttpSecretBoundary:
    """Resolve only the outgoing copy under the immutable connector launch grant."""

    def __init__(self, state: dict[str, Any], read_ciphertext: Callable[[str, str], str | None]):
        self._state = state
        self._read_ciphertext = read_ciphertext
        self._values: list[str] = []
        self.protected_values: frozenset[str] = frozenset()
        self.has_references = False
        self.request_method = "GET"
        self.request_dispatched = False

    def public_control(self, value: Any) -> Any:
        if has_http_secret(value):
            self.has_references = True
            raise HttpSecretInputError("Secret references cannot be HTTP controls.")
        return value

    def metadata(self, value: Any) -> Any:
        return redact_sensitive_structure(_public_copy(value)) if self.has_references else value

    def _resolve_reference(self, value: Any) -> str:
        from core.infrastructure.security.secret_codec import decrypt_secret

        reference = as_secret_reference(value)
        assert reference is not None
        self.has_references = True
        snapshot = frozenset(
            str(item or "").strip()
            for item in (self._state.get("_workflow_secret_ref_fingerprints") or [])
            if str(item or "").strip()
        )
        context = dict(self._state.get("runtime_context") or {})
        authorized = frozenset(
            {reference.ref_key}
            if reference.ref_key and secret_ref_fingerprint(reference.ref_key) in snapshot
            else ()
        )
        self._values.extend((reference.ref_key, reference.ciphertext))
        resolved = resolve_secret_reference(
            reference,
            account_id=str(context.get("account_id") or "").strip(),
            authorized_ref_keys=authorized,
            authorized_inline_ref_fingerprints=snapshot,
            read_ciphertext=self._read_ciphertext,
            decrypt=decrypt_secret,
        )
        self._values.append(resolved)
        self.protected_values, exceeded = collect_secret_values(self._values)
        if exceeded:
            raise HttpSecretExposureError("HTTP secret safety budget exceeded.")
        return resolved

    def resolve(self, value: Any) -> Any:
        if as_secret_reference(value) is not None:
            return self._resolve_reference(value)
        if isinstance(value, _SecretTemplate):
            return "".join(
                part if isinstance(part, str) else self._template_text(self.resolve(part.value))
                for part in value.parts
            )
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.resolve(item) for item in value]
        return value

    @staticmethod
    def _template_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def ensure_safe(self, value: Any) -> None:
        if self.has_references and value_exposes_secret(value, self.protected_values):
            raise HttpSecretExposureError("HTTP response exposed protected input material.")

    def safe_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.has_references:
            self.ensure_safe(result)
            result = redact_sensitive_structure(result)
        return result

    def response_chunks(self, chunks: Iterable[bytes], content_type: str) -> Iterable[bytes]:
        """Check decoded bytes before storage, retaining overlap across chunk boundaries."""
        if not self.protected_values:
            yield from chunks
            return
        charset = _charset_from_content_type(content_type)
        try:
            decoder = codecs.getincrementaldecoder(charset)(errors="replace")
        except LookupError:
            charset = "utf-8"
            decoder = codecs.getincrementaldecoder(charset)(errors="replace")
        decoders = [decoder]
        if codecs.lookup(charset).name != "utf-8":
            decoders.append(codecs.getincrementaldecoder("utf-8")(errors="replace"))
        # The declared charset cannot authorize raw UTF-8 credentials in stored
        # bytes. Keep both representations behind the bounded byte overlap.
        overlap = (
            max(
                max(len(value.encode(charset, errors="replace")), len(value.encode("utf-8")))
                for value in self.protected_values
            )
            + 8
        )
        text_overlap = max(len(value) for value in self.protected_values) + 1
        pending = b""
        tails = [""] * len(decoders)
        for chunk in chunks:
            data = bytes(chunk)
            for index, decoder in enumerate(decoders):
                text = tails[index] + decoder.decode(data)
                self.ensure_safe(text)
                tails[index] = text[-text_overlap:]
            pending += data
            if len(pending) > overlap:
                yield pending[:-overlap]
                pending = pending[-overlap:]
        for tail, decoder in zip(tails, decoders, strict=True):
            self.ensure_safe(tail + decoder.decode(b"", final=True))
        if pending:
            yield pending
