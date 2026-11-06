"""Value formats of report schema v1: what a slot accepts and how summaries print it.

``value_problem`` says whether a scalar fits a format (schema section 2.4);
``plain_value`` renders it for ``summary_text`` (section 4.1). Locale, time zone
and currency checks use Babel and the IANA database the runtime already ships.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import MAX_EMAX, MIN_EMIN, ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from babel import Locale, UnknownLocaleError
from babel.numbers import list_currencies

from core.application.workflow_documents.report_issues import describe_type

NUMERIC_FORMATS = frozenset({"number", "currency", "percent"})
#: Largest integer a browser holds exactly.
MAX_SAFE_INTEGER = 2**53 - 1
NULL_TEXT = "—"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$", re.ASCII
)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def number_problem(value: Any) -> tuple[str, str] | None:
    """``(code, message)`` when a number cannot be stored, else ``None``."""
    if isinstance(value, float) and not math.isfinite(value):
        return "number_not_finite", "Numbers must be finite"
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        return "number_out_of_range", f"Integers must stay within ±{MAX_SAFE_INTEGER}"
    return None


def value_problem(value: Any, fmt: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(code, message)`` when ``value`` does not fit ``fmt``; ``None`` fits."""
    if value is None:
        return None
    kind = str(fmt.get("type") or "")
    if kind in NUMERIC_FORMATS:
        if not is_number(value):
            return _mismatch(kind, value)
        return number_problem(value)
    if kind in {"text", "date", "datetime"} and not isinstance(value, str):
        return _mismatch(kind, value)
    if kind == "boolean" and not isinstance(value, bool):
        return _mismatch(kind, value)
    if kind == "date" and not _is_date(value):
        return "date_invalid", "Dates are written YYYY-MM-DD"
    if kind == "datetime" and not _is_datetime(value):
        return "date_invalid", "Date-times are RFC 3339 with Z or a ±hh:mm offset"
    return None


def _mismatch(kind: str, value: Any) -> tuple[str, str]:
    expected = "a number" if kind in NUMERIC_FORMATS else f"a {kind} value"
    if kind == "text":
        expected = "a string"
    return "value_type_mismatch", f"Format '{kind}' needs {expected}, got {describe_type(value)}"


def _is_date(value: str) -> bool:
    if not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_datetime(value: str) -> datetime | None:
    if not _DATETIME.match(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_datetime(value: str) -> bool:
    return _parse_datetime(value) is not None


def fraction_digits_problem(fmt: Mapping[str, Any]) -> str:
    low = fmt.get("minimum_fraction_digits")
    high = fmt.get("maximum_fraction_digits")
    if isinstance(low, int) and isinstance(high, int) and low > high:
        return "minimum_fraction_digits must not exceed maximum_fraction_digits"
    return ""


@lru_cache(maxsize=1)
def _currencies() -> frozenset[str]:
    return frozenset(list_currencies())


def currency_known(code: Any) -> bool:
    return isinstance(code, str) and code in _currencies()


@lru_cache(maxsize=256)
def locale_known(tag: str) -> bool:
    try:
        Locale.parse(tag, sep="-")
    except (UnknownLocaleError, ValueError, TypeError):
        return False
    return True


@lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    return frozenset(available_timezones())


def time_zone_known(name: Any) -> bool:
    return isinstance(name, str) and name in _zones()


# --- plain rendering (schema v1 section 4.1) -------------------------------


#: Exact decimal arithmetic over every finite double: the largest has 309
#: integer digits, the smallest subnormal 1074 fraction digits, and a percent
#: adds two more. With this precision ``quantize`` and ``* 100`` never round
#: away digits or raise, so the rendering is the same on every runtime.
_PLAIN_CONTEXT = Context(prec=1_500, rounding=ROUND_HALF_EVEN, Emax=MAX_EMAX, Emin=MIN_EMIN)


def _decimal(value: int | float) -> Decimal:
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def plain_number(value: int | float, fmt: Mapping[str, Any]) -> str:
    """Positional decimal: no grouping, no exponent, round half to even."""
    with localcontext(_PLAIN_CONTEXT):
        amount = _decimal(value)
        if fmt.get("type") == "percent":
            amount *= 100
        high = fmt.get("maximum_fraction_digits")
        if isinstance(high, int):
            amount = amount.quantize(Decimal(1).scaleb(-high))
        if amount == 0:
            amount = abs(amount)
    whole, _, fraction = format(amount, "f").partition(".")
    fraction = fraction.rstrip("0")
    low = fmt.get("minimum_fraction_digits")
    if isinstance(low, int):
        fraction = fraction.ljust(low, "0")
    return f"{whole}.{fraction}" if fraction else whole


def plain_value(value: Any, fmt: Mapping[str, Any], time_zone: str) -> str:
    """Render one formatted scalar for ``summary_text``."""
    if value is None:
        return NULL_TEXT
    kind = fmt.get("type")
    if kind == "boolean":
        return "true" if value else "false"
    if kind in NUMERIC_FORMATS and is_number(value):
        text = plain_number(value, fmt)
        if kind == "currency":
            return f"{text} {fmt.get('currency')}"
        return f"{text}%" if kind == "percent" else text
    if kind == "datetime":
        moment = _parse_datetime(str(value))
        if moment is not None and time_zone_known(time_zone):
            local = moment.astimezone(ZoneInfo(time_zone)).replace(microsecond=0)
            return local.isoformat()
    return str(value)


__all__ = [
    "MAX_SAFE_INTEGER",
    "NULL_TEXT",
    "NUMERIC_FORMATS",
    "currency_known",
    "fraction_digits_problem",
    "is_number",
    "locale_known",
    "number_problem",
    "plain_number",
    "plain_value",
    "time_zone_known",
    "value_problem",
]
