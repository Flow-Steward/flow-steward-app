import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo } from "react";

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Loader2 } from "lucide-react";
import { Link } from "react-router";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { fetchProjectConnectionsList } from "@/pages/projects/connections/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

type ProjectConnectionsEditorProps = {
  projectId: string;
};

/**
 * Runtime Connections is a read-only list. Connections are created by the
 * extensions themselves - only the extension knows what goes into one - so
 * connecting, changing and removing happen on the extension's page, and each
 * row links there.
 */
export function ProjectConnectionsEditor({ projectId }: Readonly<ProjectConnectionsEditorProps>) {
  const { t } = useI18n();
  const userTimezone = useUserTimezone();

  const connectionsQuery = useQuery({
    queryKey: ["projects", projectId, "connections"],
    queryFn: () => fetchProjectConnectionsList(projectId),
    enabled: Boolean(projectId),
  });

  const connections = useMemo(
    () => (Array.isArray(connectionsQuery.data) ? connectionsQuery.data : []),
    [connectionsQuery.data],
  );

  return (
    <section className="space-y-6">
      <Card className="fs-card">
        <CardHeader>
          <CardTitle className="text-lg">{t("project_detail.connections.title", "Connections")}</CardTitle>
          <CardDescription>{t("react.projects.current_extension_extension_runtime_and_connector_bindings_for_this_project", "Current extension, extension runtime, and connector bindings for this project.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {connectionsQuery.isLoading ? (
            <div className="flex items-center gap-2 text-sm fs-ink-secondary">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("react.projects.loading_connections", "Loading connections...")} </div>
          ) : null}
          {connections.map((connection) => {
            const connectionId = firstStringFromUnknownValue([connection.connection_id, ""]);
            const displayName = firstStringFromUnknownValue([connection.display_name, connectionId, "connection"]);
            const extensionId = firstStringFromUnknownValue([connection.extension_id, ""]).trim();
            const connectionType = firstStringFromUnknownValue([connection.connection_type, ""]);
            const environment = firstStringFromUnknownValue([connection.environment, ""]);
            const lastSeenOkAt = firstStringFromUnknownValue([connection.last_seen_ok_at, ""]).trim();
            const lastError = firstStringFromUnknownValue([connection.last_error, ""]).trim();
            return (
              <div
                className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm fs-ink-secondary"
                key={connectionId || JSON.stringify(connection)}
              >
                <div className="flex flex-wrap items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium fs-ink-brand">{displayName}</p>
                    <div className="mt-1 flex flex-wrap gap-2 text-xs fs-ink-tertiary">
                      {connectionId && connectionId !== displayName ? <span>{connectionId}</span> : null}
                      {extensionId ? <span>{extensionId}</span> : null}
                      <span>{firstStringFromUnknownValue([connection.connection_kind, "extension"])}</span>
                      {connectionType ? <span>{connectionType}</span> : null}
                      {environment ? <span>{environment}</span> : null}
                      <span>{firstStringFromUnknownValue([connection.health_status, "unchecked"])}</span>
                      {lastSeenOkAt ? (
                        <span>{t("react.projects.connection_last_ok", "last OK {time}", { time: formatUserDateTime(lastSeenOkAt, userTimezone) })}</span>
                      ) : null}
                      {connection.enabled === false ? <span>{t("react.projects.disabled_lowercase", "disabled")}</span> : <span>{t("react.projects.enabled_lowercase", "enabled")}</span>}
                    </div>
                    {lastError ? <p className="mt-1 text-xs fs-ink-attention">{lastError}</p> : null}
                  </div>
                  {extensionId ? (
                    <Link
                      aria-label={t("react.projects.open_the_extension_of_value1", "Open the extension of {value1}", { value1: displayName })}
                      className="fs-button secondary sm"
                      to={`/extensions/${encodeURIComponent(extensionId)}?project_id=${encodeURIComponent(projectId)}`}
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                      {t("react.projects.open_extension", "Open extension")} </Link>
                  ) : null}
                </div>
              </div>
            );
          })}
          {!connectionsQuery.isLoading && !connectionsQuery.isError && !connections.length ? (
            <p className="fs-empty-copy">{t("react.projects.connections_appear_here_when_an_extension_connects_one_for_this_project", "Connections appear here when an extension connects one for this project.")}</p>
          ) : null}
        </CardContent>
      </Card>

      {connectionsQuery.isError ? (
        <Card className="border-selected fs-surface-attention">
          <CardContent className="text-sm fs-ink-attention">
            {connectionsQuery.error instanceof Error ? connectionsQuery.error.message : t("react.projects.failed_to_load_project_connections", "Failed to load project connections.")}
          </CardContent>
        </Card>
      ) : null}
    </section>
  );
}
