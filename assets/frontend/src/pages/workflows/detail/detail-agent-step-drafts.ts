import { useLayoutEffect, useMemo, useRef, useReducer } from "react";
import { agentBindingPatch, agentMapForEdit, agentRecord, effectiveAgentBlockKey, parseAgentMap, type AgentBindingMode, type AgentMapDirection, type AgentParticipant } from "./detail-agent-step-fields";

export type AgentDraftToken = { owner: string; editor: number; request: number; version: number };
type MapDraft = { text: string; touched: boolean; error: boolean };
function initial(raw: Record<string, unknown>) {
  const key = typeof raw.participant_key === "string" ? raw.participant_key : "";
  const map = (direction: AgentMapDirection): MapDraft => ({ text: JSON.stringify(Object.hasOwn(raw, `${direction}_mapping_jsonb`) ? raw[`${direction}_mapping_jsonb`] : agentMapForEdit(raw, direction), null, 2) ?? "{}", touched: false, error: false });
  return { raw, maps: { input: map("input"), output: map("output") }, mode: (key ? "participant" : "direct") as AgentBindingMode, key, bindingTouched: false, chosenAgentId: "", blockTouched: false, overrideTouched: false };
}
export function useAgentStepDrafts({ owner, raw, createContext }: { owner: string; raw: Record<string, unknown>; createContext: unknown }) {
  const [, refresh] = useReducer((version: number) => version + 1, 0);
  const ownerRef = useRef(owner); ownerRef.current = owner;
  const rawRef = useRef(raw); rawRef.current = raw;
  const state = useRef({ owner, editor: 0, request: 0, version: 0, ...initial(raw) });
  const pending = useRef<AgentDraftToken | null>(null);
  const controls = useRef(new Map<AgentMapDirection, HTMLTextAreaElement>());
  const api = useMemo(() => {
    const redraw = () => refresh();
    const dirty = () => state.current.overrideTouched || state.current.bindingTouched || state.current.blockTouched || Object.values(state.current.maps).some((map) => map.touched);
    const current = (token: AgentDraftToken) => token.owner === ownerRef.current && token.owner === state.current.owner && token.editor === state.current.editor && token.request === state.current.request;
    const unedited = (token: AgentDraftToken) => current(token) && token.version === state.current.version;
    const capture = (): AgentDraftToken => ({ owner: state.current.owner, editor: state.current.editor, request: state.current.request, version: state.current.version });
    const reset = (next: Record<string, unknown> = state.current.raw) => {
      state.current = { owner: ownerRef.current, editor: state.current.editor + 1, request: state.current.request + 1, version: 0, ...initial(next) };
      pending.current = null; redraw();
    };
    const touch = () => { state.current.version += 1; redraw(); };
    return {
      get maps() { return state.current.maps; }, get mode() { return state.current.mode; }, get participantKey() { return state.current.key; },
      get editorKey() { return `${state.current.owner}:${state.current.editor}`; },
      get dirty() { return dirty(); }, get invalid() { return Object.values(state.current.maps).some((map) => map.touched && map.error); },
      capture, isCurrent: current, isUnedited: unedited, reset,
      acknowledgeStaged(token: AgentDraftToken, next: Record<string, unknown>): boolean {
        if (!unedited(token)) return false;
        // Local staging is the new baseline; retire server requests for the prior draft.
        reset(next);
        return true;
      },
      reconcile(next: Record<string, unknown>) {
        if (pending.current && unedited(pending.current)) { state.current = { ...state.current, ...initial(next) }; redraw(); }
        else if (!dirty()) reset(next);
      },
      mayReconcileReadback: () => !dirty() || Boolean(pending.current && unedited(pending.current)),
      beginRequest() { state.current.request += 1; pending.current = capture(); return pending.current; },
      endRequest(token: AgentDraftToken) { if (pending.current?.editor === token.editor && pending.current.request === token.request) pending.current = null; },
      touchOverride() { state.current.overrideTouched = true; touch(); },
      setControl(direction: AgentMapDirection, element: HTMLTextAreaElement | null) { if (element) controls.current.set(direction, element); else controls.current.delete(direction); },
      focusInvalid() { const direction = (["input", "output"] as const).find((name) => state.current.maps[name].touched && state.current.maps[name].error); if (direction) controls.current.get(direction)?.focus(); },
      editMap(direction: AgentMapDirection, text: string) {
        let error = false; try { parseAgentMap(text); } catch { error = true; }
        state.current.maps[direction] = { text, touched: true, error }; touch();
      },
      editMode(mode: AgentBindingMode, agentId: string) { state.current.mode = mode; state.current.bindingTouched = true; state.current.chosenAgentId = agentId; touch(); },
      editDirect() { state.current.bindingTouched = true; touch(); },
      editParticipant(key: string, agentId: string) { state.current.key = key; state.current.chosenAgentId = agentId; state.current.bindingTouched = true; touch(); },
      touchBlock() { state.current.blockTouched = true; touch(); },
      snapshot(directAgentId: string, override: Record<string, unknown>, participants: AgentParticipant[]) {
        const patch = agentBindingPatch({ touched: state.current.bindingTouched, mode: state.current.mode, key: state.current.key, directAgentId, chosenAgentId: state.current.chosenAgentId, participants });
        for (const direction of ["input", "output"] as const) if (state.current.maps[direction].touched) patch[`${direction}_mapping_jsonb`] = parseAgentMap(state.current.maps[direction].text);
        if (state.current.blockTouched || effectiveAgentBlockKey(override) !== effectiveAgentBlockKey(agentRecord(state.current.raw.override))) patch.block_key = effectiveAgentBlockKey(override);
        return patch;
      },
    };
  }, []);
  useLayoutEffect(() => { api.reset(rawRef.current); }, [owner, createContext, api]);
  const signature = JSON.stringify(raw);
  useLayoutEffect(() => { if (api.mayReconcileReadback()) api.reconcile(rawRef.current); }, [signature, api]);
  return api;
}
export type AgentStepDrafts = ReturnType<typeof useAgentStepDrafts>;
