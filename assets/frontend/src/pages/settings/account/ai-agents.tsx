import { useEffect, useId, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsBadge, DsRadio, DsValueChip, MultiCombobox, OptionalLabel } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { isHttpError } from "@/lib/http/errors";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate, formatDateTime } from "@/lib/time/date-time-format";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { AgentCodeBlock } from "@/pages/settings/account/ai-agents-copy";
import { AgentRequestCheck, AgentSetupSteps, AgentStep, AgentTokenStep, type AgentStepStatus } from "@/pages/settings/account/ai-agents-setup";
import {
  fetchAgentAccessSetup,
  fetchPersonalTokensCollection,
  issueAgentAccessToken,
} from "@/pages/settings/shared/api";
import { oauthNotice, oauthPresetDescription, oauthPresetLabel } from "@/pages/settings/shared/oauth-copy";
import type {
  AgentAccessIssueResult,
  AgentAccessTokenRequest,
  AgentClient,
  OAuthPresetId,
} from "@/pages/settings/shared/types";

import "@/pages/settings/account/ai-agents.css";

type Translate = I18nContextValue["t"];

/** Where one attempt stands. An attempt is one Idempotency-Key: at most one token. */
type IssueState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "done"; result: AgentAccessIssueResult }
  | { kind: "uncertain" }
  | { kind: "failed"; message: string };

type Attempt = { key: string; request: AgentAccessTokenRequest };

/** The open step: 1 client, 2 package, 3 access, 4 set up (with 5, the first request). */
type OpenStep = 1 | 2 | 3 | 4;

const MICROSOFT_365_CLIENT_ID = "microsoft_365_copilot";
const DAY_MS = 24 * 60 * 60 * 1000;

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

function connectionLabel(client: AgentClient, t: Translate) {
  return client.connection === "cloud" ? t("react.agent_access.in_the_cloud", "In the cloud") : t("react.agent_access.local", "On your computer");
}

/**
 * Settings → Account → AI agents: connect an AI client in five steps. Each step
 * is a numbered card; a finished one folds into its choice with Change, and the
 * token, once created, stays with the account whatever client is chosen next.
 */
