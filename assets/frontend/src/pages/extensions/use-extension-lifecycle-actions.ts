import { useI18n } from "@/lib/i18n/useI18n";
import {
  type ExtensionRecord,
  type ExtensionUninstallPlan,
  setExtensionEnabled,
  installExtensionFromFile,
  fetchExtensionUninstallPlan,
  confirmExtensionUninstall,
  checkExtensionHealth,
  fetchExtension,
} from "@/pages/extensions/api";
import { extensionInstallWarnings } from "@/pages/extensions/extension-install-warnings";
import { stringFrom } from "@/pages/extensions/extension-manifest-utils";
import { invalidateModelRoutingQueries } from "@/lib/utils/model-provider-state";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  CATALOG_RELEASE_CHANGED,
  EXTENSION_CATALOG_QUERY_KEY,
  type ExtensionCatalogInstallState,
  type ExtensionCatalogEntry,
  catalogErrorCode,
  fetchExtensionCatalog,
  installExtensionFromCatalog,
} from "@/pages/extensions/catalog-api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { MARKETPLACE_ENTRY_PARAM, withoutMarketplaceParams } from "@/pages/extensions/marketplace-model";
import { type DragEvent } from "react";
import {
  type ExtensionsViewFilter,
  withExtensionActiveState,
  extensionArtifactCatalogQueryKeys,
} from "./hub-support";
import {
  browserAuthorityApprovalFromError,
  extensionActionFailureMessage,
} from "./manifest-action-support";

import type { useExtensionsHubState } from "./use-extensions-hub-state";
import { isHttpError } from "@/lib/http/errors";

/** The refreshed plan a 409 "impact changed" carries, if this error is one. */
function stalePlanFromError(error: unknown): ExtensionUninstallPlan | null {
  if (!isHttpError(error) || error.status !== 409) return null;
  const payload = error.payload && typeof error.payload === "object"
    ? error.payload as Record<string, unknown>
    : {};
  const details = payload.details && typeof payload.details === "object"
    ? (payload.details as Record<string, unknown>).context
    : undefined;
  const candidates = [
    payload.current_plan,
    details && typeof details === "object" ? (details as Record<string, unknown>).current_plan : undefined,
  ];
  const plan = candidates.find((value): value is ExtensionUninstallPlan =>
    Boolean(value)
    && typeof value === "object"
    && typeof (value as ExtensionUninstallPlan).revision === "string"
    && Array.isArray((value as ExtensionUninstallPlan).remove));
  return plan ?? null;
}

