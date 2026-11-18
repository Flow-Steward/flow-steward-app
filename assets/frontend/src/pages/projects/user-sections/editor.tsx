import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";

import { DsAlert, DsCard, DsCheckbox, DsSelect } from "@/components/shared/design-system";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { fetchWorkflowContract } from "@/pages/projects/detail/api";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";

import { fetchSectionProject, saveProjectSections } from "./api";
import {
  buildInputFields,
  fieldKind,
  inputPaths,
  isRenderable,
  outputNames,
  resolveSchemaPath,
  WIDGETS_BY_KIND,
} from "./schema";
import type {
  JsonSchema,
  ProjectUserSection,
  ProjectUserSectionView,
  SectionColumnConfig,
  SectionFieldConfig,
} from "./types";

const JOB_PATHS = ["created_at", "status", "submitted_by", "started_at", "finished_at", "duration_ms", "job_id"];
const FORMATS = ["auto", "text", "number", "currency", "date", "datetime", "status", "file", "count", "list"];
const FOREACH_KINDS = new Set(["file", "string_list", "enum_list", "number_list", "object_list"]);

function slug(text: string): string {
  const base = text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return base || `section-${Date.now().toString(36)}`;
}

function stripRuntime(view: ProjectUserSectionView): ProjectUserSection {
  const { runtime: _runtime, ...section } = view;
  return section;
}

