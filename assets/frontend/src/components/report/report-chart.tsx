/**
 * A report chart: a captioned figure, the chart drawn by ECharts in SVG, its
 * description as the accessible name, and its values as a table behind a
 * disclosure so every number is reachable without the pointer.
 */
import { useEffect, useId, useRef } from "react";

import { buildReportChartOption, readChartTheme } from "./report-chart-option";
import type { ReportFormatContext } from "./report-format";
import type { ReportLabels } from "./report-labels";
import { ReportDataTable } from "./report-table";
import type { ReportChartComponent, ReportDataset } from "./report-types";

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

  useEffect(() => {
    const element = canvasRef.current;
    if (!element || !dataset || !hasRows) return undefined;
    let disposed = false;
    let mounted: { resize: () => void; dispose: () => void } | null = null;
    let observer: ResizeObserver | null = null;
    // Loaded on demand: the chart engine is the heaviest part of a report.
    void import("./report-chart-engine").then(({ mountReportChart }) => {
      if (disposed) return;
      mounted = mountReportChart(element, buildReportChartOption(chart, dataset, context, readChartTheme(element)));
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(() => mounted?.resize());
        observer.observe(element);
      }
    });
    return () => {
      disposed = true;
      observer?.disconnect();
      mounted?.dispose();
    };
  }, [chart, dataset, context, hasRows]);

  const dataColumns = [
    { id: "category", ...chart.category },
    ...chart.series,
  ];
  return (
    <figure aria-labelledby={titleId} className="fs-report-block fs-report-chart">
      <figcaption className="fs-report-caption" id={titleId}>
        {chart.title}
      </figcaption>
      {hasRows ? (
        <>
          <div
            aria-label={chart.description}
            className="fs-report-chart-canvas"
            data-chart-type={chart.chart_type}
            ref={canvasRef}
            role="img"
          />
          <details className="fs-report-chart-data">
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
