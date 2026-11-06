# Workflow Report Schema v1

- Status: Proposed (checkpoint 2A; pending owner approval together with
  [ADR 0059](adr/0059-workflow-report-documents.md))
- Date: 2026-11-06
- Owner package (planned): `core/application/workflow_documents/`

This document is the authoritative contract for two JSON documents:

- **ReportDefinition v1** — what an author writes: layout, labels, formats and
  bindings to the declared inputs of a Template step with `format: report`.
- **ReportSnapshot v1** — what a successful run stores: the resolved, immutable
  report with projected datasets. It is the value of the workflow output that
  `settings.callable.report_output` selects.

Neither document depends on a chart or table library, on the MCP envelope, on
React, or on HTTP. A renderer turns a snapshot into a view; the compiler turns a
definition plus input values into a snapshot. Both documents are plain JSON:
no HTML, no JavaScript, no callbacks, no library options, no URLs to fetch.

Frozen fixtures for this version live in `tests/fixtures/workflow_reports/`:

| File | Content |
| --- | --- |
| `sales_report_definition.json` | A valid ReportDefinition v1 |
| `sales_report_inputs.json` | The Template step's declared inputs and the values the compiler receives |
| `sales_report_snapshot.json` | The exact ReportSnapshot v1 the compiler must produce from the two files above |

## 1. Where the documents live

A Template step selects the report mode with `format: report`:

```json
{
  "step_id": "build_report",
  "step_kind": "template",
  "override": {
    "template": {
      "format": "report",
      "inputs": [
        {"name": "kpis", "type": "object", "required": true, "source": "state.outputs.kpis"},
        {"name": "regions", "type": "array", "required": true, "source": "state.outputs.regions"}
      ],
      "report": { "schema_version": 1, "...": "ReportDefinition v1" },
      "result_target": "state.outputs.sales_report"
    }
  }
}
```

- `inputs` keeps its existing meaning: name, type, required, state selector.
  The definition reads nothing else from workflow state.
- `report` is required when `format` is `report` and forbidden otherwise.
- `text` is forbidden when `format` is `report`.
- `result_target` receives the snapshot object, never a string.

The workflow Interface selects the published report:

```json
{
  "callable": {
    "enabled": true,
    "trigger_channels": ["ui", "api", "mcp"],
    "expected_outputs": [
      {"name": "sales_report", "type": "object", "required": true}
    ],
    "report_output": "sales_report"
  }
}
```

`report_output` names exactly one declared output, which must be `type:
object` and `required: true`. ADR 0059 records the storage and identity rules.

## 2. Common rules

### 2.1 Identifiers

| Identifier | Pattern | Unique within |
| --- | --- | --- |
| Component `id` | `^[a-z][a-z0-9_]{0,63}$` | the whole report, at any section depth |
| Dataset `id` | same | the report's datasets |
| Table column `id` | same | its table |
| Chart series `id` | same | its chart |

Component and dataset IDs are separate namespaces. IDs are stable author-chosen
names: the compiler never generates, renumbers or rewrites them, so a later
filter or a renderer state can address a component across runs of the same
definition revision.

### 2.2 Bindings

A **value binding** reads one scalar:

```json
{"input": "kpis", "path": "revenue", "default": 0}
```

- `input` — the `name` of a Template input declared on the same step.
- `path` (optional) — a relative path inside that input's value, written in the
  canonical state path grammar (`core/application/orchestration/state_paths.py`:
  `a.b`, `a[0].b`, `a.0.b` against a list) without the `state.` prefix and
  without wildcards. An absent `path` means the whole input value.
- `default` (optional) — a JSON scalar used only when the value is **missing**.

A **dataset binding** reads one array of row objects:

```json
{"input": "regions", "path": "rows", "default": []}
```

Its `default`, when given, must be the empty array.

Bindings never name `state.…` selectors. Only Template input `source`s are
workflow selectors; that keeps selector discovery, shape proof (ADR 0042) and
credential checks in their existing owners.

### 2.3 Values

Every value in a snapshot is a JSON scalar: string, number, boolean or `null`.

| Situation | Meaning |
| --- | --- |
| Resolved `null` | A value that is known to be empty. Kept as `null`; renderers show `—` (U+2014). Chart series points with `null` are gaps (line) or absent bars (bar). The binding's `default` does **not** replace it. |
| `0`, `0.0`, `false`, `""` | Real values. Never treated as missing or empty. |
| Missing (optional input not supplied, or `path` runs off the data) | The binding's `default` if one is declared; otherwise the compile fails with `report.binding_missing`. |
| Required Template input missing | The step fails before compiling, with the existing `input_missing` failure. |
| Object or array where a scalar is expected | `report.binding_not_scalar`. |
| Empty dataset array | Valid. The dataset is kept with `rows: []`; the table or chart shows its `empty_text`. |
| Dataset source that is not an array | `report.dataset_not_array`. |
| Dataset element that is not an object | `report.dataset_row_not_object`. |
| Row object without a referenced field | `report.dataset_field_missing` (a present `null` is fine). |
| Row field holding an object or array | `report.dataset_cell_not_scalar`. |