function moved<T>(items: T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (index < 0 || target < 0 || target >= items.length) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

function FieldsEditor({ schema, section, onChange }: Readonly<{
  schema: JsonSchema;
  section: ProjectUserSection;
  onChange: (fields: SectionFieldConfig[]) => void;
}>) {
  const { t } = useI18n();
  const configured = new Map(section.fields.map((field) => [field.path, field]));
  const names = Object.keys(schema.properties ?? {});
  const ordered = [
    ...section.fields.map((field) => field.path).filter((path) => names.includes(path)),
    ...names.filter((name) => !configured.has(name)),
  ];
  const required = new Set(schema.required ?? []);
  const update = (path: string, patch: Partial<SectionFieldConfig> | null) => {
    const current = ordered
      .map((name) => configured.get(name))
      .filter((field): field is SectionFieldConfig => Boolean(field));
    if (patch === null) return onChange(current.filter((field) => field.path !== path));
    const exists = current.some((field) => field.path === path);
    const next = exists
      ? current.map((field) => (field.path === path ? { ...field, ...patch } : field))
      : [...current, { path, visible: true, ...patch }];
    onChange(next.map((field, order) => ({ ...field, order })));
  };
  return (
    <div className="space-y-2">
      {ordered.map((name) => {
        const node = resolveSchemaPath(schema, name);
        const kind = fieldKind(node);
        const field = configured.get(name);
        const included = Boolean(field);
        return (
          <div className="grid items-center gap-2 md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]" key={name}>
            <DsCheckbox
              checked={included}
              disabled={!isRenderable(kind)}
              onChange={(checked) => update(name, checked ? {} : null)}
            >
              {name}
              {required.has(name) ? " *" : ""}
              {!isRenderable(kind) ? ` — ${t("react.user_sections.unsupported_field", "not supported by the form")}` : null}
            </DsCheckbox>
            <input
              aria-label={t("react.user_sections.field_label", "Label of {field}", { field: name })}
              className="fs-input"
              disabled={!included}
              onChange={(event) => update(name, { label: event.target.value })}
              placeholder={node?.title || name}
              value={field?.label ?? ""}
            />
            {isRenderable(kind) ? (
              <DsSelect
                ariaLabel={t("react.user_sections.field_widget", "Widget of {field}", { field: name })}
                disabled={!included}
                onChange={(widget) => update(name, { widget })}
                options={WIDGETS_BY_KIND[kind].map((widget) => ({ value: widget, label: widget }))}
                value={field?.widget || WIDGETS_BY_KIND[kind][0]}
              />
            ) : (
              <span />
            )}
            <span className="flex gap-1">
              <button
                aria-label={t("react.user_sections.move_up", "Move up")}
                className="fs-button sm"
                disabled={!included}
                onClick={() => onChange(moved(section.fields, section.fields.findIndex((item) => item.path === name), -1))}
                type="button"
              >
                ↑
              </button>
              <button
                aria-label={t("react.user_sections.move_down", "Move down")}
                className="fs-button sm"
                disabled={!included}
                onClick={() => onChange(moved(section.fields, section.fields.findIndex((item) => item.path === name), 1))}
                type="button"
              >
                ↓
              </button>
            </span>
          </div>
        );
      })}
      <p className="text-sm text-muted">
        {t(
          "react.user_sections.fields_hint",
          "Types, required inputs and options come from the workflow contract. To describe a list or an object, edit the contract.",
        )}
      </p>
    </div>
  );
}

function ColumnsEditor({ input, output, columns, onChange }: Readonly<{
  input: JsonSchema;
  output: JsonSchema;
  columns: SectionColumnConfig[];
  onChange: (columns: SectionColumnConfig[]) => void;
}>) {
  const { t } = useI18n();
  const pathsFor = (source: SectionColumnConfig["source"]) =>
    source === "job" ? JOB_PATHS : source === "input" ? inputPaths(input) : outputNames(output);
  const update = (index: number, patch: Partial<SectionColumnConfig>) =>
    onChange(columns.map((column, at) => (at === index ? { ...column, ...patch } : column)));
  return (
    <div className="space-y-2">
      {columns.map((column, index) => (
        <div className="grid items-center gap-2 md:grid-cols-[8rem_minmax(0,1fr)_minmax(0,1fr)_8rem_auto]" key={column.id}>
          <DsSelect
            ariaLabel={t("react.user_sections.column_source", "Source")}
            onChange={(source) => update(index, { source: source as SectionColumnConfig["source"], path: pathsFor(source as SectionColumnConfig["source"])[0] ?? "" })}
            options={[
              { value: "job", label: t("react.user_sections.source_job", "Run") },
              { value: "input", label: t("react.user_sections.source_input", "Input") },
              { value: "output", label: t("react.user_sections.source_output", "Result") },
            ]}
            value={column.source}
          />
          <DsSelect
            ariaLabel={t("react.user_sections.column_value", "Value")}
            onChange={(path) => update(index, { path })}
            options={pathsFor(column.source).map((path) => ({ value: path, label: path }))}
            value={column.path}
          />
          <input
            aria-label={t("react.user_sections.column_label", "Column title")}
            className="fs-input"
            onChange={(event) => update(index, { label: event.target.value })}
            placeholder={column.path}
            value={column.label ?? ""}
          />
          <DsSelect
            ariaLabel={t("react.user_sections.column_format", "Format")}
            onChange={(format) => update(index, { format })}
            options={FORMATS.map((format) => ({ value: format, label: format }))}
            value={column.format || "auto"}
          />
          <span className="flex gap-1">
            <button aria-label={t("react.user_sections.move_up", "Move up")} className="fs-button sm" onClick={() => onChange(moved(columns, index, -1))} type="button">↑</button>
            <button aria-label={t("react.user_sections.move_down", "Move down")} className="fs-button sm" onClick={() => onChange(moved(columns, index, 1))} type="button">↓</button>
            <button className="fs-button sm" onClick={() => onChange(columns.filter((_, at) => at !== index))} type="button">
              {t("react.user_sections.remove", "Remove")}
            </button>
          </span>
        </div>
      ))}
      <button
        className="fs-button"
        onClick={() =>
          onChange([...columns, { id: `col-${Date.now().toString(36)}`, source: "job", path: "status", format: "auto", order: columns.length }])
        }
        type="button"
      >
        {t("react.user_sections.add_column", "Add column")}
      </button>
    </div>
  );
}

function SectionSettings({ section, workflows, onChange }: Readonly<{
  section: ProjectUserSection;
  workflows: string[];
  onChange: (section: ProjectUserSection) => void;
}>) {
  const { t } = useI18n();
  const contract = useQuery({
    queryKey: ["workflows", "contract", section.workflow_id],
    queryFn: () => fetchWorkflowContract(section.workflow_id),
    enabled: Boolean(section.workflow_id),
  });
  const input = (contract.data?.interface?.input_schema ?? {}) as JsonSchema;
  const output = (contract.data?.interface?.output_schema ?? {}) as JsonSchema;
  const preview = useMemo(() => buildInputFields(input, section), [input, section]);
  const targets = Object.keys(input.properties ?? {}).filter((name) => FOREACH_KINDS.has(fieldKind(resolveSchemaPath(input, name))));
  const set = (patch: Partial<ProjectUserSection>) => onChange({ ...section, ...patch });
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 md:grid-cols-2">
        <label className="fs-field">
          <span className="fs-field-label">{t("react.user_sections.title", "Title")}</span>
          <input className="fs-input" onChange={(event) => set({ title: event.target.value })} value={section.title} />
        </label>
        <div className="fs-field">
          <span className="fs-field-label">{t("react.user_sections.workflow", "Workflow")}</span>
          <DsSelect
            ariaLabel={t("react.user_sections.workflow", "Workflow")}
            onChange={(workflowId) => set({ workflow_id: workflowId, fields: [], columns: [], submission: { mode: "bulk" } })}
            options={workflows.map((id) => ({ value: id, label: id }))}
            value={section.workflow_id}
          />
        </div>
        <label className="fs-field md:col-span-2">
          <span className="fs-field-label">{t("react.user_sections.description", "Description")}</span>
          <input className="fs-input" onChange={(event) => set({ description: event.target.value })} value={section.description ?? ""} />
        </label>
        <label className="fs-field">
          <span className="fs-field-label">{t("react.user_sections.menu_order", "Position in the menu")}</span>
          <input className="fs-input" onChange={(event) => set({ order: Number(event.target.value) || 0 })} type="number" value={section.order ?? 0} />
        </label>
        <div className="fs-field">
          <DsCheckbox checked={section.visible !== false} onChange={(visible) => set({ visible })}>
            {t("react.user_sections.visible", "Shown to operators and viewers")}
          </DsCheckbox>
        </div>
      </div>
      {contract.isError ? <DsAlert title={t("react.user_sections.contract_failed", "The workflow contract could not be loaded.")} tone="danger" /> : null}
      <DsCard title={t("react.user_sections.form_fields", "Form")}>
        <FieldsEditor onChange={(fields) => set({ fields })} schema={input} section={section} />
      </DsCard>
      <DsCard title={t("react.user_sections.columns", "History columns")}>
        <ColumnsEditor columns={section.columns} input={input} onChange={(columns) => set({ columns })} output={output} />
      </DsCard>
      <DsCard title={t("react.user_sections.sending", "Sending")}>
        <div className="grid gap-3 md:grid-cols-2">
          <DsSelect
            ariaLabel={t("react.user_sections.mode", "Mode")}
            onChange={(mode) => set({ submission: { mode: mode as "foreach" | "bulk", target_input: mode === "foreach" ? targets[0] ?? "" : "" } })}
            options={[
              { value: "foreach", label: t("react.user_sections.mode_foreach", "For each: one run per file or item") },
              { value: "bulk", label: t("react.user_sections.mode_bulk", "Bulk: one run with everything") },
            ]}
            value={section.submission.mode}
          />
          {section.submission.mode === "foreach" ? (
            <DsSelect
              ariaLabel={t("react.user_sections.foreach_target", "Split by")}
              onChange={(target_input) => set({ submission: { mode: "foreach", target_input } })}
              options={targets.map((name) => ({ value: name, label: name }))}
              value={section.submission.target_input ?? ""}
            />
          ) : null}
        </div>
      </DsCard>
      <DsCard title={t("react.user_sections.preview", "Preview")}>
        <p className="text-sm text-muted">{t("react.user_sections.preview_hint", "This is what operators see. The preview never starts the workflow.")}</p>
        <div className="grid gap-4 md:grid-cols-2">
          <WorkflowInputFields disabled fields={preview} onChange={() => undefined} values={{}} />
        </div>
      </DsCard>
      <DsCheckbox checked={section.effects_acknowledged === true} onChange={(effects_acknowledged) => set({ effects_acknowledged })}>
        {t(
          "react.user_sections.acknowledge",
          "I confirm that sending this form runs the workflow with real effects (for example, writing to connected systems).",
        )}
      </DsCheckbox>
    </div>
  );
}

