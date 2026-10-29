import { normalizeString, type RegistryEntity } from "./catalog-surface-helpers";

export function agentTypeOptionValue(row: RegistryEntity): string {
  return normalizeString(row.key || row.agent_type_key || row.agent_type_id || row.id || row.role_type);
}

export function sourceAgentTypeValue(
  source: RegistryEntity | undefined,
  agentTypes: RegistryEntity[],
): string {
  if (!source) {
    return "";
  }
  const candidates = [source.agent_type_key, source.agent_type_id, source.role_type]
    .map(normalizeString)
    .filter(Boolean);
  const matchingType = agentTypes.find((item) => {
    const typeValues = [item.key, item.agent_type_key, item.agent_type_id, item.id, item.role_type]
      .map(normalizeString)
      .filter(Boolean);
    return candidates.some((candidate) => typeValues.includes(candidate));
  });
  return matchingType ? agentTypeOptionValue(matchingType) : "";
}

export function cloneAgentTypeOptions(
  agentTypes: RegistryEntity[],
  sourceTypeId: string,
  selectedTypeId: string,
): Array<{ value: string; label: string; disabled?: boolean }> {
  return agentTypes.flatMap((item) => {
    const value = agentTypeOptionValue(item);
    if (!value) {
      return [];
    }
    const isCurrentSourceType = value === sourceTypeId;
    if (item.enabled === false && !isCurrentSourceType && value !== selectedTypeId) {
      return [];
    }
    return [{
      value,
      label: normalizeString(item.label || item.display_name) || value,
      disabled: item.enabled === false,
    }];
  });
}