How a binding finds its value, given the existing Template input rules (which
stay unchanged):

- **Whole input (no `path`).** The compiler reads the alias from the Template
  context by presence, not through `resolve_state_path`: alias present → its
  value, including `null`; alias absent (an optional input that was not
  supplied) → missing. `resolve_state_path(value, "")` cannot be reused here,
  because it reports a `null` root as not found.
- **Relative `path`.** `resolve_state_path(value, path)` on the input value:
  `found=False` → missing; a present `null` at the end of the path → `null`.
- **Where `null` can come from.** The Template type checks reject a top-level
  `null` for inputs typed `text`, `number`, `boolean`, `object` and `array`
  (`input_type` failure). A whole-input binding that must accept `null`
  therefore uses an input declared `json`. Inside an `object`, `array` or
  `json` input, a `null` reached through `path` is always allowed.

Nothing is coerced: a number held as the string `"1200.50"` is a type error,
not a number. Shape data with a Transform step before the Template.
Aggregation, sorting and filtering also belong to earlier steps; the report
shows rows in the order it receives them.

### 2.4 Formats

A format object says how a renderer presents a value and which value type the
slot accepts. Every bound value carries an explicit format; nothing is inferred.

| `type` | Accepts | Extra properties | Presentation |
| --- | --- | --- | --- |
| `text` | string, `null` | — | as is |
| `boolean` | boolean, `null` | — | localized yes/no |
| `number` | number, `null` | `minimum_fraction_digits`, `maximum_fraction_digits` (0–6) | locale grouping and decimal sign |
| `currency` | number, `null` | `currency` (required, ISO 4217 alphabetic code); fraction digits as above | locale currency format in that currency; no conversion |
| `percent` | number, `null` | fraction digits as above | fraction convention: `0.18` is 18 % |
| `date` | `YYYY-MM-DD` string, `null` | — | calendar date in the report locale; never shifted by time zone |
| `datetime` | RFC 3339 string with `Z` or `±hh:mm` offset, `null` | — | converted to the report `time_zone`, shown in the report locale |

Numbers are finite IEEE 754 doubles. `NaN`, `Infinity` and `-Infinity` are
rejected (`report.number_not_finite`); integers outside ±(2^53 − 1) are rejected
(`report.number_out_of_range`) because a browser cannot hold them exactly.
`minimum_fraction_digits` must not exceed `maximum_fraction_digits`.
A `currency` code must be a known ISO 4217 code (`report.currency_invalid`).

Slots restrict formats:

| Slot | Allowed format types |
| --- | --- |
| Text segment value | all seven |
| Metric value and comparison | `number`, `currency`, `percent` |
| Table column | all seven |
| Chart category | `text`, `number`, `date`, `datetime` |
| Chart series | `number`, `currency`, `percent` |

### 2.5 Locale and time zone

- `locale` — a BCP 47 tag of the form `language[-Script][-REGION]`, for example
  `en-US`, `de-DE`, `ru`, `sr-Latn-RS`. It must name a locale the runtime knows
  (`report.locale_invalid`).
- `time_zone` — an IANA zone such as `Europe/Berlin` or `UTC`
  (`report.time_zone_invalid`). Only `datetime` values use it.

Both are fixed in the definition and copied into the snapshot, so a stored
report renders the same way for every viewer.

## 3. ReportDefinition v1

Top level:

| Property | Required | Description |
| --- | --- | --- |
| `schema_version` | yes | The integer `1` |
| `title` | yes | Literal text, 1–200 characters |
| `locale`, `time_zone` | yes | See 2.5 |
| `summary` | no | Segments (see text component) rendered to `summary_text` |
| `datasets` | no | Up to 10 `{id, source}` entries; `source` is a dataset binding |
| `components` | yes | 1–100 top-level components |

### 3.1 Component catalog

**text** — a paragraph or a heading made of segments.

```json
{"id": "status", "type": "text", "style": "paragraph",
 "segments": [
   {"text": "Target met: "},
   {"value": {"input": "kpis", "path": "target_met"}, "format": {"type": "boolean"}}
 ]}
```

`style` is `paragraph` or `heading`. A segment is either literal `{"text"}`
(1–10,000 characters) or `{"value", "format"}`. Up to 100 segments.

