import { type ReactNode, type SubmitEvent, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { DsAlert, DsCheckbox, DsDialog, DsSelect, SensitiveInput } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { isHttpError } from "@/lib/http/errors";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { installMcpCatalogItem, MCP_CATALOG_QUERY_KEY, type McpCatalogInstallResult, type McpCatalogItem, type McpCatalogRemote, type McpCatalogVariable } from "./api";
import { keyValueObject, type KeyValueRow, RESERVED_OAUTH_PARAMETER_IDENTITIES } from "./key-value-rows";
import { KeyValueRepeater } from "./mcp-list-page-components";
import { rowsFromRecord, type ScopeRow, scopeRows } from "./mcp-list-page-helpers";
import { McpCatalogIcon, ExternalAnchor } from "./mcp-catalog-presentation";
import { signInLabel, type McpProjectOption } from "./mcp-catalog-model";

function changedCatalogItem(error: unknown): McpCatalogItem | null {
  if (!isHttpError(error) || error.status !== 409) return null;
  const payload = (error.payload ?? {}) as Record<string, unknown>;
  const nested = (payload.error ?? {}) as Record<string, unknown>;
  const details = (payload.details ?? nested.details ?? {}) as Record<string, unknown>;
  const context = (details.context ?? {}) as Record<string, unknown>;
  for (const candidate of [payload.item, nested.item, context.item]) {
    if (candidate && typeof candidate === "object" && "item_id" in candidate) {
      return candidate as McpCatalogItem;
    }
  }
  return null;
}

/** A reviewer's label, else the variable name made readable ("instance_host" reads "Instance host"). */
function variableLabel(variable: McpCatalogVariable): string {
  if (variable.label) return variable.label;
  const words = variable.name.replace(/[_-]+/gu, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function remoteDefaults(remote: McpCatalogRemote | undefined): Record<string, string> {
  const values: Record<string, string> = {};
  for (const variable of remote?.variables ?? []) {
    if (remote?.url.includes(`{${variable.name}}`)) values[variable.name] = variable.default || variable.choices[0] || "";
  }
  return values;
}

/**
 * The install form: what the catalog already knows is shown, not asked. The
 * user picks an endpoint when there is more than one, fills the variables and
 * headers the server declares, and for a service without dynamic client
 * registration enters their own OAuth client.
 */
export function McpCatalogInstallDialog({
  item,
  projectId: defaultProjectId,
  projects,
  categoryLabel,
  onClose,
  onInstalled,
}: Readonly<{
  item: McpCatalogItem;
  /** The project the page is filtered to, chosen first; "" lets the user pick. */
  projectId: string;
  projects: readonly McpProjectOption[];
  categoryLabel: string;
  onClose: () => void;
  onInstalled: (result: McpCatalogInstallResult, item: McpCatalogItem, projectId: string) => void;
}>) {
  const { t } = useI18n();
  const [projectId, setProjectId] = useState(defaultProjectId || (projects.length === 1 ? projects[0]?.id ?? "" : ""));
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState(item);
  const [remoteId, setRemoteId] = useState(item.remotes[0]?.remote_id ?? "");
  const remote = current.remotes.find((candidate) => candidate.remote_id === remoteId) ?? current.remotes[0];
  const [displayName, setDisplayName] = useState("");
  const [variables, setVariables] = useState<Record<string, string>>(() => remoteDefaults(item.remotes[0]));
  const [headers, setHeaders] = useState<Record<string, string>>({});
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [error, setError] = useState("");
  const [changed, setChanged] = useState(false);
  const firstFieldRef = useRef<HTMLInputElement | null>(null);
  // The catalog's OAuth settings for this service, shown and editable: the user
  // sees what the preset fills in and how another service would be set up.
  const [scopes, setScopes] = useState<ScopeRow[]>(() => scopeRows(item.remotes[0]?.auth.requested_scopes ?? []));
  const [parameters, setParameters] = useState<KeyValueRow[]>(() => rowsFromRecord(item.remotes[0]?.auth.authorization_parameters));
  const [refreshRequired, setRefreshRequired] = useState(Boolean(item.remotes[0]?.auth.refresh_token_required));
  const parametersRef = useRef<HTMLDivElement | null>(null);

  // Variables, headers and sign-in settings belong to one endpoint; switching starts them over.
  const selectRemote = (nextId: string) => {
    const next = current.remotes.find((candidate) => candidate.remote_id === nextId);
    setRemoteId(nextId);
    setVariables(remoteDefaults(next));
    setHeaders({});
    setScopes(scopeRows(next?.auth.requested_scopes ?? []));
    setParameters(rowsFromRecord(next?.auth.authorization_parameters));
    setRefreshRequired(Boolean(next?.auth.refresh_token_required));
  };

  const usesClient = remote?.auth.mode === "oauth2" && remote.auth.client_registration === "user_supplied";
  const shownHeaders = (remote?.headers ?? []).filter(
    (header) => remote?.auth.mode !== "oauth2" || !["authorization", "proxy-authorization", "x-api-key", "api-key", "apikey"].includes(header.name.toLowerCase()),
  );
  const urlVariables = (remote?.variables ?? []).filter((variable) => remote?.url.includes(`{${variable.name}}`));

  const usesOauth = remote?.auth.mode === "oauth2";
  const installedHere = current.installed.filter((connector) => !connector.project_id || connector.project_id === projectId);
  const parametersLabel = t("react.registry.authorization_parameters", "Authorization parameters");
  const install = useMutation({
    mutationFn: () =>
      installMcpCatalogItem(
        current.item_id,
        {
          remote_id: remote?.remote_id ?? "",
          item_revision: current.revision,
          display_name: displayName.trim(),
          variables,
          headers: Object.fromEntries(Object.entries(headers).filter(([, value]) => value.trim())),
          client_id: usesClient ? clientId.trim() : "",
          client_secret: usesClient ? clientSecret : "",
          ...(usesOauth
            ? {
                requested_scopes: scopes.map((row) => row.value.trim()).filter(Boolean),
                authorization_parameters: keyValueObject(parameters, parametersLabel, RESERVED_OAUTH_PARAMETER_IDENTITIES, t),
                refresh_token_required: refreshRequired,
              }
            : {}),
        },
        projectId,
      ),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["registry", "mcp"] });
      onInstalled(result, current, projectId);
    },
    onError: (failure) => {
      const latest = changedCatalogItem(failure);
      if (latest) {
        setCurrent(latest);
        setChanged(true);
        setError("");
        ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: MCP_CATALOG_QUERY_KEY }));
        return;
      }
      setError(
        failure instanceof Error
          ? failure.message
          : t("react.registry.mcp_catalog.install_failed", "The server could not be connected."),
      );
    },
  });

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setChanged(false);
    if (!projectId) {
      setError(t("react.registry.choose_a_project", "Choose a project."));
      return;
    }
    install.mutate();
  };

  let signInNote: ReactNode = null;
  if (remote?.auth.mode === "oauth2" && !usesClient) {
    signInNote = t(
      "react.registry.mcp_catalog.oauth_after_save",
      "After saving, choose Start Authorization on the connector page and sign in to {value0} in a new window.",
      { value0: current.title },
    );
  } else if (usesClient) {
    signInNote = t(
      "react.registry.mcp_catalog.oauth_own_client",
      "{value0} needs your own OAuth client. Create it first, then enter its ID and secret here or later on the connector page.",
      { value0: current.title },
    );
  } else if (remote?.auth.mode === "none") {
    signInNote = t("react.registry.mcp_catalog.no_sign_in", "This server needs no sign-in.");
  }

  return (
    <DsDialog
      busy={install.isPending}
      closeLabel={t("react.registry.mcp_catalog.close_install", "Close install form")}
      description={current.summary}
      eyebrow={categoryLabel}
      footer={
        <>
          <button className="fs-button" disabled={install.isPending} onClick={onClose} type="button">
            {t("extensions.repeater.cancel", "Cancel")}
          </button>
          <button className="fs-button primary" disabled={install.isPending || !remote} form="mcp-catalog-install-form" type="submit">
            {install.isPending
              ? t("settings.retention.saving", "Saving...")
              : t("react.registry.mcp_catalog.install_submit", "Install")}
          </button>
        </>
      }
      initialFocusRef={firstFieldRef}
      onClose={onClose}
      open
      title={current.title}
    >
      <form className="fs-form-grid" id="mcp-catalog-install-form" noValidate onSubmit={submit}>
        <div className="fs-field full fs-mcp-catalog-install-head">
          <McpCatalogIcon hasIcon={current.has_icon} itemId={current.item_id} title={current.title} />
          {current.description && current.description !== current.summary ? (
            <span className="fs-field-helper">{current.description}</span>
          ) : null}
          {current.official ? (
            <ExternalAnchor href={current.official.evidence_url}>
              {t("react.registry.mcp_catalog.official_by", "Official server, documented by {value0}", { value0: current.official.vendor })}
            </ExternalAnchor>
          ) : null}
          {current.preview_note ? <span className="fs-field-helper">{current.preview_note}</span> : null}
        </div>
        {changed ? (
          <div className="fs-field full">
            <DsAlert
              title={t("react.registry.mcp_catalog.changed", "This catalog entry changed while the form was open. Check it and install again.")}
              tone="warning"
            />
          </div>
        ) : null}
        <div className="fs-field full">
          <span className="fs-field-label">
            {t("events.columns.project", "Project")}
            <span aria-hidden="true" className="fs-required"> *</span>
          </span>
          <DsSelect
            ariaLabel={t("events.columns.project", "Project")}
            onChange={setProjectId}
            options={projects.map((project) => ({ value: project.id, label: project.name || project.id }))}
            placeholder={t("extensions.detail.select_project", "Select a project")}
            value={projectId}
          />
        </div>
        {installedHere.length ? (
          <div className="fs-field full">
            <span className="fs-field-helper">
              {t("react.registry.mcp_catalog.already_connected", "Already connected in this project: {value0}. Installing again adds another connection, for example to a second workspace.", {
                value0: installedHere.map((connector) => connector.display_name).join(", "),
              })}
            </span>
          </div>
        ) : null}
        {current.remotes.length > 1 ? (
          <div className="fs-field full">
            <span className="fs-field-label">{t("react.registry.mcp_catalog.endpoint", "Connect with")}</span>
            <DsSelect
              ariaLabel={t("react.registry.mcp_catalog.endpoint", "Connect with")}
              onChange={selectRemote}
              options={current.remotes.map((candidate) => ({
                value: candidate.remote_id,
                label: `${candidate.label} · ${signInLabel(candidate.auth.mode, t)}`,
              }))}
              value={remote?.remote_id ?? ""}
            />
          </div>
        ) : null}
        <div className="fs-field full">
          <label className="fs-field-label" htmlFor="mcp-catalog-install-name">{t("settings.mcp.fields.name", "Name")}</label>
          <input
            className="fs-input"
            id="mcp-catalog-install-name"
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder={current.remotes.length > 1 && remote ? `${current.title} (${remote.label})` : current.title}
            ref={firstFieldRef}
            value={displayName}
          />
        </div>
        {urlVariables.map((variable) => (
          <div className="fs-field full" key={variable.name}>
            <label className="fs-field-label" htmlFor={`mcp-catalog-variable-${variable.name}`}>
              {variableLabel(variable)}
              <span aria-hidden="true" className="fs-required"> *</span>
            </label>
            {variable.choices.length ? (
              <DsSelect
                ariaLabel={variableLabel(variable)}
                id={`mcp-catalog-variable-${variable.name}`}
                onChange={(value) => setVariables((values) => ({ ...values, [variable.name]: value }))}
                options={variable.choices.map((choice) => ({ value: choice, label: choice }))}
                value={variables[variable.name] ?? ""}
              />
            ) : (
              <input
                className="fs-input"
                id={`mcp-catalog-variable-${variable.name}`}
                onChange={(event) => setVariables((values) => ({ ...values, [variable.name]: event.target.value }))}
                required
                value={variables[variable.name] ?? ""}
              />
            )}
            {variable.description ? <span className="fs-field-helper">{variable.description}</span> : null}
            {remote?.user_defined_host ? (
              <span className="fs-field-helper">
                {t("react.registry.mcp_catalog.user_defined_host", "You set this address. Flow Steward connects to the host you enter.")}
              </span>
            ) : null}
          </div>
        ))}
        {shownHeaders.map((header) => (
          <div className="fs-field full" key={header.name}>
            <label className="fs-field-label" htmlFor={`mcp-catalog-header-${header.name}`}>
              {header.label || header.name}
              {header.required || remote?.auth.mode === "headers" ? <span aria-hidden="true" className="fs-required"> *</span> : null}
            </label>
            {header.secret ? (
              <SensitiveInput
                ariaLabel={header.label || header.name}
                autoComplete="off"
                className="fs-input"
                id={`mcp-catalog-header-${header.name}`}
                onChange={(event) => setHeaders((values) => ({ ...values, [header.name]: event.target.value }))}
                value={headers[header.name] ?? ""}
              />
            ) : (
              <input
                className="fs-input"
                id={`mcp-catalog-header-${header.name}`}
                onChange={(event) => setHeaders((values) => ({ ...values, [header.name]: event.target.value }))}
                value={headers[header.name] ?? ""}
              />
            )}
            {header.description ? <span className="fs-field-helper">{header.description}</span> : null}
          </div>
        ))}
        {usesClient ? (
          <>
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="mcp-catalog-client-id">{t("react.registry.mcp_catalog.client_id", "OAuth client ID")}</label>
              <input
                autoComplete="off"
                className="fs-input"
                id="mcp-catalog-client-id"
                onChange={(event) => setClientId(event.target.value)}
                value={clientId}
              />
            </div>
            <div className="fs-field full">
              <label className="fs-field-label" htmlFor="mcp-catalog-client-secret">{t("react.registry.mcp_catalog.client_secret", "OAuth client secret")}</label>
              <SensitiveInput
                ariaLabel={t("react.registry.mcp_catalog.client_secret", "OAuth client secret")}
                autoComplete="off"
                className="fs-input"
                id="mcp-catalog-client-secret"
                onChange={(event) => setClientSecret(event.target.value)}
                value={clientSecret}
              />
            </div>
          </>
        ) : null}
        {usesOauth ? (
          <fieldset className="fs-field full m-0 min-w-0 border-0 p-0" data-mcp-catalog-signin>
            <legend className="fs-field-label">{t("react.registry.mcp_catalog.signin_settings", "Sign-in settings")}</legend>
            <span className="fs-field-helper">
              {t(
                "react.registry.mcp_catalog.signin_settings_help",
                "Filled in for {value0} from the catalog. Change them only if your account needs it; they are saved on the connector and can be edited there later.",
                { value0: current.title },
              )}
            </span>
            <div className="fs-field full" role="group" aria-label={t("react.registry.requested_scopes", "Requested scopes")}>
              <span className="fs-field-label">{t("react.registry.requested_scopes", "Requested scopes")}</span>
              {scopes.map((scopeRow, index) => (
                <div className="fs-form-grid fs-mcp-key-value-row" key={scopeRow.id}>
                  <input
                    aria-label={t("react.registry.requested_scope_value0", "Requested scope {value0}", { value0: index + 1 })}
                    className="fs-input"
                    onChange={(event) => setScopes((rows) => rows.map((row) => (row.id === scopeRow.id ? { ...row, value: event.target.value } : row)))}
                    value={scopeRow.value}
                  />
                  <button className="fs-button secondary" onClick={() => setScopes((rows) => rows.filter((row) => row.id !== scopeRow.id))} type="button">
                    {t("extensions.repeater.remove", "Remove")}
                  </button>
                </div>
              ))}
              {scopes.length === 0 ? (
                <span className="fs-field-helper">
                  {t("react.registry.mcp_catalog.scopes_from_server", "None set: Flow Steward asks for the scopes the server names when you sign in.")}
                </span>
              ) : null}
              <button className="fs-button secondary" onClick={() => setScopes((rows) => [...rows, ...scopeRows([""])])} type="button">
                {t("react.registry.add_requested_scope", "Add requested scope")}
              </button>
            </div>
            <KeyValueRepeater
              label={parametersLabel}
              onChange={setParameters}
              reservedKeys={RESERVED_OAUTH_PARAMETER_IDENTITIES}
              rows={parameters}
              wrapperRef={parametersRef}
            />
            <div className="fs-field full">
              <DsCheckbox checked={refreshRequired} onChange={setRefreshRequired}>
                {t("react.registry.require_a_refresh_token_for_oauth_completion", "Require a refresh token for OAuth completion.")}
              </DsCheckbox>
            </div>
          </fieldset>
        ) : null}
        {signInNote ? (
          <div className="fs-field full">
            <span className="fs-field-helper">{signInNote}</span>
            {remote?.auth.setup_url || current.help_url ? (
              <ExternalAnchor href={remote?.auth.setup_url || current.help_url}>
                {t("react.registry.mcp_catalog.setup_guide", "Setup guide")}
              </ExternalAnchor>
            ) : null}
          </div>
        ) : null}
        {error ? (
          <div className="fs-field full" id="mcp-catalog-install-error">
            <DsAlert title={error} tone="danger" />
          </div>
        ) : null}
      </form>
    </DsDialog>
  );
}
