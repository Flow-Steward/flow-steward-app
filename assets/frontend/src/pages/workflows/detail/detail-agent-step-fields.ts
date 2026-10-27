export type AgentMapDirection = "input" | "output";
export type AgentBindingMode = "direct" | "participant";
export type AgentParticipant = { key: string; agentId: string };
export function agentRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
export function agentMapForEdit(raw: Record<string, unknown>, direction: AgentMapDirection): Record<string, unknown> {
  const config = agentRecord(agentRecord(raw.override).agent);
  // Agent uses dictionary presence, including {}, unlike Connector's nonempty fallback.
  for (const candidate of [raw[`${direction}_mapping_jsonb`], config[`${direction}_mapping_jsonb`], config[`${direction}_mapping`]]) {
    if (isRecord(candidate)) return candidate;
  }
  return {};
}
export function parseAgentMap(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!isRecord(value)) throw new Error("object_required");
  const finite = (item: unknown): boolean => typeof item === "number" ? Number.isFinite(item)
    : Array.isArray(item) ? item.every(finite) : isRecord(item) ? Object.values(item).every(finite) : true;
  if (!finite(value)) throw new Error("number_not_representable");
  // Preserve the existing browser/API JSON number boundary; no new safe-integer policy.
  return value;
}
export function effectiveAgentBlockKey(override: Record<string, unknown>): string {
  for (const key of ["published_block_key", "block_key"]) {
    const value = override[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}
export function applyAgentBlockIntent(override: Record<string, unknown>, key: string): Record<string, unknown> {
  const next = { ...override };
  delete next.published_block_key;
  delete next.block_key;
  if (key.trim()) next.published_block_key = key.trim();
  return next;
}
export function agentParticipants(workflow: unknown): AgentParticipant[] {
  const values = agentRecord(agentRecord(workflow).settings).participants;
  const bindings = new Map<string, AgentParticipant>();
  for (const value of Array.isArray(values) ? values : []) {
    const row = agentRecord(value);
    const key = typeof row.participant_key === "string" ? row.participant_key.trim() : "";
    const agent = row.agent_id || row.agent;
    const agentId = typeof agent === "string" ? agent.trim() : "";
    // Match current normalization for supported string declarations: last valid
    // duplicate wins; an empty agent_id may use the accepted agent alias.
    if (key && agentId) bindings.set(key, { key, agentId });
  }
  return [...bindings.values()];
}
export function agentBindingPatch({ touched, mode, key, directAgentId, chosenAgentId, participants }: {
  touched: boolean; mode: AgentBindingMode; key: string; directAgentId: string; chosenAgentId: string; participants: AgentParticipant[];
}): Record<string, unknown> {
  if (!touched) return { agent_id: directAgentId };
  if (mode === "direct") return { participant_key: "", agent_id: directAgentId };
  if (!key) return { participant_key: "", agent_id: "" };
  const current = participants.find((row) => row.key === key);
  if (current?.agentId !== chosenAgentId) throw new Error("participant_changed");
  return { participant_key: key, agent_id: chosenAgentId };
}

export function agentPreviewBinding(text: string): { valid: boolean; key: string } {
  try { const value: unknown = JSON.parse(text || "{}"); return isRecord(value) ? { valid: true, key: effectiveAgentBlockKey(value) } : { valid: false, key: "" }; }
  catch { return { valid: false, key: "" }; }
}