**metric** — one key number, optionally with a comparison.

```json
{"id": "revenue_total", "type": "metric", "label": "Revenue",
 "value": {"input": "kpis", "path": "revenue"},
 "format": {"type": "currency", "currency": "EUR"},
 "comparison": {"label": "vs previous week",
                "value": {"input": "kpis", "path": "revenue_change"},
                "format": {"type": "percent", "maximum_fraction_digits": 1}}}
```

The comparison is a value the earlier steps computed; the report does not
subtract, divide or colour it by sign in v1.

**table** — ordered columns over one dataset.

```json
{"id": "region_table", "type": "table", "caption": "Revenue by region",
 "dataset": "regions",
 "columns": [
   {"id": "region", "field": "region", "label": "Region", "format": {"type": "text"}},
   {"id": "revenue", "field": "revenue", "label": "Revenue",
    "format": {"type": "currency", "currency": "EUR"}}
 ],
 "empty_text": "No regional sales this week."}
```

`field` is the exact, case-sensitive key of each row object (1–128 characters).
Nested values are flattened by an earlier Transform step. Up to 30 columns.
Sorting and filtering controls are not part of v1.

**chart** — a `line` or `bar` chart over one dataset.

```json
{"id": "region_chart", "type": "chart", "chart_type": "bar",
 "title": "Revenue by region",
 "description": "Bar chart of weekly revenue in euros for each sales region.",
 "dataset": "regions",
 "category": {"field": "region", "label": "Region", "format": {"type": "text"}},
 "series": [{"id": "revenue", "field": "revenue", "label": "Revenue",
             "format": {"type": "currency", "currency": "EUR"}}],
 "empty_text": "No regional sales this week."}
```

`description` is the accessible text alternative (1–1,000 characters) and is
required. Categories keep dataset order. Up to 10 series. Tooltips that read
values are a renderer feature; there are no other chart options.

**section** — a titled group of components.

```json
{"id": "details", "type": "section", "title": "Returns and notes",
 "children": [ ... ]}
```

`children` holds 1–100 components. Depth counts the top level as 1; a section's
children are one level deeper; the maximum depth is 4.

### 3.2 Static rules beyond JSON Schema

The JSON Schema in section 9 checks local structure. The definition validator
also enforces:

1. Unique IDs as in 2.1 (`report.id_duplicate`).
2. Depth ≤ 4 and ≤ 100 components in total at all depths
   (`report.depth_exceeded`, `report.components_too_many`).
3. Every binding `input` names a declared Template input
   (`report.binding_input_unknown`); every `path` parses with the canonical
   grammar (`report.binding_path_invalid`).
4. Every component `dataset` names a declared dataset (`report.dataset_unknown`)
   and every declared dataset is used by a component (`report.dataset_unused`).
5. A dataset uses at most 30 distinct fields across all components that read it
   (`report.dataset_fields_too_many`).
6. Format rules from 2.4 (`report.format_not_allowed`,
   `report.fraction_digits_invalid`, `report.currency_invalid`).
7. Locale and time zone from 2.5.
8. The serialized definition is at most 64 KiB UTF-8
   (`report.definition_too_large`).

Bindings against declared input types (checked at authoring time; a binding the
declared type rules out is `report.binding_type_impossible`):

| Input type | Value binding | Dataset binding |
| --- | --- | --- |
| `text`, `number`, `boolean` | whole input only (no `path`) | never |
| `object` | with `path` only (a whole object is not a scalar) | with `path` only, for example `{"input": "report_data", "path": "rows"}`; a whole `object` input can never be an array |
| `array` | with `path` only (for example `"0.total"`) | whole input or with `path` |
| `json` | whole input or with `path` | whole input or with `path` |

Whether a `path` really yields a scalar or an array is checked at run time
(`report.binding_not_scalar`, `report.dataset_not_array`).

## 4. Compilation

The compiler is a pure function of the definition, the declared Template inputs
and the values the Template step resolved for them. It performs no I/O, reads no
clock and calls no model. The same inputs always produce byte-identical output.

1. Validate the definition (section 3). Errors stop compilation.
2. For each dataset, in declaration order: resolve its source binding, check it
   is an array of objects, and **project** it:
   - `fields` is the list of distinct row keys that components read from this
     dataset, in order of first reference during a depth-first walk of the
     components in document order (table: columns in order; chart: category,
     then series in order).
   - Each row becomes an array of cell values in `fields` order. Keys that no
     component reads are dropped and never stored.
   - Every cell is checked against every format that reads its field.
3. Walk the components and replace each value binding with its resolved value
   (or default), keeping `format`. Component, column and series objects keep all
   their other authored properties unchanged.
