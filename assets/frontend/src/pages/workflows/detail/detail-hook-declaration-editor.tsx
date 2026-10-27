import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { DsCheckbox, DsSelect, OptionalLabel, RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import type { WorkflowHookContract } from "@/pages/workflows/shared/hook-api";
import { numericDraftFromInput, numericInputValue } from "./detail-step-numeric-fields";

type ObjectValue = Record<string, unknown>;
type ValidationChange = (path: string, error: string) => void;
type Issue = { path?: string; message?: string };
type VariableInput = (props: { ariaLabel: string; ariaInvalid?: boolean; ariaDescribedBy?: string; ariaRequired?: boolean; className: string; panelId: string; placeholder: string; value: string; onChange: (value: string) => void }) => ReactNode;
type Props = {
  declaration: WorkflowHookContract;
  identityLocked: boolean;
  readOnly?: boolean;
  issues?: readonly Issue[];
  renderVariableInput: VariableInput;
  onChange: (next: WorkflowHookContract) => void;
  onValidationChange?: ValidationChange;
};

function record(value: unknown): ObjectValue {
  return value && typeof value === "object" && !Array.isArray(value) ? value as ObjectValue : {};
}
function json(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "";
}
function fieldFeedback(...messages: string[]): string {
  return [...new Set(messages.filter(Boolean))].join(" ");
}
type DeclarationRowIds = { ids: Map<string, string>; nextId: number };

/** Canonical additions get fresh IDs; renames transfer an ID in their event handler. */
function reconcileRowIds(current: DeclarationRowIds, keys: readonly string[], prefix: string): DeclarationRowIds {
  if (current.ids.size === keys.length && keys.every((key) => current.ids.has(key))) return current;
  const ids = new Map<string, string>();
  let nextId = current.nextId;
  for (const key of keys) ids.set(key, current.ids.get(key) ?? prefix + "-row-" + (++nextId));
  return { ids, nextId };
}

function useFieldError(path: string, error: string, notify?: ValidationChange) {
  useEffect(() => {
    notify?.(path, error);
    return () => notify?.(path, "");
  }, [error, notify, path]);
}

/** Invalid JSON remains a local buffer; only a parsed, accepted value is emitted. */
function JsonDraft({ value, label, path, onValid, onValidationChange, onErrorChange, objectOnly = false, allowBoolean = false, issue = "" }: Readonly<{
  value: unknown; label: string; path: string; onValid: (value: unknown) => void;
  onValidationChange?: ValidationChange; onErrorChange?: (invalid: boolean) => void; objectOnly?: boolean; allowBoolean?: boolean; issue?: string;
}>) {
  const { t } = useI18n();
  const serialized = json(value);
  const [draft, setDraft] = useState({ source: serialized, raw: serialized, error: serialized ? "" : "json" });
  if (draft.source !== serialized) setDraft({ source: serialized, raw: serialized, error: serialized ? "" : "json" });
  const raw = draft.source === serialized ? draft.raw : serialized;
  const errorKind = draft.source === serialized ? draft.error : serialized ? "" : "json";
  const error = errorKind === "object" ? t("react.workflows.hook_schema_object_required", "Enter a JSON Schema object.")
    : errorKind ? t("react.workflows.hook_valid_json_required", "Enter valid JSON.") : "";
  const errorId = useId();
  useFieldError(path, error, onValidationChange);
  useEffect(() => { onErrorChange?.(Boolean(error)); }, [error, onErrorChange]);
  const feedback = fieldFeedback(error, issue);
  return <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
    <span>{label}</span>
    <textarea aria-label={label} className="fs-input min-h-24 font-mono" value={raw}
      aria-invalid={Boolean(feedback) || undefined} aria-describedby={feedback ? errorId : undefined}
      onChange={(event) => {
        const next = event.target.value;
        try {
          const parsed: unknown = JSON.parse(next);
          if (objectOnly && !(allowBoolean && typeof parsed === "boolean") && (!parsed || typeof parsed !== "object" || Array.isArray(parsed))) {
            setDraft({ source: serialized, raw: next, error: "object" });
            return;
          }
          setDraft({ source: json(parsed), raw: next, error: "" });
          onValid(parsed);
        } catch {
          setDraft({ source: serialized, raw: next, error: "json" });
        }
      }} />
    {feedback ? <span className="text-[var(--fs-status-danger)]" id={errorId} role="alert">{feedback}</span> : null}
  </label>;
}

const BASIC_TYPES: readonly string[] = ["string", "number", "integer", "boolean", "object", "array"];
function simpleType(schema: ObjectValue): string {
  if (["$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not", "if", "then", "else"].some((key) => Object.hasOwn(schema, key))) return "";
  if (typeof schema.type === "string" && BASIC_TYPES.includes(schema.type)) return schema.type;
  if (Array.isArray(schema.type) && schema.type.length === 2 && schema.type.includes("null")) {
    const nonNull = schema.type.find((value) => value !== "null");
    if (BASIC_TYPES.includes(nonNull)) return String(nonNull);
  }
  return "";
}

/** Basic controls only change type; all other schema keywords remain intact. */
function SchemaFields({ schema, label, path, onChange, onValidationChange, issue, allowBoolean = false }: Readonly<{
  schema: unknown; label: string; path: string; onChange: (schema: ObjectValue | boolean) => void;
  onValidationChange?: ValidationChange; issue?: string; allowBoolean?: boolean;
}>) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [invalidJson, setInvalidJson] = useState(false);
  const object = record(schema);
  const type = simpleType(object);
  const nullable = Array.isArray(object.type) && object.type.includes("null");
  return <div className="flex flex-col gap-2">
    <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
      <span>{t("react.workflows.type", "Type")}</span>
      <DsSelect ariaLabel={t("react.workflows.type", "Type")} value={type || "schema_defined"}
        options={[{ value: "schema_defined", label: t("react.workflows.hook_schema_defined", "Schema-defined") }, ...BASIC_TYPES.map((value) => ({ value, label: value }))]}
        onChange={(value) => { if (value !== "schema_defined") onChange({ ...object, type: nullable ? [value, "null"] : value }); }} />
    </label>
    {type ? <DsCheckbox checked={nullable} onChange={(checked) => onChange({ ...object, type: checked ? [type, "null"] : type })}>
      {t("react.workflows.hook_allow_null", "Allow null")}
    </DsCheckbox> : <p className="text-xs fs-ink-secondary">{t("react.workflows.hook_schema_preserved", "This field uses its full schema. Edit Schema details to keep its constraints.")}</p>}
    <Button variant="secondary" size="sm" className="self-start" onClick={() => setExpanded((current) => !current)} type="button" aria-expanded={expanded} disabled={invalidJson}>
      {t("react.workflows.hook_schema_details", "Schema details")}
    </Button>
    {expanded ? <JsonDraft value={schema} label={label} path={path} objectOnly allowBoolean={allowBoolean} issue={issue}
      onValid={(value) => onChange(typeof value === "boolean" ? value : record(value))} onValidationChange={onValidationChange} onErrorChange={setInvalidJson} /> : issue ? <p className="text-xs text-[var(--fs-status-danger)]" role="alert">{issue}</p> : null}
  </div>;
}

