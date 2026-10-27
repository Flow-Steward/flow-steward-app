import { fetchJson } from "@/lib/http/client";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type EventListParams = {
  workflowId: string;
  eventFamily: string;
  eventCategory: string;
  eventType: string;
  status: string;
  fromDate: string;
  toDate: string;
  page: number;
  perPage: number;
};

export type EventFacetOption = {
  value: string;
  label?: string;
  count?: number;
  tone?: string;
};

export type EventListFacets = {
  workflows?: EventFacetOption[];
  event_families?: EventFacetOption[];
  event_categories?: EventFacetOption[];
  event_types?: EventFacetOption[];
  statuses?: EventFacetOption[];
};

export type EventListItem = {
  event_id?: string;
  id?: string;
  event_type?: string;
  event_type_label?: string;
  event_family?: string;
  event_family_label?: string;
  event_category?: string;
  event_category_label?: string;
  severity?: string;
  status?: string;
  status_label?: string;
  status_tone?: string;
  dispatch_status?: string;
  title?: string;
  detail?: string;
  agent?: string;
  step_id?: string | null;
  group_id?: string | null;
  job_id?: number | string;
  project_id?: number | string;
  project_name?: string;
  workflow_id?: string | null;
  workflow_display_name?: string | null;
  workflow_label?: string | null;
  workflow_name?: string | null;
  created_at?: string;
  [key: string]: unknown;
};

export type EventListResponse = {
  events?: EventListItem[];
  total?: number;
  page?: number;
  per_page?: number;
  facets?: EventListFacets;
  [key: string]: unknown;
};

export type EventCommandRecord = Record<string, unknown> & {
  command_id?: string;
  command_type?: string;
  command_type_label?: string;
  command_label?: string;
  label?: string;
  status?: string;
  status_label?: string;
  status_tone?: string;
};

export type EventDispatchRecord = Record<string, unknown> & {
  dispatch_id?: string;
  status?: string;
  status_label?: string;
  status_tone?: string;
  state?: string;
  attempted_at?: string;
  created_at?: string;
};

export type EventDetail = {
  event_id?: string;
  id?: string;
  status?: string;
  status_label?: string;
  status_tone?: string;
  event_type?: string;
  event_type_label?: string;
  event_family?: string;
  event_family_label?: string;
  event_category?: string;
  event_category_label?: string;
  dispatch_status?: string;
  workflow_id?: string | null;
  workflow_display_name?: string | null;
  workflow_label?: string | null;
  workflow_name?: string | null;
  dispatches?: EventDispatchRecord[];
  commands?: EventCommandRecord[];
  linked_commands?: EventCommandRecord[];
  [key: string]: unknown;
};

export async function fetchEventsList(params: EventListParams) {
  const query = new URLSearchParams();
  if (params.eventType.trim()) {
    query.set("event_type", params.eventType.trim());
  }
  if (params.workflowId.trim()) {
    query.set("workflow_id", params.workflowId.trim());
  }
  if (params.eventFamily.trim()) {
    query.set("event_family", params.eventFamily.trim());
  }
  if (params.eventCategory.trim()) {
    query.set("event_category", params.eventCategory.trim());
  }
  if (params.status.trim()) {
    query.set("status", params.status.trim());
  }
  if (params.fromDate.trim()) {
    query.set("from_date", params.fromDate.trim());
  }
  if (params.toDate.trim()) {
    query.set("to_date", params.toDate.trim());
  }
  query.set("page", stringFromUnknownValue(params.page));
  query.set("per_page", stringFromUnknownValue(params.perPage));

  const payload = await fetchJson<EventListResponse | EventListItem[]>(`/api/events?${query.toString()}`);
  if (Array.isArray(payload)) {
    return { events: payload, total: payload.length, page: params.page, per_page: params.perPage };
  }
  return {
    events: Array.isArray(payload.events) ? payload.events : [],
    total: Number(payload.total || 0),
    page: Number(payload.page || params.page),
    per_page: Number(payload.per_page || params.perPage),
    facets:
      payload.facets && typeof payload.facets === "object" && !Array.isArray(payload.facets)
        ? payload.facets
        : {},
  };
}

export async function fetchEventDetail(eventRef: string) {
  return fetchJson<EventDetail>(`/api/events/${encodeURIComponent(eventRef)}`);
}

export async function redriveEvent(eventRef: string) {
  return fetchJson<Record<string, unknown>>(`/api/events/${encodeURIComponent(eventRef)}/redrive`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
