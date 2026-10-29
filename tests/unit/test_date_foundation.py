"""Contracts for deterministic date parsing, formatting and DST resolution."""

import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from core.infrastructure.date_formatting import canonical_iso, format_date, validate_format
from core.infrastructure.date_parsing import parse_date, parse_time
from core.infrastructure.date_timezones import local_candidates, resolve_datetime, validate_timezone
from core.infrastructure.date_types import DateLexicon, DateValueError


@pytest.fixture
def english():
    return DateLexicon(
        locale="en",
        months_full=(
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        months_abbr=(
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        weekdays_full=(
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ),
        weekdays_abbr=(
            "Mon",
            "Tue",
            "Wed",
            "Thu",
            "Fri",
            "Sat",
            "Sun",
        ),
    )


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-29 17:00",
        "2026/10/29 17:00",
        "29/10/2026 17:00",
        "29.10.2026 5:00 PM",
        "10/29/2026 5pm",
        "29 Oct 2026 17:00",
        "Thu, 29 October 2026 5:00 pm",
        "20261029T170000",
    ],
)
def test_unambiguous_inputs_match(english, text):
    assert parse_date(text, lexicon=english) == datetime(2026, 10, 29, 17)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("03/04/2026", "ambiguous_date"),
        ("29/10/26", "ambiguous_date"),
        ("31.02.2026", "invalid_date"),
        ("29.02.2027", "invalid_date"),
        ("2026-10-29 24:00", "invalid_date"),
        ("2026-10-29 12:60", "invalid_date"),
        ("Thu 30 Oct 2026", "invalid_date"),
        ("17:00", "date_required"),
        ("2026-W44-4", "unsupported_date_form"),
        ("2026-302", "unsupported_date_form"),
        ("x" * 257, "input_too_long"),
        ("2026-10-29T09:00:00.123001Z", "invalid_date"),
        ("0999-10-29", "invalid_date"),
    ],
)
def test_named_parse_failures(english, text, code):
    with pytest.raises(DateValueError) as caught:
        parse_date(text, lexicon=english)
    assert caught.value.code == caught.value.reason == code


def test_explicit_formats_and_missing_date(english):
    assert parse_date("03/04/2026", input_format="DD/MM/YYYY", lexicon=english) == date(2026, 4, 3)
    assert parse_date("03/04/2026", input_format="MM/DD/YYYY", lexicon=english) == date(2026, 3, 4)
    assert parse_date("10/03/85", input_format="DD/MM/YY", lexicon=english, century=1900) == date(
        1985, 3, 10
    )
    assert parse_date(
        "2026-10-29 at 17:00", input_format="YYYY-MM-DD [at] HH:mm", lexicon=english
    ) == datetime(2026, 10, 29, 17)
    with pytest.raises(DateValueError) as caught:
        parse_date("29 at 17:00", input_format="DD [at] HH:mm", lexicon=english)
    assert caught.value.code == "date_required"
    with pytest.raises(DateValueError) as caught:
        validate_format("YYYY-MM-DD at HH:mm")
    assert caught.value.code == "unknown_format_token"
    assert parse_time("5 PM", lexicon=english).hour == 17
    assert parse_time("5:30 pm", input_format="h:mm a", lexicon=english).minute == 30


def test_iso_precision_and_unix_roundtrip(english):
    value = parse_date("2026-10-29T09:00:00.123000Z", lexicon=english)
    assert canonical_iso(value) == "2026-10-29T09:00:00.123Z"
    assert (
        format_date(value, "dddd, D MMMM YYYY HH:mm Z", lexicon=english)
        == "Thursday, 29 October 2026 09:00 +00:00"
    )
    assert (
        parse_date(format_date(value, "x", lexicon=english), input_format="x", lexicon=english)
        == value
    )
    assert canonical_iso(datetime(2026, 10, 29, tzinfo=UTC)) == "2026-10-29T00:00:00Z"
    with pytest.raises(DateValueError):
        canonical_iso(datetime(2026, 10, 29, microsecond=1))


