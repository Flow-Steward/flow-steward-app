"""Redact callback capability URLs and verification queries in HTTP access logs."""

from __future__ import annotations

import logging
import re

_CALLBACK_TARGET_RE = re.compile(r"(/webhook/extensions/[^/?\s]+/[^/?\s]+/)[^?\s]+(?:\?[^\s]*)?")


def _redact_target(value: str) -> str:
    return _CALLBACK_TARGET_RE.sub(r"\1<redacted>", value)


class CallbackAccessRedactionFilter(logging.Filter):
    """Handle uvicorn positional records and gunicorn's named access atoms."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                _redact_target(item) if isinstance(item, str) else item for item in args
            )
        elif isinstance(args, dict):
            callback_target = any(
                isinstance(args.get(key), str) and _CALLBACK_TARGET_RE.search(args[key])
                for key in ("r", "U")
            )
            if callback_target:
                redacted = dict(args)
                for key, value in args.items():
                    if key == "q":
                        redacted[key] = "<redacted>"
                    elif isinstance(value, str):
                        redacted[key] = _redact_target(value)
                record.args = redacted
        if isinstance(record.msg, str):
            record.msg = _redact_target(record.msg)
        return True


def install_callback_access_log_redaction() -> None:
    """Install once without replacing OAuth or application logging filters."""
    for name in ("uvicorn.access", "gunicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, CallbackAccessRedactionFilter) for item in logger.filters):
            logger.addFilter(CallbackAccessRedactionFilter())