4. Render `summary_text`: the plain rendering (4.1) of `summary` when given,
   otherwise the generated fallback (4.2). An authored `summary` longer than
   4,000 characters fails with `report.summary_too_long`; it is never cut.
5. Check snapshot limits (section 6), including the serialized size.

Any error fails the Template step with `failure_reason` `report_invalid` and the
full list of `report.*` errors; nothing is written to `result_target`. The
compiler never truncates rows, text or series to fit a limit.

### 4.1 Plain rendering (summary only)

`summary_text` is locale-independent plain text meant for chat transcripts and
notifications. Segments are concatenated:

| Format | Rendering |
| --- | --- |
| any, value `null` | `—` |
| `text`, `date` | the string as is |
| `boolean` | `true` / `false` |
| `number` | positional decimal without grouping or exponent. With `maximum_fraction_digits`, round half to even to that many digits, then remove trailing zeros down to `minimum_fraction_digits` (default 0). Without it, the shortest decimal that round-trips the double; an integral value has no decimal point (`21500.0` → `21500`). |
| `currency` | number rendering, a space, the code: `48210.5 EUR` |
| `percent` | value × 100 in decimal arithmetic from the shortest round-trip decimal, then number rendering, then `%`: `0.123` → `12.3%` |
| `datetime` | the instant in the report time zone as `YYYY-MM-DDTHH:MM:SS±HH:MM` (fractional seconds are not shown) |

### 4.2 Generated summary when `summary` is absent

A client without a report viewer receives `summary_text` only, so the fallback
must say more than the title. It is built deterministically from the resolved
snapshot, with no model call, as lines joined by `\n`:

1. the title;
2. then, walking components depth-first in document order (sections contribute
   only their children; text components contribute nothing):
   - metric: `<label>: <value>`, plus ` (<comparison label>: <value>)` when a
     comparison is present, values in the plain rendering of 4.1;
   - table: `<caption>: <N> rows`, N being the row count of its dataset;
   - chart: `<title>: <N> points`, N being the row count of its dataset.

The result is bounded at 4,000 characters. Lines are appended while the text
stays within 3,960 characters; if a line does not fit, the remaining K lines are
replaced by one final line `[K more items not shown]`. This is the only place
where content is left out, it is always marked, and it affects only the summary,
never the snapshot data. For the fixture definition without `summary`:

```text
Weekly sales
Revenue: 48210.5 EUR (vs previous week: 12.3%)
Orders: 0
Refund rate: —
Revenue by region: 3 rows
Revenue by region: 3 points
Returns: 0 rows
```

## 5. ReportSnapshot v1

| Property | Description |
| --- | --- |
| `kind` | The string `fs.report` |
| `schema_version` | The integer `1` |
| `title`, `locale`, `time_zone` | Copied from the definition |
| `summary_text` | Plain text, at most 4,000 characters |
| `datasets` | Array (possibly empty) of `{id, fields, rows}` in definition order |
| `components` | The resolved component tree |

The snapshot carries no workflow, job, user, clock or host data. Its identity
is the finalized job and the output name (ADR 0059); the run's own timestamps
say when it was produced.

Snapshot components have the definition shapes with these differences:

- text segment and summary bindings become `{"value": <scalar>, "format": {…}}`;
- metric `value` and `comparison.value` become a number or `null`;
- `dataset` remains the dataset ID; tables and charts never embed rows.

### 5.1 Snapshot validation

The snapshot validator runs when the compiler finishes and again at the workflow
boundary for the selected `report_output` (any producer, including a child
workflow, must pass it). Besides the JSON Schema it checks:

1. `kind` and `schema_version` first, then dispatches to the validator of that
   version (`report.kind_invalid`, `report.schema_version_unsupported`).
2. Unique IDs, depth and component count as for definitions.
3. Every component `dataset` exists; every column, category and series `field`
   is in that dataset's `fields` (`report.dataset_field_unknown`); every dataset
   is referenced and every field is read by at least one component
   (`report.dataset_unused`, `report.dataset_field_unused`).
4. Each row has exactly `len(fields)` cells (`report.row_shape_invalid`).
5. Every value and cell fits every format that reads it
   (`report.value_type_mismatch`, `report.date_invalid`,
   `report.number_not_finite`, `report.number_out_of_range`).
6. Locale, time zone and currency codes are valid.
7. All limits in section 6, including the serialized size.
8. The value is a JSON object. A JSON string that would parse to a snapshot is
   rejected (`report.snapshot_not_object`); the generic `object` coercion of the
   Interface does not apply to the report output.

JSON parsing for validation rejects `NaN` and `±Infinity` tokens.

## 6. Limits (proposed)

