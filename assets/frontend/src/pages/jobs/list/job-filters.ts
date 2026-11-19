import type { SetURLSearchParams } from "react-router";

import { expandPeriod, type PeriodValue } from "@/lib/time/period";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { jobStatusParams } from "@/pages/jobs/list/api";
import { jobStatusesOfGroups, jobStatusGroupsFromValues } from "@/pages/jobs/status-groups";

/**
 * The operator queue (ADR 0075): no stored status and no group. The server
 * matches a job waiting for approval itself or anywhere down its Invoke chain.
 */
export const AWAITING_REVIEW_STATUS = "awaiting_review";

/** Statuses of a row the server may have matched as "awaiting review". */
export const AWAITING_REVIEW_ROW_STATUSES: ReadonlySet<string> = new Set(["awaiting_approval", "awaiting_child"]);

function asksForReview(values: readonly string[]): boolean {
  return values.flatMap((value) => value.split(",")).some((value) => value.trim().toLowerCase() === AWAITING_REVIEW_STATUS);
}

/**
 * The status filter is a comma list of status group keys, "awaiting review"
 * first when chosen; the URL repeats `status` once per value. A stored status
 * from an older link means its group.
 */
export function normalizeStatusList(values: readonly string[]): string {
  const groups: string[] = jobStatusGroupsFromValues(values);
  return (asksForReview(values) ? [AWAITING_REVIEW_STATUS, ...groups] : groups).join(",");
}

/** The `status` values a filter sends: the stored statuses of its groups, and "awaiting review". */
export function jobStatusRequest(statusFilter: string): string[] {
  const values = jobStatusParams(statusFilter);
  return [...(asksForReview(values) ? [AWAITING_REVIEW_STATUS] : []), ...jobStatusesOfGroups(values)];
}

/** `hide_acknowledged=1` (or `true`) in the URL turns the toggle on. */
export function hideAcknowledgedFromUrl(value: string | null): boolean {
  return ["1", "true"].includes(String(value || "").trim().toLowerCase());
}

/**
 * The Jobs list filters read and write the URL (remembered per list, ADR 0069).
 * Dashboard metric cards and workflow links deep-link here with a status or a
 * workflow; the URL owns the current choice, including Back and Clear.
 */
export function jobFiltersFromUrl(searchParams: URLSearchParams, setSearchParams: SetURLSearchParams) {
  function setJobFilter(key: string, value: string, multiple = false) {
    setSearchParams((latest) => {
      const next = new URLSearchParams(latest);
      next.delete(key);
      if (multiple) jobStatusParams(value).forEach((status) => next.append(key, status));
      else if (value) next.set(key, value);
      return next;
    }, { replace: true });
  }
  const urlWorkflowFilter = firstStringFromUnknownValue([searchParams.get("workflow_id"), ""]).trim();
  return {
    urlWorkflowFilter,
    search: searchParams.get("search") || "",
    setSearch: (value: string) => setJobFilter("search", value),
    projectFilter: searchParams.get("project_id") || "",
    setProjectFilter: (value: string) => setJobFilter("project_id", value),
    statusFilter: normalizeStatusList(searchParams.getAll("status")),
    setStatusFilter: (value: string) => setJobFilter("status", value, true),
    hideAcknowledged: hideAcknowledgedFromUrl(searchParams.get("hide_acknowledged")),
    setHideAcknowledged: (value: boolean) => setJobFilter("hide_acknowledged", value ? "1" : ""),
    workflowFilter: urlWorkflowFilter,
    setWorkflowFilter: (value: string) => setJobFilter("workflow_id", value),
  };
}

/** The list's period in the URL: a preset, a custom pair of days, or all runs. */
export function periodFromUrl(params: URLSearchParams): PeriodValue {
  const preset = params.get("period");
  if (preset === "today" || preset === "last7" || preset === "last30") return { kind: "preset", preset };
  const from = params.get("from_date") || "";
  const to = params.get("to_date") || "";
  const validDay = (day: string) => /^\d{4}-\d{2}-\d{2}$/.test(day)
    && !Number.isNaN(Date.parse(day)) && new Date(day).toISOString().slice(0, 10) === day;
  return validDay(from) && validDay(to) && from <= to ? { kind: "custom", from, to } : { kind: "all" };
}

/**
 * What the list asks the server for a URL selection, as the list itself does:
 * the status groups as stored statuses, hidden acknowledged runs, the search and
 * the period as whole local days (`today` in the person's time zone).
 */
export function jobsListRequestFromUrl(params: URLSearchParams, today: string) {
  const { fromDate, toDate } = expandPeriod(periodFromUrl(params), today);
  return {
    status: jobStatusRequest(normalizeStatusList(params.getAll("status"))).join(","),
    hideAcknowledged: hideAcknowledgedFromUrl(params.get("hide_acknowledged")),
    search: (params.get("search") || "").trim(),
    fromDate,
    toDate,
  };
}
