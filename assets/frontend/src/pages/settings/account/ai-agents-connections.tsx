import { useQuery } from "@tanstack/react-query";

import { DsAlert } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate } from "@/lib/time/date-time-format";
import { fetchAgentConnections } from "@/pages/settings/shared/api";
import type { AgentConnection } from "@/pages/settings/shared/types";

/** One query for the AI agents page and the app shell notice. */
export const AGENT_CONNECTIONS_QUERY_KEY = ["settings", "account", "agent-connections"] as const;

/**
 * ADR 0076 §5: every AI agent connection of the person with the day it stops working.
 * Within the reminder window a connection gets a warning and the one action that
 * renews it. Every date and the window come from the server.
 */
export function AgentConnectionsList({ onConnectAgain, busy = false }: Readonly<{ onConnectAgain: (connection: AgentConnection) => void; busy?: boolean }>) {
  const { t } = useI18n();
  const query = useQuery({ queryKey: AGENT_CONNECTIONS_QUERY_KEY, queryFn: fetchAgentConnections, retry: false });
  const connections = query.data?.connections || [];
  if (!connections.length) return null;
  return (
    <section aria-labelledby="fs-agent-connections-title" className="fs-agent-sub">
      <h3 className="fs-agent-sub-title" id="fs-agent-connections-title">{t("react.agent_access.connections_title", "Your connections")}</h3>
      <ul className="fs-agent-steps">
        {connections.map((connection) => <ConnectionRow busy={busy} connection={connection} key={`${connection.kind}:${connection.id}`} onConnectAgain={onConnectAgain} />)}
      </ul>
    </section>
  );
}

function ConnectionRow({ busy, connection, onConnectAgain }: Readonly<{ busy: boolean; connection: AgentConnection; onConnectAgain: (connection: AgentConnection) => void }>) {
  const { t } = useI18n();
  const client = connection.client_label;
  const where = connection.kind === "oauth_grant" ? t("react.agent_access.in_the_cloud", "In the cloud") : t("react.agent_access.local", "On your computer");
  const ends = connection.ends_at
    ? t("react.agent_access.connection_ends_on", "Works until {date}", { date: formatDate(connection.ends_at) })
    : t("react.agent_access.connection_no_end", "Has no end date");
  const canConnectAgain = connection.renew === "connect_again" && Boolean(connection.client_id);
  return (
    <li className="fs-agent-step open fs-agent-connection">
      <div className="fs-agent-step-head">
        <div className="fs-agent-step-titles">
          <h4 className="fs-agent-step-title">{client}</h4>
          <p className="fs-agent-step-summary">{`${where} · ${ends}`}</p>
        </div>
      </div>
      {connection.ending_soon && connection.ends_at ? (
        <div className="fs-agent-step-body">
          <DsAlert title={t("react.agent_access.connection_ending_title", "{client} stops working in {days} days, on {date}", { client, days: String(connection.days_left ?? 0), date: formatDate(connection.ends_at) })} tone="warning">
            {connection.renew === "reconnect_from_client" ? (
              <p>{t("react.agent_access.connection_reconnect_from_client", "To keep it working, connect Flow Steward again from {client}. Flow Steward asks you to allow it once more.", { client })}</p>
            ) : (
              <p>{t("react.agent_access.connection_connect_again_copy", "To keep it working, connect again: you get a new key to put into {client}.", { client })}</p>
            )}
            {canConnectAgain ? (
              <div className="fs-footer-actions">
                <Button disabled={busy} onClick={() => onConnectAgain(connection)} type="button" variant="primary">{t("react.agent_access.connect_again", "Connect again")}</Button>
              </div>
            ) : null}
          </DsAlert>
        </div>
      ) : null}
    </li>
  );
}
