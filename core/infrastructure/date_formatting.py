"""Deterministic date tokenization and formatting without process locale state."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.infrastructure.date_types import (
    DateLexicon,
    DateValue,
    DateValueError,
    validate_date_value,
)

TOKENS = (
    "YYYY",
    "MMMM",
    "LLLL",
    "dddd",
    "MMM",
    "LLL",
    "ddd",
    "YY",
    "MM",
    "DD",
    "HH",
    "hh",
    "mm",
    "ss",
    "ZZ",
    "M",
    "D",
    "H",
    "h",
    "A",
    "a",
    "Z",
    "X",
    "x",
)
MAX_FORMAT_LENGTH = 128
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def tokenize_format(value: str) -> tuple[tuple[bool, str], ...]:
    """Return (is_token, text) segments, rejecting bare unknown letters."""
    if not isinstance(value, str) or not value:
        raise DateValueError("unknown_format_token", "Date format must be a nonempty string.")
    if len(value) > MAX_FORMAT_LENGTH:
        raise DateValueError("input_too_long", "Date format is limited to 128 characters.")
    result: list[tuple[bool, str]] = []
    index = 0
    while index < len(value):
        if value[index] == "[":
            end = value.find("]", index + 1)
            if end < 0:
                raise DateValueError(
                    "unknown_format_token", "Unclosed date format literal; use [literal]."
                )
            result.append((False, value[index + 1 : end]))
            index = end + 1
            continue
        token = next((item for item in TOKENS if value.startswith(item, index)), None)
        if token:
            result.append((True, token))
            index += len(token)
        elif value[index].isalpha() or value[index] == "]":
            raise DateValueError(
                "unknown_format_token",
                f"Unknown date format token at {value[index:]!r}; bracket literal text.",
            )
        else:
            result.append((False, value[index]))
            index += 1
    # One calendar field per format bounds regex work and avoids ignored conflicts.
    fields = {
        "YYYY": "year",
        "YY": "year",
        "MM": "month",
        "M": "month",
        "MMM": "month",
        "MMMM": "month",
        "LLL": "month",
        "LLLL": "month",
        "DD": "day",
        "D": "day",
        "ddd": "weekday",
        "dddd": "weekday",
        "HH": "hour",
        "H": "hour",
        "hh": "hour",
        "h": "hour",
        "mm": "minute",
        "ss": "second",
        "A": "meridian",
        "a": "meridian",
        "Z": "offset",
        "ZZ": "offset",
        "X": "epoch",
        "x": "epoch",
    }
    seen: set[str] = set()
    previous_variable = False
    for is_token, text in result:
        if not is_token:
            if text:
                previous_variable = False
            continue
        field = fields[text]
        if field in seen:
            raise DateValueError("unknown_format_token", f"Date format repeats the {field} field.")
        seen.add(field)
        numeric = text in {
            "YYYY",
            "YY",
            "MM",
            "M",
            "DD",
            "D",
            "HH",
            "H",
            "hh",
            "h",
            "mm",
            "ss",
            "X",
            "x",
        }
        variable = text in {"M", "D", "H", "h", "X", "x"}
        if not numeric:
            previous_variable = False
        if previous_variable and variable:
            raise DateValueError(
                "unknown_format_token",
                "Adjacent variable-width numeric date tokens require a separator.",
            )
        previous_variable = previous_variable or variable
    if "epoch" in seen and len(seen) != 1:
        raise DateValueError(
            "unknown_format_token", "Unix timestamp tokens cannot be mixed with calendar fields."
        )
    return tuple(result)


def validate_format(value: str) -> None:
    """Validate the supported format grammar before action execution."""
    tokenize_format(value)


def canonical_iso(value: DateValue) -> str:
    """Serialize without losing milliseconds or inventing a timezone."""
    validate_date_value(value)
    if isinstance(value, datetime):
        text = value.isoformat(timespec="milliseconds" if value.microsecond else "seconds")
        return text[:-6] + "Z" if text.endswith("+00:00") else text
    return value.isoformat()


def _epoch_text(value: DateValue, *, milliseconds: bool) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DateValueError("timezone_required", "Unix timestamps require an absolute moment.")
    try:
        delta = value.astimezone(UTC) - EPOCH
    except (OverflowError, ValueError) as exc:
        raise DateValueError(
            "invalid_date", "Unix conversion exceeds the supported calendar range."
        ) from exc
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    if milliseconds:
        return str(micros // 1000)
    return (
        format(Decimal(micros) / Decimal(1_000_000), "f").rstrip("0").rstrip(".")
        if micros % 1_000_000
        else str(micros // 1_000_000)
    )


def _token_value(token: str, value: DateValue, lexicon: DateLexicon) -> str:
    month = value.month - 1
    weekday = value.isoweekday() - 1
    calendar = {
        "YYYY": f"{value.year:04d}",
        "YY": f"{value.year % 100:02d}",
        "MM": f"{value.month:02d}",
        "M": str(value.month),
        "DD": f"{value.day:02d}",
        "D": str(value.day),
        "MMM": lexicon.months_abbr[month],
        "MMMM": lexicon.months_full[month],
        "LLL": lexicon.months_abbr_standalone[month],
        "LLLL": lexicon.months_full_standalone[month],
        "ddd": lexicon.weekdays_abbr[weekday],
        "dddd": lexicon.weekdays_full[weekday],
    }
    if token in calendar:
        return calendar[token]
    if token in {"X", "x"}:
        return _epoch_text(value, milliseconds=token == "x")
    if not isinstance(value, datetime):
        raise DateValueError("date_required", f"Token {token} needs a time component.")
    clock = {
        "HH": f"{value.hour:02d}",
        "H": str(value.hour),
        "hh": f"{value.hour % 12 or 12:02d}",
        "h": str(value.hour % 12 or 12),
        "mm": f"{value.minute:02d}",
        "ss": f"{value.second:02d}",
        "A": "AM" if value.hour < 12 else "PM",
        "a": "am" if value.hour < 12 else "pm",
    }
    if token in clock:
        return clock[token]
    offset = value.utcoffset()
    if offset is None:
        raise DateValueError("timezone_required", "Offset output requires an absolute moment.")
    if offset.microseconds:
        raise DateValueError(
            "invalid_date", "Fractional-second timezone offsets cannot be represented by Z/ZZ."
        )
    seconds = offset.days * 86400 + offset.seconds
    sign = "+" if seconds >= 0 else "-"
    hours, remaining = divmod(abs(seconds), 3600)
    minutes, seconds = divmod(remaining, 60)
    separator = ":" if token == "Z" else ""
    suffix = f"{separator}{seconds:02d}" if seconds else ""
    return f"{sign}{hours:02d}{separator}{minutes:02d}{suffix}"


def format_date(value: DateValue, output_format: str | None = None, *, lexicon: DateLexicon) -> str:
    """Format using explicit translated data and the bounded token grammar."""
    validate_date_value(value)
    if output_format is None:
        return canonical_iso(value)
    return "".join(
        _token_value(text, value, lexicon) if is_token else text
        for is_token, text in tokenize_format(output_format)
    )