def test_localized_forms_and_same_index_aliases():
    lexicon = DateLexicon(
        locale="lt",
        months_full=(
            "sausio",
            "vasario",
            "kovo",
            "balandžio",
            "gegužės",
            "birželio",
            "liepos",
            "rugpjūčio",
            "rugsėjo",
            "spalio",
            "lapkričio",
            "gruodžio",
        ),
        months_abbr=(
            "saus.",
            "vas.",
            "kov.",
            "bal.",
            "geg.",
            "birž.",
            "liep.",
            "rugp.",
            "rugs.",
            "spal.",
            "lapkr.",
            "gruod.",
        ),
        weekdays_full=(
            "pirmadienis",
            "antradienis",
            "trečiadienis",
            "ketvirtadienis",
            "penktadienis",
            "šeštadienis",
            "sekmadienis",
        ),
        weekdays_abbr=(
            "Pr",
            "An",
            "Tr",
            "Kt",
            "Pn",
            "Št",
            "Sk",
        ),
        months_full_standalone=(
            "sausis",
            "vasaris",
            "kovas",
            "balandis",
            "gegužė",
            "birželis",
            "liepa",
            "rugpjūtis",
            "rugsėjis",
            "spalis",
            "lapkritis",
            "gruodis",
        ),
    )
    assert parse_date("29 spalio 2026", input_format="D MMMM YYYY", lexicon=lexicon) == date(
        2026, 10, 29
    )
    assert parse_date("Kt, 2026 spalio 29", lexicon=lexicon) == date(2026, 10, 29)
    assert format_date(date(2026, 10, 29), "LLLL YYYY", lexicon=lexicon) == "spalis 2026"
    assert isinstance(lexicon.months_full, tuple)


@pytest.mark.parametrize(
    ("text", "code", "expected"),
    [
        ("2026-10-25T02:30", "ambiguous_local_time", "2026-10-25T00:30:00Z"),
        ("2026-03-29T02:30", "nonexistent_local_time", "2026-03-29T01:30:00Z"),
    ],
)
def test_dst_strict_and_first(english, text, code, expected):
    value = parse_date(text, lexicon=english)
    with pytest.raises(DateValueError) as caught:
        resolve_datetime(value, "Europe/Berlin")
    assert caught.value.code == code
    assert (
        canonical_iso(resolve_datetime(value, "Europe/Berlin", "first").astimezone(UTC)) == expected
    )


def test_candidates_and_legacy_validation():
    from core.application.settings import timezone_service

    assert len(local_candidates(datetime(2026, 10, 25, 2, 30), "Europe/Berlin")) == 2
    assert local_candidates(datetime(2026, 3, 29, 2, 30), "Europe/Berlin") == ()
    assert len(local_candidates(datetime(2026, 6, 1), "Europe/Berlin")) == 1
    assert validate_timezone("US/Eastern") == "America/New_York"
    assert timezone_service.validate_timezone("CET") == "CET"
    for abbreviation in ("CET", "EST", "PST"):
        with pytest.raises(DateValueError) as caught:
            validate_timezone(abbreviation)
        assert caught.value.code == "abbreviated_timezone"
    with pytest.raises(ValueError, match="Timezone identifier is required"):
        timezone_service.validate_timezone(None)
    assert timezone_service.to_utc(datetime(2026, 3, 29, 2, 30), "Europe/Berlin") == datetime(
        2026, 3, 29, 1, 30, tzinfo=UTC
    )


@pytest.mark.parametrize("text", ["2026-10-29T", "2026-10-29TT17:00", "29 Oct 2026T"])
def test_incomplete_or_repeated_time_separator_is_rejected(english, text):
    with pytest.raises(DateValueError) as caught:
        parse_date(text, lexicon=english)
    assert caught.value.code == "invalid_date"


@pytest.mark.parametrize(
    "date_format", ["MD" * 30 + "[x]", "MDYYYY", "MDDH", "YYYY-MM-DD HH hh A", "YYYY-YY-MM-DD"]
)
def test_ambiguous_or_duplicate_semantic_format_fields_are_rejected(english, date_format):
    with pytest.raises(DateValueError) as caught:
        validate_format(date_format)
    assert caught.value.code == "unknown_format_token"
    with pytest.raises(DateValueError) as caught:
        parse_date("1" * 90 + "y", input_format=date_format, lexicon=english)
    assert caught.value.code == "unknown_format_token"


def test_fixed_width_compact_format_remains_valid(english):
    validate_format("YYYYMMDDHHmmss")
    assert parse_date("20261029170503", input_format="YYYYMMDDHHmmss", lexicon=english) == datetime(
        2026, 10, 29, 17, 5, 3
    )


