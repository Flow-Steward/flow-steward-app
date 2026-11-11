"""The date and time formats a user reads every timestamp in.

The interface draws every date with the user's chosen pattern and every time with
the chosen clock. Stored values are pattern ids, never free text; an empty value means
the default (``DEFAULT_DATE_FORMAT`` with ``DEFAULT_TIME_FORMAT``).
"""

from __future__ import annotations

# Pattern ids the interface knows how to draw. ``MMM`` is the month's short name in the
# interface language ("Nov", "lapkr.").
DATE_FORMATS: tuple[str, ...] = (
    "D MMM YYYY",
    "MMM D, YYYY",
    "DD.MM.YYYY",
    "DD/MM/YYYY",
    "MM/DD/YYYY",
    "YYYY-MM-DD",
)
TIME_FORMATS: tuple[str, ...] = ("24h", "12h")

DEFAULT_DATE_FORMAT = DATE_FORMATS[0]
DEFAULT_TIME_FORMAT = TIME_FORMATS[0]


def normalize_date_format(value: object) -> str:
    """Return a known date pattern id, ``""`` for none, or raise ``ValueError``."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text not in DATE_FORMATS:
        raise ValueError("Unsupported date format")
    return text


def normalize_time_format(value: object) -> str:
    """Return a known clock id, ``""`` for none, or raise ``ValueError``."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text not in TIME_FORMATS:
        raise ValueError("Unsupported time format")
    return text


def stored_date_format(value: object) -> str:
    """A stored pattern as the interface should read it: an unknown one means the default."""
    try:
        return normalize_date_format(value) or DEFAULT_DATE_FORMAT
    except ValueError:
        return DEFAULT_DATE_FORMAT


def stored_time_format(value: object) -> str:
    """A stored clock as the interface should read it: an unknown one means the default."""
    try:
        return normalize_time_format(value) or DEFAULT_TIME_FORMAT
    except ValueError:
        return DEFAULT_TIME_FORMAT