These are domain limits for v1, **proposed until the owner approves them**. The
MCP Apps spike (task 1) only checks whether delivery is feasible; production
delivery budgets per host are measured and set by task 3, which may set lower
delivery ceilings per host but does not raise these. Exceeding any limit is an error with
a field path, never a silent truncation.

| Limit | Value | Error code |
| --- | --- | --- |
| Components, all depths | 100 | `report.components_too_many` |
| Nesting depth | 4 | `report.depth_exceeded` |
| Datasets | 10 | `report.datasets_too_many` |
| Rows per dataset | 1,000 | `report.rows_too_many` |
| Columns per table; fields per dataset | 30 | `report.columns_too_many`, `report.dataset_fields_too_many` |
| Series per chart | 10 | `report.series_too_many` |
| Segments per text or summary | 100 | `report.segments_too_many` |
| Characters in one text component or summary (literals plus string values) | 10,000 | `report.text_too_long` |
| Characters in one string value or cell | 10,000 | `report.text_too_long` |
| `summary_text` | 4,000 characters | `report.summary_too_long` |
| Title, labels, captions, `empty_text` | 200 characters (`empty_text`, `description`: 1,000) | schema `maxLength` |
| Definition size | 64 KiB UTF-8 | `report.definition_too_large` |
| Snapshot size | 1 MiB (1,048,576 bytes) UTF-8 | `report.snapshot_too_large` |
| Non-finite numbers | not allowed | `report.number_not_finite` |

Sizes are measured on `json.dumps(value, ensure_ascii=False,
separators=(",", ":"), allow_nan=False).encode("utf-8")`. The repository's
`canonical_json_bytes` uses `ensure_ascii=True`, which would count a Cyrillic
character as six bytes, so it is not the measure here.

## 7. Errors

Every error uses the existing issue shape:

```json
{"code": "report.binding_missing",
 "message": "Component 'refund_rate': kpis.refund_rate is missing and has no default",
 "path": "report.components[3].value",
 "severity": "error"}
```

Paths are relative to the document and use the same index style as other
authoring issues. The prefix says where the error arose:

| Where | Path prefix | Envelope |
| --- | --- | --- |
| Authoring validation (draft validate, save, apply changes) | `<step path>.override.template.report.` | authoring issue list |
| Template step at run time | `report.` | step failure `report_invalid` with `errors` |
| Workflow boundary for `report_output` | `outputs.<name>.` | `workflow.output.report_invalid`, nested `report.*` errors in `details.errors` |
| Interface selection | `workflow.callable.report_output` | interface issue |

Structural JSON Schema failures map to four codes: `report.property_unknown`
(additionalProperties), `report.property_required`, `report.type_invalid`
(type, const, enum, oneOf) and `report.value_invalid` (pattern, length, range).
A plain `oneOf` failure would point at the whole component, so the validator
first selects the component sub-schema by `type` (and a format sub-schema by its
`type`) and validates against that one; errors then carry the exact field path.
`report.component_type_unknown` is reported when a component `type` is not in
the catalog.

Template step configuration codes extend the existing
`workflow.step_template_*` family: `workflow.step_template_report_missing`,
`workflow.step_template_report_not_allowed` (a `report` object with another
format) and `workflow.step_template_text_not_allowed`.

Interface codes extend `workflow.interface.*`:
`workflow.interface.report_output_unknown` (not a declared output),
`workflow.interface.report_output_not_object`,
`workflow.interface.report_output_not_required` and
`workflow.interface.report_output_disabled` (selection on a disabled Interface,
which would otherwise be ignored silently).

### 7.1 Invalid payload examples

| Payload change against the fixture definition | Error |
| --- | --- |
| `components[0].type` = `"gauge"` | `report.component_type_unknown` at `components[0].type` |
| `components[1].format` = `{"type": "currency"}` | `report.type_invalid` at `components[1].format` (no `currency` code) |
| `components[1].format.currency` = `"EURO"` | `report.value_invalid` at `components[1].format.currency` |
| `components[5].dataset` = `"orders"` (not declared) | `report.dataset_unknown` at `components[5].dataset` |
| `components[6].category.format` = `{"type": "percent"}` | `report.format_not_allowed` at `components[6].category.format` |
| a second component with `id` `"region_table"` | `report.id_duplicate` |
| `components[1].value.input` = `"totals"` (no such Template input) | `report.binding_input_unknown` |
| `components[1].value.path` = `"revenue[*]"` | `report.binding_path_invalid` |
| a `style` property on a metric | `report.property_unknown` at `components[1].style` |
| `components[0].html` = `"<b>x</b>"` | `report.property_unknown` |
| input `kpis.revenue` = `"48210.50"` | `report.value_type_mismatch` at `components[1].value` |
| one row of `regions` is `["EMEA"]` | `report.dataset_row_not_object` at `datasets[0].rows[0]` |
| 1,001 rows in `regions` | `report.rows_too_many` at `datasets[0].rows` |
| snapshot `schema_version` = `2` | `report.schema_version_unsupported` |
| `report_output` names an output of type `string` | `workflow.interface.report_output_not_object` |

