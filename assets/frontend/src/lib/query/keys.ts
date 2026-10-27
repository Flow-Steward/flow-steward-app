import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
export const queryKeys = {
  authSession: ["auth", "session"] as const,
  appBootstrap: ["app", "bootstrap"] as const,
  dashboardSummary: (range: string) => ["dashboard", "summary", range] as const,
  jobsList: (params: { page: number; perPage: number; status: string; projectId: number | null }) =>
    ["jobs", "list", params] as const,
  jobDetail: (jobId: string | number) => ["jobs", "detail", stringFromUnknownValue(jobId)] as const,
  jobExecutionSteps: (jobId: string | number, initialCursor: string) =>
    ["jobs", "execution-steps", stringFromUnknownValue(jobId), initialCursor] as const,
  projectsList: (params: { page: number; perPage: number; search: string }) =>
    ["projects", "list", params] as const,
  projectDetail: (projectId: string | number) => ["projects", "detail", stringFromUnknownValue(projectId)] as const,
  eventsList: (params: {
    workflowId: string;
    eventFamily: string;
    eventCategory: string;
    eventType: string;
    status: string;
    fromDate: string;
    toDate: string;
    page: number;
    perPage: number;
  }) => ["events", "list", params] as const,
  eventDetail: (eventRef: string) => ["events", "detail", eventRef] as const,
  monitoringSummary: ["monitoring", "summary"] as const,
  monitoringStorage: ["monitoring", "storage"] as const,
  processes: ["processes"] as const,
  trash: ["trash"] as const,
  workflowsList: (params: { search: string; enabledOnly: boolean }) =>
    ["workflows", "list", params] as const,
  workflowDetail: (workflowId: string | number) => ["workflows", "detail", stringFromUnknownValue(workflowId)] as const,
  workflowDag: (workflowId: string | number) => ["workflows", "dag", stringFromUnknownValue(workflowId)] as const,
  workflowCanvasGraph: (workflowId: string | number) =>
    ["workflows", "canvas-graph", stringFromUnknownValue(workflowId)] as const,
  workflowValidation: (workflowId: string | number) =>
    ["workflows", "validation", stringFromUnknownValue(workflowId)] as const,
  workflowStats: (workflowId: string | number) =>
    ["workflows", "stats", stringFromUnknownValue(workflowId)] as const,
};