@pytest.mark.parametrize(
    ("zone", "offset", "compact"),
    [("Europe/Berlin", "+00:53:28", "+005328"), ("America/New_York", "-04:56:02", "-045602")],
)
def test_historical_timezone_offset_seconds_roundtrip_without_loss(english, zone, offset, compact):
    value = resolve_datetime(datetime(1800, 1, 1, 12), zone)
    iso = "1800-01-01T12:00:00" + offset
    assert canonical_iso(value) == iso
    assert parse_date(iso, lexicon=english).astimezone(UTC) == value.astimezone(UTC)
    assert format_date(value, "Z", lexicon=english) == offset
    assert format_date(value, "ZZ", lexicon=english) == compact
    assert parse_date(iso, input_format="YYYY-MM-DD[T]HH:mm:ssZ", lexicon=english).astimezone(
        UTC
    ) == value.astimezone(UTC)


@pytest.mark.parametrize("text", ["24:00", "12:60"])
def test_invalid_time_only_input_reports_invalid_date(english, text):
    with pytest.raises(DateValueError) as caught:
        parse_date(text, lexicon=english)
    assert caught.value.code == "invalid_date"


@pytest.mark.parametrize("date_format", ["X", "x"])
def test_unix_format_overflow_is_a_named_error(english, date_format):
    value = parse_date("9999-12-31T23:00:00-12:00", lexicon=english)
    with pytest.raises(DateValueError) as caught:
        format_date(value, date_format, lexicon=english)
    assert caught.value.code == "invalid_date"


@pytest.mark.parametrize(
    ("text", "expected_groups"),
    [
        ("5pm", ("5", None, None, None, "pm", None)),
        ("5 \t PM  Z", ("5", None, None, None, "PM", "Z")),
        ("17:00 \n +01:30", ("17", "00", None, None, None, "+01:30")),
        ("05:03:04.123 AM -0130", ("05", "03", "04", "123", "AM", "-0130")),
        ("17:00 UTC+1", ("17", "00", None, None, None, "UTC+1")),
        ("17:00   ", ("17", "00", None, None, None, None)),
    ],
)
def test_clock_capture_contract_preserves_ampm_and_offset_positions(text, expected_groups):
    from core.infrastructure.date_parsing import _CLOCK

    match = _CLOCK.fullmatch(text)
    assert match is not None
    assert match.groups() == expected_groups


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5pm", "17:00:00"),
        ("5 \t PM  Z", "17:00:00+00:00"),
        ("17:00 \n +01:30", "17:00:00+01:30"),
        ("05:03:04.123 AM -0130", "05:03:04.123000-01:30"),
        ("17:00 UTC+1", "17:00:00+01:00"),
        ("12am", "00:00:00"),
        ("12pm", "12:00:00"),
    ],
)
def test_automatic_date_and_time_share_whitespace_ampm_offset_semantics(english, text, expected):
    parsed_time = parse_time(text, lexicon=english)
    assert parsed_time.isoformat() == expected
    assert parse_date("2026-10-29 " + text, lexicon=english) == datetime.combine(
        date(2026, 10, 29), parsed_time
    )


@pytest.mark.parametrize(
    ("length", "code"),
    [(256, "invalid_date"), (257, "input_too_long")],
)
def test_adversarial_clock_keeps_input_limit_and_named_rejection(english, length, code):
    # The length limit applies before whitespace normalization.
    text = "17:00" + " " * (length - 6) + "!"
    with pytest.raises(DateValueError) as error:
        parse_time(text, lexicon=english)
    assert error.value.code == code


def test_raw_clock_rejects_long_whitespace_suffix_in_bounded_time():
    from core.infrastructure import date_parsing

    # Public parsers already cap/normalize input. Exercise the production matcher
    # itself so an overlapping repeat cannot reappear behind those protections.
    # A subprocess timeout bounds failure even if the old quadratic regex returns.
    script = """
from core.infrastructure.date_parsing import _CLOCK
for text in (
    '17:00' + ' ' * 200_000 + '!',
    '5' + ' ' * 200_000 + 'pm' + ' ' * 200_000 + '!',
    '17:00' + ' ' * 200_000 + '+01:x',
):
    assert _CLOCK.fullmatch(text) is None
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(date_parsing.__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
