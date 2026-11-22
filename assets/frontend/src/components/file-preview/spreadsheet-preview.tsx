/**
 * An XLSX, XLS or CSV file as a read-only table, one sheet per tab (ADR 0080).
 *
 * SheetJS Community Edition reads the file in the page. Formulas are never
 * calculated: a cell shows the value the spreadsheet saved with it, formatted
 * by its own number format, and the default date format reads as YYYY-MM-DD.
 * Only the first rows and columns are drawn, with a note saying how many there are.
 * Every value is shown as text; nothing in a cell is interpreted as markup.
 */
import { useId, useMemo, useState } from "react";
import { read, utils, type ParsingOptions, type Range, type WorkBook, type WorkSheet } from "xlsx";

import { DsTabs } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";

import { previewFailureMessage } from "./file-preview-messages";
import { SPREADSHEET_PREVIEW_COLUMNS, SPREADSHEET_PREVIEW_ROWS, csvPreviewSlice } from "./spreadsheet-preview-model";

const ZIP_SIGNATURE = [0x50, 0x4b, 0x03, 0x04];
const COMPOUND_FILE_SIGNATURE = [0xd0, 0xcf, 0x11, 0xe0];

const PARSING: ParsingOptions = {
  dense: true,
  cellFormula: false,
  cellHTML: false,
  cellStyles: false,
  cellDates: false,
  dateNF: "yyyy-mm-dd",
};

type ParsedBook = { workbook: WorkBook; csvRows: number | null } | null;

function startsWith(bytes: Uint8Array, signature: readonly number[]): boolean {
  return signature.every((value, index) => bytes[index] === value);
}

/** XLSX (zip) and XLS (compound file) are read as bytes; CSV and text exports as UTF-8 text. */
function readWorkbook(data: ArrayBuffer): ParsedBook {
  const bytes = new Uint8Array(data);
  try {
    if (startsWith(bytes, ZIP_SIGNATURE) || startsWith(bytes, COMPOUND_FILE_SIGNATURE)) {
      return { workbook: read(bytes, { ...PARSING, type: "array", sheetRows: SPREADSHEET_PREVIEW_ROWS }), csvRows: null };
    }
    const text = new TextDecoder("utf-8").decode(bytes);
    if (text.trimStart().startsWith("<")) {
      return { workbook: read(text, { ...PARSING, type: "string", raw: true, sheetRows: SPREADSHEET_PREVIEW_ROWS }), csvRows: null };
    }
    // CSV keeps its values as written; only the rows drawn are parsed.
    const slice = csvPreviewSlice(text, SPREADSHEET_PREVIEW_ROWS);
    return { workbook: read(slice.text, { ...PARSING, type: "string", raw: true }), csvRows: slice.total };
  } catch {
    return null;
  }
}

function sheetRange(sheet: WorkSheet | undefined, key: "!ref" | "!fullref"): Range | null {
  const reference: unknown = sheet?.[key];
  return typeof reference === "string" && reference ? utils.decode_range(reference) : null;
}

type SheetView = {
  columns: string[];
  rows: { number: number; cells: string[] }[];
  totalRows: number;
  totalColumns: number;
};

function sheetView(sheet: WorkSheet | undefined, csvRows: number | null): SheetView {
  const range = sheetRange(sheet, "!ref");
  if (!sheet || !range) return { columns: [], rows: [], totalRows: 0, totalColumns: 0 };
  const full = sheetRange(sheet, "!fullref") ?? range;
  const lastRow = Math.min(range.e.r, range.s.r + SPREADSHEET_PREVIEW_ROWS - 1);
  const lastColumn = Math.min(range.e.c, range.s.c + SPREADSHEET_PREVIEW_COLUMNS - 1);
  const shown: Range = { s: range.s, e: { r: lastRow, c: lastColumn } };
  const values = utils.sheet_to_json<unknown[]>(sheet, { header: 1, range: shown, raw: false, defval: "", blankrows: true });
  const columns: string[] = [];
  for (let column = range.s.c; column <= lastColumn; column += 1) columns.push(utils.encode_col(column));
  return {
    columns,
    rows: values.map((row, index) => ({
      number: range.s.r + index + 1,
      cells: columns.map((_, column) => {
        const value = row[column];
        return value === undefined || value === null ? "" : String(value);
      }),
    })),
    totalRows: csvRows ?? full.e.r - full.s.r + 1,
    totalColumns: full.e.c - full.s.c + 1,
  };
}

export default function SpreadsheetPreview({ data, name }: Readonly<{ data: ArrayBuffer; name: string }>) {
  const { t } = useI18n();
  const idPrefix = useId().replaceAll(":", "");
  const parsed = useMemo(() => readWorkbook(data), [data]);
  const [active, setActive] = useState("0");
  const sheetNames = parsed?.workbook.SheetNames ?? [];
  const index = Math.min(Number(active), Math.max(0, sheetNames.length - 1));
  const sheet = parsed ? parsed.workbook.Sheets[sheetNames[index] ?? ""] : undefined;
  const view = useMemo(() => sheetView(sheet, parsed?.csvRows ?? null), [sheet, parsed]);

  if (!parsed || !sheetNames.length) {
    return (
      <p className="fs-file-preview-status fs-ink-danger" role="alert">
        {previewFailureMessage("unreadable", t)}
      </p>
    );
  }
  const notes = [
    view.totalRows > view.rows.length
      ? t("react.jobs.file_preview_rows_shown", "Showing {shown} of {total} rows.", { shown: String(view.rows.length), total: String(view.totalRows) })
      : "",
    view.totalColumns > view.columns.length
      ? t("react.jobs.file_preview_columns_shown", "Showing {shown} of {total} columns.", { shown: String(view.columns.length), total: String(view.totalColumns) })
      : "",
  ].filter(Boolean);
  return (
    <div className="fs-file-preview-sheet" data-testid="file-preview-spreadsheet">
      {sheetNames.length > 1 ? (
        <DsTabs
          active={String(index)}
          ariaLabel={t("react.jobs.file_preview_sheets", "Sheets of {name}", { name })}
          idPrefix={idPrefix}
          items={sheetNames.map((sheetName, key) => ({ key: String(key), label: sheetName }))}
          onSelect={setActive}
          revealActiveTab
        />
      ) : null}
      <div
        aria-labelledby={sheetNames.length > 1 ? `${idPrefix}-tab-${index}` : undefined}
        id={`${idPrefix}-panel-${index}`}
        role={sheetNames.length > 1 ? "tabpanel" : undefined}
      >
        {notes.length ? <p className="fs-file-preview-note">{notes.join(" ")}</p> : null}
        {view.rows.length ? (
          <div className="fs-table-scroll fs-file-preview-sheet-scroll">
            <table aria-label={sheetNames[index]} className="fs-table fs-file-preview-sheet-table">
              <thead>
                <tr>
                  <th aria-hidden="true" />
                  {view.columns.map((column) => (
                    <th key={column} scope="col">
                      {column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {view.rows.map((row) => (
                  <tr key={row.number}>
                    <th scope="row">{row.number}</th>
                    {row.cells.map((cell, column) => (
                      <td key={view.columns[column]}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="fs-empty-copy">{t("react.jobs.file_preview_empty_sheet", "This sheet is empty.")}</p>
        )}
      </div>
    </div>
  );
}
