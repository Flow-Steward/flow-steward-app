import type { ExtensionCallbackRegistration } from "./api";
import { stringFrom } from "./extension-manifest-utils";

export type CallbackSetupCredential = { callbackUrl: string; verifyToken?: string };
export type CallbackSetupCredentials = Record<string, CallbackSetupCredential>;

export function callbackSetupTarget(
  component: Record<string, unknown>,
  declarations: Array<Record<string, unknown>>,
  selectedConnectionId = "",
) {
  const callbackId = stringFrom(component.callback_id || component.callbackId);
  const connectionScoped = declarations.some((declaration) => (
    declaration.callback_id === callbackId && declaration.connection_scoped === true
  ));
  return {
    callbackId,
    connectionId: connectionScoped ? selectedConnectionId : "",
    connectionRequired: connectionScoped,
    manual: component.setup_mode === "manual",
  };
}

export function callbackSetupCredentialKey(component: Record<string, unknown>, connectionId: string) {
  return JSON.stringify([
    stringFrom(component.component_id || component.id || component.key || component.type),
    stringFrom(component.callback_id || component.callbackId),
    connectionId,
  ]);
}

export function callbackSetupRegistration(
  registrations: ExtensionCallbackRegistration[],
  callbackId: string,
  connectionId: string,
) {
  return registrations.find((item) => item.callback_id === callbackId
    && stringFrom(item.connection_id) === connectionId);
}
