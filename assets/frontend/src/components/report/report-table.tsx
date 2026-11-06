/**
 * A semantic report table: TanStack Table is the headless row model, the markup
 * and styles are ours (the design-system table classes). Rows keep the order the
 * report stores; there is no sorting or filtering in v1.
 */
import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { formatReportValue, type ReportFormatContext } from "./report-format";
import { isNumericFormat, type ReportDataset, type ReportScalar, type ReportTableColumn } from "./report-types";

type Row = ReportScalar[];

export function ReportDataTable({
  caption,
  columns,
  dataset,
  emptyText,
  context,
}: Readonly<{
  caption: string;
  columns: readonly ReportTableColumn[];
  dataset: ReportDataset | undefined;
  emptyText: string;
  context: ReportFormatContext;
}>) {
  const rows = useMemo<Row[]>(() => dataset?.rows ?? [], [dataset]);
  const numeric = useMemo(
    () => new Set(columns.filter((column) => isNumericFormat(column.format)).map((column) => column.id)),
    [columns],
  );
  const columnDefs = useMemo<ColumnDef<Row, ReportScalar>[]>(
    () =>
      columns.map((column) => {
        const index = dataset ? dataset.fields.indexOf(column.field) : -1;
        return {
          id: column.id,
          header: column.label,
          accessorFn: (row: Row) => (index >= 0 ? (row[index] ?? null) : null),
          cell: (info) => formatReportValue(info.getValue(), column.format, context),
        };
      }),
    [columns, dataset, context],
  );
  // TanStack Table returns fresh functions on every render, so React Compiler
  // rightly leaves this component unmemoized; the rows are small and immutable.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({ data: rows, columns: columnDefs, getCoreRowModel: getCoreRowModel() });

  if (!rows.length) {
    return (
      <div className="fs-report-block">
        <p className="fs-report-caption">{caption}</p>
        <p className="fs-report-empty">{emptyText}</p>
      </div>
    );
  }
  return (
    <div className="fs-table-scroll fs-report-table">
      <table className="fs-table embedded">
        <caption className="fs-report-caption">{caption}</caption>
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => (
                <th
                  className={numeric.has(header.column.id) ? "fs-report-num" : undefined}
                  key={header.id}
                  scope="col"
                >
                  {flexRender(header.column.columnDef.header, header.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id}>
              {row.getVisibleCells().map((cell) => (
                <td className={numeric.has(cell.column.id) ? "fs-report-num" : undefined} key={cell.id}>
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
