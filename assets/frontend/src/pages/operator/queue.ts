/**
 * The operator queue (ADR 0075): a job page opened from a section's list keeps
 * that list in its address (the list's own filter keys, its page and page size),
 * so it can step to the previous and next job of the same list and lead back to it.
 */
import type { JobsListFilters } from "@/pages/jobs/list/api";
import { jobsListRequestFromUrl } from "@/pages/jobs/list/job-filters";

import { operatorFilterKey, operatorFiltersParam } from "./section-filters";
import type { OperatorFilterView } from "./types";

/** The list's own URL keys; anything else in the address is not list context. */
const LIST_KEYS = ["search", "status", "hide_acknowledged", "period", "from_date", "to_date", "page", "per_page"];

function isListKey(key: string): boolean {
  return LIST_KEYS.includes(key) || key.startsWith("f.");
}

/** Only the list context of an address, in its order. */
export function listContext(params: URLSearchParams): URLSearchParams {
  return new URLSearchParams(Array.from(params).filter(([key]) => isListKey(key)));
}

function withQuery(path: string, params: URLSearchParams): string {
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

function sectionPath(projectId: string, workflowId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/work`;
}

/** A job of a section, opened with the list it belongs to. */
export function operatorJobPath(projectId: string, workflowId: string, jobId: string, params: URLSearchParams): string {
  return withQuery(`${sectionPath(projectId, workflowId)}/jobs/${encodeURIComponent(jobId)}`, listContext(params));
}

/** The section's list with the same filters, page and page size. */
export function sectionListPath(projectId: string, workflowId: string, params: URLSearchParams): string {
  return withQuery(sectionPath(projectId, workflowId), listContext(params));
}

/** The list context of a row: the list's URL filters with its current page. */
export function rowContext(params: URLSearchParams, page: number, perPage: number): URLSearchParams {
  const context = listContext(params);
  context.delete("page");
  context.delete("per_page");
  if (page > 1) context.set("page", String(page));
  if (perPage !== 25) context.set("per_page", String(perPage));
  return context;
}

/**
 * The request of the section's list for this address, without its page: the
 * same filters, so the queue's position and neighbours are those of the list.
 * Without list context in the address it is the section's default list.
 */
export function queueListFilters(
  projectId: string,
  workflowId: string,
  filters: readonly OperatorFilterView[],
  params: URLSearchParams,
  today: string,
): Omit<JobsListFilters, "page" | "perPage"> {
  const request = jobsListRequestFromUrl(params, today);
  const values = Object.fromEntries(filters.map((filter) => [operatorFilterKey(filter), params.get(operatorFilterKey(filter)) ?? ""]));
  return {
    ...request,
    projectId,
    workflowId,
    operatorView: true,
    operatorFilters: operatorFiltersParam(filters, values),
  };
}

/** J/K and shortcuts are for the page, not for someone typing in a field. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return Boolean(target.closest("[role='combobox'], [role='listbox'], [contenteditable='true']"));
  const type = (target as HTMLInputElement).type;
  return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(type);
}