export function AiAgentsSection() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const localGroupId = useId();
  const cloudGroupId = useId();
  const projectsLabelId = useId();
  const setupQuery = useQuery({ queryKey: ["settings", "account", "agent-access"], queryFn: fetchAgentAccessSetup, retry: false });
  const setup = setupQuery.data;
  const [openStep, setOpenStep] = useState<OpenStep>(1);
  const setupLoaderShown = useLoaderDelay(!setup && !setupQuery.isError);
  const [clientId, setClientId] = useState("");
  const [clientMissing, setClientMissing] = useState(false);
  const [packageId, setPackageId] = useState("");
  const [packageMissing, setPackageMissing] = useState(false);
  const [presetId, setPresetId] = useState<OAuthPresetId | "">("");
  const [expiryDays, setExpiryDays] = useState(30);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [issue, setIssue] = useState<IssueState>({ kind: "idle" });
  const [secret, setSecret] = useState("");
  const attempt = useRef<Attempt | null>(null);
  const inFlight = useRef(false);

  const client: AgentClient | undefined = setup?.clients.find((item) => item.id === clientId);
  const choice = client?.packages.find((item) => item.package_id === packageId && item.available);
  const packageMeta = setup?.packages.find((item) => item.id === choice?.package_id);
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

  const result = issue.kind === "done" ? issue.result : null;
  const tokenId = result?.token.id || "";
  const steps = useMemo(() => choice?.steps || [], [choice]);

  function chooseClient(id: string) {
    setClientId(id);
    setClientMissing(false);
    const usable = setup?.clients.find((item) => item.id === id)?.packages.filter((item) => item.available) || [];
    // Another client keeps the package chosen before, when it offers that package.
    if (usable.some((item) => item.package_id === packageId)) return;
    const operator = usable.find((item) => item.package_id === "flow-steward-operator");
    setPackageId(operator?.package_id || (usable.length === 1 ? usable[0].package_id : ""));
  }

  function continueFromClient() {
    if (!client) {
      setClientMissing(true);
      return;
    }
    setOpenStep(2);
  }

  function continueFromPackage() {
    if (!choice) {
      setPackageMissing(true);
      return;
    }
    // A package without MCP needs no access; a token already created is still there.
    setOpenStep(!packageMeta?.needs_mcp || (usesToken && result) ? 4 : 3);
  }

  function startOver() {
    attempt.current = null;
    setSecret("");
    setIssue({ kind: "idle" });
  }

  function createAnotherToken() {
    startOver();
    setOpenStep(3);
  }

  async function send(current: Attempt) {
    if (inFlight.current) return;
    inFlight.current = true;
    setIssue({ kind: "submitting" });
    try {
      const issued = await issueAgentAccessToken(current.key, current.request);
      setSecret(issued.secret || "");
      setIssue({ kind: "done", result: issued });
      setOpenStep(4);
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
  if (!setup || setupLoaderShown) return setupLoaderShown ? <p className="fs-field-helper"><DsLoader label={t("react.agent_access.loading", "Loading the AI agent setup…")} variant="orbit-inline" /></p> : null;

  const blockers = client?.mcp_blockers || [];
  const needsAccess = Boolean(packageMeta?.needs_mcp);
  const mcpBlocked = Boolean(needsAccess && blockers.length);
  const titles = {
    client: t("react.agent_access.step_client_title", "AI client"),
    package: t("react.agent_access.step_package_title", "Package"),
    access: t("react.agent_access.step_access_title", "Access"),
    setup: client ? t("react.agent_access.step_setup_title", "Set up {client}", { client: client.label }) : t("react.agent_access.step_setup_title_no_client", "Set up the client"),
    firstRequest: t("react.agent_access.step_first_request_title", "First request"),
  };
  const notNeeded = t("react.agent_access.access_not_needed", "Not needed: the Developer package does not connect to this installation.");
  const changing = issue.kind === "submitting";

  function statusOf(step: OpenStep | 5): AgentStepStatus {
    if (step === openStep) return "current";
    if (step === 5) {
      if (openStep !== 4) return "next";
      if (!needsAccess) return "skipped";
      return mcpBlocked ? "next" : "open";
    }
    if (step > openStep) return "next";
    return step === 3 && !needsAccess ? "skipped" : "done";
  }

  function changeButton(step: OpenStep, name: string) {
    return (
      <button aria-label={t("react.agent_access.change_step", "Change {step}", { step: name })} className="fs-button ghost sm" disabled={changing} onClick={() => setOpenStep(step)} type="button">
        {t("react.agent_access.change", "Change")}
      </button>
    );
  }

  function renderClientStep() {
    const status = statusOf(1);
    if (status === "done" && client) {
      return <AgentStep actions={changeButton(1, titles.client)} number={1} status="done" summary={`${client.label} · ${connectionLabel(client, t)}`} title={titles.client} />;
    }
    const groups = [
      { id: localGroupId, label: t("react.agent_access.local", "On your computer"), clients: setup?.clients.filter((item) => item.connection !== "cloud") || [] },
      { id: cloudGroupId, label: t("react.agent_access.in_the_cloud", "In the cloud"), clients: setup?.clients.filter((item) => item.connection === "cloud") || [] },
    ].filter((group) => group.clients.length);
    return (
      <AgentStep number={1} status="current" summary={t("react.agent_access.choose_client", "Which client do you use?")} title={titles.client}>
        <fieldset className="fs-agent-fieldset">
          <legend className="fs-sr-only">{titles.client}</legend>
          <div className="fs-agent-client-groups">
            {groups.map((group) => (
              <div aria-labelledby={group.id} className="fs-agent-options" key={group.id} role="group">
                <span className="fs-agent-microlabel" id={group.id}>{group.label}</span>
                {group.clients.map((item) => (
                  <div className="fs-agent-option" key={item.id}>
                    <DsRadio checked={clientId === item.id} name="agent-access-client" onChange={() => chooseClient(item.id)} value={item.id}><span className="fs-agent-option-name">{item.label}</span></DsRadio>
                    <span className="fs-agent-option-description">{t(item.description_key, item.description)}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
          {clientMissing ? <p className="fs-field-helper error" role="alert">{t("react.agent_access.choose_client_to_continue", "Choose a client to continue.")}</p> : null}
        </fieldset>
        <div className="fs-agent-step-footer"><button className="fs-button primary" onClick={continueFromClient} type="button">{t("react.agent_access.continue", "Continue")}</button></div>
      </AgentStep>
    );
  }

  function packageName(id: string) {
    return setup?.packages.find((pkg) => pkg.id === id)?.display_name || id;
  }

  function notVerifiedBadge() {
    return <DsBadge tone="orange">{t("react.agent_access.not_verified", "Not verified in this client")}</DsBadge>;
  }

  function renderPackageStep() {
    const status = statusOf(2);
    if (status === "next" || !client) return <AgentStep number={2} status="next" title={titles.package} />;
    if (status === "done" && choice) {
      return <AgentStep actions={changeButton(2, titles.package)} number={2} status="done" summary={<><span>{packageName(choice.package_id)}</span>{choice.verified ? null : notVerifiedBadge()}</>} title={titles.package} />;
    }
    return (
      <AgentStep number={2} status="current" summary={t("react.agent_access.package_description", "The package teaches the agent how to work with Flow Steward. It holds no address and no token.")} title={titles.package}>
        <fieldset className="fs-agent-fieldset">
          <legend className="fs-field-label">{t("react.agent_access.choose_package", "What will you do with the agent?")}</legend>
          <div className="fs-agent-options">
            {/* A package this client cannot use is not shown. */}
            {client.packages.filter((item) => item.available).map((item) => {
              const meta = setup?.packages.find((pkg) => pkg.id === item.package_id);
              return (
                <div className="fs-agent-option" key={item.package_id}>
                  <DsRadio checked={packageId === item.package_id} name="agent-access-package" onChange={() => { setPackageId(item.package_id); setPackageMissing(false); }} value={item.package_id}><span className="fs-agent-option-name">{meta?.display_name || item.package_id}</span>{item.verified ? null : notVerifiedBadge()}</DsRadio>
                  {meta ? <span className="fs-agent-option-description">{t(meta.description_key, meta.description)}</span> : null}
                  {item.message_key ? <span className="fs-agent-option-description">{t(item.message_key, item.message)}</span> : null}
                </div>
              );
            })}
          </div>
          {packageMissing ? <p className="fs-field-helper error" role="alert">{t("react.agent_access.choose_package_to_continue", "Choose a package to continue.")}</p> : null}
        </fieldset>
        <div className="fs-agent-step-footer"><button className="fs-button primary" onClick={continueFromPackage} type="button">{t("react.agent_access.continue", "Continue")}</button></div>
      </AgentStep>
    );
  }

  function renderBlockers(tone: "warning" | "danger") {
    return blockers.map((code) => { const notice = blockerNotice(code, t); return <DsAlert key={code} title={notice.title} tone={tone}>{notice.copy}</DsAlert>; });
  }

  function renderOauthAccess(status: AgentStepStatus) {
    if (!client) return null;
    const summary = t("react.agent_access.oauth_access_summary", "{client} connects through OAuth: you choose access in Flow Steward when you connect.", { client: client.label });
    if (status === "done") return <AgentStep number={3} status="done" summary={summary} title={titles.access} />;
    const [before, after = ""] = t("react.agent_access.oauth_no_token", "No token is created on this page. The connection then appears in {link}.").split("{link}");
    return (
      <AgentStep number={3} status="current" summary={summary} title={titles.access}>
        {renderBlockers("warning")}
        {client.id === MICROSOFT_365_CLIENT_ID ? <p className="fs-agent-text">{t("react.agent_access.oauth_m365_admin", "Your organisation's administrator adds Flow Steward to Microsoft 365 once. After that, you sign in to Flow Steward when you connect and choose access there.")}</p> : null}
        <p className="fs-agent-text">{t("react.agent_access.oauth_access_body", "When you add Flow Steward in {client}, you sign in to Flow Steward and choose there what the agent may do, which projects it reaches and when access expires, then confirm.", { client: client.label })}</p>
        <p className="fs-agent-text">{before}<a className="fs-agent-link" href="/settings/account?tab=tokens">{t("react.agent_access.connected_apps_link", "Personal tokens › Connected AI apps")}</a>{after}</p>
        <div className="fs-agent-step-footer"><button className="fs-button primary" onClick={() => setOpenStep(4)} type="button">{t("react.agent_access.continue", "Continue")}</button></div>
      </AgentStep>
    );
  }

  function renderAccessStep() {
    const status = statusOf(3);
    if (status === "next" || !client || !choice) return <AgentStep number={3} status="next" title={titles.access} />;
    if (status === "skipped") return <AgentStep number={3} status="skipped" summary={notNeeded} title={titles.access} />;
    if (choice.auth === "oauth") return renderOauthAccess(status);
    if (status === "done" && tokenId) {
      return <AgentTokenStep disabled={changing} number={3} onCreateAnother={createAnotherToken} onRevoked={() => setSecret("")} presetLabel={oauthPresetLabel(attempt.current?.request.preset || presetId, t)} title={titles.access} tokenId={tokenId} />;
    }
    const expiresOn = formatDate(new Date(Date.now() + expiryDays * DAY_MS).toISOString());
    return (
      <AgentStep number={3} status="current" summary={t("react.agent_access.token_access_description", "{client} uses a personal access token from your account. It can never do more than you can.", { client: client.label })} title={titles.access}>
        {renderBlockers("danger")}
        <fieldset className="fs-agent-fieldset" disabled={locked}>
          <legend className="fs-field-label">{t("react.agent_access.access_level", "What the agent may do")}</legend>
          <div className="fs-agent-options">
            {/* Only what this user can choose; an option they cannot pick is not shown. */}
            {setup?.presets.filter((preset) => preset.available).map((preset) => (
              <div className="fs-agent-option" key={preset.id}>
                <DsRadio checked={presetId === preset.id} name="agent-access-preset" onChange={() => setPresetId(preset.id)} value={preset.id}><span className="fs-agent-option-name">{oauthPresetLabel(preset.id, t)}</span></DsRadio>
                <span className="fs-agent-option-description">{oauthPresetDescription(preset.id, t)}</span>
                <span className="fs-agent-option-chips">{preset.scopes.map((scope) => <DsValueChip key={scope}>{scope}</DsValueChip>)}</span>
              </div>
            ))}
          </div>
        </fieldset>
        <div className="fs-agent-fieldset">
          <OptionalLabel id={projectsLabelId}>{t("react.oauth.column_projects", "Projects")}</OptionalLabel>
          <MultiCombobox ariaLabel={t("react.oauth.column_projects", "Projects")} disabled={locked} loading={projectsQuery.isLoading} loadingLabel={t("react.settings.loading_projects", "Loading projects…")} onChange={setProjectIds} options={projectOptions} placeholder={t("react.settings.search_projects", "Search projects")} selected={projectIds} />
          <span className="fs-field-helper">{t("react.agent_access.projects_help", "Leave empty for every project within your access. Choosing projects limits runs, jobs and workflows to them; agents and skills belong to the whole account.")}</span>
        </div>
        <fieldset className="fs-agent-fieldset" disabled={locked}>
          <legend className="fs-field-label">{t("react.settings.expiration", "Expiration")}</legend>
          <div className="fs-agent-inline-options">{setup?.expiry_days_options.map((days) => <DsRadio checked={expiryDays === days} key={days} name="agent-access-expiry" onChange={() => setExpiryDays(days)} value={String(days)}>{t("react.settings.value1_days", "{value1} days", { value1: String(days) })}</DsRadio>)}</div>
          <span className="fs-field-helper">{t("react.agent_access.token_expires_on", "The token expires on {date}.", { date: expiresOn })}</span>
        </fieldset>
        {renderIssue()}
      </AgentStep>
    );
  }

  function renderIssue() {
    if (issue.kind === "uncertain") {
      return (
        <DsAlert title={t("react.agent_access.uncertain_title", "We could not confirm whether the token was created")} tone="warning">
          <p>{t("react.agent_access.uncertain_copy", "The answer was lost. Check again: it asks about the same attempt, so it never creates a second token. Tokens this wizard created are listed below; revoke any you do not want.")}</p>
          <ul>{wizardTokens.map((token) => <li key={token.id}>{token.label} · {token.created_at ? formatDateTime(token.created_at) : ""}</li>)}</ul>
          <div className="fs-footer-actions">
            <button className="fs-button primary" onClick={() => attempt.current && ignoreAsyncResult(send(attempt.current))} type="button">{t("react.agent_access.check_again", "Check again")}</button>
            <a className="fs-button" href="/settings/account?tab=tokens">{t("react.agent_access.open_personal_tokens", "Open personal tokens")}</a>
            <button className="fs-button ghost" onClick={startOver} type="button">{t("react.agent_access.new_attempt", "Start a new attempt")}</button>
          </div>
        </DsAlert>
      );
    }
    return (
      <>
        {issue.kind === "failed" ? <DsAlert title={t("react.settings.token_could_not_be_created", "Token could not be created")} tone="danger">{issue.message}</DsAlert> : null}
        <div className="fs-agent-step-footer">
          <Button busy={issue.kind === "submitting"} busyLabel={t("react.settings.creating", "Creating…")} disabled={!presetId || mcpBlocked} onClick={createToken} type="button" variant="primary">{t("react.settings.create_token", "Create token")}</Button>
        </div>
      </>
    );
  }

  function setupSummary() {
    const hasPlugin = steps.some((step) => step.kind === "package");
    if (!needsAccess) {
      return hasPlugin
        ? t("react.agent_access.setup_description_developer_plugin", "The plugin installs the Developer package. It needs no token.")
        : t("react.agent_access.setup_description_no_mcp", "This package does not connect to this installation, so no token is needed.");
    }
    if (steps.some((step) => step.kind === "package" && step.uses_mcp_url)) {
      return t("react.agent_access.setup_description_plugin", "The plugin carries this installation's MCP address and your token, so there is nothing to configure.");
    }
    return t("react.agent_access.setup_description", "The address is this installation's public MCP address.");
  }

  function renderSetupStep() {
    if (statusOf(4) !== "current" || !client || !choice) return <AgentStep number={4} status="next" title={titles.setup} />;
    return (
      <AgentStep number={4} status="current" summary={setupSummary()} title={titles.setup}>
        {usesToken && secret ? (
          <DsAlert title={t("react.agent_access.token_in_setup_title", "Your token is already in this setup")} tone="success">
            {t("react.agent_access.token_in_setup_copy", "This page shows it only now. After you leave, create another token if you need it.")}
          </DsAlert>
        ) : null}
        {usesToken && !secret ? (
          // The secret is shown once: a revoked token, or one from an earlier visit, cannot fill this in.
          <DsAlert
            title={result?.secret_already_shown
              ? t("react.agent_access.secret_already_shown_title", "This token already exists; its secret was shown once")
              : t("react.agent_access.token_missing_title", "This setup has no token")}
            tone="warning"
          >
            <p>{result?.secret_already_shown
              ? t("react.agent_access.secret_already_shown_copy", "This attempt created the token earlier and the secret cannot be shown again. If you did not save it, revoke the token in step 3 and create a new one.")
              : t("react.agent_access.token_missing_copy", "A token is shown only once, so this page cannot fill it in again. Create another token to fill in this setup.")}</p>
            <div className="fs-footer-actions"><button className="fs-button" onClick={createAnotherToken} type="button">{t("react.agent_access.create_another_token", "Create another token")}</button></div>
          </DsAlert>
        ) : null}
        {/* Blocked: only the package steps; no address to paste and no token to use. */}
        <AgentSetupSteps packageId={choice.package_id} packageVersion={packageMeta?.version || ""} secret={usesToken ? secret : ""} steps={mcpBlocked ? steps.filter((step) => !step.uses_mcp_url && !step.uses_token) : steps} />
      </AgentStep>
    );
  }

  function renderFirstRequestStep() {
    const status = statusOf(5);
    if (status === "skipped") return <AgentStep number={5} status="skipped" summary={notNeeded} title={titles.firstRequest} />;
    if (status !== "open" || !setup || !choice) return <AgentStep number={5} status="next" title={titles.firstRequest} />;
    const request = setup.first_request;
    const description = request.description_key ? t(request.description_key, request.description, request.values) : "";
    return (
      <AgentStep number={5} status="open" summary={description} title={titles.firstRequest}>
        <AgentCodeBlock label={t(request.title_key, request.title, request.values)} prose text={request.content} />
        {usesToken && tokenId ? <AgentRequestCheck tokenId={tokenId} /> : null}
        {choice.auth === "oauth" ? <p className="fs-field-helper">{t("react.agent_access.usage_oauth", "The approved access, with its expiry, scopes and last use, appears under Personal tokens → Connected AI apps.")}</p> : null}
      </AgentStep>
    );
  }

  return (
    <div className="fs-panel-stack fs-agent-setup">
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("react.agent_access.title", "AI agents")}</h2><p>{t("react.agent_access.description", "Connect an AI client to Flow Steward: pick the client and the package, get the settings filled in, and make the first request.")}</p></div></div>
      <ol className="fs-agent-steps">
        {renderClientStep()}
        {renderPackageStep()}
        {renderAccessStep()}
        {renderSetupStep()}
        {renderFirstRequestStep()}
      </ol>
    </div>
  );
}
