import { useQueryClient, useQuery, useQueries } from "@tanstack/react-query";
import { useParams, useLocation, useNavigate } from "react-router";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useLatestSearchParams } from "@/lib/routing/use-latest-search-params";
import { useRef, useState, useEffect, useMemo } from "react";
import {
  normalizeActiveTab,
  type ComponentError,
  stringFrom,
  extensionState,
  objectOrEmpty,
  manifestUiPages,
  listFromUnknown,
  isProjectScopedExtension,
  manifestPageRequiresProject,
  focusManifestAnchor,
  manifestQueryValues,
  componentQueryId,
  componentQueryParamsForComponent,
} from "@/pages/extensions/extension-manifest-utils";
import { type ExtensionCatalogInstallState } from "@/pages/extensions/catalog-api";
import { type ExtensionActionResult } from "@/pages/extensions/extension-action-result-panel";
import {
  type ExtensionUninstallPlan,
  fetchExtensions,
  fetchExtensionArchiveLifecycleAvailability,
  fetchExtension,
  fetchExtensionCompiledManifest,
  normalizeExtensionRecord,
  fetchExtensionPreview,
  fetchExtensionCallbacks,
  fetchExtensionExternalEffects,
  executeExtensionQuery,
} from "@/pages/extensions/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  normalizeWorkspaceId,
  readStoredWorkspaceId,
  WORKSPACE_SELECTED_EVENT,
} from "@/lib/utils/workspace-selection";
import { stringFromUnknownValue, firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { oauthProvidersFromManifest } from "@/pages/extensions/extension-oauth-types";
import { useI18n } from "@/lib/i18n/useI18n";
import { localizeExtensionIdentity, localizeExtensionManifest, localeBundlesFrom } from "@/pages/extensions/extension-locale";
import {
  fetchExtensionOAuthConfigurations,
  fetchExtensionOAuthSetupStatus,
} from "@/pages/extensions/extension-oauth-api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  fetchProjectConnectionsList,
  fetchExtensionProjectState,
} from "@/pages/projects/connections/api";
import { manifestFieldControlId } from "@/pages/extensions/extension-manifest-fields";
import { normalizeComponentPayload } from "@/pages/extensions/payload";
import {
  type PendingManifestActionIntent,
  type PendingBrowserAuthorityApproval,
} from "./manifest-action-support";
import {
  type ExtensionsViewFilter,
  type ExtensionHealthStatus,
  type OAuthCallbackNotice,
  oauthCallbackNoticeFromSearch,
  extensionCategory,
  catalogLabel,
  anyTrue,
  extensionDetailBackLink,
  uninstallExtensionForPlan,
} from "./hub-support";
import { oauthConnectionTypesFromManifest } from "./connection-form-support";

