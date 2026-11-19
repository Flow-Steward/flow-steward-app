import type { FormFieldView } from "@/components/workflow-inputs/form-field";

/** What the operator and viewer pages read from the server (ADR 0075). */

export type OperatorIssue = { code: string; message: string; path?: string; severity?: string };

/** One menu item: a published workflow in one project the caller can see. */
export type OperatorSectionItem = {
  project_id: string;
  project_name: string;
  workspace_id?: string | null;
  workflow_id: string;
  workflow_name: string;
  title: string;
  runnable: boolean;
  blocked_reasons: OperatorIssue[];
};

export type OperatorListColumn = {
  id: string;
  source: "job" | "input" | "output";
  path: string;
  label?: string;
  format?: string;
};

export type OperatorFilterType = "search" | "number_range" | "date_range" | "boolean" | "choice" | "choice_multi";

export type OperatorFilterView = {
  id: string;
  source: "job" | "input" | "output";
  path: string;
  type: OperatorFilterType;
  label?: string;
  options?: string[];
};

export type OperatorSubmission = { mode: "foreach" | "bulk"; target_input?: string };

/** How a finished run shows one output (`settings.operator.result.fields`). */
export type OperatorResultFieldConfig = { label?: string; display?: string; hidden?: boolean; decimals?: number | null };

export type OperatorSettings = {
  published?: boolean;
  menu_title?: string;
  submission?: OperatorSubmission;
  result?: { order?: string[]; fields?: Record<string, OperatorResultFieldConfig> };
  list?: { columns?: OperatorListColumn[]; filters?: Array<Omit<OperatorFilterView, "options">> };
  [key: string]: unknown;
};

export type OperatorRuntimeView = {
  workflow_id: string;
  workflow_name: string;
  workflow_revision: number;
  title: string;
  published: boolean;
  runnable: boolean;
  effects_acknowledged: boolean;
  settings: OperatorSettings | null;
  form_fields: FormFieldView[];
  filters: OperatorFilterView[];
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  issues: OperatorIssue[];
};

export type OperatorSectionDetail = OperatorSectionItem & {
  workflow_revision: number;
  effects_acknowledged: boolean;
  operator: OperatorRuntimeView;
};

/** A stored file value as a column shows it. */
export type OperatorFileValue = {
  filename?: string;
  content_type?: string;
  size_bytes?: number;
  artifact_id?: string;
};

/** What a job of a section submitted and returned (`job_values` of the job's review read). */
export type OperatorJobValues = {
  status?: string;
  submitted_by?: Record<string, unknown> | null;
  inputs?: Record<string, unknown>;
  /** Only once the run succeeded. */
  outputs?: Record<string, unknown> | null;
};

/** One correction of a decided review: what the application found and what the person made of it. */
export type OperatorReviewChange = { field: string; before?: unknown; after?: unknown };

/**
 * The last decided typed review of a job, here or on the Invoke child that held
 * it. `access: "none"`: decided by people with access to that job; nothing else.
 */
export type OperatorReviewDecision = {
  access: "read" | "none";
  job_id?: string | null;
  approval_id?: string | null;
  approved?: boolean;
  actor?: string | null;
  actor_type?: string | null;
  timestamp?: string | null;
  comment?: string | null;
  /** Enough of each field to show its value by type: a list of objects as a table. */
  fields?: Array<{ name: string; label?: string; type?: string; format?: string | null; items?: Record<string, unknown> | null; properties?: unknown }>;
  values?: Record<string, unknown>;
  changes?: OperatorReviewChange[];
};
