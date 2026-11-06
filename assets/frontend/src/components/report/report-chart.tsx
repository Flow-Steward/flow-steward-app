/**
 * A report chart: a captioned figure, the chart drawn by ECharts in SVG, its
 * description as the accessible name, and its values as a table behind a
 * disclosure so every number is reachable without the pointer.
 */
import { useEffect, useId, useRef, useState } from "react";

import type { MountedChart } from "./report-chart-engine";
import { loadReportChartEngine } from "./report-chart-loader";
import { buildReportChartOption, readChartTheme } from "./report-chart-option";
import type { ReportFormatContext } from "./report-format";
import type { ReportLabels } from "./report-labels";
import { ReportDataTable } from "./report-table";
import type { ReportChartComponent, ReportDataset } from "./report-types";
import { useThemeRevision } from "./use-theme-revision";

/** Internal ids of the data table: prefixed so a series id can never equal the category's. */
const CATEGORY_COLUMN_ID = "c:category";
const seriesColumnId = (id: string) => `s:${id}`;

export function ReportChart({
  chart,
  dataset,
  context,
  labels,
}: Readonly<{
  chart: ReportChartComponent;
  dataset: ReportDataset | undefined;
  context: ReportFormatContext;
  labels: ReportLabels;
}>) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const hasRows = Boolean(dataset?.rows.length);
  const themeRevision = useThemeRevision();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const element = canvasRef.current;
    if (!element || !dataset || !hasRows) return undefined;
    let disposed = false;
    let mounted: MountedChart | null = null;
    let observer: ResizeObserver | null = null;
    const fail = () => {
      if (!disposed) setFailed(true);
    };
    // Loaded on demand: the chart engine is the heaviest part of a report. A
    // failed load or draw leaves a visible notice and the data table.
    loadReportChartEngine()
      .then(({ mountReportChart }) => {
        if (disposed) return;
        try {
          mounted = mountReportChart(
            element,
            buildReportChartOption(chart, dataset, context, readChartTheme(element)),
          );
        } catch {
          fail();
          return;
        }
        setFailed(false);
        if (typeof ResizeObserver !== "undefined") {
          observer = new ResizeObserver(() => mounted?.resize());
          observer.observe(element);
        }
      })
      .catch(fail);
    return () => {
      disposed = true;
      observer?.disconnect();
      mounted?.dispose();
    };
  }, [chart, dataset, context, hasRows, themeRevision]);

  const dataColumns = [
    { ...chart.category, id: CATEGORY_COLUMN_ID },
    ...chart.series.map((series) => ({ ...series, id: seriesColumnId(series.id) })),
  ];
  return (
    <figure aria-labelledby={titleId} className="fs-report-block fs-report-chart">
      <figcaption className="fs-report-caption" id={titleId}>
        {chart.title}
      </figcaption>
      {hasRows ? (
        <>
          {failed ? (
            <p className="fs-report-empty" role="status">
              {labels.chartUnavailable}
            </p>
          ) : null}
          <div
            aria-label={chart.description}
            className="fs-report-chart-canvas"
            data-chart-type={chart.chart_type}
            hidden={failed}
            ref={canvasRef}
            role="img"
          />
          <details className="fs-report-chart-data" open={failed || undefined}>
            <summary>{labels.showData}</summary>
            <ReportDataTable
              caption={labels.chartDataCaption.replace("{title}", () => chart.title)}
              columns={dataColumns}
              context={context}
              dataset={dataset}
              emptyText={chart.empty_text}
            />
          </details>
        </>
      ) : (
        <p className="fs-report-empty">{chart.empty_text}</p>
      )}
    </figure>
  );
}
