"""Immutable, locale-independent values shared by deterministic date actions."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import TypeAlias

DateValue: TypeAlias = date | datetime
MIN_YEAR = 1000
MAX_YEAR = 9999
DATE_ERROR_CODES = frozenset(
    {
        "invalid_date",
        "ambiguous_date",
        "unsupported_date_form",
        "ambiguous_local_time",
        "nonexistent_local_time",
        "date_required",
        "input_too_long",
        "invalid_amount",
        "unknown_format_token",
        "unknown_locale",
        "timezone_required",
        "unknown_timezone",
        "abbreviated_timezone",
        "invalid_interval",
        "empty_interval_list",
        "unsupported_unit",
    }
)


class DateValueError(ValueError):
    """A stable action error code with a human-readable correction message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = code


def normalize_name(value: str) -> str:
    """Normalize an alias without merging names of different calendar indexes."""
    return unicodedata.normalize("NFC", value).casefold().rstrip(".")


def validate_date_value(value: DateValue) -> DateValue:
    """Reject unsupported years and nonzero precision smaller than a millisecond."""
    if not isinstance(value, date) or not MIN_YEAR <= value.year <= MAX_YEAR:
        raise DateValueError("invalid_date", "Date year must be between 1000 and 9999.")
    if isinstance(value, datetime) and value.microsecond % 1000:
        raise DateValueError(
            "invalid_date", "Submillisecond precision is not supported; no digits are discarded."
        )
    return value


@dataclass(frozen=True)
class DateLexicon:
    """Translated data only; tuple position defines month or ISO weekday number."""

    locale: str
    months_full: tuple[str, ...]
    months_abbr: tuple[str, ...]
    weekdays_full: tuple[str, ...]
    weekdays_abbr: tuple[str, ...]
    weekdays_short: tuple[str, ...] = ()
    months_full_standalone: tuple[str, ...] = ()
    months_abbr_standalone: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        fallbacks = {
            "weekdays_short": "weekdays_abbr",
            "months_full_standalone": "months_full",
            "months_abbr_standalone": "months_abbr",
        }
        for field in fields(self):
            if field.name == "locale":
                continue
            values = tuple(getattr(self, field.name))
            if not values and field.name in fallbacks:
                values = tuple(getattr(self, fallbacks[field.name]))
            expected = 12 if field.name.startswith("months_") else 7
            if len(values) != expected or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise DateValueError(
                    "unknown_locale",
                    f"Locale {self.locale!r} requires {expected} names for {field.name}.",
                )
            if ("abbr" in field.name or "short" in field.name) and any(
                any(char.isspace() for char in item) for item in values
            ):
                raise DateValueError(
                    "unknown_locale", f"Locale {self.locale!r} has whitespace in an abbreviation."
                )
            object.__setattr__(self, field.name, values)
        self.aliases("month")
        self.aliases("weekday")

    def aliases(self, kind: str) -> dict[str, int]:
        """Return aliases whose ambiguity is checked within one calendar namespace."""
        names = (
            ("months_full", "months_abbr", "months_full_standalone", "months_abbr_standalone")
            if kind == "month"
            else ("weekdays_full", "weekdays_abbr", "weekdays_short")
        )
        result: dict[str, int] = {}
        for name in names:
            for number, spelling in enumerate(getattr(self, name), 1):
                key = normalize_name(spelling)
                if key in result and result[key] != number:
                    raise DateValueError(
                        "unknown_locale",
                        f"Locale {self.locale!r} has ambiguous {kind} alias {spelling!r}.",
                    )
                result[key] = number
        return result
