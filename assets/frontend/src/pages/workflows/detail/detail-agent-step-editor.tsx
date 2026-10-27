import { useId } from "react";
import { DsSelect, RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { agentParticipants, applyAgentBlockIntent, effectiveAgentBlockKey, parseAgentMap } from "./detail-agent-step-fields";
import type { WorkflowStepContractPreview } from "@/pages/workflows/shared/api";
import type { AgentStepDrafts } from "./detail-agent-step-drafts";

export type AgentStepEditorContext = {
  stepContractPreviewQuery?: { data?: WorkflowStepContractPreview; isLoading?: boolean; isError?: boolean };
  agentDrafts: AgentStepDrafts; agentWorkflowRecord: unknown; stepAgentId: string; stepAgentLocked: boolean;
  setStepAgentId: (value: string) => void; stepAgentOptions: Array<{ value: string; label: string }>;
  agentCatalogQuery: { isLoading?: boolean; isError?: boolean };
  agentBlockCatalogQuery: { isLoading?: boolean; isError?: boolean };
  agentBlockOptions: Array<{ value: string; label: string; disabled?: boolean }>;
  stepOverrideDraft: string; setStepOverrideDraft: (value: string) => void; setStepBlockKey: (value: string) => void;
};
export function AgentBindingFields({ ctx }: Readonly<{ ctx: AgentStepEditorContext }>) {
  const { t } = useI18n(); const id = useId(); const draft = ctx.agentDrafts;
  const participants = agentParticipants(ctx.agentWorkflowRecord);
  const bound = participants.find((row) => row.key === draft.participantKey);
  const participantOptions: Array<{ value: string; label: string; disabled?: boolean }> = participants.map((row) => ({ value: row.key, label: row.key }));
  if (draft.participantKey && !bound) participantOptions.push({ value: draft.participantKey, label: draft.participantKey, disabled: true });
  return <div className="space-y-3">
    <label className="block"><RequiredLabel>{t("react.workflows.agent_binding", "Agent binding")}</RequiredLabel>
      <DsSelect ariaLabel={t("react.workflows.agent_binding", "Agent binding")} required value={draft.mode} options={[{ value: "direct", label: t("react.workflows.direct_agent", "Direct agent") }, { value: "participant", label: t("react.workflows.workflow_participant", "Workflow participant") }]}
        onChange={(mode) => { const next = mode === "participant" ? "participant" : "direct"; const agent = next === "participant" ? bound?.agentId || "" : bound?.agentId || ctx.stepAgentId; draft.editMode(next, agent); ctx.setStepAgentId(agent); }} />
    </label>
    {draft.mode === "participant" ? <label className="block"><RequiredLabel>{t("react.workflows.workflow_participant", "Workflow participant")}</RequiredLabel>
      <DsSelect ariaLabel={t("react.workflows.workflow_participant", "Workflow participant")} required value={draft.participantKey} options={[{ value: "", label: t("react.workflows.choose_participant", "Choose participant") }, ...participantOptions]}
        onChange={(key) => { const agent = participants.find((row) => row.key === key)?.agentId || ""; draft.editParticipant(key, agent); ctx.setStepAgentId(agent); }} describedBy={!bound ? `${id}-participant` : undefined} />
      {!bound ? <span id={`${id}-participant`} className="fs-field-helper">{t("react.workflows.agent_participant_unresolved", "This participant is not currently declared. Choose a declared participant or Direct agent to repair the binding.")}</span> : null}
    </label> : null}
    <label className="block">{draft.mode === "direct" ? <RequiredLabel>{t("react.workflows.agent", "Agent")}</RequiredLabel> : <span className="fs-field-label">{t("react.workflows.agent", "Agent")}</span>}
      <DsSelect ariaLabel={t("react.workflows.agent", "Agent")} required={draft.mode === "direct"} id="step-agent-select" value={draft.mode === "participant" ? bound?.agentId || ctx.stepAgentId : ctx.stepAgentId} options={ctx.stepAgentOptions}
        disabled={draft.mode === "participant" || ctx.stepAgentLocked || ctx.agentCatalogQuery.isLoading} onChange={(value) => { draft.editDirect(); ctx.setStepAgentId(value); }} />
    </label>
    {ctx.agentCatalogQuery.isLoading ? <p className="fs-field-helper">{t("react.workflows.loading_agent_catalog", "Loading agent catalog…")}</p> : null}
    {ctx.agentCatalogQuery.isError ? <p className="fs-field-helper">{t("react.workflows.agent_catalog_could_not_be_loaded_reopen_this_step_to_retry", "Agent catalog could not be loaded. Reopen this step to retry.")}</p> : null}
    {!ctx.agentCatalogQuery.isLoading && !ctx.agentCatalogQuery.isError && !ctx.stepAgentOptions.length ? <p className="fs-field-helper">{t("react.workflows.no_agents_are_available_create_or_enable_an_agent_in_registry_before_saving_this_step", "No agents are available. Create or enable an agent in Registry before saving this step.")}</p> : null}
    <AgentContractMappingFields ctx={ctx} />
  </div>;
}

function AgentContractMappingFields({ ctx }: Readonly<{ ctx: AgentStepEditorContext }>) {
  const { t } = useI18n(); const preview = ctx.stepContractPreviewQuery;
  if (preview?.isLoading) return <p className="fs-field-helper">{t("react.workflows.agent_contract_loading", "Loading Agent inputs and outputs…")}</p>;
  if (preview?.isError) return <p className="fs-field-helper">{t("react.workflows.agent_contract_unavailable", "Agent inputs and outputs could not be loaded. Existing mappings are retained.")}</p>;
  if (!preview?.data) return null;
  const contract = preview.data;
  const inputs = [...(contract.locked_required || []), ...(contract.unresolved_required || []), ...(contract.optional || []), ...(contract.custom || [])];
  return <section className="space-y-3" aria-label={t("react.workflows.agent_inputs_outputs", "Agent inputs and outputs")}>
    <p className="fs-field-label">{contract.skill?.label || contract.skill?.id}</p>
    {(["input", "output"] as const).map((direction) => {
      const map = ctx.agentDrafts.maps[direction];
      let values: Record<string, unknown> = {}; let valid = true;
      try { values = parseAgentMap(map.text); } catch { valid = false; }
      const rows = direction === "input" ? inputs : contract.outputs || [];
      return rows.map((row) => {
        const name = String(row.name || ""); if (!name) return null;
        const value = values[name]; const structured = value !== undefined && typeof value !== "string";
        const label = direction === "input" ? t("react.workflows.agent_input_source", "Input source: {name}", { name }) : t("react.workflows.agent_output_target", "Output target: {name}", { name });
        return <label className="block" key={`${direction}:${name}`}><span className="fs-field-label">{label}{row.required ? ` (${t("react.workflows.required", "Required")})` : ""}</span>
          <input className="fs-input" aria-label={label} disabled={!valid || structured} value={typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value)} placeholder={direction === "input" ? String(row.source || "state.inputs." + name) : "state.outputs." + name}
            onChange={(event) => { const next = { ...values }; if (event.target.value === "") delete next[name]; else next[name] = event.target.value; ctx.agentDrafts.editMap(direction, JSON.stringify(next, null, 2)); }} />
          {row.description ? <span className="fs-field-helper">{String(row.description)}</span> : null}
          {!valid || structured ? <span className="fs-field-helper">{t("react.workflows.agent_mapping_advanced_value", "This mapping is retained. Use the Advanced JSON editor to change its JSON value.")}</span> : null}
        </label>;
      });
    })}
    <p className="fs-field-helper">{t("react.workflows.agent_mapping_fields_help", "Map inputs from state or message selectors, or use literal: for a fixed value. Leave blank to use the available input automatically. Output targets store the Agent’s declared results in state.")}</p>
  </section>;
}
export function AgentAdvancedFields({ ctx }: Readonly<{ ctx: AgentStepEditorContext }>) {
  const { t } = useI18n(); const id = useId(); const draft = ctx.agentDrafts;
  let override: Record<string, unknown> = {}; let validOverride = true;
  try { const parsed: unknown = JSON.parse(ctx.stepOverrideDraft || "{}"); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) validOverride = false; else override = parsed as Record<string, unknown>; } catch { validOverride = false; }
  return <div className="space-y-3">
    <label className="block"><span className="fs-field-label">{t("react.workflows.agent_contract", "Agent contract")}</span>
      <DsSelect ariaLabel={t("react.workflows.agent_contract", "Agent contract")} value={effectiveAgentBlockKey(override)} disabled={!validOverride || ctx.agentBlockCatalogQuery.isLoading}
        options={[{ value: "", label: t("react.workflows.auto_from_selected_agent", "Auto from selected agent") }, ...ctx.agentBlockOptions]}
        onChange={(key) => {
          if (!validOverride) return;
          draft.touchBlock();
          ctx.setStepBlockKey(key);
          ctx.setStepOverrideDraft(JSON.stringify(applyAgentBlockIntent(override, key), null, 2));
        }} />
    </label>
    {ctx.agentBlockCatalogQuery.isError ? <p className="fs-field-helper">{t("react.workflows.agent_block_catalog_unavailable", "Agent contracts could not be loaded. The configured contract is retained.")}</p> : null}
    <p className="fs-field-helper">{t("react.workflows.agent_mapping_effective_help", "These mappings control the Agent step inputs and state outputs. Compatibility mappings in Override JSON remain saved separately.")}</p>
    {(["input", "output"] as const).map((direction) => {
      const map = draft.maps[direction]; const label = direction === "input" ? t("react.workflows.agent_input_mapping_json", "Agent input mapping JSON") : t("react.workflows.agent_output_mapping_json", "Agent output mapping JSON");
      return <label className="block" key={direction}><span className="fs-field-label">{label}</span>
        <textarea aria-label={label} aria-invalid={map.error || undefined} aria-describedby={map.error ? `${id}-${direction}-error` : `${id}-help`} className="fs-input min-h-28 font-mono" value={map.text} ref={(element) => draft.setControl(direction, element)} onChange={(event) => draft.editMap(direction, event.target.value)} />
        {map.error ? <span id={`${id}-${direction}-error`} className="fs-field-helper error">{t("react.workflows.agent_mapping_json_invalid", "Enter a JSON object with finite numbers. The incomplete draft has not been saved.")}</span> : null}
      </label>;
    })}
    <p id={`${id}-help`} className="fs-field-helper">{t("react.workflows.agent_mapping_json_help", "Use a JSON object. Input values keep their JSON types; strings may be state/message selectors or literal: values. An empty object clears a mapping.")}</p>
  </div>;
}
