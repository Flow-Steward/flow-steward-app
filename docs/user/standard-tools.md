# Standard tools

Standard tools provide deterministic operations for workflow transform and
connector steps. Their availability follows the version deployed in your
workspace. This guide describes the `std.date` contract and current
standard-tool compatibility details.

## Date, time and interval actions

`std.date` exposes these 15 actions in transform pipelines and as built-in
connector operations:

| Action | Purpose |
|---|---|
| `date.parse` | Parse a date or date-time and return canonical ISO text. |
| `date.to_utc` | Convert an absolute date-time to UTC (`Z`). |
| `date.to_timezone` | Render an absolute date-time in an IANA timezone. |
| `date.format` | Parse using an optional input format and render using an output format. |
| `date.combine` | Combine a calendar date and local time, optionally resolving it in a timezone. |
| `date.add` | Add calendar or elapsed-time units. |
| `date.diff` | Return an elapsed or full calendar-unit difference. |
| `date.part` | Read a date or time component, such as weekday or month. |
| `date.start_of` / `date.end_of` | Get the start or exclusive next boundary of a day, week, month or year. |
| `date.compare` | Compare two absolute moments. |
| `date.interval_make` | Create an interval from an absolute start and end. |
| `date.interval_duration` | Measure an interval in minutes, hours or days. |
| `date.interval_intersect_all` | Intersect a list of intervals. |
| `date.interval_slots` | Generate bounded meeting slots inside an interval. |

Dates are text values. Intervals are objects shaped as
`{"empty": false, "start": "…Z", "end": "…Z"}`. An empty interval is
`{"empty": true, "start": null, "end": null}`. Intervals are half-open:
their start is included and their end is excluded. `date.interval_make` requires
end after start; an empty interval has duration zero. Slots use canonical UTC
boundaries, and the date engine caps generation at 10,000 slots before building
the result. `max_slots` can request a smaller result.

### Timezones and daylight saving

An ISO value with an explicit offset, such as `2026-10-29T09:00:00Z`, identifies
an absolute moment. A value without an offset, such as `2026-10-29T10:00:00`, is
a calendar value; the action does not assume UTC or the server's timezone.
Calendar operations such as adding a day can work on a timezone-free value.
Operations that need an absolute moment—UTC/timezone conversion, comparison,
elapsed minutes or hours, and intervals—need an explicit offset in the value or
a `timezone` for the local value. Otherwise they return `timezone_required`.
Adding days, weeks, months or years is calendar arithmetic; adding minutes or
hours to an aware value is elapsed-time arithmetic. `date.diff` counts full
calendar days, months or years: crossing midnight by two minutes is still zero
full days. Calendar differences between aware or mixed values require an
explicit timezone so both values use the same calendar view.

Timezone parameters use IANA names such as `Europe/Berlin` and
`America/New_York`. A local time in a daylight-saving gap or fold is rejected
with `nonexistent_local_time` or `ambiguous_local_time` by default. Set
`dst_policy` to `first` to choose the first occurrence of a repeated time or
shift a missing time forward by the size of the gap. For example, Berlin
`2026-10-25T02:30` is ambiguous by default; with `dst_policy=first`, it resolves
to `2026-10-25T00:30:00Z`.

Canonical ISO output always includes seconds. A nonzero millisecond fraction is
written with three digits, such as `2026-10-29T09:00:00.123Z`; a zero fraction
is omitted. Values with nonzero precision below one millisecond are rejected
rather than silently rounded or truncated. `date.end_of` returns the next
period boundary (exclusive), so a local DST day can be 23 or 25 elapsed hours.

### Formats and localized names

Format strings use explicit tokens rather than process-locale rules. Common
tokens include `YYYY`/`YY` (year), `MM`/`M` (month number), `DD`/`D` (day),
`MMMM`/`MMM` (localized month name), `LLLL`/`LLL` (standalone month name),
`dddd`/`ddd` (weekday), `HH`/`H` and `hh`/`h` (24-hour and 12-hour clocks),
`mm`, `ss`, `A`/`a` (AM/PM), `Z`/`ZZ` (offset), and `X`/`x` (Unix
seconds/milliseconds). Literal words go in square brackets, for example
`YYYY-MM-DD [at] HH:mm`; unbracketed letters that are not tokens are rejected.

`locale` selects translated month and weekday names; `output_locale` can select
the output language separately. For example, formatting with `D MMMM YYYY`
produces `29 October 2026` in English and `29 spalio 2026` in Lithuanian.
`LLLL YYYY` uses the standalone form, such as `spalis 2026` in Lithuanian.

An explicit input and output format can convert a US-style date without
guessing its ordering:

```text
action: date.format
value: "10/29/2026 5:00 PM"
input_format: "MM/DD/YYYY h:mm A"
output_format: "DD.MM.YYYY HH:mm"
result: "29.10.2026 17:00"
```

Without an explicit format, `03/04/2026` is ambiguous and returns
`ambiguous_date` rather than choosing a date ordering. See the
[timezone-data and release reproducibility guide](../operations/date-time-reproducibility.md).

### Meeting availability example

On 29 October 2026, three participants provide local availability:

| Timezone | Local window | UTC window |
|---|---|---|
| `Europe/Berlin` | 10:00–13:00 | 09:00–12:00Z |
| `Europe/London` | 09:00–11:30 | 09:00–11:30Z |
| `America/New_York` | 05:00–08:00 | 09:00–12:00Z |

Use `date.combine` for each local date/time, convert each boundary with
`date.to_utc`, build the windows with `date.interval_make`, and intersect them
with `date.interval_intersect_all`. The common window is 09:00–11:30Z (150
minutes). With `duration_minutes=45`, `step_minutes=15`, and `max_slots=3`,
`date.interval_slots` returns starts at 09:00Z, 09:15Z, and 09:30Z; in Berlin
those starts are 10:00, 10:15, and 10:30. These calculations use deterministic
date operations rather than model-generated timezone arithmetic.

## Other standard-tool additions

The built-in catalog groups deterministic operations by tool:

| Tool ID | Operation family |
|---|---|
| `std.text` | String cleanup, case, replacement, padding and length. |
| `std.numbers` | Number coercion, arithmetic, rounding, floor and ceiling. |
| `std.money` | Strict decimal coercion, arithmetic and rounding; values retain decimal text scale. |
| `std.boolean` | Explicit boolean-token and numeric coercion. |
| `std.array` | Length, first item, indexed access, compaction and text joining. |
| `std.json` | JSON selection, omission, update, keys and values. |
| `std.date` | Date, timezone and interval actions described above. |

The number action family is named `number.*`, while its built-in connector
identity remains `std.numbers` to preserve existing saved connector references.
This identity is not renamed to `std.number`. The connector catalog also
provides `std.money`, `std.array`, and `std.boolean` operations; each follows the
existing tool's declared value and result types.

Standard operations are listed in AI Builder discovery. Discovery visibility
does not grant runtime permission: a tool configured as `connector_only`
continues to be restricted by the agent resolver. Availability and permissions
are determined by the deployed tool catalog.
