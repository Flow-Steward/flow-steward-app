import { useI18n } from "@/lib/i18n/useI18n";
import { ArrowRight } from "lucide-react";
import { useEffect, useId, useRef, type PropsWithChildren, type ReactNode } from "react";
import { Link, NavLink } from "react-router";

import { useLoaderDelay } from "@/components/shared/use-loader-delay";

/** An absent value, styled so it does not read as data. */
/**
 * Brand glyph, not an icon: geometry and stroke come verbatim from the
 * MetricCard catalog preview, where it belongs to variants A and E only.
 */
function MetricWave() {
  return (
    <svg
      aria-hidden="true"
      className="fs-metric-wave"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 40 24"
    >
      <path d="M2 16c4-8 7-8 10 0s7 8 10-4 7-2 10 4 4 4 6 0" />
    </svg>
  );
}

/**
 * The published MetricCard. One numeric signal per card, the figure leading and
 * one line of supporting context pinned to the bottom.
 *
 * `attention` is reserved for work that needs a human and only ever above zero:
 * at zero the card stays on the neutral surface beside its neighbours, because
 * "nothing is wrong" must not read like "work succeeded". `danger` follows the
 * same rule for work that broke, on the status danger tone, and wins over
 * `attention`. `unavailable` is the separate published state for a value that
 * cannot be read at all.
 */
export function DsMetricCard({
  label,
  figure,
  meta,
  delta,
  to,
  ariaLabel,
  attention = false,
  danger = false,
  unavailable = false,
  density = "standard",
  wave = true,
}: Readonly<{
  label: string;
  /** Already formatted: the card does not decide number formatting. */
  figure: string;
  /** One line of context; longer copy clamps rather than wrapping. */
  meta?: ReactNode;
  delta?: { label: string; tone?: "positive" | "attention" | "neutral" };
  /** Makes the whole card the navigation target, with the published affordance. */
  to?: string;
  /** Required with `to`: the figure alone is not an accessible name. */
  ariaLabel?: string;
  attention?: boolean;
  danger?: boolean;
  unavailable?: boolean;
  density?: "hero" | "standard" | "compact";
  /** The wave belongs to variants A and E; drop it on a bare figure card. */
  wave?: boolean;
}>) {
  let state: "danger" | "attention" | "unavailable" | undefined;
  if (danger) state = "danger";
  else if (attention) state = "attention";
  else if (unavailable) state = "unavailable";
  const body = (
    <>
      <span className="fs-metric-top">
        <span className="fs-metric-label">{label}</span>
        {wave ? <MetricWave /> : null}
      </span>
      <span className="fs-metric-figure">{figure}</span>
      {meta || delta ? (
        <span className="fs-metric-meta">
          {delta ? <span className="fs-metric-delta" data-tone={delta.tone}>{delta.label}</span> : null}
          {meta ? <span>{meta}</span> : null}
        </span>
      ) : null}
      {to ? <ArrowRight aria-hidden="true" className="fs-metric-go" /> : null}
    </>
  );
  if (!to) {
    return <div className="fs-metric" data-density={density} data-state={state}>{body}</div>;
  }
  return (
    <Link aria-label={ariaLabel} className="fs-metric" data-density={density} data-state={state} to={to}>
      {body}
    </Link>
  );
}

export type DsTimelineEvent = {
  id: string;
  title: string;
  /** Rendered inside a real <time>; pass the machine value as `dateTime`. */
  time: string;
  dateTime?: string;
  /** Sequence emphasis for the current actionable step. Never decoration. */
  current?: boolean;
};

/**
 * The published Timeline: chronological events on a compact rail.
 *
 * Choose it only when the order changes how the events are understood. It is
 * not an activity feed - the catalog reserves that for actor-rich, avatar and
 * reply content, and does not publish it as a component.
 *
 * The published anatomy wraps the list in its own 311px card. This renders bare
 * so the consuming card provides the surface, because card.md forbids a card
 * inside a card; every other value is the canonical geometry.
 */
/**
 * The timeline's pending state on the canon Skeleton (data-display/skeleton.md):
 * the rail is known before the data, so it renders for real, and only the two
 * lines of each event stand in. Three events, fixed; nothing for the first
 * 400ms; one status for the region.
 */
