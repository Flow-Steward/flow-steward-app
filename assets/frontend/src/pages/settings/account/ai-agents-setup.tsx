import { useState, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { Check, Download } from "lucide-react";

import { DsAlert, DsBadge, DsToast } from "@/components/shared/design-system";
import { InlineConfirm } from "@/components/shared/inline-confirm";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { ArchiveLayout } from "@/pages/settings/account/agent-package-archive";
import { AgentCodeBlock } from "@/pages/settings/account/ai-agents-copy";
import { PlatformClientStep } from "@/pages/settings/account/ai-agents-platform";
import { fetchAgentAccessToken, revokePersonalToken } from "@/pages/settings/shared/api";
import type { AgentAccessToken, AgentSetupStep } from "@/pages/settings/shared/types";
import { formatDate } from "@/lib/time/date-time-format";

type Translate = I18nContextValue["t"];

const TOKEN_PLACEHOLDER = "{token}";

/** The step text with the secret, or a visible placeholder when there is none. */
function fillToken(content: string, secret: string, placeholder: string): string {
  return content.split(TOKEN_PLACEHOLDER).join(secret || placeholder);
}

function downloadBlob(fileName: string, blob: Blob) {
  // A blob handle, never the content in a URL; the file never leaves this browser.
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

function stepTitle(step: AgentSetupStep, t: Translate) {
  return t(step.title_key, step.title, step.values);
}

function stepDescription(step: AgentSetupStep, t: Translate) {
  return step.description_key ? t(step.description_key, step.description, step.values) : "";
}

function stepHint(step: AgentSetupStep, t: Translate) {
  return step.hint_key ? t(step.hint_key, step.hint || "", step.values) : "";
}

/** A hint names a command between backticks; it reads as code, the rest as text. */
function withCode(text: string): ReactNode[] {
  return text.split("`").map((part, index) => (index % 2 ? <code key={`${index}-${part}`}>{part}</code> : part));
}

/**
 * One step of the AI agents setup. A finished step folds into its summary row,
 * the current one is open, a later one is a numbered row and a skipped one says
 * why it is not needed.
 */
export type AgentStepStatus = "done" | "current" | "open" | "next" | "skipped";

export function AgentStep({
  actions,
  children,
  number,
  status,
  summary,
  title,
}: Readonly<{ actions?: ReactNode; children?: ReactNode; number: number; status: AgentStepStatus; summary?: ReactNode; title: string }>) {
  const { t } = useI18n();
  return (
    <li aria-current={status === "current" ? "step" : undefined} className={`fs-agent-step ${status}`}>
      <div className="fs-agent-step-head">
        <span aria-hidden="true" className="fs-agent-step-mark">{status === "done" ? <Check /> : number}</span>
        <div className="fs-agent-step-titles">
          <h3 className="fs-agent-step-title"><span className="fs-sr-only">{t("react.agent_access.step_prefix", "Step {number}.", { number: String(number) })} </span>{title}</h3>
          {summary ? <p className="fs-agent-step-summary">{summary}</p> : null}
        </div>
        {actions ? <div className="fs-agent-step-actions">{actions}</div> : null}
      </div>
      {children ? <div className="fs-agent-step-body">{children}</div> : null}
    </li>
  );
}

/** Step 4: what to do in the client, as the server described it. */
export function AgentSetupSteps({
  packageId = "",
  packageVersion = "",
  secret,
  steps,
}: Readonly<{ packageId?: string; packageVersion?: string; secret: string; steps: AgentSetupStep[] }>) {
  const { t } = useI18n();
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const placeholder = t("react.agent_access.token_placeholder", "<your personal token>");

  async function downloadArchive(step: AgentSetupStep) {
    try {
      // Loaded on demand: the package files are bundled only into this chunk.
      const { buildArchive } = await import("@/pages/settings/account/agent-package-archive");
      downloadBlob(step.file_name, buildArchive(step.archive_layout as ArchiveLayout, packageId, { servers: fillToken(step.content, secret, ""), manifestDir: step.manifest_dir }));
      setFeedback({ tone: "success", message: t("react.agent_access.downloaded", "Downloaded {name}.", { name: step.file_name }) });
    } catch {
      setFeedback({ tone: "danger", message: t("react.agent_access.download_failed", "The file could not be built. Reload the page and try again.") });
    }
    globalThis.setTimeout(() => setFeedback(null), 2400);
  }

  return (
    <>
      {steps.map((step) => {
        const description = stepDescription(step, t);
        const hint = stepHint(step, t);
        const text = fillToken(step.content, secret, placeholder);
        const waitsForToken = step.uses_token && !secret;
        if (step.kind === "notice") {
          return <DsAlert key={step.id} title={stepTitle(step, t)}>{description}</DsAlert>;
        }
        return (
          <div className="fs-agent-sub" key={step.id}>
            <h4 className="fs-agent-sub-title">{stepTitle(step, t)}</h4>
            {description ? <p className="fs-agent-text">{description}</p> : null}
            {step.kind === "platform" ? (
              <PlatformClientStep mcpUrl={step.content} packageVersion={packageVersion} step={step} />
            ) : step.kind === "package" ? (
              <>
                {waitsForToken ? <span className="fs-field-helper">{t("react.agent_access.package_after_token", "The download is ready once the token is created.")}</span> : null}
                <div className="fs-footer-actions">
                  <button className="fs-button primary" disabled={waitsForToken} onClick={() => ignoreAsyncResult(downloadArchive(step))} type="button"><Download aria-hidden="true" />{t("react.agent_access.download_file", "Download {name}", { name: step.file_name })}</button>
                </div>
              </>
            ) : step.kind === "link" ? (
              <div className="fs-footer-actions">
                <a className="fs-button" href={step.url} rel="noreferrer noopener" target="_blank">
                  {step.url.includes("/releases/") ? t("react.agent_access.open_release", "Open the release") : t("react.agent_access.open_readme", "Open the README")}
                </a>
              </div>
            ) : (
              <>
                <AgentCodeBlock disabled={waitsForToken} label={stepTitle(step, t)} text={text} />
                {waitsForToken ? <span className="fs-field-helper">{t("react.agent_access.token_appears_here", "The token appears here once it is created. It is shown only once.")}</span> : null}
                {step.kind === "file" && step.file_name ? (
                  <div className="fs-footer-actions">
                    <button className="fs-button ghost" disabled={waitsForToken} onClick={() => downloadBlob(step.file_name, new Blob([text], { type: "text/plain;charset=utf-8" }))} type="button">{t("react.agent_access.download_file", "Download {name}", { name: step.file_name })}</button>
                  </div>
                ) : null}
              </>
            )}
            {hint ? <p className="fs-field-helper">{withCode(hint)}</p> : null}
          </div>
        );
      })}
      {feedback ? <DsToast tone={feedback.tone}>{feedback.message}</DsToast> : null}
    </>
  );
}

function tokenState(token: AgentAccessToken, t: Translate = fallbackContext.t) {
  if (token.revoked) return { tone: "neutral" as const, label: t("react.settings.revoked", "Revoked") };
  if (token.expired || (token.expires_at && new Date(token.expires_at).getTime() < Date.now())) {
    return { tone: "neutral" as const, label: t("react.settings.expired", "Expired") };
  }
  return { tone: "green" as const, label: t("account_settings.status.active", "Active") };
}

/** The token record: step 3 shows its properties and step 5 its first use, from one query. */
function useAgentToken(tokenId: string) {
  return useQuery({
    queryKey: ["settings", "account", "agent-access", "token", tokenId],
    queryFn: () => fetchAgentAccessToken(tokenId),
    retry: false,
  });
}

/**
 * Step 3 once the token exists: its properties in the summary row, Create
 * another token and a quiet Revoke that confirms in place. A token is never
 * edited, so there is no Change here.
 */
export function AgentTokenStep({
  disabled = false,
  number,
  onCreateAnother,
  onRevoked,
  presetLabel,
  title,
  tokenId,
}: Readonly<{ disabled?: boolean; number: number; onCreateAnother: () => void; onRevoked: () => void; presetLabel: string; title: string; tokenId: string }>) {
  const { t } = useI18n();
  const tokenQuery = useAgentToken(tokenId);
  const [confirming, setConfirming] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState("");
  const token = tokenQuery.data?.token;

  async function revoke() {
    setRevoking(true);
    setRevokeError("");
    try {
      await revokePersonalToken(tokenId);
      setConfirming(false);
      await tokenQuery.refetch();
      onRevoked();
    } catch (error) {
      setRevokeError(error instanceof Error ? error.message : t("react.agent_access.revoke_failed", "The token could not be revoked."));
    } finally {
      setRevoking(false);
    }
  }

  let summary: ReactNode;
  if (tokenQuery.isError) {
    summary = t("react.agent_access.token_status_unavailable", "The token's state could not be loaded");
  } else if (token) {
    const state = tokenState(token, t);
    const projects = token.allowed_project_ids || [];
    const facts = [
      presetLabel,
      projects.length ? t("react.oauth.project_count", "{value1} projects", { value1: String(projects.length) }) : t("react.agent_access.all_projects", "All projects"),
    ];
    if (token.expires_at) facts.push(t("react.agent_access.token_expires", "Expires {date}", { date: formatDate(token.expires_at) }));
    summary = <><DsBadge tone={state.tone}>{state.label}</DsBadge><span>{facts.filter(Boolean).join(" · ")}</span></>;
  } else {
    summary = t("react.agent_access.loading_token", "Loading the token's state…");
  }

  const actions = (
    <>
      <button className="fs-button ghost sm" disabled={disabled || revoking} onClick={onCreateAnother} type="button">{t("react.agent_access.create_another_token", "Create another token")}</button>
      {token && !token.revoked ? <button className="fs-button ghost-danger sm" disabled={disabled || confirming} onClick={() => setConfirming(true)} type="button">{t("react.agent_access.revoke", "Revoke")}</button> : null}
    </>
  );

  return (
    <AgentStep actions={actions} number={number} status="done" summary={summary} title={title}>
      {confirming && token ? (
        <InlineConfirm
          cancelLabel={t("common.cancel", "Cancel")}
          confirmLabel={t("react.settings.revoke_token", "Revoke token")}
          error={revokeError}
          onCancel={() => { setConfirming(false); setRevokeError(""); }}
          onConfirm={() => ignoreAsyncResult(revoke())}
          pending={revoking}
          pendingLabel={t("react.settings.revoking", "Revoking…")}
          question={t("react.settings.revoke_value1", "Revoke “{value1}”?", { value1: token.label || t("react.settings.token_lowercase", "token") })}
          tone="danger"
        >
          {t("react.settings.clients_using_this_token_lose_access_immediately_this_action_cannot_be_undone", "Clients using this token lose access immediately. This action cannot be undone.")}
        </InlineConfirm>
      ) : null}
    </AgentStep>
  );
}

/**
 * Step 5's check: only that the token was used, never which client used it;
 * copying or downloading the settings is not a connection.
 */
export function AgentRequestCheck({ tokenId }: Readonly<{ tokenId: string }>) {
  const { t } = useI18n();
  const tokenQuery = useAgentToken(tokenId);
  const usedAt = tokenQuery.data?.token?.last_used_at || "";
  return (
    <div className="fs-agent-check">
      <div className="fs-agent-check-text">
        <span className="fs-agent-check-title">
          <span aria-hidden="true" className="fs-agent-check-dot" data-used={usedAt ? "true" : undefined} />
          {usedAt ? t("react.oauth.access_used_on", "Access used {value1}", { value1: formatDate(usedAt) }) : t("react.agent_access.no_request_yet", "No request yet")}
        </span>
        {usedAt ? null : <span className="fs-field-helper">{t("react.agent_access.not_used_help", "Copying or downloading the settings does not count. The time appears after the first request with this token.")}</span>}
      </div>
      <button className="fs-button secondary" disabled={tokenQuery.isFetching} onClick={() => ignoreAsyncResult(tokenQuery.refetch())} type="button">{t("react.agent_access.check_again", "Check again")}</button>
    </div>
  );
}