export function useExtensionsHubState() {
  const queryClient = useQueryClient();
  const { extensionId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [, updateSearchParams] = useLatestSearchParams();
  const [workspaceId, setWorkspaceId] = useState(readStoredWorkspaceId);
  const workspaceIdRef = useRef(workspaceId);
  const [projectWorkspaceId, setProjectWorkspaceId] = useState(() => workspaceId);
  const [workspaceChanged, setWorkspaceChanged] = useState(false);
  useEffect(() => {
    const onWorkspaceSelected = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string }>).detail || {};
      const nextWorkspaceId = normalizeWorkspaceId(detail.workspaceId);
      if (workspaceIdRef.current === nextWorkspaceId) return;
      workspaceIdRef.current = nextWorkspaceId;
      setWorkspaceId(nextWorkspaceId);
      setWorkspaceChanged(true);
      updateSearchParams((latest) => {
        latest.delete("project_id");
        return latest;
      });
    };
    globalThis.addEventListener(WORKSPACE_SELECTED_EVENT, onWorkspaceSelected);
    return () => globalThis.removeEventListener(WORKSPACE_SELECTED_EVENT, onWorkspaceSelected);
  }, [updateSearchParams]);
  const canAdministerPlatform = useHasCapability("administer_platform");
  const canManageProject = useHasCapability("mutate_projects");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pendingManifestActionIntentRef = useRef<PendingManifestActionIntent | null>(null);
  const [activeTab, setActiveTab] = useState<"installed" | "marketplace">(
    normalizeActiveTab(location.hash),
  );
  const [viewFilter, setViewFilter] = useState<ExtensionsViewFilter>("active");
  const [extensionSearch, setExtensionSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [busyById, setBusyById] = useState<Record<string, boolean>>({});
  const [statusById, setStatusById] = useState<ExtensionHealthStatus>({});
  const [toggleErrorById, setToggleErrorById] = useState<ExtensionHealthStatus>({});
  const [installDropActive, setInstallDropActive] = useState(false);
  const [pendingBrowserApproval, setPendingBrowserApproval] =
    useState<PendingBrowserAuthorityApproval | null>(null);
  const [catalogInstallById, setCatalogInstallById] = useState<
    Record<string, ExtensionCatalogInstallState>
  >({});
  const [browserApprovalError, setBrowserApprovalError] = useState("");
  const [callbackStatuses, setCallbackStatuses] = useState<
    Record<string, Record<string, unknown>>
  >({});
  const [componentErrors, setComponentErrors] = useState<Record<string, ComponentError>>({});
  const [actionResultByExtension, setActionResultByExtension] = useState<
    Record<string, ExtensionActionResult>
  >({});
  const [componentQueryParams, setComponentQueryParams] = useState<
    Record<string, Record<string, unknown>>
  >({});
  const [formValuesByComponent, setFormValuesByComponent] = useState<Record<string, Record<string, unknown>>>({});
  const [componentQueryData, setComponentQueryData] = useState<
    Record<string, Record<string, unknown>>
  >({});
  const [componentQueryRevision, setComponentQueryRevision] = useState(0);
  const [activeExtensionPageId, setActiveExtensionPageId] = useState(() =>
    stringFrom(new URLSearchParams(location.search).get("extension_page")),
  );
  const [uninstallPlan, setUninstallPlan] = useState<ExtensionUninstallPlan | null>(null);
  const [uninstallError, setUninstallError] = useState("");
  const [oauthCallbackNotice, setOauthCallbackNotice] = useState<OAuthCallbackNotice | null>(null);
  const oauthCallbackRefetchedKeyRef = useRef("");

  const { locale, t } = useI18n();
  useEffect(() => {
    const notice = oauthCallbackNoticeFromSearch(
      location.search,
      location.hash,
      stringFrom(extensionId), t
    );
    if (!notice) {
      return;
    }
    setOauthCallbackNotice(notice);
    const params = new URLSearchParams(location.search);
    params.delete("oauth");
    params.delete("reason");
    params.delete("oauth_connection_id");
    params.delete("oauth_connection_preserved");
    const search = params.toString();
    ignoreAsyncResult(navigate({
      pathname: location.pathname,
      search: search ? `?${search}` : "",
      hash: location.hash,
    }, { replace: true }));
  }, [extensionId, location.hash, location.pathname, location.search, navigate, t]);

  useEffect(() => {
    setActiveTab(normalizeActiveTab(location.hash));
  }, [location.hash]);

  // The server names each extension in the user's language.
  const extensionsQuery = useQuery({
    queryKey: ["extensions", "all", locale],
    queryFn: () => fetchExtensions("all"),
    enabled: !extensionId,
    refetchOnMount: "always",
  });
  const archiveLifecycleQuery = useQuery({
    queryKey: ["extensions", "archive-lifecycle-capability"],
    queryFn: fetchExtensionArchiveLifecycleAvailability,
    refetchOnMount: "always",
  });
  const archiveLifecycleAvailable = archiveLifecycleQuery.isSuccess
    && archiveLifecycleQuery.data?.available === true;
  const archiveLifecycleUnavailable = archiveLifecycleQuery.isSuccess
    && !archiveLifecycleAvailable;
  const archiveLifecycleVerificationFailed = archiveLifecycleQuery.isError;

  const extensions = useMemo(() => extensionsQuery.data || [], [extensionsQuery.data]);
  const stateFilteredExtensions = useMemo(() => {
    // A broken install (files missing, invalid bundle) is not active either: it
    // belongs with the disabled ones, where its Uninstall can be reached.
    if (viewFilter === "disabled") {
      return extensions.filter((item) => extensionState(item) !== "active");
    }
    if (viewFilter === "all") {
      return extensions;
    }
    return extensions.filter((item) => extensionState(item) === "active");
  }, [extensions, viewFilter]);
  // AC17-R66: counted over every install, not the filtered view, because the whole point is
  // to name what the active filter is hiding.
  const disabledExtensionCount = useMemo(
    () => extensions.filter((item) => extensionState(item) !== "active").length,
    [extensions],
  );
  const categoryOptions = useMemo(
    () => [...new Set(stateFilteredExtensions.map((item) => extensionCategory(item.category)))]
      .sort((left, right) => catalogLabel(left).localeCompare(catalogLabel(right))),
    [stateFilteredExtensions],
  );
  const visibleExtensions = useMemo(() => {
    const normalizedSearch = extensionSearch.trim().toLowerCase();
    return stateFilteredExtensions.filter((item) => {
      if (categoryFilter !== "all" && extensionCategory(item.category) !== categoryFilter) {
        return false;
      }
      if (!normalizedSearch) {
        return true;
      }
      const searchableText = `${stringFromUnknownValue(item.display_name)} ${item.extension_id}`
        .toLowerCase();
      return searchableText.includes(normalizedSearch);
    });
  }, [categoryFilter, extensionSearch, stateFilteredExtensions]);
  const hasCatalogFilters = anyTrue([Boolean(extensionSearch.trim()), categoryFilter !== "all"]);
  const detailMode = Boolean(extensionId);
  const urlProjectId = useMemo(
    () => new URLSearchParams(location.search).get("project_id") || "",
    [location.search],
  );
  // A workspace event can render before its URL update; never use the old project
  // for the new workspace's options or project-owned requests during that gap.
  const selectedProjectId = projectWorkspaceId === workspaceId ? urlProjectId : "";
  useEffect(() => {
    if (!urlProjectId) setProjectWorkspaceId(workspaceId);
  }, [urlProjectId, workspaceId]);
  const detailBackLink = extensionDetailBackLink(canAdministerPlatform, selectedProjectId, t);
  const selectedExtension = useMemo(
    () => extensions.find((item) => item.extension_id === extensionId),
    [extensionId, extensions],
  );
  const detailQuery = useQuery({
    queryKey: ["extensions", "detail", extensionId || ""],
    queryFn: () => fetchExtension(firstStringFromUnknownValue([extensionId, ""])),
    enabled: Boolean(extensionId),
    refetchOnMount: "always",
  });
  const storedRememberedProjectId = useMemo(() => {
    if (globalThis.window === undefined) {
      return "";
    }
    return stringFrom(globalThis.localStorage.getItem("platform-last-project-id"));
  }, []);

  const manifestQuery = useQuery({
    queryKey: ["extensions", "compiled-manifest", extensionId || ""],
    queryFn: () => fetchExtensionCompiledManifest(firstStringFromUnknownValue([extensionId, ""])),
    enabled: Boolean(extensionId),
    refetchOnMount: "always",
  });
  // What the user reads in their language; ids keep reading the manifest itself.
  const localizedManifest = useMemo(
    () => localizeExtensionManifest(manifestQuery.data, locale),
    [locale, manifestQuery.data],
  );
  const sourceDetailExtension = useMemo(() => {
    if (detailQuery.data) {
      return detailQuery.data;
    }
    if (selectedExtension && !detailQuery.isError) {
      return selectedExtension;
    }
    if (!extensionId) {
      return null;
    }
    const manifestEntity = objectOrEmpty(manifestQuery.data);
    return normalizeExtensionRecord({
      extension_id: extensionId,
      display_name: manifestEntity.display_name || manifestEntity.name || extensionId,
      description: manifestEntity.description || "",
      version: manifestEntity.version || "",
      compiled_manifest: manifestEntity,
      manifest: manifestEntity.descriptor || manifestEntity.manifest || {},
      vendor_name: manifestEntity.vendor_name || "",
      support_url: manifestEntity.support_url || "",
      website_url: manifestEntity.website_url || "",
      broken: false,
      state: "unknown",
    });
  }, [detailQuery.data, detailQuery.isError, extensionId, manifestQuery.data, selectedExtension]);
  const detailExtension = useMemo(
    () => sourceDetailExtension
      ? localizeExtensionIdentity(sourceDetailExtension, localeBundlesFrom(manifestQuery.data), locale)
      : sourceDetailExtension,
    [locale, manifestQuery.data, sourceDetailExtension],
  );
  const uninstallPlanExtension = uninstallExtensionForPlan(
    uninstallPlan,
    extensions,
    detailExtension,
  );
  const uninstallLifecycleAvailable = Boolean(
    uninstallPlanExtension && archiveLifecycleAvailable,
  );
  const llmProviderContract = useMemo(() => {
    const runtime = objectOrEmpty(objectOrEmpty(manifestQuery.data).runtime);
    return objectOrEmpty(objectOrEmpty(runtime.extension_contract_v2).llm_provider);
  }, [manifestQuery.data]);
  const extensionInstallId = stringFrom(
    detailExtension?.install_id || detailExtension?.extension_install_id,
  );
  const llmProviderInstallId = useMemo(() => Object.keys(llmProviderContract).length
    ? stringFrom(detailExtension?.install_id || detailExtension?.extension_install_id || detailExtension?.extension_id)
    : "", [detailExtension, llmProviderContract]);
  const llmProviderConnectionConfig = useMemo(() => {
    const installConfig = objectOrEmpty(detailExtension?.config_jsonb);
    return objectOrEmpty(installConfig.llm_provider_connection_config);
  }, [detailExtension]);
  const activationBlockedReason = useMemo(
    () => llmProviderInstallId
      && !detailExtension?.active
      && stringFrom(detailExtension?.runtime_mode).toLowerCase() === "remote_https"
      && !stringFrom(detailExtension?.remote_base_url)
      ? "Save a public HTTPS runtime endpoint below before enabling this extension."
      : "",
    [detailExtension, llmProviderInstallId],
  );
  const manifestPages = useMemo(() => manifestUiPages(localizedManifest), [localizedManifest]);
  const oauthProviderSetupPages = useMemo(
    () => new Map(
      listFromUnknown(objectOrEmpty(manifestQuery.data).oauth_providers)
        .map((provider) => [
          stringFrom(provider.provider_id),
          stringFrom(provider.setup_page_id),
        ] as const)
        .filter(([providerId, setupPageId]) => Boolean(providerId && setupPageId)),
    ),
    [manifestQuery.data],
  );
  const oauthProviders = useMemo(
    () => oauthProvidersFromManifest(localizedManifest),
    [localizedManifest],
  );
  const oauthReadinessQueries = useQueries({
    queries: oauthProviders.map((provider) => ({
      queryKey: [
        "extension-oauth",
        selectedProjectId,
        extensionInstallId,
        provider.provider_id,
        "configurations",
      ],
      queryFn: () => fetchExtensionOAuthConfigurations({
        projectId: selectedProjectId,
        installId: extensionInstallId,
        providerId: provider.provider_id,
      }),
      enabled: Boolean(selectedProjectId && extensionInstallId && provider.provider_id),
    })),
  });
  const oauthSetupStatusQueries = useQueries({
    queries: oauthProviders.map((provider) => ({
      queryKey: [
        "extension-oauth",
        selectedProjectId,
        extensionInstallId,
        provider.provider_id,
        "setup-status",
      ],
      queryFn: () => fetchExtensionOAuthSetupStatus({
        projectId: selectedProjectId,
        installId: extensionInstallId,
        providerId: provider.provider_id,
      }),
      enabled: Boolean(selectedProjectId && extensionInstallId && provider.provider_id),
    })),
  });
  const oauthProviderReadiness = Object.fromEntries(
    oauthProviders.map((provider, index) => {
      const query = oauthReadinessQueries[index];
      const statusQuery = oauthSetupStatusQueries[index];
      const credentialsReady = query?.data?.some((configuration) => (
        configuration.status === "ready_for_authorization" || configuration.status === "verified"
      ));
      const ready = credentialsReady && statusQuery?.data?.callback_registration_ready === true;
      let readiness: "blocked" | "loading" | "ready" = "blocked";
      if (query?.isPending || statusQuery?.isPending) readiness = "loading";
      else if (ready) readiness = "ready";
      return [provider.provider_id, readiness];
    }),
  ) as Record<string, "blocked" | "loading" | "ready">;
  const oauthConnectionTypes = useMemo(
    () => oauthConnectionTypesFromManifest(manifestQuery.data),
    [manifestQuery.data],
  );
  const projectRequired = useMemo(
    () => isProjectScopedExtension(manifestQuery.data),
    [manifestQuery.data],
  );
  const activeManifestPage = manifestPages.find((page) => stringFrom(page.page_id || page.id || page.key) === activeExtensionPageId) || manifestPages[0];
  const connectionCreationBlockedReason = "";
  const activePageRequiresProject = manifestPageRequiresProject(
    activeManifestPage,
    projectRequired,
  );
  const activePageProjectId = activePageRequiresProject ? selectedProjectId : "";
  useQuery({
    queryKey: ["extensions", "preview", extensionId || "", activePageProjectId],
    queryFn: () =>
      fetchExtensionPreview(
        firstStringFromUnknownValue([extensionId, ""]),
        activePageProjectId || undefined,
      ),
    enabled: Boolean(extensionId && canAdministerPlatform),
  });
  const externalEffectsQuery = useQuery({
    queryKey: ["extensions", "external-effects", extensionInstallId, selectedProjectId],
    queryFn: () => fetchExtensionExternalEffects(selectedProjectId, extensionInstallId),
    enabled: Boolean(detailExtension?.active && extensionInstallId && selectedProjectId),
    retry: false,
    refetchOnMount: "always",
  });
  const callbacksQuery = useQuery({
    queryKey: ["extensions", "callbacks", extensionId || "", activePageProjectId],
    queryFn: () => fetchExtensionCallbacks(firstStringFromUnknownValue([extensionId, ""]), activePageProjectId),
    enabled: Boolean(canManageProject && detailExtension?.extension_id && activePageProjectId),
  });
  const projectsQuery = useQuery({
    queryKey: ["extensions", "projects", detailExtension?.extension_id || "", workspaceId],
    queryFn: () => fetchProjectsList(
      { page: 1, perPage: 200, search: "", workspaceId },
      { presentation: "background" },
    ),
    enabled: Boolean(detailMode && detailExtension?.extension_id && activePageRequiresProject),
  });
  const projects = useMemo(
    () => (Array.isArray(projectsQuery.data?.projects) ? projectsQuery.data.projects : []),
    [projectsQuery.data?.projects],
  );
  const rememberedProjectId = !workspaceChanged
    || projects.some((project) => stringFrom(project.id) === storedRememberedProjectId)
    ? storedRememberedProjectId
    : "";
  const projectConnectionsQuery = useQuery({
    queryKey: ["extensions", "project-connections", detailExtension?.extension_id || "", activePageProjectId],
    queryFn: () => fetchProjectConnectionsList(activePageProjectId),
    enabled: Boolean(detailExtension?.active && activePageProjectId),
  });
  const refetchCallbackConnections = projectConnectionsQuery.refetch;
  const oauthCallbackConnection = useMemo(() => {
    if (
      !oauthCallbackNotice?.connectionId
      || !activePageProjectId
      || oauthCallbackNotice.projectId !== activePageProjectId
    ) return undefined;
    return (projectConnectionsQuery.data || []).find((row) => (
      stringFrom(row.connection_id) === oauthCallbackNotice.connectionId
      && stringFrom(row.extension_install_id) === extensionInstallId
    ));
  }, [
    activePageProjectId,
    extensionInstallId,
    oauthCallbackNotice?.connectionId,
    oauthCallbackNotice?.projectId,
    projectConnectionsQuery.data,
  ]);
  const oauthFocusConnectionId = oauthCallbackConnection
    ? stringFrom(oauthCallbackConnection.connection_id)
    : "";

  useEffect(() => {
    if (!oauthCallbackNotice) return;
    const currentPageId = stringFrom(new URLSearchParams(location.search).get("extension_page"));
    if (
      oauthCallbackNotice.extensionId !== extensionId
      || oauthCallbackNotice.projectId !== selectedProjectId
      || oauthCallbackNotice.pageId !== currentPageId
    ) {
      setOauthCallbackNotice(null);
    }
  }, [extensionId, location.search, oauthCallbackNotice, selectedProjectId]);

  useEffect(() => {
    if (!oauthCallbackNotice?.connectionId || !activePageProjectId) {
      oauthCallbackRefetchedKeyRef.current = "";
      return;
    }
    if (!projectConnectionsQuery.isFetched) return;
    const refetchKey = `${activePageProjectId}:${oauthCallbackNotice.connectionId}`;
    if (oauthCallbackRefetchedKeyRef.current === refetchKey) return;
    oauthCallbackRefetchedKeyRef.current = refetchKey;
    ignoreAsyncResult(refetchCallbackConnections());
  }, [
    activePageProjectId,
    oauthCallbackNotice?.connectionId,
    projectConnectionsQuery.isFetched,
    refetchCallbackConnections,
  ]);

  useEffect(() => {
    if (!oauthCallbackNotice?.helpAnchor || oauthCallbackNotice.pageId !== activeExtensionPageId) return;
    focusManifestAnchor(activeExtensionPageId, oauthCallbackNotice.helpAnchor);
  }, [activeExtensionPageId, activeManifestPage, oauthCallbackNotice?.helpAnchor, oauthCallbackNotice?.pageId]);
  const projectStateQuery = useQuery({
    queryKey: ["extensions", "project-state", detailExtension?.extension_id || "", activePageProjectId],
    queryFn: () => fetchExtensionProjectState(activePageProjectId, detailExtension!.extension_id),
    enabled: Boolean(detailExtension?.active && activePageProjectId),
  });
  const projectOptions = useMemo(() => {
    if (!selectedProjectId || workspaceChanged) {
      return projects;
    }
    const hasSelectedProject = projects.some(
      (project) => stringFrom(project.id) === selectedProjectId,
    );
    if (hasSelectedProject) {
      return projects;
    }
    return [
      {
        id: selectedProjectId,
        name: `Project ${selectedProjectId}`,
      },
      ...projects,
    ];
  }, [projects, selectedProjectId, workspaceChanged]);

  function handleProjectSelection(nextProjectId: string) {
    const normalizedProjectId = stringFrom(nextProjectId);
    if (workspaceChanged && normalizedProjectId && !projects.some((project) => stringFrom(project.id) === normalizedProjectId)) return;
    setProjectWorkspaceId(workspaceId);
    if (globalThis.window !== undefined && normalizedProjectId) {
      globalThis.localStorage.setItem("platform-last-project-id", normalizedProjectId);
    }
    const params = new URLSearchParams(location.search);
    if (normalizedProjectId) {
      params.set("project_id", normalizedProjectId);
    } else {
      params.delete("project_id");
    }
    const search = params.toString();
    ignoreAsyncResult(navigate({
      pathname: location.pathname,
      search: search ? `?${search}` : "",
      hash: location.hash || "#installed",
    }));
  }

  // A failed save names the field it refused. Moving focus there means the user
  // lands on the control to correct instead of hunting for it from a message at
  // the top of the form.
  useEffect(() => {
    for (const [componentId, entry] of Object.entries(componentErrors)) {
      if (!entry.fieldPath) {
        continue;
      }
      const control = globalThis.document?.getElementById(
        manifestFieldControlId(componentId, entry.fieldPath),
      );
      if (control instanceof HTMLElement) {
        control.focus();
        return;
      }
    }
  }, [componentErrors]);

  function handleExtensionPageSelection(pageId: string) {
    const normalizedPageId = stringFrom(pageId);
    // The result of an action belongs to the page it was run from. Carrying it
    // onto the documentation tabs pushed the content the user came to read below
    // a diagnostic panel they had already finished with.
    setActionResultByExtension({});
    setComponentErrors({});
    setActiveExtensionPageId(normalizedPageId);
    const params = new URLSearchParams(location.search);
    if (normalizedPageId) {
      params.set("extension_page", normalizedPageId);
    } else {
      params.delete("extension_page");
    }
    params.delete("oauth_provider_id");
    const search = params.toString();
    ignoreAsyncResult(navigate({
      pathname: location.pathname,
      search: search ? `?${search}` : "",
      hash: location.hash,
    }, { replace: true }));
  }

  useEffect(() => {
    const firstPageId = stringFrom(manifestPages[0]?.page_id || manifestPages[0]?.id || manifestPages[0]?.key);
    const params = new URLSearchParams(location.search);
    const providerHint = stringFrom(params.get("oauth_provider_id"));
    const hintedSetupPageId = stringFrom(oauthProviderSetupPages.get(providerHint));
    const hasHintedSetupPage = Boolean(
      hintedSetupPageId
      && manifestPages.some(
        (page) => stringFrom(page.page_id || page.id || page.key) === hintedSetupPageId,
      ),
    );
    let requestedPageId = stringFrom(params.get("extension_page"));
    if (providerHint) requestedPageId = hasHintedSetupPage ? hintedSetupPageId : "";
    if (providerHint) {
      setActiveExtensionPageId(hasHintedSetupPage ? hintedSetupPageId : firstPageId);
      return;
    }
    const hasRequestedPage = Boolean(
      requestedPageId
      && manifestPages.some(
        (page) => stringFrom(page.page_id || page.id || page.key) === requestedPageId,
      ),
    );
    setActiveExtensionPageId(hasRequestedPage ? requestedPageId : firstPageId);
  }, [extensionId, location.search, manifestPages, oauthProviderSetupPages]);

  useEffect(() => {
    setComponentErrors({});
    setComponentQueryParams((current) => Object.keys(current).length ? {} : current);
    setComponentQueryData({});
    setFormValuesByComponent({});
  }, [activeExtensionPageId, activePageProjectId, detailExtension?.extension_id]);

  useEffect(() => {
    if (!activeManifestPage) {
      return;
    }
    const components = listFromUnknown(activeManifestPage.components);
    setFormValuesByComponent((current) => {
      let changed = false;
      const next = { ...current };
      for (const component of components) {
        const componentId = stringFrom(component.component_id || component.id || component.key);
        if (!componentId || next[componentId]) {
          continue;
        }
        const values = manifestQueryValues(componentQueryData[componentId] || component);
        if (!Object.keys(values).length) {
          continue;
        }
        next[componentId] = values;
        changed = true;
      }
      return changed ? next : current;
    });
  }, [activeManifestPage, componentQueryData]);

  useEffect(() => {
    const extensionKey = detailExtension?.extension_id;
    const activePageId = stringFrom(
      activeManifestPage?.page_id || activeManifestPage?.id || activeManifestPage?.key,
    );
    if (
      !extensionKey
      || !activeExtensionPageId
      || !activeManifestPage
      || (activePageId && activeExtensionPageId !== activePageId)
      || (activePageRequiresProject && !activePageProjectId)
    ) {
      return;
    }
    const components = listFromUnknown(activeManifestPage.components);
    const queryComponents = components.filter((component) => Boolean(componentQueryId(component)));
    if (!queryComponents.length) {
      return;
    }
    let disposed = false;
    const run = async () => {
      for (const component of queryComponents) {
        const queryId = componentQueryId(component);
        const componentId = stringFrom(component.component_id || component.id || component.key);
        if (!queryId || !componentId) {
          continue;
        }
        try {
          const queryParams = {
            ...componentQueryParamsForComponent(component),
            ...objectOrEmpty(componentQueryParams[componentId]),
          };
          const payload = await executeExtensionQuery(extensionKey, queryId, {
            component_id: componentId,
            context: activePageProjectId ? { project_id: activePageProjectId } : {},
            page_id: activePageId,
            params: queryParams,
          });
          if (disposed) {
            return;
          }
          const normalized =
            payload && typeof payload === "object" && !Array.isArray(payload)
              ? normalizeComponentPayload(payload)
              : {};
          setComponentQueryData((current) => ({ ...current, [componentId]: normalized }));
          setComponentErrors((current) => {
            if (!current[componentId]) {
              return current;
            }
            const next = { ...current };
            delete next[componentId];
            return next;
          });
        } catch (error) {
          if (disposed) {
            return;
          }
          setComponentErrors((current) => ({
            ...current,
            [componentId]: {
              message:
                error instanceof Error ? error.message : t("react.extensions.failed_to_load_extension_component_data", "Failed to load extension component data."),
              fieldPath: "",
            },
          }));
        }
      }
    };
    ignoreAsyncResult(run());
    return () => {
      disposed = true;
    };
  }, [
    activeManifestPage,
    activeExtensionPageId,
    componentQueryRevision,
    componentQueryParams,
    detailExtension?.extension_id,
    activePageProjectId,
    activePageRequiresProject,
    t,
  ]);

  function setComponentError(
    component: Record<string, unknown>,
    message: string,
    fieldPath = "",
  ) {
    const componentId = stringFrom(component.component_id || component.id || component.key);
    if (!componentId) {
      return;
    }
    setComponentErrors((current) => ({ ...current, [componentId]: { message, fieldPath } }));
  }

  return {
    extensions,
    setComponentError,
    setActiveTab,
    updateSearchParams,
    setViewFilter,
    setBusyById,
    setStatusById,
    setToggleErrorById,
    queryClient,
    setComponentErrors,
    setFormValuesByComponent,
    setBrowserApprovalError,
    setPendingBrowserApproval,
    archiveLifecycleAvailable,
    setCatalogInstallById,
    pendingBrowserApproval,
    busyById,
    setInstallDropActive,
    setUninstallError,
    setUninstallPlan,
    uninstallPlan,
    uninstallLifecycleAvailable,
    selectedExtension,
    navigate,
    setComponentQueryData,
    setActionResultByExtension,
    pendingManifestActionIntentRef,
    externalEffectsQuery,
    selectedProjectId,
    canManageProject,
    setComponentQueryRevision,
    activePageProjectId,
    detailExtension,
    formValuesByComponent,
    activeManifestPage,
    manifestQuery,
    callbacksQuery,
    setCallbackStatuses,
    extensionInstallId,
    location,
    projectConnectionsQuery,
    oauthConnectionTypes,
    projectStateQuery,
    oauthCallbackNotice,
    oauthCallbackConnection,
    archiveLifecycleQuery,
    archiveLifecycleVerificationFailed,
    archiveLifecycleUnavailable,
    setOAuthCallbackNotice: setOauthCallbackNotice,
    detailMode,
    extensionId,
    visibleExtensions,
    detailBackLink,
    activeTab,
    installDropActive,
    viewFilter,
    fileInputRef,
    setExtensionSearch,
    extensionSearch,
    setCategoryFilter,
    categoryFilter,
    categoryOptions,
    statusById,
    toggleErrorById,
    activationBlockedReason,
    connectionCreationBlockedReason,
    actionResultByExtension,
    callbackStatuses,
    componentErrors,
    componentQueryData,
    componentQueryParams,
    handleProjectSelection,
    manifestPages,
    oauthProviderReadiness,
    projectOptions,
    activePageRequiresProject,
    projectsQuery,
    rememberedProjectId,
    handleExtensionPageSelection,
    setComponentQueryParams,
    oauthFocusConnectionId,
    llmProviderInstallId,
    llmProviderContract,
    llmProviderConnectionConfig,
    extensionsQuery,
    detailQuery,
    hasCatalogFilters,
    disabledExtensionCount,
    canAdministerPlatform,
    catalogInstallById,
    uninstallError,
    browserApprovalError,
  };
}