function DsTimelineLoading({ ariaLabel }: Readonly<{ ariaLabel: string }>) {
  const visible = useLoaderDelay();
  return (
    <div aria-label={ariaLabel} role="status">
      {visible ? (
        <ol aria-busy="true" className="fs-timeline">
          {[0, 1, 2].map((row, index) => (
            <li className="fs-timeline-event" key={row}>
              <span aria-hidden="true" className="fs-timeline-rail">
                <span className="fs-timeline-dot" />
                {index < 2 ? <span className="fs-timeline-line" /> : null}
              </span>
              <div aria-hidden="true" className="fs-timeline-content fs-skeleton-lines">
                <span className="fs-skeleton fs-skeleton-line" style={{ width: `${70 - index * 12}%` }} />
                <span className="fs-skeleton fs-skeleton-line fs-timeline-skeleton-time" />
              </div>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

export function DsTimeline({
  ariaLabel,
  events,
  loading = false,
  emptyMessage: suppliedEmptyMessage,
}: Readonly<{
  ariaLabel: string;
  events: readonly DsTimelineEvent[];
  loading?: boolean;
  emptyMessage?: string;
}>) {
  const { t } = useI18n();
  const emptyMessage = suppliedEmptyMessage ?? t("react.shared.no_activity_yet", "No activity yet.");
  if (loading) {
    return <DsTimelineLoading ariaLabel={ariaLabel} />;
  }
  if (!events.length) {
    // Empty replaces the rail with a concise message rather than an empty rail.
    return <p className="fs-empty-copy">{emptyMessage}</p>;
  }
  return (
    <ol aria-label={ariaLabel} className="fs-timeline">
      {events.map((event, index) => (
        <li
          aria-current={event.current ? "step" : undefined}
          className="fs-timeline-event"
          key={event.id}
        >
          <span aria-hidden="true" className="fs-timeline-rail">
            <span className={event.current ? "fs-timeline-dot current" : "fs-timeline-dot"} />
            {index < events.length - 1 ? <span className="fs-timeline-line" /> : null}
          </span>
          <div className="fs-timeline-content">
            <strong className="fs-timeline-title" title={event.title}>{event.title}</strong>
            <time className="fs-timeline-time" dateTime={event.dateTime}>{event.time}</time>
          </div>
        </li>
      ))}
    </ol>
  );
}

export function BlankCell() {
  const { t } = useI18n();
  return <span className="fs-blank-cell" title={t("react.shared.no_value", "No value")}>—</span>;
}

export type DsTabItem = { key: string; label: string };

/** Reveal only within this tab strip; never scroll its page or body ancestors. */
function revealTabHorizontally(tab: HTMLElement | null | undefined, scrollport: HTMLElement | null) {
  if (!tab || !scrollport) return;
  const tabRect = tab.getBoundingClientRect();
  const visibleLeft = scrollport.getBoundingClientRect().left + scrollport.clientLeft;
  const visibleRight = visibleLeft + scrollport.clientWidth;
  if (tabRect.left < visibleLeft) scrollport.scrollLeft += tabRect.left - visibleLeft;
  else if (tabRect.right > visibleRight) scrollport.scrollLeft += tabRect.right - visibleRight;
}

/**
 * In-page tabs: a real tablist whose children are tabs, with roving tabindex
 * and arrow-key movement. Use DsTabLinks instead when the tabs are routes.
 */
export function DsTabs({
  ariaLabel,
  items,
  active,
  onSelect,
  idPrefix,
  panelIdFor,
  revealActiveTab = false,
}: Readonly<{
  ariaLabel: string;
  items: readonly DsTabItem[];
  active: string;
  onSelect: (key: string) => void;
  /** Links each tab to the panel it controls. */
  idPrefix: string;
  /** Override the default ids when a consumer already owns fixed panel ids. */
  panelIdFor?: (key: string) => string;
  /** Opt in to keeping the active/focused tab inside its horizontal strip. */
  revealActiveTab?: boolean;
}>) {
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (revealActiveTab) revealTabHorizontally(tabRefs.current[active], scrollRef.current);
  }, [active, items, revealActiveTab]);
  function move(offset: number) {
    const index = items.findIndex((item) => item.key === active);
    const next = items[(index + offset + items.length) % items.length];
    if (next) {
      onSelect(next.key);
      tabRefs.current[next.key]?.focus();
    }
  }
  return (
    // The scroll container, as DsTabLinks already does: a row wider than the
    // surface scrolls rather than squeezing or wrapping its labels.
    <div className="fs-tabs-scroll" ref={scrollRef} onFocusCapture={(event) => {
      if (revealActiveTab && event.target instanceof HTMLButtonElement && event.target.getAttribute("role") === "tab") {
        revealTabHorizontally(event.target, scrollRef.current);
      }
    }}>
      <div aria-label={ariaLabel} className="fs-tabs-list" role="tablist">
        {items.map((item) => {
          const selected = item.key === active;
          return (
            <button
              aria-controls={panelIdFor?.(item.key) ?? `${idPrefix}-panel-${item.key}`}
              aria-selected={selected}
              className="fs-tab"
              id={`${idPrefix}-tab-${item.key}`}
              key={item.key}
              onClick={() => onSelect(item.key)}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
                  event.preventDefault();
                  move(event.key === "ArrowRight" ? 1 : -1);
                }
              }}
              ref={(element) => {
                tabRefs.current[item.key] = element;
              }}
              role="tab"
              tabIndex={selected ? 0 : -1}
              type="button"
            >
              {item.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export type DsTabLink = { label: string; to: string; end?: boolean };

/**
 * The published Tabs visual for sub-navigation between routes. These are links,
 * so the current one is marked with aria-current="page" rather than
 * aria-selected, and the whole set is a nav rather than a tablist.
 */
export function DsTabLinks({
  ariaLabel,
  items,
}: Readonly<{ ariaLabel: string; items: readonly DsTabLink[] }>) {
  return (
    <nav aria-label={ariaLabel} className="fs-tabs-scroll">
      <div className="fs-tabs-list">
        {items.map((item) => (
          <NavLink className="fs-tab" end={item.end} key={item.to} to={item.to}>
            {item.label}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}

export type DsProgressState = "running" | "success" | "attention" | "danger" | "paused";

/**
 * Determinate progress for one running process. Published spec:
 * 03_components/data-display/progress.md. Never render a percentage the payload
 * cannot support - omit `value` and the bar reports an unknown total instead.
 */
export function DsProgress({
  label,
  value,
  max = 100,
  valueText,
  meta,
  state = "running",
}: Readonly<{
  label: string;
  value?: number;
  max?: number;
  valueText?: string;
  meta?: ReactNode;
  state?: DsProgressState;
}>) {
  const labelId = useId();
  const valueId = useId();
  const known = typeof value === "number";
  const percent = known ? Math.max(0, Math.min((value / max) * 100, 100)) : undefined;
  return (
    <div className={`fs-progress${known ? "" : " fs-progress-indeterminate"}`} data-state={state}>
      <div className="fs-progress-header">
        <span className="fs-progress-label" id={labelId}>{label}</span>
        {known ? <span className="fs-progress-value" id={valueId}>{valueText ?? `${Math.round(percent ?? 0)}%`}</span> : null}
      </div>
      <progress
        aria-describedby={known && valueText ? valueId : undefined}
        aria-labelledby={labelId}
        aria-valuetext={valueText}
        className="fs-sr-only"
        max={max}
        value={known ? value : undefined}
      />
      <div aria-hidden="true" className="fs-progress-track">
        <div className="fs-progress-indicator" style={known ? { width: `${percent}%` } : undefined} />
      </div>
      {meta ? <p className="fs-progress-meta">{meta}</p> : null}
    </div>
  );
}

/**
 * Verbatim machine output. Published spec:
 * 03_components/data-display/log-output.md. Text is never reformatted, and a
 * long line wraps rather than truncating.
 */
export function DsLogOutput({
  label,
  children,
  scroll = false,
}: PropsWithChildren<{ label: string; scroll?: boolean }>) {
  const labelId = useId();
  return (
    <section>
      <p className="fs-log-label" id={labelId}>{label}</p>
      <div
        aria-labelledby={labelId}
        className="fs-log-output"
        data-scroll={scroll ? "true" : undefined}
        role="log"
        // A scrollable log must be reachable by keyboard; the rule does not
        // know that role="log" makes this a scroll container, not decoration.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={scroll ? 0 : undefined}
      >
        {children}
      </div>
    </section>
  );
}
