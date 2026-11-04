import { useEffect, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsBadge, DsCard, DsRadio, MultiCombobox } from "@/components/shared/design-system";
import { isHttpError } from "@/lib/http/errors";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { AgentSetupSteps, AgentTokenUsage } from "@/pages/settings/account/ai-agents-setup";
import {
  fetchAgentAccessSetup,
  fetchPersonalTokensCollection,
  issueAgentAccessToken,
} from "@/pages/settings/shared/api";
import { oauthNotice, oauthPresetDescription, oauthPresetLabel } from "@/pages/settings/shared/oauth-copy";
import type {
  AgentAccessIssueResult,
  AgentAccessSetup,
  AgentAccessTokenRequest,
  AgentClient,
  OAuthPresetId,
} from "@/pages/settings/shared/types";

type Translate = I18nContextValue["t"];

/** Where one attempt stands. An attempt is one Idempotency-Key: at most one token. */
type IssueState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "done"; result: AgentAccessIssueResult }
  | { kind: "uncertain" }
  | { kind: "failed"; message: string };

type Attempt = { key: string; request: AgentAccessTokenRequest };

function newAttemptKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function blockerNotice(code: string, t: Translate = fallbackContext.t) {
  if (code === "public_address_missing") {
    return {
      title: t("react.agent_access.public_address_missing_title", "This installation has no public address"),
      copy: t("react.agent_access.public_address_missing_copy", "FS_PUBLIC_BASE_URL is not set, so there is no MCP address to give an AI client. An administrator sets it to the address people open Flow Steward with."),
    };
  }
  if (code === "internal_address") {
    return {
      title: t("react.agent_access.internal_address_title", "The MCP address is an internal one"),
      copy: t("react.agent_access.internal_address_copy", "FS_MCP_RESOURCE_URL names a service inside the installation's network, which an AI client cannot reach. An administrator removes the override or sets it to a public address."),
    };
  }
  return oauthNotice(code, t);
}

/** A refusal the user can act on; anything else is "we do not know if it was created". */
function definiteFailure(error: unknown, t: Translate): string | null {
  if (!isHttpError(error) || error.status >= 500) return null;
  const code = (error.payload as { error?: string } | null)?.error || "";
  if (code === "attempt_in_progress") return null;
  if (code === "exceeds_authority") return t("react.agent_access.exceeds_authority", "This access is more than your own role allows.");
  if (code === "project_not_accessible") return t("react.agent_access.project_not_accessible", "One of the selected projects is not available to you.");
  return error.message;
}