export function useExtensionLifecycleActions(hubState: ReturnType<typeof useExtensionsHubState>) {
  const { t } = useI18n();
  const {
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
    extensionId: routeExtensionId,
    navigate,
  } = hubState;

  /**
   * The tab lives in the hash, through the router so the Catalog's own URL
   * writes see it. The Catalog's browse state (`mk_*`) belongs to that tab:
   * leaving for Installed drops it, so coming back starts from the whole
   * catalog. Choosing Catalog, including the tab already shown, closes an open
   * extension's page and keeps the search and filters: the tab is the more
   * visible way back to the results.
   */
  function switchTab(nextTab: "installed" | "marketplace") {
    setActiveTab(nextTab);
    updateSearchParams(
      (latest) => {
        if (nextTab === "installed") return withoutMarketplaceParams(latest);
        const next = new URLSearchParams(latest);
        next.delete(MARKETPLACE_ENTRY_PARAM);
        return next;
      },
      { hash: `#${nextTab}` },
    );
  }

  function handleViewFilterChange(nextFilter: ExtensionsViewFilter) {
    setViewFilter(nextFilter);
  }

  async function withBusy(extensionKey: string, action: () => Promise<void>) {
    setBusyById((current) => ({ ...current, [extensionKey]: true }));
    try {
      await action();
    } finally {
      setBusyById((current) => ({ ...current, [extensionKey]: false }));
    }
  }

  async function handleToggle(extension: ExtensionRecord) {
    const extensionKey = extension.extension_id;
    const nextActive = !extension.active;
    if (extension.broken && !extension.active) {
      setStatusById((current) => ({
        ...current,
        [extensionKey]: extension.broken_reason || "Broken extensions cannot be enabled.",
      }));
      return;
    }
    if (
      nextActive
      && stringFrom(extension.runtime_mode).toLowerCase() === "remote_https"
      && !stringFrom(extension.remote_base_url)
    ) {
      setStatusById((current) => ({
        ...current,
        [extensionKey]:
          "Save a public HTTPS runtime endpoint on the extension page before enabling.",
      }));
      return;
    }
    const applyToggle = async (approvedImpactHash = "") => {
      await withBusy(extensionKey, async () => {
        setToggleErrorById((current) => ({ ...current, [extensionKey]: "" }));
        setStatusById((current) => ({ ...current, [extensionKey]: "Updating active state..." }));
        if (approvedImpactHash) {
          await setExtensionEnabled(extensionKey, nextActive, approvedImpactHash);
        } else {
          await setExtensionEnabled(extensionKey, nextActive);
        }
        const nextState = nextActive ? "active" : "disabled";
        // Every language's copy of the list carries the same state.
        queryClient.setQueriesData<ExtensionRecord[] | undefined>(
          { queryKey: ["extensions", "all"] },
          (current) => withExtensionActiveState(
            current,
            extensionKey,
            nextActive,
            nextState,
          ),
        );
        queryClient.setQueryData<ExtensionRecord | undefined>(
          ["extensions", "detail", extensionKey],
          (current) => (current ? { ...current, active: nextActive, state: nextState } : current),
        );
        if (!nextActive) {
          setComponentErrors({});
          setFormValuesByComponent({});
        }
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["extensions", "all"] }),
          queryClient.invalidateQueries({ queryKey: ["extensions", "detail", extensionKey] }),
          ...extensionArtifactCatalogQueryKeys.map((queryKey) =>
            queryClient.invalidateQueries({ queryKey }),
          ),
          invalidateModelRoutingQueries(queryClient),
        ]);
        setStatusById((current) => ({
          ...current,
          [extensionKey]: `Extension ${nextActive ? "activated" : "disabled"}.`,
        }));
      });
    };
    try {
      await applyToggle();
    } catch (error) {
      const approval = browserAuthorityApprovalFromError(error);
      if (approval && nextActive) {
        setStatusById((current) => ({
          ...current,
          [extensionKey]: "Awaiting browser authority approval.",
        }));
        setBrowserApprovalError("");
        setPendingBrowserApproval({
          approval,
          cancelMessage: "Activation cancelled.",
          retry: applyToggle,
          statusKey: extensionKey,
        });
        return;
      }
      const failureMessage = extensionActionFailureMessage(
        error,
        `Failed to ${nextActive ? "activate" : "disable"} extension.`
      );
      setStatusById((current) => ({
        ...current,
        [extensionKey]: failureMessage,
      }));
      setToggleErrorById((current) => ({ ...current, [extensionKey]: failureMessage }));
    }
  }

  async function handleInstallFileChange(file: File | null) {
    if (!file || !archiveLifecycleAvailable) {
      return;
    }
    const syntheticKey = "__install__";
    const installFile = async (approvedImpactHash = "") => {
      await withBusy(syntheticKey, async () => {
        setToggleErrorById((current) => ({ ...current, [syntheticKey]: "" }));
        setStatusById((current) => ({ ...current, [syntheticKey]: `Installing ${file.name}...` }));
        const payload = approvedImpactHash
          ? await installExtensionFromFile(file, approvedImpactHash)
          : await installExtensionFromFile(file);
        await refreshAfterInstalledSetChanged();
        setViewFilter("active");
        const installedExtension = payload.extension;
        const operation = firstStringFromUnknownValue([installedExtension?.lifecycle_operation, ""]).trim().toLowerCase();
        const displayName = firstStringFromUnknownValue([installedExtension?.display_name, installedExtension?.extension_id, file.name]).trim();
        const fromVersion = firstStringFromUnknownValue([installedExtension?.from_version, ""]).trim();
        const toVersion = firstStringFromUnknownValue([installedExtension?.to_version, installedExtension?.version, ""]).trim();
        let versionChange = "";
        if (fromVersion && toVersion) {
          versionChange = ` from ${fromVersion} to ${toVersion}`;
        } else if (toVersion) {
          versionChange = ` to ${toVersion}`;
        }
        const installedVersion = toVersion ? ` ${toVersion}` : "";
        const doneMessage = operation === "updated"
          ? `Updated ${displayName}${versionChange}.`
          : `Installed ${displayName}${installedVersion}.`;
        // The install check's warnings are for the administrator to read now,
        // not only on the extension page.
        const installedId = firstStringFromUnknownValue([installedExtension?.extension_id, ""]).trim();
        let installed: ExtensionRecord | null = null;
        try {
          installed = installedId ? ((await fetchExtension(installedId)) ?? null) : null;
        } catch {
          installed = null;
        }
        const warnings = extensionInstallWarnings(installed);
        const statusMessage = warnings.length
          ? `${doneMessage} Install check, ${warnings.length} to review: ${warnings.join("; ")}`
          : doneMessage;
        setStatusById((current) => ({
          ...current,
          [syntheticKey]: statusMessage,
        }));
      });
    };
    try {
      await installFile();
    } catch (error) {
      const approval = browserAuthorityApprovalFromError(error);
      if (approval) {
        setStatusById((current) => ({
          ...current,
          [syntheticKey]: "Awaiting browser authority approval.",
        }));
        setBrowserApprovalError("");
        setPendingBrowserApproval({
          approval,
          cancelMessage: "Installation cancelled.",
          retry: installFile,
          statusKey: syntheticKey,
        });
        return;
      }
      const failureMessage = extensionActionFailureMessage(
        error,
        "Failed to install extension package."
      );
      setStatusById((current) => ({
        ...current,
        [syntheticKey]: failureMessage,
      }));
      // The same slot carries progress, success and failure. Marking which one
      // this is here — where the code already knows — is what lets the notice
      // read as a failure without anyone having to interpret its text.
      setToggleErrorById((current) => ({ ...current, [syntheticKey]: failureMessage }));
    }
  }

  /**
   * Everything that goes stale when the installed set changes. Both install
   * paths use it: a catalog install of an LLM provider or an artifact
   * extension left model routing and the artifact catalogs showing the old
   * world until a reload.
   */
  async function refreshAfterInstalledSetChanged() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["extensions", "all"] }),
      queryClient.invalidateQueries({ queryKey: EXTENSION_CATALOG_QUERY_KEY }),
      ...extensionArtifactCatalogQueryKeys.map((queryKey) =>
        queryClient.invalidateQueries({ queryKey }),
      ),
      invalidateModelRoutingQueries(queryClient),
    ]);
  }

  function setCatalogInstall(extensionId: string, next: ExtensionCatalogInstallState) {
    setCatalogInstallById((current) => ({ ...current, [extensionId]: next }));
  }

  const AWAITING_APPROVAL: ExtensionCatalogInstallState = {
    phase: "awaiting_approval",
    status: "Waiting for browser authority approval.",
  };

  /**
   * After the server refused a release the catalog no longer lists, reload the
   * listing so the card and the page show what is published now, and say so.
   * Nothing is installed: the user presses the button again, which now names
   * the new version. Undefined when the reloaded listing does not explain the
   * refusal, and the server's own reason should stand.
   */
  async function supersededState(
    entry: ExtensionCatalogEntry,
  ): Promise<ExtensionCatalogInstallState | undefined> {
    let current: ExtensionCatalogEntry | undefined;
    try {
      const listing = await queryClient.fetchQuery({
        queryKey: EXTENSION_CATALOG_QUERY_KEY,
        queryFn: fetchExtensionCatalog,
        staleTime: 0,
      });
      current = listing.items.find((item) => item.extension_id === entry.extension_id);
    } catch {
      return undefined;
    }
    if (!current) return undefined;
    if (current.version !== entry.version) {
      return {
        phase: "idle",
        outcome: "superseded",
        status: t("react.extensions.a_newer_version_value0_was_published", "A newer version {value0} was published.", { value0: current.version }),
      };
    }
    if (current.release_zip_sha256 !== entry.release_zip_sha256) {
      return {
        phase: "idle",
        outcome: "superseded",
        status: t("react.extensions.this_release_was_republished_check_it_and_try_again", "This release was republished. Check it and try again."),
      };
    }
    return undefined;
  }

  /**
   * Install one catalog entry, or update an older install to it, routing a
   * `412` into the same browser-authority approval modal the file-upload
   * install uses. An update is the same call: the server sends a newer
   * release through the archive installer's update path.
   *
   * The whole lifecycle is tracked here rather than in the card, because only
   * this function knows whether a `412` ended in an install, a cancellation or
   * a failure. It reports `Installed.` or `Updated to X.` on exactly one path:
   * a request that came back without raising.
   */
  function installCatalogExtension(entry: ExtensionCatalogEntry) {
    const extensionId = entry.extension_id;
    const updating = entry.display_state === "update_available";
    const cancelledText = updating
      ? t("react.extensions.update_cancelled_browser_authority_was_not_approved", "Update cancelled; browser authority was not approved.")
      : t("react.extensions.install_cancelled_browser_authority_was_not_approved", "Install cancelled; browser authority was not approved.");

    const submit = async (approvedImpactHash = "") => {
      setCatalogInstall(extensionId, { phase: "installing", status: updating ? "Updating..." : "Installing..." });
      await installExtensionFromCatalog(
        entry.extension_id,
        entry.version,
        entry.release_zip_sha256,
        approvedImpactHash,
      );
      await refreshAfterInstalledSetChanged();
      setCatalogInstall(
        extensionId,
        updating
          ? { phase: "idle", status: `Updated to ${entry.version}.`, outcome: "updated" }
          : { phase: "idle", status: "Installed.", outcome: "installed" },
      );
    };

    const start = async () => {
      try {
        await submit();
      } catch (error) {
        const approval = browserAuthorityApprovalFromError(error);
        if (!approval) {
          const superseded = catalogErrorCode(error) === CATALOG_RELEASE_CHANGED
            ? await supersededState(entry)
            : undefined;
          setCatalogInstall(extensionId, superseded ?? {
            phase: "idle",
            status: extensionActionFailureMessage(error, updating ? "Update failed." : "Install failed.", t),
            outcome: "failed",
          });
          return;
        }
        setCatalogInstall(extensionId, AWAITING_APPROVAL);
        setPendingBrowserApproval({
          approval,
          cancelMessage: cancelledText,
          statusKey: extensionId,
          onAbandoned: () =>
            setCatalogInstall(extensionId, {
              phase: "idle",
              status: cancelledText,
              outcome: "cancelled",
            }),
          retry: async (impactHash: string) => {
            try {
              await submit(impactHash);
            } catch (retryError) {
              if (catalogErrorCode(retryError) === CATALOG_RELEASE_CHANGED) {
                const superseded = await supersededState(entry);
                setCatalogInstall(extensionId, superseded ?? {
                  phase: "idle",
                  status: extensionActionFailureMessage(retryError, updating ? "Update failed." : "Install failed.", t),
                  outcome: "failed",
                });
                // The approval names obsolete bytes. End it; a new click pins
                // the current release and asks for its own approval if needed.
                return;
              }
              // The modal stays open on a retry failure, so the card stays in
              // the pending state rather than claiming a terminal result.
              setCatalogInstall(extensionId, AWAITING_APPROVAL);
              throw retryError;
            }
          },
        });
      }
    };

    ignoreAsyncResult(start());
  }

  async function confirmBrowserAuthorityApproval() {
    const pending = pendingBrowserApproval;
    if (!pending) return;
    setBrowserApprovalError("");
    try {
      await pending.retry(pending.approval.impactHash);
      setPendingBrowserApproval(null);
    } catch (error) {
      const stale = browserAuthorityApprovalFromError(error);
      if (stale) {
        setPendingBrowserApproval({ ...pending, approval: stale });
        setBrowserApprovalError(
          t("react.extensions.the_extension_changed_while_approval_was_pending_review_the_updated_authority", "The extension changed while approval was pending. Review the updated authority."),
        );
        return;
      }
      setBrowserApprovalError(
        extensionActionFailureMessage(error, "Failed to apply browser authority approval.", t),
      );
    }
  }

  function handleInstallDragOver(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    if (archiveLifecycleAvailable && !busyById.__install__) {
      setInstallDropActive(true);
    }
  }

  function handleInstallDragLeave(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) {
      return;
    }
    setInstallDropActive(false);
  }

  async function handleInstallDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    setInstallDropActive(false);
    if (!archiveLifecycleAvailable || busyById.__install__) {
      return;
    }
    const nextFile = event.dataTransfer?.files?.[0] || null;
    if (!nextFile) {
      return;
    }
    if (!nextFile.name.toLowerCase().endsWith(".zip")) {
      const rejection = "Install extension from file accepts only .zip archives.";
      setStatusById((current) => ({ ...current, __install__: rejection }));
      setToggleErrorById((current) => ({ ...current, __install__: rejection }));
      return;
    }
    await handleInstallFileChange(nextFile);
  }

  async function handleUninstall(extension: ExtensionRecord) {
    if (!archiveLifecycleAvailable) {
      return;
    }
    const extensionKey = extension.extension_id;
    setUninstallError("");
    try {
      setUninstallPlan(await fetchExtensionUninstallPlan(extensionKey));
    } catch (error) {
      // A click must never do nothing: say why the plan did not open.
      setStatusById((current) => ({
        ...current,
        [extensionKey]: `Couldn't load the uninstall plan: ${extensionActionFailureMessage(
          error,
          "the server did not answer.", t
        )}`,
      }));
    }
  }

  async function confirmUninstall() {
    const plan = uninstallPlan;
    if (!plan || !uninstallLifecycleAvailable) return;
    const extensionKey = plan.extension_id;
    await withBusy(extensionKey, async () => {
      setStatusById((current) => ({ ...current, [extensionKey]: "Uninstalling extension..." }));
      try {
        await confirmExtensionUninstall(extensionKey, plan.revision);
      } catch (error) {
        // Never leave the row saying "Uninstalling…" after a refusal.
        const refreshed = stalePlanFromError(error);
        if (refreshed) {
          setUninstallPlan(refreshed);
          setUninstallError(t("react.extensions.the_plan_changed_review_it_and_confirm_again", "The plan changed; review it and confirm again."));
          setStatusById((current) => ({ ...current, [extensionKey]: "" }));
          return;
        }
        setUninstallError(error instanceof Error ? error.message : t("react.extensions.extension_uninstall_failed", "Extension uninstall failed."));
        setStatusById((current) => ({ ...current, [extensionKey]: "Uninstall failed." }));
        return;
      }
      setUninstallPlan(null);
      // Decided before the refetch: once the list reloads without it, nothing
      // in the list names the extension any more (the detail page stayed on
      // a stale card because of that).
      const onItsOwnPage = routeExtensionId === extensionKey;
      queryClient.removeQueries({ queryKey: ["extensions", "detail", extensionKey] });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["extensions", "all"] }),
        ...extensionArtifactCatalogQueryKeys.map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }),
        ),
        invalidateModelRoutingQueries(queryClient),
      ]);
      if (onItsOwnPage) {
        ignoreAsyncResult(navigate("/extensions#installed"));
      }
      setStatusById((current) => ({
        ...current,
        [extensionKey]: "Extension uninstalled.",
      }));
    });
  }

  async function handleHealthCheck(extension: ExtensionRecord) {
    const extensionKey = extension.extension_id;
    await withBusy(extensionKey, async () => {
      setStatusById((current) => ({ ...current, [extensionKey]: "Checking health..." }));
      let payload: Awaited<ReturnType<typeof checkExtensionHealth>>;
      try {
        payload = await checkExtensionHealth(extensionKey);
      } catch (error) {
        setStatusById((current) => ({
          ...current,
          [extensionKey]: `Couldn't check health: ${extensionActionFailureMessage(
            error,
            "the server did not answer.", t
          )}`,
        }));
        return;
      }
      const status = firstStringFromUnknownValue([payload.status, "unknown"]).trim().toLowerCase();
      const reason = firstStringFromUnknownValue([payload.reason, payload.detail, ""]).trim();
      const healthy = status === "healthy";
      const summary = healthy
        ? "Healthy"
        : `Unhealthy${reason ? ` — ${reason}` : ""}`;
      setStatusById((current) => ({
        ...current,
        [extensionKey]: summary,
      }));
    });
  }
  return {
    withBusy,
    switchTab,
    handleInstallDragOver,
    handleInstallDragLeave,
    handleInstallDrop,
    handleViewFilterChange,
    handleInstallFileChange,
    handleHealthCheck,
    handleToggle,
    handleUninstall,
    installCatalogExtension,
    confirmUninstall,
    confirmBrowserAuthorityApproval,
  };
}
