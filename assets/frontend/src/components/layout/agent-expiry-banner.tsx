import { useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { DsAlert } from "@/components/shared/design-system-primitives";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate } from "@/lib/time/date-time-format";
import { AGENT_CONNECTIONS_QUERY_KEY } from "@/pages/settings/account/agent-connections-query";
import { fetchAgentConnections } from "@/pages/settings/shared/api";
import type { AgentConnection } from "@/pages/settings/shared/types";

const RECHECK_MS = 60 * 60_000;
const DISMISSED_KEY = "fs.agent-expiry-notice.dismissed";
export const AI_AGENTS_PATH = "/settings/account?tab=ai-agents";

function connectionKey(connection: AgentConnection): string {
  return `${connection.kind}:${connection.id}`;
}

function readDismissed(): string[] {
  try {
    const raw = globalThis.localStorage?.getItem(DISMISSED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function writeDismissed(keys: string[]) {
  try {
    globalThis.localStorage?.setItem(DISMISSED_KEY, JSON.stringify(keys.slice(-200)));
  } catch {
    // A browser without storage shows the notice again next time; nothing else changes.
  }
}

/**
 * ADR 0076 §5: one line in the app shell while any of the person's AI agent
 * connections stops working within the server's reminder window. It links to the AI
 * agents page, where each connection has the action that renews it. Dismissing hides
 * it for those connections in this browser; another connection ending brings it back.
 */
export function AgentExpiryBanner() {
  const { t } = useI18n();
  const [dismissed, setDismissed] = useState<string[]>(readDismissed);
  const { data } = useQuery({
    queryKey: AGENT_CONNECTIONS_QUERY_KEY,
    queryFn: fetchAgentConnections,
    retry: false,
    staleTime: RECHECK_MS,
    refetchInterval: RECHECK_MS,
  });
  const listed = data?.connections;
  const ending = (Array.isArray(listed) ? listed : [])
    .filter((connection) => connection.ending_soon && !dismissed.includes(connectionKey(connection)));
  if (!ending.length) return null;
  const [first] = ending;
  const title = ending.length === 1
    ? t("react.agent_access.expiry_notice_one", "Your AI agent connection {client} stops working on {date}.", { client: first.client_label, date: formatDate(first.ends_at) })
    : t("react.agent_access.expiry_notice_many", "{count} of your AI agent connections stop working soon.", { count: String(ending.length) });

  function dismiss() {
    const next = [...dismissed, ...ending.map(connectionKey)];
    setDismissed(next);
    writeDismissed(next);
  }

  return (
    <div className="px-4 py-2" data-testid="agent-expiry-notice">
      <DsAlert title={title} tone="warning">
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          <a className="fs-link" href={AI_AGENTS_PATH}>{t("react.agent_access.expiry_notice_link", "Open AI agents")}</a>
          <button className="fs-link" onClick={dismiss} type="button">{t("react.agent_access.expiry_notice_dismiss", "Dismiss")}</button>
        </div>
      </DsAlert>
    </div>
  );
}