## 8. Versioning and future boundary

- `schema_version` is an integer. Readers dispatch on (`kind`,
  `schema_version`); an unknown version is an error, never a best-effort read.
- A stored v1 snapshot stays readable for as long as its job is retained. A new
  version adds a validator and a renderer path; it does not rewrite history.
- Stable component, dataset, column and series IDs, and datasets stored apart
  from components, leave room for local filters in a renderer without changing
  stored data.
- A future refresh is a separate, authorized operation that produces a new run
  and a new snapshot. A snapshot never contains instructions to refetch data,
  and an existing snapshot is never modified.

## 9. JSON Schemas (draft 2020-12)

Both schemas are strict: every object sets `additionalProperties: false`.
Rules that JSON Schema cannot express are listed in 3.2 and 5.1.

### 9.1 ReportDefinition v1

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:flow-steward:workflow-report:definition:1",
  "title": "Flow Steward ReportDefinition v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "title", "locale", "time_zone", "components"],
  "properties": {
    "schema_version": {"const": 1},
    "title": {"$ref": "#/$defs/label"},
    "locale": {"$ref": "#/$defs/locale"},
    "time_zone": {"$ref": "#/$defs/time_zone"},
    "summary": {"$ref": "#/$defs/segments"},
    "datasets": {"type": "array", "maxItems": 10, "items": {"$ref": "#/$defs/dataset"}},
    "components": {"$ref": "#/$defs/components"}
  },
  "$defs": {
    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
    "label": {"type": "string", "minLength": 1, "maxLength": 200},
    "note": {"type": "string", "minLength": 1, "maxLength": 1000},
    "locale": {"type": "string", "pattern": "^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$"},
    "time_zone": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$"},
    "field": {"type": "string", "minLength": 1, "maxLength": 128},
    "fraction_digits": {"type": "integer", "minimum": 0, "maximum": 6},
    "format_text": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "text"}}},
    "format_boolean": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "boolean"}}},
    "format_date": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "date"}}},
    "format_datetime": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "datetime"}}},
    "format_number": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "number"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_percent": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "percent"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_currency": {
      "type": "object", "additionalProperties": false, "required": ["type", "currency"],
      "properties": {
        "type": {"const": "currency"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_boolean"},
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}, {"$ref": "#/$defs/format_date"},
        {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "numeric_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}
      ]
    },
    "category_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_number"},
        {"$ref": "#/$defs/format_date"}, {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "input_name": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"},
    "relative_path": {"type": "string", "minLength": 1, "maxLength": 512},
    "value_binding": {
      "type": "object", "additionalProperties": false, "required": ["input"],
      "properties": {
        "input": {"$ref": "#/$defs/input_name"},
        "path": {"$ref": "#/$defs/relative_path"},
        "default": {"type": ["string", "number", "boolean", "null"], "maxLength": 10000}
      }
    },
    "dataset_binding": {
      "type": "object", "additionalProperties": false, "required": ["input"],
      "properties": {
        "input": {"$ref": "#/$defs/input_name"},
        "path": {"$ref": "#/$defs/relative_path"},
        "default": {"type": "array", "maxItems": 0}
      }
    },
    "segment": {
      "oneOf": [
        {
          "type": "object", "additionalProperties": false, "required": ["text"],
          "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 10000}}
        },
        {
          "type": "object", "additionalProperties": false, "required": ["value", "format"],
          "properties": {"value": {"$ref": "#/$defs/value_binding"}, "format": {"$ref": "#/$defs/format"}}
        }
      ]
    },
    "segments": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/segment"}},
    "dataset": {
      "type": "object", "additionalProperties": false, "required": ["id", "source"],
      "properties": {"id": {"$ref": "#/$defs/id"}, "source": {"$ref": "#/$defs/dataset_binding"}}
    },
    "components": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/component"}},
    "component": {
      "oneOf": [
        {"$ref": "#/$defs/text_component"}, {"$ref": "#/$defs/metric_component"},
        {"$ref": "#/$defs/table_component"}, {"$ref": "#/$defs/chart_component"},
        {"$ref": "#/$defs/section_component"}
      ]
    },
    "text_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "style", "segments"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "text"},
        "style": {"enum": ["paragraph", "heading"]},
        "segments": {"$ref": "#/$defs/segments"}
      }
    },
    "metric_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "label", "value", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "metric"},
        "label": {"$ref": "#/$defs/label"},
        "value": {"$ref": "#/$defs/value_binding"},
        "format": {"$ref": "#/$defs/numeric_format"},
        "comparison": {
          "type": "object", "additionalProperties": false, "required": ["label", "value", "format"],
          "properties": {
            "label": {"$ref": "#/$defs/label"},
            "value": {"$ref": "#/$defs/value_binding"},
            "format": {"$ref": "#/$defs/numeric_format"}
          }
        }
      }
    },
    "table_column": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/format"}
      }
    },
    "table_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "caption", "dataset", "columns", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "table"},
        "caption": {"$ref": "#/$defs/label"},
        "dataset": {"$ref": "#/$defs/id"},
        "columns": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"$ref": "#/$defs/table_column"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "chart_category": {
      "type": "object", "additionalProperties": false, "required": ["field", "label", "format"],
      "properties": {
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/category_format"}
      }
    },
    "chart_series": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/numeric_format"}
      }
    },
    "chart_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "chart_type", "title", "description", "dataset", "category", "series", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "chart"},
        "chart_type": {"enum": ["line", "bar"]},
        "title": {"$ref": "#/$defs/label"},
        "description": {"$ref": "#/$defs/note"},
        "dataset": {"$ref": "#/$defs/id"},
        "category": {"$ref": "#/$defs/chart_category"},
        "series": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"$ref": "#/$defs/chart_series"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "section_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "title", "children"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "section"},
        "title": {"$ref": "#/$defs/label"},
        "children": {"$ref": "#/$defs/components"}
      }
    }
  }
}
```

### 9.2 ReportSnapshot v1

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:flow-steward:workflow-report:snapshot:1",
  "title": "Flow Steward ReportSnapshot v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["kind", "schema_version", "title", "locale", "time_zone", "summary_text", "datasets", "components"],
  "properties": {
    "kind": {"const": "fs.report"},
    "schema_version": {"const": 1},
    "title": {"$ref": "#/$defs/label"},
    "locale": {"$ref": "#/$defs/locale"},
    "time_zone": {"$ref": "#/$defs/time_zone"},
    "summary_text": {"type": "string", "minLength": 1, "maxLength": 4000},
    "datasets": {"type": "array", "maxItems": 10, "items": {"$ref": "#/$defs/dataset"}},
    "components": {"$ref": "#/$defs/components"}
  },
  "$defs": {
    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
    "label": {"type": "string", "minLength": 1, "maxLength": 200},
    "note": {"type": "string", "minLength": 1, "maxLength": 1000},
    "locale": {"type": "string", "pattern": "^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$"},
    "time_zone": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$"},
    "field": {"type": "string", "minLength": 1, "maxLength": 128},
    "scalar": {"type": ["string", "number", "boolean", "null"], "maxLength": 10000},
    "number_or_null": {"type": ["number", "null"]},
    "fraction_digits": {"type": "integer", "minimum": 0, "maximum": 6},
    "format_text": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "text"}}},
    "format_boolean": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "boolean"}}},
    "format_date": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "date"}}},
    "format_datetime": {"type": "object", "additionalProperties": false, "required": ["type"], "properties": {"type": {"const": "datetime"}}},
    "format_number": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "number"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_percent": {
      "type": "object", "additionalProperties": false, "required": ["type"],
      "properties": {
        "type": {"const": "percent"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format_currency": {
      "type": "object", "additionalProperties": false, "required": ["type", "currency"],
      "properties": {
        "type": {"const": "currency"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "minimum_fraction_digits": {"$ref": "#/$defs/fraction_digits"},
        "maximum_fraction_digits": {"$ref": "#/$defs/fraction_digits"}
      }
    },
    "format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_boolean"},
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}, {"$ref": "#/$defs/format_date"},
        {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "numeric_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_number"}, {"$ref": "#/$defs/format_currency"},
        {"$ref": "#/$defs/format_percent"}
      ]
    },
    "category_format": {
      "oneOf": [
        {"$ref": "#/$defs/format_text"}, {"$ref": "#/$defs/format_number"},
        {"$ref": "#/$defs/format_date"}, {"$ref": "#/$defs/format_datetime"}
      ]
    },
    "dataset": {
      "type": "object", "additionalProperties": false, "required": ["id", "fields", "rows"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "fields": {"type": "array", "minItems": 1, "maxItems": 30, "uniqueItems": true, "items": {"$ref": "#/$defs/field"}},
        "rows": {
          "type": "array", "maxItems": 1000,
          "items": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"$ref": "#/$defs/scalar"}}
        }
      }
    },
    "segment": {
      "oneOf": [
        {
          "type": "object", "additionalProperties": false, "required": ["text"],
          "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 10000}}
        },
        {
          "type": "object", "additionalProperties": false, "required": ["value", "format"],
          "properties": {"value": {"$ref": "#/$defs/scalar"}, "format": {"$ref": "#/$defs/format"}}
        }
      ]
    },
    "components": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/component"}},
    "component": {
      "oneOf": [
        {"$ref": "#/$defs/text_component"}, {"$ref": "#/$defs/metric_component"},
        {"$ref": "#/$defs/table_component"}, {"$ref": "#/$defs/chart_component"},
        {"$ref": "#/$defs/section_component"}
      ]
    },
    "text_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "style", "segments"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "text"},
        "style": {"enum": ["paragraph", "heading"]},
        "segments": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/segment"}}
      }
    },
    "metric_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "label", "value", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "metric"},
        "label": {"$ref": "#/$defs/label"},
        "value": {"$ref": "#/$defs/number_or_null"},
        "format": {"$ref": "#/$defs/numeric_format"},
        "comparison": {
          "type": "object", "additionalProperties": false, "required": ["label", "value", "format"],
          "properties": {
            "label": {"$ref": "#/$defs/label"},
            "value": {"$ref": "#/$defs/number_or_null"},
            "format": {"$ref": "#/$defs/numeric_format"}
          }
        }
      }
    },
    "table_column": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/format"}
      }
    },
    "table_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "caption", "dataset", "columns", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "table"},
        "caption": {"$ref": "#/$defs/label"},
        "dataset": {"$ref": "#/$defs/id"},
        "columns": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"$ref": "#/$defs/table_column"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "chart_category": {
      "type": "object", "additionalProperties": false, "required": ["field", "label", "format"],
      "properties": {
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/category_format"}
      }
    },
    "chart_series": {
      "type": "object", "additionalProperties": false, "required": ["id", "field", "label", "format"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "field": {"$ref": "#/$defs/field"},
        "label": {"$ref": "#/$defs/label"},
        "format": {"$ref": "#/$defs/numeric_format"}
      }
    },
    "chart_component": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "type", "chart_type", "title", "description", "dataset", "category", "series", "empty_text"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "chart"},
        "chart_type": {"enum": ["line", "bar"]},
        "title": {"$ref": "#/$defs/label"},
        "description": {"$ref": "#/$defs/note"},
        "dataset": {"$ref": "#/$defs/id"},
        "category": {"$ref": "#/$defs/chart_category"},
        "series": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"$ref": "#/$defs/chart_series"}},
        "empty_text": {"$ref": "#/$defs/note"}
      }
    },
    "section_component": {
      "type": "object", "additionalProperties": false, "required": ["id", "type", "title", "children"],
      "properties": {
        "id": {"$ref": "#/$defs/id"},
        "type": {"const": "section"},
        "title": {"$ref": "#/$defs/label"},
        "children": {"$ref": "#/$defs/components"}
      }
    }
  }
}
```