/** Settings → Account → AI agents: connect an AI client in five steps (task 4). */
export function AiAgentsSection() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const setupQuery = useQuery({ queryKey: ["settings", "account", "agent-access"], queryFn: fetchAgentAccessSetup, retry: false });
  const setup = setupQuery.data;
  const [clientId, setClientId] = useState("");
  const [packageId, setPackageId] = useState("");
  const [presetId, setPresetId] = useState<OAuthPresetId | "">("");
  const [expiryDays, setExpiryDays] = useState(30);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [issue, setIssue] = useState<IssueState>({ kind: "idle" });
  const [secret, setSecret] = useState("");
  const attempt = useRef<Attempt | null>(null);
  const inFlight = useRef(false);

  const client: AgentClient | undefined = setup?.clients.find((item) => item.id === clientId);
  const choice = client?.packages.find((item) => item.package_id === packageId);
  const packageMeta = setup?.packages.find((item) => item.id === packageId);
  const usesToken = choice?.auth === "personal_token";
  const locked = issue.kind !== "idle" && issue.kind !== "failed";
  const projectsQuery = useQuery({ queryKey: ["settings", "account", "agent-access", "projects"], queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }), enabled: usesToken });
  const projectOptions = (projectsQuery.data?.projects || []).map((project) => ({ value: String(project.id), label: project.name?.trim() || t("react.settings.unnamed_project", "Unnamed project") }));
  const wizardTokensQuery = useQuery({ queryKey: ["settings", "account", "tokens", "agent-access-lookup"], queryFn: () => fetchPersonalTokensCollection(1, 25), enabled: issue.kind === "uncertain" });
  const wizardTokens = (wizardTokensQuery.data?.tokens || []).filter((token) => token.label.startsWith("AI agent"));

  useEffect(() => {
    if (!setup) return;
    setExpiryDays(setup.default_expiry_days);
    const firstAvailable = setup.presets.find((preset) => preset.available);
    setPresetId(firstAvailable ? firstAvailable.id : "");
  }, [setup]);

  const tokenId = issue.kind === "done" ? issue.result.token.id : "";
  const steps = useMemo(() => choice?.steps || [], [choice]);

  function chooseClient(id: string) {
    setClientId(id);
    const usable = setup?.clients.find((item) => item.id === id)?.packages.filter((item) => item.available) || [];
    const operator = usable.find((item) => item.package_id === "flow-steward-operator");
    setPackageId(operator?.package_id || (usable.length === 1 ? usable[0].package_id : ""));
  }

  function startOver() {
    attempt.current = null;
    setSecret("");
    setIssue({ kind: "idle" });
  }

  async function send(current: Attempt) {
    if (inFlight.current) return;
    inFlight.current = true;
    setIssue({ kind: "submitting" });
    try {
      const result = await issueAgentAccessToken(current.key, current.request);
      setSecret(result.secret || "");
      setIssue({ kind: "done", result });
      await queryClient.invalidateQueries({ queryKey: ["settings", "account", "tokens"] });
    } catch (error) {
      const message = definiteFailure(error, t);
      if (message === null) {
        setIssue({ kind: "uncertain" });
      } else {
        // Refused before anything was created: the next click is a new attempt.
        attempt.current = null;
        setIssue({ kind: "failed", message });
      }
    } finally {
      inFlight.current = false;
    }
  }

  function createToken() {
    if (!client || !choice || !presetId || inFlight.current) return;
    attempt.current ??= {
      key: newAttemptKey(),
      request: { client_id: client.id, package_id: choice.package_id, preset: presetId, expiry_days: expiryDays, project_ids: projectIds },
    };
    ignoreAsyncResult(send(attempt.current));
  }

  if (setupQuery.isError) {
    return <DsAlert title={t("react.agent_access.setup_unavailable", "The AI agent setup could not be loaded")} tone="danger">{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert>;
  }
  if (!setup) return <p className="fs-field-helper">{t("react.agent_access.loading", "Loading the AI agent setup…")}</p>;

  const blockers = client?.mcp_blockers || [];
  const mcpBlocked = Boolean(packageMeta?.needs_mcp && blockers.length);
  const loaded: AgentAccessSetup = setup;
  const result = issue.kind === "done" ? issue.result : null;

  function renderAccess() {
    if (!client || !choice || !packageMeta?.needs_mcp) return null;
    if (choice.auth === "oauth") {
      return (
        <DsCard description={t("react.agent_access.oauth_access_description", "{client} connects from its own servers. You choose the access in Flow Steward when it connects: sign in, pick the access, projects and expiry, and approve. No token is created here.", { client: client.label })} title={t("react.agent_access.step3_title", "3. Access")}>
          {blockers.map((code) => { const notice = blockerNotice(code, t); return <DsAlert key={code} title={notice.title} tone="warning">{notice.copy}</DsAlert>; })}
          {blockers.length ? null : <DsAlert title={t("react.agent_access.oauth_ready_title", "Ready for cloud AI apps")} tone="success">{t("react.agent_access.oauth_ready_copy", "After you approve, the access appears under Personal tokens → Connected AI apps, where you can revoke it.")}</DsAlert>}
        </DsCard>
      );
    }
    return (
      <DsCard description={t("react.agent_access.token_access_description", "{client} uses a personal access token from your account. It can never do more than you can.", { client: client.label })} title={t("react.agent_access.step3_title", "3. Access")}>
        {blockers.map((code) => { const notice = blockerNotice(code, t); return <DsAlert key={code} title={notice.title} tone="danger">{notice.copy}</DsAlert>; })}
        <fieldset className="fs-field" disabled={locked}>
          <legend className="fs-field-label">{t("react.agent_access.access_level", "What the agent may do")}</legend>
          {/* Only what this user can choose; an option they cannot pick is not shown. */}
          {loaded.presets.filter((preset) => preset.available).map((preset) => (
            <div className="fs-field" key={preset.id}>
              <DsRadio checked={presetId === preset.id} name="agent-access-preset" onChange={() => setPresetId(preset.id)} value={preset.id}>{oauthPresetLabel(preset.id, t)}</DsRadio>
              <span className="fs-field-helper">{oauthPresetDescription(preset.id, t)} <code className="fs-value-chip">{preset.scopes.join(", ")}</code></span>
            </div>
          ))}
        </fieldset>
        <div className="fs-field">
          <span className="fs-field-label">{t("react.oauth.column_projects", "Projects")}</span>
          <MultiCombobox ariaLabel={t("react.oauth.column_projects", "Projects")} disabled={locked || projectsQuery.isLoading} onChange={setProjectIds} options={projectOptions} placeholder={t("react.settings.search_projects", "Search projects")} selected={projectIds} />
          <span className="fs-field-helper">{t("react.agent_access.projects_help", "Empty means every project within your access. Choosing projects limits runs, jobs and workflows to them; agents and skills belong to the whole account and are not limited by project.")}</span>
        </div>
        <fieldset className="fs-field" disabled={locked}>
          <legend className="fs-field-label">{t("react.settings.expiration", "Expiration")}</legend>
          <div className="fs-radio-grid">{loaded.expiry_days_options.map((days) => <DsRadio checked={expiryDays === days} key={days} name="agent-access-expiry" onChange={() => setExpiryDays(days)} value={String(days)}>{t("react.settings.value1_days", "{value1} days", { value1: String(days) })}</DsRadio>)}</div>
        </fieldset>
        {renderIssue()}
      </DsCard>
    );
  }

  function renderIssue() {
    if (issue.kind === "uncertain") {
      return (
        <DsAlert title={t("react.agent_access.uncertain_title", "We could not confirm whether the token was created")} tone="warning">
          <p>{t("react.agent_access.uncertain_copy", "The answer was lost. Check again: it asks about the same attempt, so it never creates a second token. Tokens this wizard created are listed below; revoke any you do not want.")}</p>
          <ul>{wizardTokens.map((token) => <li key={token.id}>{token.label} · {token.created_at ? new Date(token.created_at).toLocaleString() : ""}</li>)}</ul>
          <div className="fs-footer-actions">
            <button className="fs-button primary" onClick={() => attempt.current && ignoreAsyncResult(send(attempt.current))} type="button">{t("react.agent_access.check_again", "Check again")}</button>
            <a className="fs-button" href="/settings/account?tab=tokens">{t("react.agent_access.open_personal_tokens", "Open personal tokens")}</a>
            <button className="fs-button ghost" onClick={startOver} type="button">{t("react.agent_access.new_attempt", "Start a new attempt")}</button>
          </div>
        </DsAlert>
      );
    }
    if (result) {
      return (
        <>
          {result.secret_already_shown ? (
            <DsAlert title={t("react.agent_access.secret_already_shown_title", "This token already exists; its secret was shown once")} tone="warning">
              <p>{t("react.agent_access.secret_already_shown_copy", "This attempt created the token earlier and the secret cannot be shown again. If you did not save it, revoke the token below and create a new one.")}</p>
            </DsAlert>
          ) : null}
          {secret ? (
            <DsAlert title={t("react.agent_access.token_ready_title", "The token is created and already in the setup below")} tone="success">
              {t("react.agent_access.token_ready_copy", "Download or copy what step 4 shows: nothing else to fill in. This page shows the token only now; after you leave, create another one if you need it.")}
            </DsAlert>
          ) : null}
          <div className="fs-footer-actions"><button className="fs-button ghost" onClick={startOver} type="button">{t("react.agent_access.create_another_token", "Create another token")}</button></div>
          <span className="fs-field-helper">{t("react.agent_access.create_another_token_help", "The token above keeps working until it expires or you revoke it in step 5.")}</span>
        </>
      );
    }
    return (
      <>
        {issue.kind === "failed" ? <DsAlert title={t("react.settings.token_could_not_be_created", "Token could not be created")} tone="danger">{issue.message}</DsAlert> : null}
        <div className="fs-footer-actions">
          <button className="fs-button primary" disabled={issue.kind === "submitting" || !presetId || mcpBlocked} onClick={createToken} type="button">{issue.kind === "submitting" ? t("react.settings.creating", "Creating…") : t("react.settings.create_token", "Create token")}</button>
        </div>
      </>
    );
  }

  return (
    <div className="fs-panel-stack">
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("react.agent_access.title", "AI agents")}</h2><p>{t("react.agent_access.description", "Connect an AI client to Flow Steward: pick the client and the package, get the settings filled in, and make the first request.")}</p></div></div>
      <DsCard title={t("react.agent_access.step1_title", "1. AI client")}>
        <fieldset className="fs-field" disabled={locked}>
          <legend className="fs-field-label">{t("react.agent_access.choose_client", "Which client do you use?")}</legend>
          {setup.clients.map((item) => (
            <div className="fs-field" key={item.id}>
              <DsRadio checked={clientId === item.id} name="agent-access-client" onChange={() => chooseClient(item.id)} value={item.id}>{item.label} <DsBadge>{item.connection === "cloud" ? t("react.agent_access.cloud", "Cloud") : t("react.agent_access.local", "On your computer")}</DsBadge></DsRadio>
              <span className="fs-field-helper">{t(item.description_key, item.description)}</span>
            </div>
          ))}
        </fieldset>
      </DsCard>
      {client ? (
        <DsCard description={t("react.agent_access.package_description", "The package teaches the agent how to work with Flow Steward. It holds no address and no token.")} title={t("react.agent_access.step2_title", "2. Package")}>
          <fieldset className="fs-field" disabled={locked}>
            <legend className="fs-field-label">{t("react.agent_access.choose_package", "What will you do with the agent?")}</legend>
            {/* A package this client cannot use is not shown. */}
            {client.packages.filter((item) => item.available).map((item) => {
              const meta = setup.packages.find((pkg) => pkg.id === item.package_id);
              return (
                <div className="fs-field" key={item.package_id}>
                  <DsRadio checked={packageId === item.package_id} name="agent-access-package" onChange={() => setPackageId(item.package_id)} value={item.package_id}>{meta?.display_name || item.package_id} {item.verified ? null : <DsBadge tone="orange">{t("react.agent_access.not_verified", "Not verified in this client")}</DsBadge>}</DsRadio>
                  <span className="fs-field-helper">{meta ? t(meta.description_key, meta.description) : ""}</span>
                  {item.message_key ? <span className="fs-field-helper">{t(item.message_key, item.message)}</span> : null}
                </div>
              );
            })}
          </fieldset>
        </DsCard>
      ) : null}
      {renderAccess()}
      {client && choice ? (
        <DsCard description={packageMeta?.needs_mcp ? t("react.agent_access.setup_description", "The address is this installation's public MCP address.") : t("react.agent_access.setup_description_no_mcp", "This package does not connect to this installation, so no token is needed.")} title={t("react.agent_access.step4_title", "4. Set up {client}", { client: client.label })}>
          {/* Blocked: only the package steps; no address to paste and no token to use. */}
          <AgentSetupSteps packageId={choice.package_id} secret={secret} steps={mcpBlocked ? steps.filter((step) => !step.uses_mcp_url && !step.uses_token) : steps} />
        </DsCard>
      ) : null}
      {client && choice && packageMeta?.needs_mcp && !mcpBlocked ? (
        <DsCard title={t("react.agent_access.step5_title", "5. First request")}>
          <AgentSetupSteps secret="" steps={[setup.first_request]} />
          {usesToken && tokenId ? <AgentTokenUsage onRevoked={() => setSecret("")} tokenId={tokenId} /> : null}
          {usesToken && !tokenId ? <p className="fs-field-helper">{t("react.agent_access.usage_after_token", "Once the token exists, its expiry, scopes and last use appear here.")}</p> : null}
          {choice.auth === "oauth" ? <p className="fs-field-helper">{t("react.agent_access.usage_oauth", "The approved access, with its expiry, scopes and last use, appears under Personal tokens → Connected AI apps.")}</p> : null}
        </DsCard>
      ) : null}
    </div>
  );
}
