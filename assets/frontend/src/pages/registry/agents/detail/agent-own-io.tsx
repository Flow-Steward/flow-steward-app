import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchJson } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";
import { useKeyedState } from "@/lib/react/useKeyedState";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import { normalizeRecordList } from "@/pages/registry/shared/detail-helpers";
import {
  ContractEditorCard,
  type ContractDraftItem,
  emptyContractDraftItem,
  normalizeCatalogFieldOptions,
  normalizeContractDraftItems,
} from "@/pages/registry/skills/detail/contract-editor";

import { ContractPanel } from "./contract-panel";

type IoKind = "inputs" | "outputs";

/** A materialized field the agent inherits from a skill or a tool; own rows are edited below. */
function isInherited(row: Record<string, unknown>): boolean {
  const source = firstStringFromUnknownValue([row.source, ""]).trim();
  return Boolean(source) && source !== "own" && source !== "agent";
}

function ownFieldsUrl(agentId: string, kind: IoKind): string {
  return `/api/agents/${encodeURIComponent(agentId)}/own-${kind}`;
}

/**
 * The agent's own inputs and outputs, saved through the own-fields endpoints so a
 * shared skill and the other agents using it are never changed. Fields from skills
 * and tools stay read-only, with their source.
 */
export function AgentOwnIoPanel({
  agentId,
  materializedInputs,
  materializedOutputs,
  onSaved,
}: Readonly<{
  agentId: string;
  materializedInputs: Array<Record<string, unknown>>;
  materializedOutputs: Array<Record<string, unknown>>;
  onSaved: () => Promise<unknown>;
}>) {
  const { t } = useI18n();
  const [status, setStatus] = useState<Record<IoKind, string>>({ inputs: "", outputs: "" });
  const [savingKind, setSavingKind] = useState<IoKind | "">("");
  const ownInputsQuery = useQuery({
    queryKey: ["/api/agents", "own-inputs", agentId] as const,
    queryFn: async () => normalizeRecordList(await fetchJson<unknown>(ownFieldsUrl(agentId, "inputs"))),
    enabled: Boolean(agentId),
    retry: false,
  });
  const ownOutputsQuery = useQuery({
    queryKey: ["/api/agents", "own-outputs", agentId] as const,
    queryFn: async () => normalizeRecordList(await fetchJson<unknown>(ownFieldsUrl(agentId, "outputs"))),
    enabled: Boolean(agentId),
    retry: false,
  });
  const fieldCatalogQuery = useQuery({
    queryKey: ["/api/fields", "catalog"] as const,
    queryFn: () => fetchRegistryList("/api/fields"),
    retry: false,
  });
  const fieldOptions = useMemo(
    () => normalizeCatalogFieldOptions(fieldCatalogQuery.data || []),
    [fieldCatalogQuery.data],
  );
  const inputSeed = useMemo(
    () => normalizeContractDraftItems(ownInputsQuery.data || [], false),
    [ownInputsQuery.data],
  );
  const outputSeed = useMemo(
    () => normalizeContractDraftItems(ownOutputsQuery.data || [], true),
    [ownOutputsQuery.data],
  );
  const [inputDrafts, setInputDrafts] = useKeyedState<ContractDraftItem[]>(
    `${agentId}:own-inputs:${JSON.stringify(inputSeed)}`,
    inputSeed,
  );
  const [outputDrafts, setOutputDrafts] = useKeyedState<ContractDraftItem[]>(
    `${agentId}:own-outputs:${JSON.stringify(outputSeed)}`,
    outputSeed,
  );

  async function save(kind: IoKind) {
    const drafts = kind === "inputs" ? inputDrafts : outputDrafts;
    const items = drafts
      .map((item) => ({
        description: item.description.trim(),
        name: item.name.trim(),
        required: item.required,
        type: item.type.trim() || "text",
      }))
      .filter((item) => Boolean(item.name));
    setStatus((current) => ({ ...current, [kind]: "" }));
    setSavingKind(kind);
    try {
      await fetchJson<unknown>(ownFieldsUrl(agentId, kind), {
        body: JSON.stringify(items),
        method: "PUT",
      });
      await Promise.all([
        (kind === "inputs" ? ownInputsQuery : ownOutputsQuery).refetch(),
        onSaved(),
      ]);
      // Literal like the skill editor's: the card colours a status by the word "saved".
      setStatus((current) => ({ ...current, [kind]: "Saved." }));
    } catch (error) {
      const fallback = kind === "inputs"
        ? t("react.registry.display_failed_save_inputs", "Failed to save inputs.")
        : t("react.registry.display_failed_save_outputs", "Failed to save outputs.");
      setStatus((current) => ({ ...current, [kind]: error instanceof Error ? error.message : fallback }));
    } finally {
      setSavingKind("");
    }
  }

  function editor(kind: IoKind) {
    const isInputs = kind === "inputs";
    const setDrafts = isInputs ? setInputDrafts : setOutputDrafts;
    const query = isInputs ? ownInputsQuery : ownOutputsQuery;
    return (
      <ContractEditorCard
        addLabel={isInputs ? t("react.registry.display_add_input", "+ Add Input") : t("react.registry.display_add_output", "+ Add Output")}
        contractOptions={[]}
        emptyMessage={isInputs
          ? t("react.registry.agent_no_own_inputs", "This agent has no own inputs.")
          : t("react.registry.agent_no_own_outputs", "This agent has no own outputs.")}
        fieldOptions={fieldOptions}
        items={isInputs ? inputDrafts : outputDrafts}
        onAdd={() => {
          setDrafts((current) => [...current, emptyContractDraftItem(!isInputs)]);
          setStatus((current) => ({ ...current, [kind]: "" }));
        }}
        onChange={(index, next) => {
          setDrafts((current) => current.map((item, i) => (i === index ? { ...item, ...next } : item)));
        }}
        onRemove={(index) => {
          setDrafts((current) => current.filter((_, i) => i !== index));
          setStatus((current) => ({ ...current, [kind]: "" }));
        }}
        onSave={() => ignoreAsyncResult(save(kind))}
        requiredByDefault={!isInputs}
        saveLabel={isInputs ? t("react.registry.display_save_inputs", "Save Inputs") : t("react.registry.display_save_outputs", "Save Outputs")}
        saveStatus={status[kind]}
        saving={savingKind === kind || query.isFetching}
        showDataContract={false}
        title={isInputs
          ? t("react.registry.agent_own_inputs", "Own Inputs")
          : t("react.registry.agent_own_outputs", "Own Outputs")}
      />
    );
  }

  return (
    <>
      <ContractPanel
        emptyMessage={t("react.registry.agent_no_inherited_inputs", "No inputs come from skills or tools.")}
        rows={materializedInputs.filter(isInherited)}
        title={t("react.registry.agent_inherited_inputs", "Inherited Inputs")}
      />
      {editor("inputs")}
      <ContractPanel
        emptyMessage={t("react.registry.agent_no_inherited_outputs", "No outputs come from skills or tools.")}
        rows={materializedOutputs.filter(isInherited)}
        title={t("react.registry.agent_inherited_outputs", "Inherited Outputs")}
      />
      {editor("outputs")}
    </>
  );
}