In checkpoint 2B these two schemas move into package files under
`core/application/workflow_documents/`; a unit test then asserts that the
package files and the blocks above are identical, so there is one contract and
no second copy that can drift.

## 10. Fixture walkthrough

`sales_report_inputs.json` declares four Template inputs; `commentary` is
optional and absent from `values`.

| Case | Where in the fixture | Result in the snapshot |
| --- | --- | --- |
| Metric with currency and comparison | `revenue_total` | `value` 48210.5, EUR format; comparison 0.123 as percent |
| Zero is a value | `orders` metric reads `kpis.orders` = 0 | `value: 0` |
| Null is kept | `refund_rate` reads `kpis.refund_rate` = `null` | `value: null` |
| False is a value | `status` segment reads `kpis.target_met` = `false` | `value: false` |
| Optional input with default | `commentary` segment, input not supplied | the default text |
| 3 of 5 source columns | `regions` rows have `region`, `orders`, `revenue`, `margin`, `manager`; the table reads three, the chart reuses two of them | dataset `fields` = `region`, `revenue`, `margin`; `orders` and `manager` are not stored |
| Zero and null cells | APAC row: `revenue` 0, `margin` null | `["APAC", 0, null]` |
| Bar chart | `region_chart` | reads the same projected dataset |
| Section | `details` with a table and a text | nested component tree |
| Empty dataset | `returns` = `[]` | `{"id": "returns", "fields": [...], "rows": []}`; table shows `empty_text` |
| Date and datetime | `kpis.week`, `kpis.as_of` | stored as given; rendered in `en-US` and `Europe/Berlin` |
| Summary | `summary` segments | `Revenue 48210.5 EUR in the week of 2026-11-02; target met: false.` |
