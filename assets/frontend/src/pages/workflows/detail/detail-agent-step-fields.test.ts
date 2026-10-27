import { afterEach, expect, it, vi } from "vitest";
import { agentBindingPatch, agentMapForEdit, agentParticipants, applyAgentBlockIntent, effectiveAgentBlockKey, parseAgentMap } from "./detail-agent-step-fields";
import { fetchWorkflowStepContractPreview } from "@/pages/workflows/shared/api";

afterEach(() => vi.restoreAllMocks());
it.each(["input", "output"] as const)("uses Agent dictionary-presence precedence for %s without mutating any alias", (direction) => {
  const top = `${direction}_mapping_jsonb`; const alias = `${direction}_mapping`;
  const raw = { [top]: {}, override: { agent: { [top]: { a: "state.nested" }, [alias]: { b: "state.alias" } } } };
  expect(agentMapForEdit(raw, direction)).toEqual({});
  const absent = { override: raw.override };
  expect(agentMapForEdit(absent, direction)).toEqual({ a: "state.nested" });
  expect(agentMapForEdit({ override: { agent: { [top]: {}, [alias]: { b: "state.alias" } } } }, direction)).toEqual({});
  expect(agentMapForEdit({ override: { agent: { [alias]: { b: "state.alias" } } } }, direction)).toEqual({ b: "state.alias" });
  expect(raw.override.agent[top]).toEqual({ a: "state.nested" });
});
it("preserves complete JSON types and own keys with an explicit object codec", () => {
  const text = '{"__proto__":null,"text":"false","code":"001","nullText":"null","flag":false,"zero":0,"nil":null,"blank":"","array":[0,false,null],"object":{"source":"state.inputs.request"},"selector":"literal:null"}';
  const result = parseAgentMap(text);
  expect(result).toEqual(JSON.parse(text));
  expect(Object.hasOwn(result, "__proto__")).toBe(true); expect(Object.getPrototypeOf(result)).toBe(Object.prototype);
  expect(parseAgentMap("{}")).toEqual({});
});
it.each(['{"x":', '[]', 'null', '{"x":1e999}'])("does not silently serialize invalid/unrepresentable edited map %s", (text) => expect(() => parseAgentMap(text)).toThrow());
it.each([9007199254740992, 9007199254740994, 1e20])("retains the existing finite JSON numeric boundary for exactly representable %s", (value) => {
  expect(parseAgentMap(JSON.stringify({ value }))).toEqual({ value });
});
it("uses actual explicit block precedence and removes both aliases only for an explicit Auto", () => {
  const original = { published_block_key: " block.a ", block_key: "block.old", extra_prompt: "Keep" };
  expect(effectiveAgentBlockKey(original)).toBe("block.a");
  expect(effectiveAgentBlockKey({ published_block_key: " ", block_key: "block.old" })).toBe("block.old");
  expect(applyAgentBlockIntent(original, "block.b")).toEqual({ extra_prompt: "Keep", published_block_key: "block.b" });
  expect(applyAgentBlockIntent(original, "")).toEqual({ extra_prompt: "Keep" });
  expect(original.block_key).toBe("block.old");
});
it("does not add an untouched orphan key, and makes explicit binding intention atomic", () => {
  const participants = agentParticipants({ settings: { participants: [{ participant_key: "writer", agent_id: "writer" }, { participant_key: "writer", agent_id: "other" }, { participant_key: "reviewer", agent_id: "reviewer" }] } });
  const base = { mode: "participant" as const, key: "orphan", directAgentId: "writer", chosenAgentId: "", participants };
  expect(agentBindingPatch({ ...base, touched: false })).toEqual({ agent_id: "writer" });
  expect(agentBindingPatch({ ...base, mode: "direct", touched: true })).toEqual({ participant_key: "", agent_id: "writer" });
  expect(agentBindingPatch({ ...base, key: "reviewer", chosenAgentId: "reviewer", touched: true })).toEqual({ participant_key: "reviewer", agent_id: "reviewer" });
  expect(() => agentBindingPatch({ ...base, key: "reviewer", chosenAgentId: "writer", touched: true })).toThrow("participant_changed");
  expect(agentBindingPatch({ ...base, key: "", touched: true })).toEqual({ participant_key: "", agent_id: "" });
});
it("matches supported participant normalization for duplicate declarations and the agent alias, using settings only", () => {
  expect(agentParticipants({ participants: [{ participant_key: "foreign", agent_id: "foreign" }], settings: { participants: [
    { participant_key: " author ", agent_id: "writer" }, { participant_key: "author", agent_id: "", agent: " reviewer " },
    { participant_key: "author", agent_id: "" }, { participant_key: "other", agent: "writer" },
  ] } })).toEqual([{ key: "author", agentId: "reviewer" }, { key: "other", agentId: "writer" }]);
});
it("encodes explicit Auto separately from omitted legacy preview and retains actual step/participant context", async () => {
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
  await fetchWorkflowStepContractPreview("workflow", { stepId: "agent_a", agentId: "writer", participantKey: "author", blockBindingMode: "auto" });
  const automatic = new URL(String(transport.mock.calls[0][0]), "https://example.test");
  expect(Object.fromEntries(automatic.searchParams)).toEqual({ step_id: "agent_a", agent_id: "writer", participant_key: "author", block_binding_mode: "auto" });
  transport.mockResolvedValueOnce(new Response("{}", { status: 200 }));
  await fetchWorkflowStepContractPreview("workflow", { stepId: "agent_a", agentId: "writer" });
  expect(String(transport.mock.calls.at(-1)?.[0])).not.toContain("block_binding_mode");
});
