"""Strict date parsing with explicit formats, locale data and millisecond precision."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from core.infrastructure.date_formatting import EPOCH, tokenize_format
from core.infrastructure.date_types import (
    DateLexicon,
    DateValue,
    DateValueError,
    normalize_name,
    validate_date_value,
)

MAX_INPUT_LENGTH = 256
_NUMERIC_DATE = re.compile(r"^(\d{1,4})([-/.])(\d{1,2})\2(\d{2,4})(?=$|[ T])")
_ISO_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?=$|[ T])")
_CLOCK = re.compile(
    r"^(\d{1,2})(?::(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(?:(am|pm)\s*)?(Z|[+-](?:\d{2}:\d{2}(?::\d{2})?|\d{4}(?:\d{2})?)|UTC[+-]\d{1,2})?$",
    re.IGNORECASE,
)


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise DateValueError("invalid_date", "Date input must be text.")
    if len(value) > MAX_INPUT_LENGTH:
        raise DateValueError("input_too_long", "Date input is limited to 256 characters.")
    clean = unicodedata.normalize("NFC", value)
    for char in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        clean = clean.replace(char, "")
    return " ".join(clean.split())


def _precision(fraction: str) -> int:
    if any(char != "0" for char in fraction[3:]):
        raise DateValueError(
            "invalid_date", "Submillisecond precision is not supported; no digits are discarded."
        )
    return int((fraction[:3] + "000")[:3]) * 1000


def _offset(value: str | None):
    if not value:
        return None
    if value.upper() == "Z":
        return UTC
    if value.upper().startswith("UTC"):
        value = value[3:]
        value = value[0] + value[1:].zfill(2) + ":00"
    clean = value.replace(":", "")
    hours, minutes = int(clean[1:3]), int(clean[3:5])
    seconds = int(clean[5:7]) if len(clean) == 7 else 0
    if hours > 23 or minutes > 59 or seconds > 59:
        raise DateValueError("invalid_date", "UTC offset must have valid hours and minutes.")
    return timezone(
        timedelta(seconds=(hours * 3600 + minutes * 60 + seconds) * (-1 if clean[0] == "-" else 1))
    )


def _clock(value: str) -> time:
    if re.fullmatch(r"\d{6}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", value, re.IGNORECASE):
        value = value[:2] + ":" + value[2:4] + ":" + value[4:]
    match = _CLOCK.fullmatch(value)
    if not match:
        raise DateValueError("invalid_date", "Invalid time; use HH:mm or h:mm AM/PM.")
    hour, minute, second, fraction, ampm, offset = match.groups()
    hour_number = int(hour)
    if ampm:
        if not 1 <= hour_number <= 12:
            raise DateValueError("invalid_date", "12-hour time requires an hour from 1 to 12.")
        hour_number = hour_number % 12 + (12 if ampm.lower() == "pm" else 0)
    elif minute is None:
        raise DateValueError("invalid_date", "A time without AM/PM must include minutes.")
    try:
        return time(
            hour_number,
            int(minute or 0),
            int(second or 0),
            _precision(fraction or ""),
            tzinfo=_offset(offset),
        )
    except ValueError as exc:
        raise DateValueError("invalid_date", f"Invalid time: {value}") from exc


def _name_pattern(aliases: dict[str, int]) -> str:
    return (
        "(?:"
        + "|".join(re.escape(name) + r"\.?" for name in sorted(aliases, key=len, reverse=True))
        + ")"
    )


def _format_pattern(input_format: str, lexicon: DateLexicon) -> tuple[str, list[tuple[str, str]]]:
    patterns = {
        "YYYY": r"\d{4}",
        "YY": r"\d{2}",
        "MM": r"\d{2}",
        "M": r"\d{1,2}",
        "DD": r"\d{2}",
        "D": r"\d{1,2}",
        "HH": r"\d{2}",
        "H": r"\d{1,2}",
        "hh": r"\d{2}",
        "h": r"\d{1,2}",
        "mm": r"\d{2}",
        "ss": r"\d{2}",
        "A": r"am|pm",
        "a": r"am|pm",
        "Z": r"Z|[+-]\d{2}:\d{2}(?::\d{2})?",
        "ZZ": r"Z|[+-]\d{4}(?:\d{2})?",
        "X": r"[+-]?\d+(?:\.\d+)?",
        "x": r"[+-]?\d+",
    }
    patterns.update(
        dict.fromkeys(("MMM", "MMMM", "LLL", "LLLL"), _name_pattern(lexicon.aliases("month")))
    )
    patterns.update(dict.fromkeys(("ddd", "dddd"), _name_pattern(lexicon.aliases("weekday"))))
    segments: list[str] = []
    captures: list[tuple[str, str]] = []
    for is_token, token in tokenize_format(input_format):
        if not is_token:
            segments.append(r"\s+" if token.isspace() else re.escape(token))
            continue
        group = f"f{len(captures)}"
        captures.append((group, token))
        segments.append(f"(?P<{group}>{patterns[token]})")
    return "".join(segments), captures


def _timestamp(value: str, token: str) -> datetime:
    try:
        with localcontext() as context:
            context.prec = len(value) + 10
            amount = Decimal(value) * (1000 if token == "X" else 1)
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise DateValueError(
                "invalid_date", "Unix time must be exactly representable in milliseconds."
            )
        result = EPOCH + timedelta(milliseconds=int(amount))
    except (InvalidOperation, OverflowError, ValueError) as exc:
        if isinstance(exc, DateValueError):
            raise
        raise DateValueError(
            "invalid_date", "Unix time is outside the supported date range."
        ) from exc
    validate_date_value(result)
    return result


def _components(
    value: str, input_format: str, lexicon: DateLexicon, century: int
) -> dict[str, int | str]:
    pattern, captures = _format_pattern(input_format, lexicon)
    match = re.fullmatch(pattern, value, re.IGNORECASE)
    if not match:
        raise DateValueError("invalid_date", f"Input does not match input_format {input_format!r}.")
    result: dict[str, int | str] = {}
    numeric = {
        "YYYY": "year",
        "YY": "year",
        "MM": "month",
        "M": "month",
        "DD": "day",
        "D": "day",
        "HH": "hour",
        "H": "hour",
        "hh": "hour12",
        "h": "hour12",
        "mm": "minute",
        "ss": "second",
    }
    for group, token in captures:
        raw = match[group]
        if token in numeric:
            name = numeric[token]
            parsed: int | str = int(raw) + (century if token == "YY" else 0)
        elif token in {"MMM", "MMMM", "LLL", "LLLL"}:
            name, parsed = "month", lexicon.aliases("month")[normalize_name(raw)]
        elif token in {"ddd", "dddd"}:
            name, parsed = "weekday", lexicon.aliases("weekday")[normalize_name(raw)]
        elif token in {"A", "a"}:
            name, parsed = "ampm", raw.casefold()
        elif token in {"Z", "ZZ"}:
            name, parsed = "offset", raw
        else:
            name, parsed = token, raw
        if name in result and result[name] != parsed:
            raise DateValueError("invalid_date", f"Conflicting {name} values in input format.")
        result[name] = parsed
    return result


def _component_time(parts: dict[str, int | str]) -> time:
    hour = int(parts.get("hour", 0))
    if "hour12" in parts:
        twelve = int(parts["hour12"])
        if "ampm" not in parts or not 1 <= twelve <= 12:
            raise DateValueError(
                "invalid_date", "12-hour time requires AM/PM and an hour from 1 to 12."
            )
        hour = twelve % 12 + (12 if parts["ampm"] == "pm" else 0)
    elif "ampm" in parts:
        raise DateValueError("invalid_date", "AM/PM requires a 12-hour token.")
    try:
        return time(
            hour,
            int(parts.get("minute", 0)),
            int(parts.get("second", 0)),
            tzinfo=_offset(str(parts["offset"]) if "offset" in parts else None),
        )
    except ValueError as exc:
        raise DateValueError("invalid_date", "Invalid clock fields in date format.") from exc


def _formatted_date(value: str, input_format: str, lexicon: DateLexicon, century: int) -> DateValue:
    parts = _components(value, input_format, lexicon, century)
    if "X" in parts or "x" in parts:
        if len(parts) != 1:
            raise DateValueError(
                "invalid_date", "Unix timestamp format cannot be mixed with calendar fields."
            )
        token = "X" if "X" in parts else "x"
        return _timestamp(str(parts[token]), token)
    if not {"year", "month", "day"} <= parts.keys():
        raise DateValueError(
            "date_required", "A complete calendar date requires year, month and day."
        )
    try:
        calendar = date(int(parts["year"]), int(parts["month"]), int(parts["day"]))
    except ValueError as exc:
        raise DateValueError(
            "invalid_date", "Invalid calendar date; do not overflow month or day."
        ) from exc
    if "weekday" in parts and calendar.isoweekday() != parts["weekday"]:
        raise DateValueError("invalid_date", "Weekday name does not match the calendar date.")
    if any(key in parts for key in ("hour", "hour12", "minute", "second", "offset")):
        return datetime.combine(calendar, _component_time(parts))
    return calendar


def _time_suffix(value: str) -> str:
    if not value:
        return ""
    suffix = value[1:].strip()
    if value[0] not in {" ", "T"} or not suffix:
        raise DateValueError(
            "invalid_date", "A date-time separator must be followed by a valid time."
        )
    return suffix


def _numeric_calendar(value: str) -> tuple[date, str] | None:
    match = _NUMERIC_DATE.match(value)
    if match:
        first, _, middle, last = match.groups()
        if len(first) == 4:
            year, month, day = int(first), int(middle), int(last)
        elif len(last) != 4:
            raise DateValueError(
                "ambiguous_date", "A two-digit year requires an explicit input_format."
            )
        elif int(first) <= 12 and int(middle) <= 12:
            raise DateValueError(
                "ambiguous_date",
                "Date can be day-first or month-first; set input_format to DD/MM/YYYY or MM/DD/YYYY.",
            )
        elif int(first) > 12:
            year, month, day = int(last), int(middle), int(first)
        else:
            year, month, day = int(last), int(first), int(middle)
    else:
        match = _ISO_COMPACT_DATE.match(value)
        if not match:
            return None
        year, month, day = map(int, match.groups())
    try:
        return date(year, month, day), _time_suffix(value[match.end() :])
    except ValueError as exc:
        raise DateValueError(
            "invalid_date", "Invalid calendar date; do not overflow month or day."
        ) from exc


def _automatic_date(value: str, lexicon: DateLexicon) -> DateValue:
    if re.match(r"^\d{4}-?W", value, re.IGNORECASE) or re.match(r"^\d{4}-\d{3}(?=$|[ T])", value):
        raise DateValueError(
            "unsupported_date_form", "ISO week and ordinal dates are not supported; use YYYY-MM-DD."
        )
    weekday = None
    first = re.match(r"^([^\s,]+)[\s,]+", value)
    if first and normalize_name(first[1]) in lexicon.aliases("weekday"):
        weekday = lexicon.aliases("weekday")[normalize_name(first[1])]
        value = value[first.end() :]
    numeric = _numeric_calendar(value)
    if numeric:
        calendar, suffix = numeric
    else:
        month = _name_pattern(lexicon.aliases("month"))
        variants = (
            rf"(?P<d>\d{{1,2}}) (?P<m>{month}) (?P<y>\d{{4}})",
            rf"(?P<m>{month}) (?P<d>\d{{1,2}}),? (?P<y>\d{{4}})",
            rf"(?P<y>\d{{4}}) (?P<m>{month}) (?P<d>\d{{1,2}})",
        )
        match = next(
            (
                found
                for pattern in variants
                if (found := re.match(pattern + r"(?=$|[ T])", value, re.IGNORECASE))
            ),
            None,
        )
        if not match:
            if _CLOCK.fullmatch(value):
                _clock(value)
                raise DateValueError(
                    "date_required", "A date is required; time-only input belongs to date.combine."
                )
            raise DateValueError(
                "invalid_date", "Unrecognized date; supply an explicit input_format and locale."
            )
        try:
            calendar = date(
                int(match["y"]),
                lexicon.aliases("month")[normalize_name(match["m"])],
                int(match["d"]),
            )
        except ValueError as exc:
            raise DateValueError("invalid_date", "Invalid calendar date.") from exc
        suffix = _time_suffix(value[match.end() :])
    if weekday is not None and calendar.isoweekday() != weekday:
        raise DateValueError("invalid_date", "Weekday name does not match the calendar date.")
    return datetime.combine(calendar, _clock(suffix)) if suffix else calendar


def parse_date(
    value: str, *, input_format: str | None = None, lexicon: DateLexicon, century: int = 2000
) -> DateValue:
    """Parse complete dates without guessing locale, century, timezone or missing fields."""
    clean = _text(value)
    if isinstance(century, bool) or century not in (1900, 2000):
        raise DateValueError("invalid_date", "century must be 1900 or 2000.")
    result = (
        _formatted_date(clean, input_format, lexicon, century)
        if input_format is not None
        else _automatic_date(clean, lexicon)
    )
    validate_date_value(result)
    return result


def parse_time(
    value: str, *, input_format: str | None = None, lexicon: DateLexicon, century: int = 2000
) -> time:
    """Parse a time-of-day for combine, without weakening parse_date's full-date guard."""
    clean = _text(value)
    if input_format is None:
        return _clock(clean)
    parts = _components(clean, input_format, lexicon, century)
    if any(key in parts for key in ("year", "month", "day", "weekday", "X", "x")) or not (
        "hour" in parts or "hour12" in parts
    ):
        raise DateValueError(
            "invalid_date", "time_format must contain a time of day and no calendar date."
        )
    return _component_time(parts)
