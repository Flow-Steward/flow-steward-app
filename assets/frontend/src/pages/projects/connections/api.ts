import { fetchJson } from "@/lib/http/client";

export type ProjectConnection = Record<string, unknown>;
export type ExtensionProjectState = Record<string, unknown>;

function listFromPayload(payload: unknown): ProjectConnection[] {
  if (Array.isArray(payload)) {
    return payload.filter((row): row is ProjectConnection => Boolean(row) && typeof row === "object");
  }
  if (payload && typeof payload === "object") {
    const map = payload as Record<string, unknown>;
    if (Array.isArray(map.connections)) {
      return map.connections.filter((row): row is ProjectConnection => Boolean(row) && typeof row === "object");
    }
    for (const value of Object.values(map)) {
      if (Array.isArray(value)) {
        return value.filter((row): row is ProjectConnection => Boolean(row) && typeof row === "object");
      }
    }
  }
  return [];
}

export async function fetchProjectConnectionsList(projectId: string) {
  const payload = await fetchJson<unknown>(`/api/projects/${encodeURIComponent(projectId)}/connections`);
  return listFromPayload(payload);
}

export async function createProjectConnection(projectId: string, payload: ProjectConnection) {
  return fetchJson<ProjectConnection>(`/api/projects/${encodeURIComponent(projectId)}/connections`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateProjectConnection(
  projectId: string,
  connectionId: string,
  payload: ProjectConnection,
) {
  return fetchJson<ProjectConnection>(
    `/api/projects/${encodeURIComponent(projectId)}/connections/${encodeURIComponent(connectionId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export async function deleteProjectConnection(projectId: string, connectionId: string) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/connections/${encodeURIComponent(connectionId)}`,
    {
      method: "DELETE",
    },
  );
}

export async function saveProjectConnectionSecrets(
  projectId: string,
  connectionId: string,
  secrets: Record<string, string>,
) {
  return fetchJson<ProjectConnection>(
    `/api/projects/${encodeURIComponent(projectId)}/connections/${encodeURIComponent(connectionId)}/secrets`,
    {
      method: "POST",
      body: JSON.stringify({ secrets }),
    },
  );
}

export async function startProjectConnectionOAuth(
  projectId: string,
  connectionId: string,
  payload: Record<string, unknown> = {},
) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/connections/${encodeURIComponent(connectionId)}/oauth/start`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function disconnectProjectConnectionOAuth(projectId: string, connectionId: string) {
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/connections/${encodeURIComponent(connectionId)}/oauth/disconnect`,
    { method: "POST" },
  );
}

function extensionStateListFromPayload(payload: unknown): ExtensionProjectState[] {
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const map = payload as Record<string, unknown>;
    if (Array.isArray(map.state)) {
      return map.state.filter((row): row is ExtensionProjectState => Boolean(row) && typeof row === "object");
    }
  }
  return [];
}

export async function fetchExtensionProjectState(
  projectId: string,
  extensionId: string,
  params: { namespace?: string; connectionId?: string } = {},
) {
  const searchParams = new URLSearchParams();
  if (params.namespace) {
    searchParams.set("namespace", params.namespace);
  }
  if (params.connectionId) {
    searchParams.set("connection_id", params.connectionId);
  }
  const suffix = searchParams.toString() ? `?${searchParams.toString()}` : "";
  const payload = await fetchJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/extension-state/${encodeURIComponent(extensionId)}${suffix}`,
  );
  return extensionStateListFromPayload(payload);
}

export async function putExtensionProjectState(
  projectId: string,
  extensionId: string,
  namespace: string,
  stateKey: string,
  payload: {
    value_jsonb: Record<string, unknown>;
    connection_id?: string | null;
    expected_revision?: number | null;
  },
) {
  return fetchJson<ExtensionProjectState>(
    `/api/projects/${encodeURIComponent(projectId)}/extension-state/${encodeURIComponent(extensionId)}/${encodeURIComponent(namespace)}/${encodeURIComponent(stateKey)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export async function deleteExtensionProjectState(
  projectId: string,
  extensionId: string,
  namespace: string,
  stateKey: string,
  params: { expectedRevision?: number | null } = {},
) {
  const searchParams = new URLSearchParams();
  if (params.expectedRevision !== null && params.expectedRevision !== undefined) {
    searchParams.set("expected_revision", String(params.expectedRevision));
  }
  const suffix = searchParams.toString() ? `?${searchParams.toString()}` : "";
  return fetchJson<Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/extension-state/${encodeURIComponent(extensionId)}/${encodeURIComponent(namespace)}/${encodeURIComponent(stateKey)}${suffix}`,
    {
      method: "DELETE",
    },
  );
}
