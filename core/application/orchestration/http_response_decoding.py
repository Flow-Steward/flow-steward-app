"""Bounded HTTP response decoding and safe download filename parsing."""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable
from email.message import Message
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

import httpx

_MESSAGE_RESPONSE_EXCEEDS_MAX_RESPONSE_BYTES = "response exceeds max_response_bytes"

RESPONSE_TEXT_EXCERPT_CHARS = 2000

DECODED_RESPONSE_CHUNK_BYTES = 64 * 1024

_PERCENT_HEX = set("0123456789ABCDEFabcdef")

_RFC5987_ATTR_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$&+-.^_`|~"
)

_RFC5987_MIME_CHARSET_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&+-^_`{}~"
)


class _ResponseTooLargeError(ValueError):
    """Raised when decoded response bytes cross the configured cap."""


def _response_payload(response: httpx.Response) -> tuple[Any, str]:
    content_type = str(response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            parsed = response.json()
            excerpt = json.dumps(parsed, ensure_ascii=False)[:2000]
            return parsed, excerpt
        except Exception:
            pass
    text = response.text
    return text[:2000], text[:2000]


def _bounded_prefix(chunks: Iterable[bytes], *, limit: int) -> bytes:
    buffer = bytearray()
    for chunk in chunks:
        data = bytes(chunk or b"")
        if not data:
            continue
        remaining = limit - len(buffer)
        if remaining <= 0:
            break
        buffer.extend(data[:remaining])
        if len(buffer) >= limit:
            break
    return bytes(buffer)


def _charset_from_content_type(content_type: str) -> str:
    msg = Message()
    msg["content-type"] = str(content_type or "")
    return msg.get_content_charset() or "utf-8"


def _decode_text(payload: bytes, content_type: str) -> str:
    charset = _charset_from_content_type(content_type)
    try:
        return bytes(payload).decode(charset, errors="replace")
    except LookupError:
        return bytes(payload).decode("utf-8", errors="replace")


def _decode_excerpt(payload: bytes, content_type: str = "") -> str:
    return _decode_text(payload, content_type)[:RESPONSE_TEXT_EXCERPT_CHARS]


def _inline_payload_from_bytes(payload: bytes, content_type: str) -> tuple[Any, str]:
    if "application/json" in str(content_type or "").lower():
        try:
            parsed = json.loads(bytes(payload))
            return parsed, json.dumps(parsed, ensure_ascii=False)[:RESPONSE_TEXT_EXCERPT_CHARS]
        except Exception:
            pass
    excerpt = _decode_excerpt(payload, content_type)
    return excerpt, excerpt


def _collect_inline_body(chunks: Iterable[bytes], max_size: int) -> tuple[bool, bytes]:
    buffer = bytearray()
    for chunk in chunks:
        data = bytes(chunk or b"")
        if not data:
            continue
        if len(buffer) + len(data) > max_size:
            return False, b""
        buffer.extend(data)
    return True, bytes(buffer)


def _content_encodings(headers: httpx.Headers | dict[str, Any]) -> list[str]:
    raw = str(headers.get("content-encoding") or "").strip().lower()
    if not raw or raw == "identity":
        return []
    return [part.strip() for part in raw.split(",") if part.strip() and part.strip() != "identity"]


def _decoded_identity_chunks(
    response: httpx.Response,
    *,
    max_size: int,
    stop_at_limit: bool,
) -> Iterable[bytes]:
    total = 0
    for chunk in response.iter_raw(chunk_size=DECODED_RESPONSE_CHUNK_BYTES):
        data = bytes(chunk or b"")
        if not data:
            continue
        remaining = max_size - total
        if len(data) > remaining:
            if stop_at_limit and remaining > 0:
                yield data[:remaining]
                return
            raise _ResponseTooLargeError(_MESSAGE_RESPONSE_EXCEEDS_MAX_RESPONSE_BYTES)
        total += len(data)
        yield data
        if stop_at_limit and total >= max_size:
            return


def _zlib_decoder(encoding: str, *, raw_deflate: bool = False) -> Any:
    if encoding == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        wbits = -zlib.MAX_WBITS if raw_deflate else zlib.MAX_WBITS
        return zlib.decompressobj(wbits)
    raise httpx.DecodingError(f"Unsupported content-encoding for bounded http_request: {encoding}")


def _bounded_decoder_output_limit(total: int, max_size: int, stop_at_limit: bool) -> int:
    remaining = max_size - total
    if stop_at_limit:
        return max(1, min(DECODED_RESPONSE_CHUNK_BYTES, max(remaining, 0)))
    return max(1, min(DECODED_RESPONSE_CHUNK_BYTES, remaining + 1))


def _raise_if_decoder_not_complete(decoder: Any, encoding: str) -> None:
    if bool(getattr(decoder, "unused_data", b"")):
        raise httpx.DecodingError(f"Concatenated {encoding} response is not supported")
    if not bool(getattr(decoder, "eof", False)):
        raise httpx.DecodingError(f"Incomplete {encoding} response")


def _decoded_zlib_chunks(
    response: httpx.Response,
    *,
    encoding: str,
    max_size: int,
    stop_at_limit: bool,
) -> Iterable[bytes]:
    decoder = _zlib_decoder(encoding)
    total = 0
    raw_deflate = False
    try:
        for raw in response.iter_raw(chunk_size=DECODED_RESPONSE_CHUNK_BYTES):
            pending = bytes(raw or b"")
            while pending:
                remaining = max_size - total
                if stop_at_limit and remaining <= 0:
                    return
                output_limit = _bounded_decoder_output_limit(total, max_size, stop_at_limit)
                before_pending = pending
                try:
                    decoded = decoder.decompress(pending, output_limit)
                except zlib.error:
                    if encoding != "deflate" or total > 0 or raw_deflate:
                        raise
                    decoder = _zlib_decoder(encoding, raw_deflate=True)
                    raw_deflate = True
                    decoded = decoder.decompress(pending, output_limit)
                pending = decoder.unconsumed_tail
                if not decoded and not pending:
                    break
                if not decoded and pending == before_pending:
                    raise httpx.DecodingError(
                        f"Failed to make progress decoding {encoding} response"
                    )
                if len(decoded) > remaining:
                    if stop_at_limit:
                        yield decoded[:remaining]
                        return
                    raise _ResponseTooLargeError(_MESSAGE_RESPONSE_EXCEEDS_MAX_RESPONSE_BYTES)
                if decoded:
                    total += len(decoded)
                    yield decoded
                    if stop_at_limit and total >= max_size:
                        return
        if stop_at_limit:
            return
        _raise_if_decoder_not_complete(decoder, encoding)
        remaining = max_size - total
        output_limit = _bounded_decoder_output_limit(total, max_size, stop_at_limit=False)
        decoded = decoder.flush(output_limit)
        if not decoded:
            return
        if len(decoded) > remaining:
            raise _ResponseTooLargeError(_MESSAGE_RESPONSE_EXCEEDS_MAX_RESPONSE_BYTES)
        yield decoded
    except zlib.error as exc:
        raise httpx.DecodingError(f"Failed to decode {encoding} response: {exc}") from exc


def _decoded_response_chunks(
    response: httpx.Response,
    *,
    max_size: int,
    stop_at_limit: bool = False,
) -> Iterable[bytes]:
    encodings = _content_encodings(response.headers)
    if not encodings:
        yield from _decoded_identity_chunks(
            response,
            max_size=max_size,
            stop_at_limit=stop_at_limit,
        )
        return
    if len(encodings) == 1 and encodings[0] in {"gzip", "deflate"}:
        yield from _decoded_zlib_chunks(
            response,
            encoding=encodings[0],
            max_size=max_size,
            stop_at_limit=stop_at_limit,
        )
        return
    raise httpx.DecodingError(
        "Unsupported content-encoding for bounded http_request: " + ", ".join(encodings)
    )


def _trustworthy_content_length(headers: httpx.Headers | dict[str, Any]) -> int | None:
    raw = str(headers.get("content-length") or "").strip()
    if not raw:
        return None
    encoding = str(headers.get("content-encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _response_can_have_body(method: str, status_code: int) -> bool:
    if str(method or "").upper() == "HEAD":
        return False
    if status_code in {204, 304}:
        return False
    return not 100 <= status_code <= 199


def _media_type(content_type: str) -> str:
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    return value or "application/octet-stream"


def _safe_plain_filename(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("artifact filename is empty")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in text):
        raise ValueError("artifact filename contains control characters")
    if "\x00" in text or "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError("artifact filename must be a safe plain filename")
    return text


def _validate_percent_triplets(value: str) -> None:
    idx = 0
    while idx < len(value):
        if value[idx] != "%":
            idx += 1
            continue
        if (
            idx + 2 >= len(value)
            or value[idx + 1] not in _PERCENT_HEX
            or value[idx + 2] not in _PERCENT_HEX
        ):
            raise ValueError("artifact filename contains invalid percent encoding")
        idx += 3


def _validate_rfc5987_value_chars(value: str) -> None:
    idx = 0
    while idx < len(value):
        char = value[idx]
        if char == "%":
            if (
                idx + 2 >= len(value)
                or value[idx + 1] not in _PERCENT_HEX
                or value[idx + 2] not in _PERCENT_HEX
            ):
                raise ValueError("artifact filename contains invalid percent encoding")
            idx += 3
            continue
        if char not in _RFC5987_ATTR_CHARS:
            raise ValueError("artifact filename* contains invalid RFC 5987 value characters")
        idx += 1


def _validate_rfc5987_charset(value: str) -> None:
    charset = str(value or "")
    if not charset or any(char not in _RFC5987_MIME_CHARSET_CHARS for char in charset):
        raise ValueError("artifact filename* charset is not valid RFC 5987 data")


def _validate_rfc5987_language(value: str) -> None:
    language = str(value or "")
    if not language:
        return
    parts = language.split("-")
    if any(not part or len(part) > 8 or not part.isascii() or not part.isalnum() for part in parts):
        raise ValueError("artifact filename* language is not valid RFC 5987 data")
    if not parts[0].isalpha() or not 2 <= len(parts[0]) <= 8:
        raise ValueError("artifact filename* language is not valid RFC 5987 data")


def _unquote_to_text_strict(value: str, *, charset: str) -> str:
    _validate_percent_triplets(value)
    try:
        return unquote_to_bytes(value).decode(charset, errors="strict")
    except LookupError as exc:
        raise ValueError("artifact filename uses unsupported charset") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("artifact filename contains invalid encoded filename data") from exc


def _unquote_header_parameter_value(value: str) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw, False
    unescaped: list[str] = []
    escaped = False
    for char in raw[1:-1]:
        if escaped:
            unescaped.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        unescaped.append(char)
    if escaped:
        unescaped.append("\\")
    return "".join(unescaped), True


def _split_content_disposition_params(value: str) -> dict[str, tuple[str, bool]]:
    params: dict[str, tuple[str, bool]] = {}
    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    escaped = False
    for char in str(value or ""):
        if escaped:
            current.append(char)
            escaped = False
            continue
        if in_quote and char == "\\":
            current.append(char)
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            current.append(char)
            continue
        if char == ";" and not in_quote:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())

    for part in parts[1:]:
        name, separator, raw_value = part.partition("=")
        if not separator:
            continue
        name = name.strip().lower()
        if name and name not in params:
            params[name] = _unquote_header_parameter_value(raw_value)
    return params


def _decode_rfc5987_filename(value: str, *, quoted: bool) -> str:
    if quoted:
        raise ValueError("artifact filename* must be an unquoted RFC 5987 ext-value")
    charset, separator, rest = str(value or "").partition("'")
    if not separator or not charset:
        raise ValueError("artifact filename* is not valid RFC 5987 data")
    _validate_rfc5987_charset(charset)
    language, separator, encoded = rest.partition("'")
    if not separator:
        raise ValueError("artifact filename* is not valid RFC 5987 data")
    _validate_rfc5987_language(language)
    _validate_rfc5987_value_chars(encoded)
    return _safe_plain_filename(_unquote_to_text_strict(encoded, charset=charset))


def _filename_from_content_disposition(value: str) -> str:
    if not str(value or "").strip():
        return ""
    params = _split_content_disposition_params(value)
    if "filename*" in params:
        filename_ext, quoted = params["filename*"]
        return _decode_rfc5987_filename(filename_ext, quoted=quoted)
    filename = params.get("filename", ("", False))[0]
    return _safe_plain_filename(filename) if filename else ""


def _filename_from_url_path(rendered_url: str) -> str:
    basename = str(urlparse(rendered_url).path or "").rstrip("/").rsplit("/", 1)[-1]
    if not basename:
        return ""
    return _safe_plain_filename(_unquote_to_text_strict(basename, charset="utf-8"))
