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

export type OperatorSettings = {
  published?: boolean;
  menu_title?: string;
  submission?: OperatorSubmission;
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
