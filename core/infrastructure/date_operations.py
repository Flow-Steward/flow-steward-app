"""Pure dispatch for standard date operations with explicitly supplied lexicons."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

from core.infrastructure import date_arithmetic, date_intervals
from core.infrastructure.date_formatting import canonical_iso, format_date, validate_format
from core.infrastructure.date_parsing import parse_date, parse_time
from core.infrastructure.date_timezones import resolve_datetime, validate_timezone
from core.infrastructure.date_types import DateLexicon, DateValueError


def date_operation_locales(params: Mapping[str, object]) -> tuple[str, str]:
    """Normalize optional locale absence, without guessing invalid explicit values."""
    locale = params.get("locale")
    if locale is None:
        locale = "en"
    output_locale = params.get("output_locale")
    if output_locale is None:
        output_locale = locale
    names = []
    for value in (locale, output_locale):
        if not isinstance(value, str) or not value.strip():
            raise DateValueError("unknown_locale", f"Unknown date locale: {value!r}.")
        names.append(value.strip().lower())
    return names[0], names[1]


def _lexicon(lexicons: Mapping[str, DateLexicon], locale: str) -> DateLexicon:
    if locale not in lexicons:
        raise DateValueError("unknown_locale", f"Unknown date locale: {locale!r}.")
    return lexicons[locale]


def validate_date_operation_params(action_id: str, params: Mapping[str, object]) -> None:
    """Validate date format syntax, including at configuration boundaries."""
    for name in ("input_format", "output_format", "date_format", "time_format"):
        value = params.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise DateValueError("unknown_format_token", f"{name} must be a format string.")
            if len(value) > 128:
                raise DateValueError("input_too_long", f"{name} exceeds 128 characters.")
            validate_format(value)
    if params.get("dst_policy", "error") not in ("error", "first"):
        raise DateValueError("invalid_date", "dst_policy must be error or first.")
    century = params.get("century", 2000)
    if type(century) is not int or century not in (1900, 2000):
        raise DateValueError("invalid_date", "century must be 1900 or 2000.")


def execute_date_operation(
    action_id: str,
    value: object,
    params: Mapping[str, object],
    *,
    lexicons: Mapping[str, DateLexicon],
) -> object:
    """Run a date action without filesystem, clock, locale or environment reads."""
    validate_date_operation_params(action_id, params)
    operation = action_id.removeprefix("date.")
    options: dict[str, Any] = dict(params)
    # Casts at this raw JSON boundary preserve values, including missing None;
    # the existing core validators still own named errors and validation order.
    if operation == "interval_duration":
        return date_intervals.interval_duration(value, options.get("unit", "minutes"))
    if operation == "interval_intersect_all":
        return date_intervals.interval_intersect_all(value)
    if operation == "interval_slots":
        return date_intervals.interval_slots(
            value,
            cast(int, options.get("duration_minutes")),
            cast(int, options.get("step_minutes")),
            cast(int, options.get("max_slots")),
            align_minutes=options.get("align_minutes"),
            align_timezone=options.get("align_timezone", "UTC"),
        )
    locale, output_locale = date_operation_locales(options)
    lexicon = _lexicon(lexicons, locale)
    output_lexicon = _lexicon(lexicons, output_locale)
    parse_options: dict[str, Any] = {"lexicon": lexicon, "century": options.get("century", 2000)}
    tz_options: dict[str, Any] = {
        "timezone": options.get("timezone"),
        "dst_policy": options.get("dst_policy", "error"),
    }
    parsed = parse_date(
        cast(str, value),
        input_format=options.get("date_format")
        if operation == "combine"
        else options.get("input_format"),
        **parse_options,
    )
    if operation == "combine":
        if isinstance(parsed, datetime):
            raise DateValueError(
                "invalid_date", "date.combine requires a date without a time component."
            )
        clock = parse_time(
            cast(str, options.get("time")), input_format=options.get("time_format"), **parse_options
        )
        return canonical_iso(resolve_datetime(datetime.combine(parsed, clock), **tz_options))
    if operation == "parse":
        return canonical_iso(resolve_datetime(parsed, **tz_options))
    if operation == "to_utc":
        if options.get("timezone") is not None:
            validate_timezone(options["timezone"])
        resolved = (
            parsed
            if isinstance(parsed, datetime) and parsed.tzinfo is not None
            else resolve_datetime(parsed, **tz_options, require_aware=True)
        )
        if not isinstance(resolved, datetime):
            raise DateValueError("timezone_required", "An explicit offset or timezone is required.")
        try:
            utc_value = resolved.astimezone(UTC)
        except (OverflowError, ValueError) as error:
            raise DateValueError(
                "invalid_date", "UTC conversion exceeds the supported calendar range."
            ) from error
        return canonical_iso(utc_value)
    if operation == "to_timezone":
        absolute = resolve_datetime(parsed, require_aware=True)
        return format_date(
            resolve_datetime(absolute, **tz_options), options.get("output_format"), lexicon=lexicon
        )
    if operation == "format":
        return format_date(
            resolve_datetime(parsed, **tz_options),
            options.get("output_format"),
            lexicon=output_lexicon,
        )
    if operation == "add":
        result = date_arithmetic.add(
            parsed, cast(int, options.get("amount")), cast(str, options.get("unit")), **tz_options
        )
    elif operation in ("diff", "compare", "interval_make"):
        key = {"diff": "from", "compare": "to", "interval_make": "end"}[operation]
        other = parse_date(
            cast(str, options.get(key)), input_format=options.get("input_format"), **parse_options
        )
        if operation == "diff":
            return date_arithmetic.diff(parsed, other, cast(str, options.get("unit")), **tz_options)
        if operation == "compare":
            return date_arithmetic.compare(parsed, other, **tz_options)
        return date_intervals.interval_make(parsed, other, **tz_options)
    elif operation == "part":
        return date_arithmetic.part(parsed, cast(str, options.get("part")), **tz_options)
    elif operation in ("start_of", "end_of"):
        function = date_arithmetic.start_of if operation == "start_of" else date_arithmetic.end_of
        result = function(parsed, cast(str, options.get("unit")), **tz_options)
    else:
        raise DateValueError("invalid_date", f"Unknown date action: {action_id}.")
    if not isinstance(result, date):
        raise DateValueError("invalid_date", "Date operation returned an invalid calendar value.")
    return canonical_iso(result)
