import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchAgentFileFormats } from "@/pages/workflows/shared/api";
import { isAgentFileStatePath } from "./detail-agent-file-path";
import { RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";

type FileBinding = { name: string; source: string };
type VariableInput = (args: { ariaLabel: string; ariaRequired: boolean; ariaInvalid: boolean; ariaDescribedBy: string; className: string; onChange: (value: string) => void; panelId: string; placeholder: string; value: string; accept: (path: string) => boolean }) => ReactNode;

function AgentFileFormats() {
  const { t } = useI18n();
  const formats = useQuery({ queryKey: ["workflows", "meta", "agent-file-formats"], queryFn: fetchAgentFileFormats, retry: false });
  if (formats.isPending) return <p className="fs-field-helper" role="status">{t("react.workflows.agent_file_formats_loading", "Loading text file formats…")}</p>;
  if (formats.isError || !formats.data) return <p className="fs-field-helper" role="status">{t("react.workflows.agent_file_formats_unavailable", "Text file formats are unavailable.")}</p>;
  if (!formats.data.text_extensions.length) return <p className="fs-field-helper" role="status">{t("react.workflows.agent_file_formats_disabled", "Text file reading is disabled.")}</p>;
  return <div className="fs-field-helper"><p>{t("react.workflows.agent_file_formats_list", "Text file formats: {formats}", { formats: formats.data.text_extensions.join(", ") })}</p><p>{t("react.workflows.agent_file_formats_encoding", "Text must be UTF-8. RTF is read as source, not converted.")}</p></div>;
}

export function AgentFiles({ value, onChange, renderVariableInput, suggestions = [] }: Readonly<{ value: string; onChange: (value: string) => void; renderVariableInput?: VariableInput; suggestions?: Array<{ options: Array<{ value: string; valueType?: string }> }> }>) {
  const { t } = useI18n();
  const [identityState, setIdentityState] = useState<{ ids: number[]; next: number }>({ ids: [], next: 0 });
  let override: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(value || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    override = parsed as Record<string, unknown>;
  } catch { return null; }
  if (override.agent !== undefined && (!override.agent || typeof override.agent !== "object" || Array.isArray(override.agent))) {
    return <p className="fs-field-helper" role="alert">{t("react.workflows.agent_files_uneditable", "The saved file bindings cannot be edited here. Repair them in Override JSON.")}</p>;
  }
  const agent = override.agent && typeof override.agent === "object" && !Array.isArray(override.agent) ? override.agent as Record<string, unknown> : {};
  const raw = agent.file_bindings;
  if (raw !== undefined && (!Array.isArray(raw) || raw.some((row) => !row || typeof row !== "object" || Array.isArray(row) || typeof row.name !== "string" || typeof row.source !== "string"))) {
    return <p className="fs-field-helper" role="alert">{t("react.workflows.agent_files_uneditable", "The saved file bindings cannot be edited here. Repair them in Override JSON.")}</p>;
  }
  const files = (raw || []) as FileBinding[];
  let identities = identityState.ids;
  if (identities.length !== files.length) {
    identities = identities.slice(0, files.length);
    let next = identityState.next;
    while (identities.length < files.length) identities.push(next++);
    setIdentityState({ ids: identities, next });
  }
  const filePaths = new Set(suggestions.flatMap((group) => group.options.filter((option) => /^(file|artifact|artifact_handle)$/i.test(option.valueType || "") && isAgentFileStatePath(option.value)).map((option) => option.value)));
  const nameValid = (file: FileBinding) => /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(file.name) && files.filter((item) => item.name === file.name).length === 1;
  const save = (next: FileBinding[]) => onChange(JSON.stringify({ ...override, agent: { ...agent, file_bindings: next } }, null, 2));
  const update = (index: number, patch: Partial<FileBinding>) => save(files.map((file, i) => i === index ? { ...file, ...patch } : file));
  return <section className="space-y-3" aria-label={t("react.workflows.agent_files", "Files")}>
    <p className="fs-field-label">{t("react.workflows.agent_files", "Files")}</p>
    <p className="fs-field-helper">{t("react.workflows.agent_files_help", "Choose files from earlier workflow steps. Text files are read in pages. Images, audio and video require a compatible model and provider. Compatible built-in Google Gemini models support video. Process unsupported media in a preceding connector step and pass its text output to the agent.")}</p>
    <AgentFileFormats />
    <p className="fs-field-helper">{t("react.workflows.agent_files_limits", "Up to 10 files, 50 MiB each and 100 MiB total. Native media is limited to 5 MiB per file and 10 MiB total. Text is read in bounded pages.")}</p>
    {files.map((file, index) => {
      const number = index + 1;
      const sourceLabel = t("react.workflows.agent_file_source", "File source {number}", { number });
      const sourceArgs = { ariaLabel: sourceLabel, ariaRequired: true, ariaInvalid: Boolean(file.source && !isAgentFileStatePath(file.source)), ariaDescribedBy: `agent-file-source-help-${index}`, className: "fs-input", onChange: (source: string) => update(index, { source }), panelId: `agent-file-source-${index}`, placeholder: "state.document_file", value: file.source, accept: (path: string) => filePaths.has(path) };
      return <div key={identities[index]} className="space-y-2 rounded-lg border border-[var(--fs-border-default)] p-3">
        <label className="block"><RequiredLabel>{t("react.workflows.agent_file_name", "File label {number}", { number })}</RequiredLabel><input className="fs-input" aria-label={t("react.workflows.agent_file_name", "File label {number}", { number })} aria-invalid={file.name !== "" && !nameValid(file) || undefined} aria-describedby={`agent-file-name-help-${index}`} required value={file.name} onChange={(event) => update(index, { name: event.target.value })} /></label><p id={`agent-file-name-help-${index}`} className="fs-field-helper">{t("react.workflows.agent_file_name_help", "Name used by the agent: start with a letter, then use up to 63 letters, numbers, underscores or hyphens. Each label must be unique.")}</p>
        <label className="block"><RequiredLabel>{sourceLabel}</RequiredLabel>{renderVariableInput ? renderVariableInput(sourceArgs) : <input className="fs-input" aria-label={sourceLabel} aria-invalid={sourceArgs.ariaInvalid || undefined} aria-describedby={sourceArgs.ariaDescribedBy} required value={file.source} placeholder={sourceArgs.placeholder} onChange={(event) => update(index, { source: event.target.value })} />}</label><p id={sourceArgs.ariaDescribedBy} className="fs-field-helper">{t("react.workflows.agent_file_source_help", "Choose an earlier file or enter its state path, such as state.document_file.")}</p>
        <button type="button" className="fs-btn fs-btn-secondary" onClick={() => { setIdentityState((current) => ({ ...current, ids: current.ids.filter((_, i) => i !== index) })); save(files.filter((_, i) => i !== index)); }}>{t("react.workflows.agent_file_remove", "Remove file {number}", { number })}</button>
      </div>;
    })}
    <button type="button" className="fs-btn fs-btn-secondary" disabled={files.length >= 10} onClick={() => save([...files, { name: "", source: "" }])}>{t("react.workflows.agent_file_add", "Add file")}</button>
  </section>;
}
