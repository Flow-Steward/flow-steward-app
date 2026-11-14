import type { SetURLSearchParams } from "react-router";

import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { jobStatusParams } from "@/pages/jobs/list/api";
import { jobStatusGroupsFromValues } from "@/pages/jobs/status-groups";

/**
 * The status filter is a comma list of status group keys; the URL repeats
 * `status` once per group. A stored status from an older link means its group.
 */
export function normalizeStatusList(values: readonly string[]): string {
  return jobStatusGroupsFromValues(values).join(",");
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