function RowName({ name, label, path, valid, onRename, onValidationChange }: Readonly<{
  name: string; label: string; path: string; valid: (next: string) => boolean;
  onRename: (next: string) => void; onValidationChange?: ValidationChange;
}>) {
  const { t } = useI18n();
  const [raw, setRaw] = useState(name);
  const errorId = useId();
  const error = valid(raw) ? "" : t("react.workflows.hook_unique_name_required", "Enter a valid, unique name.");
  useFieldError(path, error, onValidationChange);
  return <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
    <RequiredLabel>{label}</RequiredLabel>
    <input className="fs-input" aria-label={label} required value={raw} aria-invalid={Boolean(error) || undefined} aria-describedby={error ? errorId : undefined}
      onChange={(event) => { const next = event.target.value; setRaw(next); if (valid(next)) onRename(next); }} />
    {error ? <span className="text-[var(--fs-status-danger)]" id={errorId} role="alert">{error}</span> : null}
  </label>;
}

export function HookDeclarationEditor({ declaration, identityLocked, readOnly = false, issues = [], renderVariableInput, onChange, onValidationChange }: Readonly<Props>) {
  const { t } = useI18n();
  const id = useId();
  const nextId = useRef(0);
  const schema = record(declaration.input_schema);
  const properties = record(schema.properties);
  const outputs = declaration.additional_outputs || {};
  const rowKeys = [...Object.keys(properties).map((name) => "input:" + name), ...Object.keys(outputs).map((name) => "output:" + name)];
  const [rowIds, setRowIds] = useState(() => reconcileRowIds({ ids: new Map(), nextId: 0 }, rowKeys, id));
  const currentRows = reconcileRowIds(rowIds, rowKeys, id);
  if (currentRows !== rowIds) setRowIds(currentRows);
  const [newInputs, setNewInputs] = useState<string[]>([]);
  const [newOutputs, setNewOutputs] = useState<string[]>([]);
  const [enabledDefaults, setEnabledDefaults] = useState(new Set<string>());
  const [advancedSchema, setAdvancedSchema] = useState(false);
  const [invalidAdvancedSchema, setInvalidAdvancedSchema] = useState(false);
  const canonicalVersion = String(numericInputValue(declaration.contract_version));
  const [versionDraft, setVersionDraft] = useState({ source: canonicalVersion, raw: canonicalVersion });
  if (versionDraft.source !== canonicalVersion) setVersionDraft({ source: canonicalVersion, raw: canonicalVersion });
  const versionRaw = versionDraft.source === canonicalVersion ? versionDraft.raw : canonicalVersion;
  const required = Array.isArray(schema.required) ? schema.required.filter((value): value is string => typeof value === "string") : [];
  const mapping = declaration.input_mapping || {};
  const mutable = declaration.mutable_fields || [];
  const stage = declaration.stage_contract || {};
  const version = numericDraftFromInput(versionRaw);
  const versionError = typeof version === "number" && Number.isInteger(version) && version > 0 ? "" : t("react.workflows.hook_positive_version_required", "Enter a whole number greater than zero.");
  useFieldError("hook.contract_version", versionError, onValidationChange);
  const hookIdError = /^[a-z][a-z0-9_]*$/.test(declaration.hook_id || "") ? "" : t("react.workflows.hook_id_required", "Enter a Hook ID using lowercase letters, numbers and underscores.");
  useFieldError("hook.hook_id", hookIdError, onValidationChange);
  const hookNameError = declaration.display_name?.trim() ? "" : t("react.workflows.hook_name_required", "Enter a Hook name.");
  useFieldError("hook.display_name", hookNameError, onValidationChange);
  const summaryError = stage.summary?.trim() ? "" : t("react.workflows.hook_stage_summary_required", "Enter a stage summary.");
  useFieldError("hook.stage_contract.summary", summaryError, onValidationChange);
  const mappingError = required.some((name) => !String(mapping[name] || "").trim()) ? t("react.workflows.hook_required_input_source", "Choose a source for each required input.") : "";
  useFieldError("hook.input_mapping", mappingError, onValidationChange);
  const issueFor = (path: string) => issues.find((issue) => issue.path === path || issue.path?.endsWith("." + path))?.message || "";
  const idFeedback = hookIdError || issueFor("hook.hook_id");
  const nameFeedback = hookNameError || issueFor("hook.display_name");
  const versionFeedback = versionError || issueFor("hook.contract_version");
  const summaryFeedback = fieldFeedback(summaryError, issueFor("hook.stage_contract.summary"), summaryError ? issueFor("hook.stage_contract") : "");
  const sourceFeedback = (name: string) => fieldFeedback(required.includes(name) && !String(mapping[name] || "").trim() ? t("react.workflows.hook_required_input_source", "Choose a source for each required input.") : "", issueFor("hook.input_mapping." + name));
  const keyFor = (kind: string, name: string) => {
    const key = kind + ":" + name;
    return currentRows.ids.get(key)!;
  };
  const patch = (fields: Partial<WorkflowHookContract>) => onChange({ ...declaration, ...fields });
  const inputNames = [...Object.keys(properties).map((name) => ({ name, rowId: keyFor("input", name) })), ...newInputs.map((rowId) => ({ name: "", rowId }))];
  const outputNames = [...Object.keys(outputs).map((name) => ({ name, rowId: keyFor("output", name) })), ...newOutputs.map((rowId) => ({ name: "", rowId }))];
  const renameInput = (name: string, next: string, rowId: string) => {
    if (next === name) return;
    const nextProperties = Object.fromEntries(Object.entries(properties).map(([key, value]) => [key === name ? next : key, value]));
    if (!name) nextProperties[next] = { type: "string" };
    const nextMapping = Object.fromEntries(Object.entries(mapping).map(([key, value]) => [key === name ? next : key, value]));
    setRowIds((current) => { const ids = new Map(current.ids); ids.delete("input:" + name); ids.set("input:" + next, rowId); return { ...current, ids }; });
    setNewInputs((rows) => rows.filter((value) => value !== rowId));
    patch({ input_schema: { ...schema, properties: nextProperties, ...(schema.required ? { required: required.map((value) => value === name ? next : value) } : {}) }, input_mapping: nextMapping, ...(declaration.mutable_fields ? { mutable_fields: mutable.map((value) => value === name ? next : value) } : {}) });
  };
  const renameOutput = (name: string, next: string, rowId: string) => {
    if (next === name) return;
    const nextOutputs = Object.fromEntries(Object.entries(outputs).map(([key, value]) => [key === name ? next : key, value]));
    if (!name) nextOutputs[next] = { schema: { type: "string" }, optional: true };
    setRowIds((current) => { const ids = new Map(current.ids); ids.delete("output:" + name); ids.set("output:" + next, rowId); return { ...current, ids }; });
    setNewOutputs((rows) => rows.filter((value) => value !== rowId));
    patch({ additional_outputs: nextOutputs });
  };
  return <fieldset disabled={readOnly} className="flex min-w-0 flex-col gap-4 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
    <legend className="px-1 text-sm font-medium fs-ink-primary">{t("react.workflows.hook_declaration", "Hook declaration")}</legend>
    {issues.some((issue) => issue.message) ? <ul className="text-xs text-[var(--fs-status-danger)]" role="alert">
      {issues.map((issue, index) => issue.message ? <li key={String(issue.path) + ":" + index}>{issue.message}</li> : null)}
    </ul> : null}
    <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
      <RequiredLabel>{t("react.workflows.hook_id", "Hook ID")}</RequiredLabel>
      <input className="fs-input" aria-label={t("react.workflows.hook_id", "Hook ID")} required readOnly={identityLocked} value={declaration.hook_id || ""} aria-invalid={Boolean(idFeedback) || undefined} aria-describedby={idFeedback ? id + "-id-error" : undefined} onChange={(event) => patch({ hook_id: event.target.value })} />
      {idFeedback ? <span id={id + "-id-error"} className="text-[var(--fs-status-danger)]" role="alert">{idFeedback}</span> : null}
    </label>
    <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
      <RequiredLabel>{t("react.workflows.hook_contract_version", "Contract version")}</RequiredLabel>
      <input className="fs-input" type="text" inputMode="numeric" aria-label={t("react.workflows.hook_contract_version", "Contract version")} required value={versionRaw}
        aria-invalid={Boolean(versionFeedback) || undefined} aria-describedby={versionFeedback ? id + "-version-error" : undefined}
        onChange={(event) => {
          const raw = event.target.value;
          const parsed = numericDraftFromInput(raw);
          if (typeof parsed === "number" && Number.isInteger(parsed) && parsed > 0) {
            setVersionDraft({ source: String(parsed), raw });
            patch({ contract_version: parsed });
          } else setVersionDraft({ source: canonicalVersion, raw });
        }} />
      {versionFeedback ? <span id={id + "-version-error"} className="text-[var(--fs-status-danger)]" role="alert">{versionFeedback}</span> : null}
    </label>
    <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
      <RequiredLabel>{t("react.workflows.hook_name", "Hook name")}</RequiredLabel>
      <input className="fs-input" aria-label={t("react.workflows.hook_name", "Hook name")} required value={declaration.display_name || ""} aria-invalid={Boolean(nameFeedback) || undefined} aria-describedby={nameFeedback ? id + "-name-error" : undefined} onChange={(event) => patch({ display_name: event.target.value })} />
      {nameFeedback ? <span id={id + "-name-error"} className="text-[var(--fs-status-danger)]" role="alert">{nameFeedback}</span> : null}
    </label>
    <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
      <OptionalLabel>{t("react.workflows.hook_description_label", "Hook description")}</OptionalLabel>
      <textarea className="fs-input" value={declaration.description || ""} onChange={(event) => patch({ description: event.target.value })} />
    </label>
    <fieldset className="flex min-w-0 flex-col gap-3">
      <legend className="text-sm font-medium fs-ink-primary">{t("react.workflows.inputs", "Inputs")}</legend>
      {inputNames.map(({ name, rowId }) => <fieldset className="flex min-w-0 flex-col gap-2 rounded-lg border border-[var(--fs-border-default)] p-3" key={rowId}>
        <legend className="px-1 text-xs font-medium fs-ink-primary">{name ? t("react.workflows.hook_input_name", "Input {name}", { name }) : t("react.workflows.hook_new_input", "New input")}</legend>
        <RowName name={name} label={t("react.workflows.hook_input_name_label", "Input name")} path={"hook.input_name." + rowId}
          valid={(next) => Boolean(next.trim() && (next === name || !Object.hasOwn(properties, next)) && !Object.hasOwn(outputs, next))}
          onRename={(next) => renameInput(name, next, rowId)} onValidationChange={onValidationChange} />
        {name ? <>
          <SchemaFields allowBoolean schema={properties[name]} label={t("react.workflows.hook_input_schema", "Input schema")} path={"hook.input_schema.properties." + name} issue={issueFor("hook.input_schema.properties." + name)}
            onChange={(value) => patch({ input_schema: { ...schema, properties: { ...properties, [name]: value } } })} onValidationChange={onValidationChange} />
          <DsCheckbox checked={required.includes(name)} onChange={(checked) => patch({ input_schema: { ...schema, required: checked ? [...required, name] : required.filter((value) => value !== name) } })}>
            {t("react.workflows.required", "Required")}
          </DsCheckbox>
          <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
            <span>{required.includes(name) ? <RequiredLabel>{t("react.workflows.mapped_from", "Mapped from")}</RequiredLabel> : t("react.workflows.mapped_from", "Mapped from")}</span>
            {renderVariableInput({ ariaLabel: t("react.workflows.mapped_from", "Mapped from"), ariaInvalid: Boolean(sourceFeedback(name)), ariaRequired: required.includes(name), ariaDescribedBy: sourceFeedback(name) ? rowId + "-source-error" : undefined, className: "fs-input", panelId: rowId + "-source", placeholder: "state.inputs." + name, value: mapping[name] || "", onChange: (value) => {
              const next = { ...mapping };
              if (value.trim()) next[name] = value;
              else delete next[name];
              patch({ input_mapping: next });
            } })}
            {sourceFeedback(name) ? <span id={rowId + "-source-error"} className="text-[var(--fs-status-danger)]" role="alert">{sourceFeedback(name)}</span> : null}
          </label>
          <DsCheckbox checked={mutable.includes(name)} onChange={(checked) => patch({ mutable_fields: checked ? [...mutable, name] : mutable.filter((value) => value !== name) })}>
            {t("react.workflows.hook_allow_override", "Allow override")}
          </DsCheckbox>
        </> : null}
        <Button variant="secondary" size="sm" className="self-start" type="button" onClick={() => {
          setNewInputs((rows) => rows.filter((value) => value !== rowId));
          if (!name) return;
          setRowIds((current) => { const ids = new Map(current.ids); ids.delete("input:" + name); return { ...current, ids }; });
          patch({ input_schema: { ...schema, properties: Object.fromEntries(Object.entries(properties).filter(([key]) => key !== name)), ...(schema.required ? { required: required.filter((value) => value !== name) } : {}) }, input_mapping: Object.fromEntries(Object.entries(mapping).filter(([key]) => key !== name)), ...(declaration.mutable_fields ? { mutable_fields: mutable.filter((value) => value !== name) } : {}) });
        }}>{t("react.workflows.hook_remove_input", "Remove input")}</Button>
      </fieldset>)}
      <Button variant="secondary" size="sm" className="self-start" type="button" onClick={() => { const rowId = id + "-new-input-" + (++nextId.current); setNewInputs((rows) => [...rows, rowId]); }}>{t("react.workflows.hook_add_input", "Add input")}</Button>
      {mappingError ? <p className="text-xs text-[var(--fs-status-danger)]" role="alert">{mappingError}</p> : null}
      <Button variant="secondary" size="sm" className="self-start" type="button" aria-expanded={advancedSchema} disabled={invalidAdvancedSchema} onClick={() => setAdvancedSchema((value) => !value)}>{t("react.workflows.hook_advanced_input_schema", "Advanced input schema")}</Button>
      {advancedSchema ? <JsonDraft value={schema} label={t("react.workflows.hook_input_object_schema", "Input object schema")} path="hook.input_schema" objectOnly onValidationChange={onValidationChange} onErrorChange={setInvalidAdvancedSchema}
        onValid={(value) => patch({ input_schema: record(value) })} issue={issueFor("hook.input_schema")} /> : null}
    </fieldset>
    <fieldset className="flex min-w-0 flex-col gap-3">
      <legend className="text-sm font-medium fs-ink-primary">{t("react.workflows.additional_outputs", "Additional outputs")}</legend>
      {outputNames.map(({ name, rowId }) => {
        const output = outputs[name] || {};
        const useDefault = Object.hasOwn(output, "default") || enabledDefaults.has(rowId);
        const defaultError = name && !output.optional && !Object.hasOwn(output, "default") ? t("react.workflows.hook_output_default_required", "A default is required for a required output.") : "";
        return <OutputRow key={rowId} name={name} rowId={rowId} output={output} useDefault={useDefault} defaultError={defaultError}
          validName={(next) => Boolean(/^[a-z][a-z0-9_]*$/.test(next) && (next === name || !Object.hasOwn(outputs, next)) && !Object.hasOwn(properties, next))}
          onRename={(next) => renameOutput(name, next, rowId)} onChange={(next) => patch({ additional_outputs: { ...outputs, [name]: next } })}
          onRemove={() => {
            setNewOutputs((rows) => rows.filter((value) => value !== rowId));
            setEnabledDefaults((current) => { const next = new Set(current); next.delete(rowId); return next; });
            setRowIds((current) => { const ids = new Map(current.ids); ids.delete("output:" + name); return { ...current, ids }; });
            if (name) patch({ additional_outputs: Object.fromEntries(Object.entries(outputs).filter(([key]) => key !== name)) });
          }}
          onToggleDefault={(checked) => {
            setEnabledDefaults((current) => {
              const next = new Set(current);
              if (checked) next.add(rowId);
              else next.delete(rowId);
              return next;
            });
            if (!checked && Object.hasOwn(output, "default")) { const next = { ...output }; delete next.default; patch({ additional_outputs: { ...outputs, [name]: next } }); }
          }}
          schemaIssue={issueFor("hook.additional_outputs." + name + ".schema") || issueFor("hook.additional_outputs." + name)} defaultIssue={issueFor("hook.additional_outputs." + name + ".default")} onValidationChange={onValidationChange} />;
      })}
      <Button variant="secondary" size="sm" className="self-start" type="button" onClick={() => { const rowId = id + "-new-output-" + (++nextId.current); setNewOutputs((rows) => [...rows, rowId]); }}>{t("react.workflows.hook_add_output", "Add output")}</Button>
    </fieldset>
    <label className="flex flex-col gap-1 text-xs fs-ink-secondary">
      <RequiredLabel>{t("react.workflows.hook_stage_summary", "Stage summary")}</RequiredLabel>
      <textarea className="fs-input" aria-label={t("react.workflows.hook_stage_summary", "Stage summary")} required value={stage.summary || ""} aria-invalid={Boolean(summaryFeedback) || undefined} aria-describedby={summaryFeedback ? id + "-summary-error" : undefined} onChange={(event) => patch({ stage_contract: { ...stage, summary: event.target.value } })} />
      {summaryFeedback ? <span id={id + "-summary-error"} className="text-[var(--fs-status-danger)]" role="alert">{summaryFeedback}</span> : null}
    </label>
    {(["effects_before", "effects_after"] as const).map((field) => <label className="flex flex-col gap-1 text-xs fs-ink-secondary" key={field} htmlFor={id + "-" + field}>
      <OptionalLabel>{field === "effects_before" ? t("react.workflows.hook_effects_before_label", "Effects before") : t("react.workflows.hook_effects_after_label", "Effects after")}</OptionalLabel>
      <EffectLines id={id + "-" + field} value={stage[field] || []} onChange={(value) => patch({ stage_contract: { ...stage, [field]: value } })} />
    </label>)}
    <p className="text-xs fs-ink-secondary">{t("react.workflows.hook_output_help", "An unbound hook passes its published inputs and declared defaults. A connected workflow changes only mapped writable inputs or declared outputs; missing mapped outputs are errors. Author variables remain unchanged. Published output:")} <code>{"state.hooks." + (declaration.hook_id || "<hook_id>") + ".output"}</code></p>
  </fieldset>;
}

