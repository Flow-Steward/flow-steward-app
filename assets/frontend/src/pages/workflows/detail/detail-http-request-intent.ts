import { useLayoutEffect, useMemo, useRef, useReducer } from "react";
import { HTTP_FIELDS, type HttpField } from "./detail-http-request-fields";

export type HttpIntentToken = { owner: string; editor: number; request: number; stepEdit: number; fields: Partial<Record<HttpField, number>> };
export type HttpReadbackIdentity = { workflowId: string; revision: number; signature: string };
type HttpReadbackProof = Omit<HttpReadbackIdentity, "signature"> & { token: HttpIntentToken; signature?: string };
export type HttpRegistration = { owner: string; editor: number; name: string };
export type HttpFieldFeedback = { field: HttpField; message: string; token: HttpIntentToken; focus?: boolean };
type Register = (name: string, error: string, current?: () => boolean) => void;
const dependencies: Partial<Record<HttpField, HttpField[]>> = { auth_ref: ["auth_mode"], url: ["auth_mode"], max_response_bytes: ["response_mode"], artifact_filename: ["response_mode"] };

/** Page-owned intent. Equality of restored values never restores an old token. */
export function useHttpRequestIntent(owner: string, register: Register) {
  const ownerRef = useRef(owner);
  const callback = useRef(register);
  const state = useRef({ owner, editor: 0, request: 0, stepEdit: 0, fields: {} as Partial<Record<HttpField, number>>, raw: new Map<string, string>(), rawGeneration: new Map<string, number>() });
  const pending = useRef<HttpIntentToken | null>(null);
  const acknowledgedReadback = useRef<HttpReadbackProof | null>(null);
  const [, refresh] = useReducer((version: number) => version + 1, 0);
  const api = useMemo(() => {
    const current = (token: HttpIntentToken, field?: HttpField) => token.owner === ownerRef.current && token.owner === state.current.owner && token.editor === state.current.editor && token.request === state.current.request && (!field || [field, ...(dependencies[field] || [])].every((key) => (token.fields[key] ?? 0) === (state.current.fields[key] ?? 0)));
    const reset = () => {
      const old = [...state.current.rawGeneration.keys()];
      pending.current = null;
      acknowledgedReadback.current = null;
      state.current = { owner: ownerRef.current, editor: state.current.editor + 1, request: state.current.request + 1, stepEdit: 0, fields: {}, raw: new Map(), rawGeneration: new Map() };
      old.forEach((name) => callback.current(name, ""));
      refresh();
    };
    const capture = (): HttpIntentToken => ({ owner: state.current.owner, editor: state.current.editor, request: state.current.request, stepEdit: state.current.stepEdit, fields: { ...state.current.fields } });
    const unedited = (token: HttpIntentToken) => current(token) && token.stepEdit === state.current.stepEdit && [...new Set([...Object.keys(token.fields), ...Object.keys(state.current.fields)])].every((field) => (token.fields[field as HttpField] ?? 0) === (state.current.fields[field as HttpField] ?? 0));
    return {
      beginEditorSession: reset,
      capture,
      isCurrent: current,
      isUnedited: unedited,
      ownsPendingReadback: () => Boolean(pending.current && unedited(pending.current)),
      acknowledgeRequest(token: HttpIntentToken, scope: Pick<HttpReadbackIdentity, "workflowId" | "revision">) {
        if (scope.workflowId && Number.isSafeInteger(scope.revision) && scope.revision > 0 && unedited(token)) {
          acknowledgedReadback.current = { token, workflowId: scope.workflowId, revision: scope.revision };
        }
      },
      recordAcknowledgedReadback(token: HttpIntentToken, identity: HttpReadbackIdentity) {
        const proof = acknowledgedReadback.current;
        if (proof?.token === token && proof.workflowId === identity.workflowId && proof.revision === identity.revision &&
          identity.signature && unedited(token)) acknowledgedReadback.current = { token, workflowId: identity.workflowId, revision: identity.revision, signature: identity.signature };
      },
      reconcileAcknowledgedReadback(identity: HttpReadbackIdentity) {
        const proof = acknowledgedReadback.current;
        if (proof?.workflowId !== identity.workflowId || proof.revision !== identity.revision || !unedited(proof.token)) {
          acknowledgedReadback.current = null;
          return false;
        }
        // Before transport settles, the explicit ACK authorizes only its own revision.
        if (pending.current === proof.token) return true;
        acknowledgedReadback.current = null;
        return proof.signature === identity.signature;
      },
      mayReconcileReadback: () => state.current.raw.size === 0 && (!pending.current || unedited(pending.current)),
      // Common step controls retire whole-step Save authority, not unchanged HTTP field errors.
      touchStepParameters() {
        acknowledgedReadback.current = null;
        state.current.stepEdit += 1;
        refresh();
      },
      touchField(field: HttpField) {
        acknowledgedReadback.current = null;
        state.current.fields[field] = (state.current.fields[field] ?? 0) + 1;
        refresh();
      },
      touchOverride() {
        acknowledgedReadback.current = null;
        HTTP_FIELDS.forEach((field) => { state.current.fields[field] = (state.current.fields[field] ?? 0) + 1; });
        refresh();
      },
      beginRequest() { acknowledgedReadback.current = null; state.current.request += 1; pending.current = capture(); return pending.current; },
      endRequest(token: HttpIntentToken) {
        if (pending.current?.editor === token.editor && pending.current.request === token.request) pending.current = null;
        if (acknowledgedReadback.current?.token === token && !acknowledgedReadback.current.signature) acknowledgedReadback.current = null;
      },
      registration(id: string, editor = state.current.editor): HttpRegistration {
        const name = `http:${editor}:${id}`;
        if (editor === state.current.editor && !state.current.rawGeneration.has(name)) state.current.rawGeneration.set(name, 0);
        return { owner: state.current.owner, editor, name };
      },
      raw(reg: HttpRegistration, message: string) {
        const editorOwns = () => reg.owner === ownerRef.current && reg.owner === state.current.owner && reg.editor === state.current.editor;
        if (!editorOwns()) return;
        const generation = (state.current.rawGeneration.get(reg.name) ?? 0) + 1;
        state.current.rawGeneration.set(reg.name, generation);
        const owns = () => editorOwns() && state.current.rawGeneration.get(reg.name) === generation;
        if (message) state.current.raw.set(reg.name, message); else state.current.raw.delete(reg.name);
        callback.current(reg.name, message, owns);
      },
      release(reg: HttpRegistration) {
        state.current.raw.delete(reg.name);
        state.current.rawGeneration.delete(reg.name);
        // Unique editor/control identity; never clears a newer registration.
        callback.current(reg.name, "");
      },
      hasRaw: () => state.current.owner === ownerRef.current && state.current.raw.size > 0,
      firstRaw: () => state.current.raw.values().next().value as string | undefined,
    };
  }, []);
  useLayoutEffect(() => {
    ownerRef.current = owner;
    callback.current = register;
    if (state.current.owner !== owner) api.beginEditorSession();
  }, [api, owner, register]);
  return api;
}
export type HttpRequestIntent = ReturnType<typeof useHttpRequestIntent>;
