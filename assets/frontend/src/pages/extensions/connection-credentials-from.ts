import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { listFromUnknown, objectOrEmpty, stringFrom } from "@/pages/extensions/extension-manifest-utils";

import { fetchExtensionOAuthConfigurations, type OAuthConfigurationView } from "./extension-oauth-api";
import { errorCode } from "./extension-oauth-setup-model";

/**
 * Connections that take their credentials from an OAuth configuration.
 *
 * A connection type may declare `credentials_from`: it then signs in as the
 * application with values read from one OAuth configuration of the same install
 * and project. The connection stores only which configuration it uses; the
 * borrowed values are never entered on the connection itself.
 */

/** Form value (and request field) naming the configuration a connection uses. */
export const CREDENTIALS_CONFIGURATION_FIELD = "credentials_configuration_id";
export const CREDENTIALS_UNAVAILABLE = "credentials_unavailable";
export const CREDENTIALS_CONFIGURATION_REQUIRED = "credentials_configuration_required";

export type CredentialsFromTarget = {
  into: "config" | "secrets";
  /** The connection config or secret name the value is placed under. */
  as: string;
};

export type ConnectionCredentialsFrom = {
  oauthProviderId: string;
  /** OAuth credential field id -> where it lands on the connection. */
  fields: Record<string, CredentialsFromTarget>;
};

/** One provider's configurations as the connection form offers them. */
export type CredentialsConfigurationOptions = {
  status: "loading" | "error" | "ready";
  configurations: OAuthConfigurationView[];
  /** The page where the provider's configurations are set up, when it has one. */
  setupPageId: string;
  retry?: () => void;
};

function credentialsFromDeclaration(value: unknown): ConnectionCredentialsFrom | null {
  const block = objectOrEmpty(value);
  const oauthProviderId = stringFrom(block.oauth_provider_id).trim();
  if (!oauthProviderId) return null;
  const fields: Record<string, CredentialsFromTarget> = {};
  for (const [fieldId, rawTarget] of Object.entries(objectOrEmpty(block.fields))) {
    const target = objectOrEmpty(rawTarget);
    const into = stringFrom(target.into).trim();
    const name = stringFrom(target.as).trim() || fieldId.trim();
    if (fieldId.trim() && (into === "config" || into === "secrets") && name) {
      fields[fieldId.trim()] = { into, as: name };
    }
  }
  return { oauthProviderId, fields };
}

/** The manifest's connection types that declare `credentials_from`, by type id. */
export function connectionCredentialsFromByType(manifest: unknown): Record<string, ConnectionCredentialsFrom> {
  const result: Record<string, ConnectionCredentialsFrom> = {};
  for (const row of listFromUnknown(objectOrEmpty(manifest).connection_types)) {
    const typeId = stringFrom(row.connection_type_id || row.connection_type).trim();
    const declaration = credentialsFromDeclaration(row.credentials_from);
    if (typeId && declaration) result[typeId] = declaration;
  }
  return result;
}

/** Connection config and secret names whose values come from the configuration. */
export function borrowedConnectionPaths(credentialsFrom: ConnectionCredentialsFrom | null | undefined): Set<string> {
  return new Set(Object.values(credentialsFrom?.fields ?? {}).map((target) => target.as));
}

/** The configuration the form uses: the chosen one, else the only one there is. */
export function chosenCredentialsConfigurationId(
  values: Record<string, unknown>,
  configurations: readonly OAuthConfigurationView[],
): string {
  const chosen = stringFrom(values[CREDENTIALS_CONFIGURATION_FIELD]);
  if (chosen) return chosen;
  return configurations.length === 1 ? configurations[0].configuration_id : "";
}

export function credentialsUnavailable(row: Record<string, unknown> | undefined): boolean {
  return stringFrom(row?.credentials_status) === CREDENTIALS_UNAVAILABLE;
}

export function credentialsUnavailableMessage(t: I18nContextValue["t"] = fallbackContext.t): string {
  return t(
    "react.extensions.credentials_unavailable_configuration_removed_or_incomplete",
    "Credentials unavailable: its sign-in configuration was removed or is incomplete.",
  );
}

export function credentialsConfigurationRequiredMessage(t: I18nContextValue["t"] = fallbackContext.t): string {
  return t(
    "react.extensions.choose_the_sign_in_configuration_this_connection_takes_its_credentials_from",
    "Choose the sign-in configuration this connection takes its credentials from.",
  );
}

/**
 * The message for a refused save the server names by a credentials code, so it
 * can be shown beside the configuration select; "" for any other failure.
 */
export function credentialsConfigurationSaveError(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const code = errorCode(error);
  if (code === CREDENTIALS_CONFIGURATION_REQUIRED) return credentialsConfigurationRequiredMessage(t);
  if (code === CREDENTIALS_UNAVAILABLE) {
    return t(
      "react.extensions.the_chosen_sign_in_configuration_cannot_provide_this_connection_s_credentials",
      "The chosen sign-in configuration cannot provide this connection's credentials. It was removed, belongs to another provider, or is missing a required value. Choose another configuration or complete it.",
    );
  }
  return "";
}

function oauthProviderSetupPages(manifest: unknown): Record<string, string> {
  return Object.fromEntries(
    listFromUnknown(objectOrEmpty(manifest).oauth_providers)
      .map((provider) => [stringFrom(provider.provider_id), stringFrom(provider.setup_page_id)] as const)
      .filter(([providerId, setupPageId]) => Boolean(providerId && setupPageId)),
  );
}

/**
 * For each provider the `credentials_from` declarations name, that install's
 * configurations in the project. Shares the query the OAuth setup page keeps
 * current, so a configuration added there is offered here.
 */
export function useCredentialsConfigurations({
  credentialsFromByType,
  installId,
  manifest,
  projectId,
}: Readonly<{
  credentialsFromByType: Record<string, ConnectionCredentialsFrom>;
  installId: string;
  manifest: unknown;
  projectId: string;
}>): Record<string, CredentialsConfigurationOptions> {
  const setupPages = useMemo(() => oauthProviderSetupPages(manifest), [manifest]);
  const providerIds = useMemo(
    () => [...new Set(Object.values(credentialsFromByType).map((spec) => spec.oauthProviderId))].sort((left, right) => left.localeCompare(right)),
    [credentialsFromByType],
  );
  const queries = useQueries({
    queries: providerIds.map((providerId) => ({
      queryKey: ["extension-oauth", projectId, installId, providerId, "configurations"],
      queryFn: () => fetchExtensionOAuthConfigurations({ projectId, installId, providerId }),
      enabled: Boolean(projectId && installId && providerId),
    })),
  });
  const configurationsByProvider: Record<string, CredentialsConfigurationOptions> = {};
  providerIds.forEach((providerId, index) => {
    const query = queries[index];
    let status: CredentialsConfigurationOptions["status"] = "loading";
    if (query?.isError) status = "error";
    else if (query?.isSuccess) status = "ready";
    configurationsByProvider[providerId] = {
      status,
      configurations: query?.data ?? [],
      setupPageId: setupPages[providerId] ?? "",
      retry: () => { void query?.refetch(); },
    };
  });
  return configurationsByProvider;
}
