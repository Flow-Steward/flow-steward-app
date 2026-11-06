/**
 * The one report renderer, shared by the job result page, the Template preview
 * and the MCP Apps viewer. It shows a stored ReportSnapshot as it is: no
 * fetching, no refresh, no state beyond the chart data disclosure.
 *
 * Every string from a report reaches the page as React text, never as HTML.
 * Metrics use the published MetricCard markup (`fs-metric`) without importing
 * the card itself, which would pull the router into the MCP Apps bundle.
 */
import { Fragment, useMemo, type ReactNode } from "react";

import "./report.css";

import { ReportChart } from "./report-chart";
import { formatReportValue, type ReportFormatContext } from "./report-format";
import { defaultReportLabels, type ReportLabels } from "./report-labels";
import { ReportDataTable } from "./report-table";
import {
  readReport,
  type ReportComponent,
  type ReportDataset,
  type ReportMetricComponent,
  type ReportSegment,
  type ReportSnapshot,
} from "./report-types";

type RenderContext = {
  format: ReportFormatContext;
  datasets: ReadonlyMap<string, ReportDataset>;
  labels: ReportLabels;
};

const MAX_HEADING_LEVEL = 6;

function Heading({ level, className, children }: Readonly<{ level: number; className: string; children: ReactNode }>) {
  const capped = Math.min(Math.max(level, 2), MAX_HEADING_LEVEL);
  const Tag = `h${capped}` as "h2" | "h3" | "h4" | "h5" | "h6";
  return <Tag className={className}>{children}</Tag>;
}

function Segments({ segments, ctx }: Readonly<{ segments: readonly ReportSegment[]; ctx: RenderContext }>) {
  return (
    <>
      {segments.map((segment, index) =>
        "text" in segment ? (
          <Fragment key={index}>{segment.text}</Fragment>
        ) : (
          <span className="fs-report-value" key={index}>
            {formatReportValue(segment.value, segment.format, ctx.format)}
          </span>
        ),
      )}
    </>
  );
}

function Metric({ metric, ctx }: Readonly<{ metric: ReportMetricComponent; ctx: RenderContext }>) {
  const comparison = metric.comparison;
  return (
    <div aria-label={metric.label} className="fs-metric fs-report-metric" data-density="standard" role="group">
      <span className="fs-metric-top">
        <span className="fs-metric-label">{metric.label}</span>
      </span>
      <span className="fs-metric-figure">{formatReportValue(metric.value, metric.format, ctx.format)}</span>
      {comparison ? (
        <span className="fs-metric-meta">
          <span>
            {comparison.label}: {formatReportValue(comparison.value, comparison.format, ctx.format)}
          </span>
        </span>
      ) : null}
    </div>
  );
}

/** Consecutive metrics share one responsive grid; every other component stands alone. */
function groupComponents(components: readonly ReportComponent[]): Array<ReportComponent | ReportMetricComponent[]> {
  const groups: Array<ReportComponent | ReportMetricComponent[]> = [];
  for (const component of components) {
    const last = groups.at(-1);
    if (component.type === "metric") {
      if (Array.isArray(last)) last.push(component);
      else groups.push([component]);
    } else {
      groups.push(component);
    }
  }
  return groups;
}

function ComponentList({
  components,
  depth,
  ctx,
}: Readonly<{ components: readonly ReportComponent[]; depth: number; ctx: RenderContext }>) {
  return (
    <>
      {groupComponents(components).map((group) =>
        Array.isArray(group) ? (
          <div
            className="fs-metric-grid fs-report-metrics"
            data-columns={String(Math.min(Math.max(group.length, 2), 4))}
            key={group[0].id}
          >
            {group.map((metric) => (
              <Metric ctx={ctx} key={metric.id} metric={metric} />
            ))}
          </div>
        ) : (
          <ReportPart component={group} ctx={ctx} depth={depth} key={group.id} />
        ),
      )}
    </>
  );
}

function ReportPart({ component, depth, ctx }: Readonly<{ component: ReportComponent; depth: number; ctx: RenderContext }>) {
  switch (component.type) {
    case "text":
      return component.style === "heading" ? (
        <Heading className="fs-report-heading" level={depth + 2}>
          <Segments ctx={ctx} segments={component.segments} />
        </Heading>
      ) : (
        <p className="fs-report-paragraph">
          <Segments ctx={ctx} segments={component.segments} />
        </p>
      );
    case "table":
      return (
        <ReportDataTable
          caption={component.caption}
          columns={component.columns}
          context={ctx.format}
          dataset={ctx.datasets.get(component.dataset)}
          emptyText={component.empty_text}
        />
      );
    case "chart":
      return (
        <ReportChart
          chart={component}
          context={ctx.format}
          dataset={ctx.datasets.get(component.dataset)}
          labels={ctx.labels}
        />
      );
    case "section":
      return (
        <section className="fs-report-section">
          <Heading className="fs-report-section-title" level={depth + 2}>
            {component.title}
          </Heading>
          <ComponentList components={component.children} ctx={ctx} depth={depth + 1} />
        </section>
      );
    default:
      return null;
  }
}

function ReportDocument({ report, labels }: Readonly<{ report: ReportSnapshot; labels: ReportLabels }>) {
  const ctx = useMemo<RenderContext>(
    () => ({
      format: { locale: report.locale, timeZone: report.time_zone, labels },
      datasets: new Map(report.datasets.map((dataset) => [dataset.id, dataset])),
      labels,
    }),
    [report, labels],
  );
  return (
    <article className="fs-report" lang={report.locale}>
      <Heading className="fs-report-title" level={2}>
        {report.title}
      </Heading>
      <ComponentList components={report.components} ctx={ctx} depth={1} />
    </article>
  );
}

export function ReportView({ report, labels = defaultReportLabels }: Readonly<{ report: unknown; labels?: ReportLabels }>) {
  const read = readReport(report);
  if (read.status !== "ok") {
    return (
      <p className="fs-report-notice" role="status">
        {read.status === "unsupported" ? labels.unsupported : labels.invalid}
      </p>
    );
  }
  return <ReportDocument labels={labels} report={read.report} />;
}