/** Retain a trailing newline while the canonical effect list contains only names. */
function EffectLines({ id, value, onChange }: Readonly<{ id: string; value: string[]; onChange: (value: string[]) => void }>) {
  const canonical = value.join("\n");
  const [draft, setDraft] = useState({ source: canonical, raw: canonical });
  if (draft.source !== canonical) setDraft({ source: canonical, raw: canonical });
  const raw = draft.source === canonical ? draft.raw : canonical;
  return <textarea className="fs-input" id={id} value={raw} onChange={(event) => {
    const next = event.target.value;
    const names = next.split("\n").map((line) => line.trim()).filter(Boolean);
    setDraft({ source: names.join("\n"), raw: next });
    onChange(names);
  }} />;
}

type OutputSpec = NonNullable<WorkflowHookContract["additional_outputs"]>[string];
function OutputRow({ name, rowId, output, useDefault, defaultError, validName, onRename, onChange, onRemove, onToggleDefault, schemaIssue, defaultIssue, onValidationChange }: Readonly<{
  name: string; rowId: string; output: OutputSpec; useDefault: boolean; defaultError: string;
  validName: (value: string) => boolean; onRename: (value: string) => void; onChange: (value: OutputSpec) => void;
  onRemove: () => void; onToggleDefault: (checked: boolean) => void; schemaIssue: string; defaultIssue: string; onValidationChange?: ValidationChange;
}>) {
  const { t } = useI18n();
  useFieldError("hook.additional_outputs." + name + ".default", defaultError, onValidationChange);
  return <fieldset className="flex min-w-0 flex-col gap-2 rounded-lg border border-[var(--fs-border-default)] p-3">
    <legend className="px-1 text-xs font-medium fs-ink-primary">{name ? t("react.workflows.hook_output_name", "Output {name}", { name }) : t("react.workflows.hook_new_output", "New output")}</legend>
    <RowName name={name} label={t("react.workflows.hook_output_name_label", "Output name")} path={"hook.output_name." + rowId} valid={validName} onRename={onRename} onValidationChange={onValidationChange} />
    {name ? <>
      <SchemaFields schema={record(output.schema)} label={t("react.workflows.hook_output_schema", "Output schema")} path={"hook.additional_outputs." + name + ".schema"} issue={schemaIssue} onChange={(value) => onChange({ ...output, schema: record(value) })} onValidationChange={onValidationChange} />
      <DsCheckbox checked={Boolean(output.optional)} onChange={(checked) => onChange({ ...output, optional: checked })}>{t("react.workflows.hook_optional_output", "Optional output")}</DsCheckbox>
      <DsCheckbox checked={useDefault} onChange={onToggleDefault}>{t("react.workflows.hook_use_default", "Use default")}</DsCheckbox>
      {useDefault ? <JsonDraft value={output.default} label={t("react.workflows.hook_default_value", "Default value")} path={"hook.additional_outputs." + name + ".default_json"} issue={defaultIssue} onValid={(value) => onChange({ ...output, default: value })} onValidationChange={onValidationChange} /> : null}
      {defaultError ? <p className="text-xs text-[var(--fs-status-danger)]" role="alert">{defaultError}</p> : null}
    </> : null}
    <Button variant="secondary" size="sm" className="self-start" type="button" onClick={onRemove}>{t("react.workflows.hook_remove_output", "Remove output")}</Button>
  </fieldset>;
}
