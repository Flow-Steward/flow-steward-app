/** Project user sections (ADR 0075): what the server stores and serves. */

export type SectionFieldConfig = {
  path: string;
  label?: string;
  help_text?: string;
  widget?: string;
  order?: number;
  visible?: boolean;
};

export type SectionColumnConfig = {
  id: string;
  source: "job" | "input" | "output";
  path: string;
  label?: string;
  format?: string;
  order?: number;
};

export type SectionSubmission = {
  mode: "foreach" | "bulk";
  target_input?: string;
};

export type ProjectUserSection = {
  id: string;
  title: string;
  description?: string;
  icon?: string;
  workflow_id: string;
  visible?: boolean;
  order?: number;
  fields: SectionFieldConfig[];
  columns: SectionColumnConfig[];
  submission: SectionSubmission;
  effects_acknowledged?: boolean;
};

export type SectionIssue = { code: string; message: string; path: string; severity?: string };

export type JsonSchema = {
  type?: unknown;
  format?: string;
  enum?: unknown[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  items?: JsonSchema;
  required?: string[];
  minItems?: number;
  "x-fs-type"?: string;
  writeOnly?: boolean;
  [key: string]: unknown;
};

export type SectionRuntime = {
  workflow_name: string;
  workflow_revision: number;
  input_schema: JsonSchema;
  output_schema: JsonSchema;
  runnable: boolean;
  issues: SectionIssue[];
};

export type ProjectUserSectionView = ProjectUserSection & { runtime: SectionRuntime };

/** Navigation entry from the projects list. */
export type ProjectUserSectionBrief = {
  id: string;
  title: string;
  icon?: string;
  workflow_id: string;
  order?: number;
};

export type SectionJobRow = {
  job_id: string;
  status?: string;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  last_error?: string | null;
  task_source?: string | null;
  section_values?: Record<string, unknown> | null;
  submitted_by?: { id?: string; kind?: string } | null;
};