/** The admin's editor of a project's user sections (ADR 0075). */
export function ProjectUserSectionsEditor({ projectId }: Readonly<{ projectId: string }>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const sections = useQuery({ queryKey: queryKeys.projectSections(projectId), queryFn: () => fetchSectionProject(projectId) });
  const bindings = useQuery({
    queryKey: ["projects", "workflow-bindings", projectId],
    queryFn: () => fetchProjectWorkflowBindings(projectId),
  });
  const [draft, setDraft] = useState<ProjectUserSection[] | null>(null);
  const [selected, setSelected] = useState(0);
  const [message, setMessage] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    // A new project resets the draft to its own saved sections.
    setDraft(null);
    setSelected(0);
  }, [projectId]);
  const saved = useMemo(() => (sections.data?.user_sections ?? []).map(stripRuntime), [sections.data]);
  const current = draft ?? saved;
  const workflows = bindings.data?.workflow_ids ?? [];
  const issues = sections.data?.user_sections.find((section) => section.id === current[selected]?.id)?.runtime.issues ?? [];

  const change = (index: number, section: ProjectUserSection) => setDraft(current.map((item, at) => (at === index ? section : item)));
  const add = () => {
    const title = t("react.user_sections.new_section", "New section");
    const next: ProjectUserSection = {
      id: slug(`${title}-${current.length + 1}`),
      title,
      workflow_id: workflows[0] ?? "",
      visible: false,
      order: current.length,
      fields: [],
      columns: [],
      submission: { mode: "bulk" },
      effects_acknowledged: false,
    };
    setDraft([...current, next]);
    setSelected(current.length);
  };
  const save = async () => {
    setSaving(true);
    setMessage(null);
    try {
      await saveProjectSections(projectId, current);
      setDraft(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.projectSections(projectId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.sectionNavigation });
      setMessage({ tone: "success", text: t("react.user_sections.saved", "Sections saved.") });
    } catch (error) {
      // The draft stays so the admin can fix what the server refused.
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : t("react.user_sections.save_failed", "Sections could not be saved.") });
    } finally {
      setSaving(false);
    }
  };

  if (sections.isLoading) return <p>{t("react.user_sections.loading_editor", "Opening the sections")}</p>;
  const section = current[selected];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        {current.map((item, index) => (
          <button className={`fs-button sm${index === selected ? " primary" : ""}`} key={`${item.id}-${index}`} onClick={() => setSelected(index)} type="button">
            {item.title || item.id}
          </button>
        ))}
        <button className="fs-button sm" disabled={!workflows.length} onClick={add} type="button">
          {t("react.user_sections.add_section", "Add section")}
        </button>
      </div>
      {!workflows.length ? <DsAlert title={t("react.user_sections.no_workflows", "Link a workflow to this project first.")} tone="neutral" /> : null}
      {section ? (
        <>
          <SectionSettings onChange={(next) => change(selected, next)} section={section} workflows={workflows} />
          {issues.length && !draft ? (
            <DsAlert title={t("react.user_sections.saved_issues", "This section needs attention")} tone="warning">
              {issues.map((issue) => <p key={`${issue.path}-${issue.code}`}>{issue.message}</p>)}
            </DsAlert>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <button
              className="fs-button"
              onClick={() => {
                setDraft(current.filter((_, at) => at !== selected));
                setSelected(0);
              }}
              type="button"
            >
              {t("react.user_sections.delete_section", "Delete section")}
            </button>
            {!draft && saved.some((item) => item.id === section.id) ? (
              <Link className="fs-button" to={`/projects/${encodeURIComponent(projectId)}/sections/${encodeURIComponent(section.id)}`}>
                {t("react.user_sections.open_section", "Open section")}
              </Link>
            ) : null}
          </div>
        </>
      ) : (
        <p>{t("react.user_sections.empty", "No sections yet. Add one to give operators a form and a history for a workflow.")}</p>
      )}
      {message ? <DsAlert title={message.text} tone={message.tone} /> : null}
      <div>
        <button className="fs-button primary" disabled={saving || draft === null} onClick={() => void save()} type="button">
          {t("react.user_sections.save", "Save sections")}
        </button>
      </div>
    </div>
  );
}
