import { useMemo } from "react";

import {
  listFromUnknown,
  objectOrEmpty,
  stringFrom,
} from "@/pages/extensions/extension-manifest-utils";

import {
  PAGE_CONNECTION_PARAM,
  pageConnectionCandidates,
  pageConnectionTypes,
  selectedPageConnectionId,
} from "./extension-page-connection";

type SearchParamsUpdate = (change: (latest: URLSearchParams) => URLSearchParams) => void;

export type ExtensionPageConnection = {
  /** The page's connection types; empty for a page without a connection. */
  types: string[];
  typeLabel: string;
  projectId: string;
  candidates: Array<Record<string, unknown>>;
  selectedId: string;
  loading: boolean;
  /** Operators and administrators load connection-query data; a viewer does not. */
  canLoad: boolean;
  /** Whether the page's queries may run: no connection needed, or one is chosen. */
  ready: boolean;
  /** The first page of this extension with a connection form of the page's types. */
  setupPageId: string;
  select: (connectionId: string) => void;
};

function connectionTypeLabel(manifest: unknown, types: readonly string[]): string {
  const rows = listFromUnknown(objectOrEmpty(manifest).connection_types);
  return types
    .map((type) => {
      const row = rows.find((candidate) => stringFrom(candidate.connection_type_id) === type);
      return stringFrom(row?.display_name || row?.title, type);
    })
    .join(" / ");
}

function connectionFormTypes(component: Record<string, unknown>): string[] {
  const data = objectOrEmpty(component.data);
  return [
    stringFrom(data.connection_type),
    ...Object.values(objectOrEmpty(data.connection_types))
      .map((variant) => stringFrom(objectOrEmpty(variant).connection_type)),
  ].filter(Boolean);
}

function setupPageFor(pages: ReadonlyArray<Record<string, unknown>>, types: readonly string[]): string {
  const page = pages.find((candidate) => listFromUnknown(candidate.components).some((component) => (
    stringFrom(component.type) === "connection_form"
    && connectionFormTypes(component).some((type) => types.includes(type))
  )));
  return stringFrom(page?.page_id);
}

export function useExtensionPageConnection({
  page,
  pages,
  manifest,
  connections,
  connectionsPending,
  installId,
  projectId,
  canManageProject,
  search,
  updateSearchParams,
}: {
  page: Record<string, unknown> | undefined;
  pages: ReadonlyArray<Record<string, unknown>>;
  manifest: unknown;
  connections: ReadonlyArray<Record<string, unknown>> | undefined;
  connectionsPending: boolean;
  installId: string;
  projectId: string;
  canManageProject: boolean;
  search: string;
  updateSearchParams: SearchParamsUpdate;
}): ExtensionPageConnection {
  const typesKey = JSON.stringify(pageConnectionTypes(page));
  const types = useMemo(() => JSON.parse(typesKey) as string[], [typesKey]);
  const candidates = useMemo(
    () => pageConnectionCandidates(connections, types, installId),
    [connections, installId, types],
  );
  const requestedId = stringFrom(new URLSearchParams(search).get(PAGE_CONNECTION_PARAM));
  const selectedId = types.length ? selectedPageConnectionId(candidates, requestedId) : "";
  const loading = Boolean(types.length && projectId && connectionsPending);
  return {
    types,
    typeLabel: connectionTypeLabel(manifest, types),
    projectId,
    candidates,
    selectedId,
    loading,
    canLoad: canManageProject,
    ready: !types.length || Boolean(canManageProject && selectedId),
    setupPageId: types.length ? setupPageFor(pages, types) : "",
    select: (connectionId: string) => updateSearchParams((latest) => {
      const normalized = stringFrom(connectionId);
      if (normalized) latest.set(PAGE_CONNECTION_PARAM, normalized);
      else latest.delete(PAGE_CONNECTION_PARAM);
      return latest;
    }),
  };
}
