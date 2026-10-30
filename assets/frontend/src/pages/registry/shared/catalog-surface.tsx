import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCreateDialog } from "./catalog-create-dialog";
import { useCatalogCategories } from "./catalog-categories";
import { buildCreatePayload, cloneIdentifier, cloneBodyKey, renderWhen, renderChoice } from "./catalog-create-payload";
import { useCallback, useContext, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { Link, useHref, useNavigate } from "react-router";

import { fetchJson } from "@/lib/http/client";
import {
  createRegistryEntity,
  deleteMcpProjectedToolConfirmed,
  deleteRegistryEntity,
  fetchMcpProjectedToolDeletePlan,
  fetchRegistryList,
  type McpProjectedToolDeletePlan,
  toggleRegistryEntity,
} from "@/pages/registry/shared/api";
import { buildGridConfig } from "@/pages/registry/shared/catalog-grid-config";
import { McpToolDeletePlanModal } from "@/pages/registry/shared/mcp-tool-delete-plan-modal";
import {
  columnsFromGridConfig,
  visibleRowActions,
} from "@/components/shared/data-grid-adapter";
import {
  AdvancedTableFilters,
  DsAlert,
  DsDialog,
  DsPageHeader,
  DsTable,
  PageContainer,
  RowActionMenu,
  Search,
} from "@/components/shared/design-system";
import {
  applyContractValidation,
  buildGridRows,
  buildPayload,
  EMPTY_FORM,
  modalIdForEndpoint,
  normalizeString,
  objectOrEmpty,
  registryListPath,
} from "@/pages/registry/shared/catalog-surface-helpers";
import type {
  EntityForm,
  GridEntityRow,
  RegistryEntity,
} from "@/pages/registry/shared/catalog-surface-helpers";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useHasCapability } from "@/lib/auth/capabilities";
import { AuthContext } from "@/lib/auth/context";

type RegistryCatalogSurfaceProps = {
  createLabel?: string;
  description: string;
  endpoint: string;
  idKey: string;
  listQueryKey: string[];
  listGridId?: string;
  readOnlyNotice?: string;
  readOnly?: boolean;
  secondaryLinks?: Array<{ label: string; to: string }>;
  title: string;
};

const EMPTY_CATALOG_FILTERS: Record<string, string> = {};
function catalogFilterValue(row: GridEntityRow, field: string): string {
  const value = field === "category"
    ? normalizeString(row._category || row.category)
    : normalizeString(field === "model" ? row._model : row._impl_type);
  return value === "—" ? "" : value;
}

function catalogFilterFields(endpoint: string, t: I18nContextValue["t"] = fallbackContext.t): Array<{ key: string; label: string }> {
  if (endpoint === "/api/agents") return [{ key: "category", label: t("trash.columns.category", "Category") }, { key: "model", label: t("registry.agents.field.model", "Model") }];
  if (endpoint === "/api/tools") return [{ key: "category", label: t("trash.columns.category", "Category") }, { key: "implementation", label: t("registry.tools.field.implementation", "Implementation") }];
  if (endpoint === "/api/skills") return [{ key: "category", label: t("trash.columns.category", "Category") }];
  return [];
}

function catalogFilterOption(field: string, value: string, t: I18nContextValue["t"] = fallbackContext.t) {
  const implementationLabels: Record<string, string> = {
    internal: t("react.registry.internal_implementation", "Internal"),
    mcp: "MCP",
    native: t("react.registry.native_implementation", "Native"),
  };
  const missingLabels: Record<string, string> = {
    category: t("react.registry.uncategorized", "Uncategorized"),
    model: t("react.registry.no_model", "No model"),
    implementation: t("react.registry.unknown_implementation", "Unknown implementation"),
  };
  const label = value
    ? (field === "implementation" ? implementationLabels[value] || value : value)
    : (missingLabels[field] || t("react.registry.unspecified", "Unspecified"));
  // Missing is distinct from an actual category/model literally named "missing".
  return { label, value: value ? `value:${value}` : "missing" };
}

export function RegistryCatalogSurface({
  createLabel = "Entry",
  description,
  endpoint,
  idKey,
  listQueryKey,
  listGridId,
  readOnlyNotice,
  readOnly = false,
  secondaryLinks = [],
  title,
}: Readonly<RegistryCatalogSurfaceProps>) {
  const { t } = useI18n();
  const accountId = useContext(AuthContext)?.session?.user?.account_id;
  const canAdministerPlatform = useHasCapability("administer_platform");
  const effectiveReadOnly = readOnly || !canAdministerPlatform;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [createForm, setCreateForm] = useState<EntityForm>(EMPTY_FORM);
  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [displayRows, setDisplayRows] = useState<GridEntityRow[]>([]);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [filtersByEndpoint, setFiltersByEndpoint] = useState<Record<string, Record<string, string>>>({});
  const activeFilters = filtersByEndpoint[endpoint] || EMPTY_CATALOG_FILTERS;
  const statusFilter = activeFilters.status || "";
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [mcpDeletePlan, setMcpDeletePlan] = useState<McpProjectedToolDeletePlan | null>(null);
  const [mcpDeleteError, setMcpDeleteError] = useState("");
  const [createError, setCreateError] = useState("");
  const listPath = useMemo(() => registryListPath(endpoint), [endpoint]);
  const listHref = useHref(listPath);
  const createModalId = useMemo(() => modalIdForEndpoint(endpoint), [endpoint]);

  const [category, setCategory] = useState("");
  const [agentSystemPrompt, setAgentSystemPrompt] = useState("");
  const [agentModelRoute, setAgentModelRoute] = useState("");

  const [toolPrompt, setToolPrompt] = useState("");
  const [toolImplementationType, setToolImplementationType] = useState("internal");

  const [skillInstructions, setSkillInstructions] = useState("");

  const [guardrailMode, setGuardrailMode] = useState<"deterministic" | "semantic">("deterministic");
  const [guardrailRuleType, setGuardrailRuleType] = useState("forbidden_patterns");
  const [guardrailSeverity, setGuardrailSeverity] = useState<"error" | "warning">("warning");
  const [guardrailConfigJson, setGuardrailConfigJson] = useState('{"patterns": []}');
  const [guardrailPromptText, setGuardrailPromptText] = useState("");
  const [guardrailAgentIds, setGuardrailAgentIds] = useState<string[]>([]);
  const [guardrailAgentSearch, setGuardrailAgentSearch] = useState("");

  const listQuery = useQuery({
    queryKey: listQueryKey,
    queryFn: () => fetchRegistryList(endpoint),
  });

  const modelOptionsQuery = useQuery({
    queryKey: ["model-selection", "options"],
    queryFn: () => fetchJson<Record<string, unknown>>("/api/model-selection/options"),
    enabled: endpoint === "/api/agents" && !effectiveReadOnly,
  });
  const agentModelOptions = useMemo(() => {
    const providerModels = objectOrEmpty(modelOptionsQuery.data?.provider_models);
    const providerLabels = new Map(
      (Array.isArray(modelOptionsQuery.data?.provider_options) ? modelOptionsQuery.data.provider_options : [])
        .map((option) => {
          const row = objectOrEmpty(option);
          return [normalizeString(row.provider_id), normalizeString(row.label)] as const;
        }),
    );
    const chatRoutes = Object.entries(providerModels).flatMap(([provider, models]) =>
      Array.isArray(models)
        ? models.flatMap((model) => {
            const row = objectOrEmpty(model);
            const modelId = normalizeString(row.model_id);
            return modelId
              ? [{
                  provider,
                  modelId,
                  label: normalizeString(row.label) || modelId,
                  providerLabel: providerLabels.get(provider) || provider,
                }]
              : [];
          })
        : [],
    );
    return chatRoutes;
  }, [modelOptionsQuery.data]);

  const guardrailAgentsQuery = useQuery({
    queryKey: ["registry", "guardrail-create-agents", accountId],
    queryFn: () => fetchRegistryList("/api/agents?usage=guardrail"),
    enabled: endpoint === "/api/guardrails" && !effectiveReadOnly && createFormOpen,
  });

  const items = useMemo(
    () => (Array.isArray(listQuery.data) ? listQuery.data : []),
    [listQuery.data],
  );

  const categories = useCatalogCategories();
  const rows = useMemo(
    () => buildGridRows(endpoint, idKey, items, t, categories.labelOf),
    [categories.labelOf, endpoint, idKey, items, t],
  );
  const showsIdentifierField = endpoint !== "/api/guardrails";
  const requiresIdentifier = endpoint === "/api/agents" || endpoint === "/api/tools" || endpoint === "/api/skills";
  const requiresDisplayName = endpoint === "/api/tools" || endpoint === "/api/skills" || endpoint === "/api/guardrails";
  const guardrailAgentIdSet = useMemo(() => new Set(guardrailAgentIds), [guardrailAgentIds]);
  const guardrailAgentOptions = useMemo(
    () => (Array.isArray(guardrailAgentsQuery.data) ? guardrailAgentsQuery.data : []),
    [guardrailAgentsQuery.data],
  );
  const guardrailAgentRows = useMemo(
    () => guardrailAgentOptions.flatMap((agent) => {
      const agentId = normalizeString(agent.agent_id || agent.id || agent.name);
      if (!agentId) {
        return [];
      }
      const title = normalizeString(agent.display_name || agent.name) || agentId;
      const selected = guardrailAgentIdSet.has(agentId);
      return [{
        id: agentId,
        title,
        subtitle: agentId,
        selected,
        searchText: `${agentId} ${title}`,
        badges: [
          { label: selected ? t("project_history.selected", "selected") : t("react.registry.available", "available"), tone: selected ? "linked" as const : "available" as const },
          ...(agent.enabled === false ? [{ label: t("settings.mcp.status.disabled", "Disabled"), tone: "disabled" as const }] : []),
        ],
      }];
    }),
    [guardrailAgentIdSet, guardrailAgentOptions, t],
  );

  const guardrailEligibleAgentIds = new Set(guardrailAgentRows.map((row) => row.id));
  const unavailableGuardrailAgentIds = guardrailAgentIds.filter((id) => !guardrailEligibleAgentIds.has(id));
  const guardrailAssignmentRows = [
    ...guardrailAgentRows,
    ...unavailableGuardrailAgentIds.map((id) => ({
      id,
      title: id,
      selected: true,
      disabled: true,
      searchText: id,
      description: guardrailAgentsQuery.isSuccess && guardrailAgentsQuery.fetchStatus === "idle"
        ? t("react.registry.guardrail_agent_unavailable", "This agent is unavailable for guardrail assignment. Remove its reference before saving.")
        : t("react.registry.guardrail_agent_unverified", "The selected agent reference is retained while assignment availability is being checked."),
      badges: [{
        label: guardrailAgentsQuery.isSuccess && guardrailAgentsQuery.fetchStatus === "idle"
          ? t("monitoring.health.unavailable", "Unavailable")
          : t("project_history.selected", "selected"),
        tone: "disabled" as const,
      }],
    })),
  ];
  const guardrailScopeValid = guardrailAgentsQuery.isSuccess
    && guardrailAgentsQuery.fetchStatus === "idle"
    && guardrailAgentIds.length > 0
    && !unavailableGuardrailAgentIds.length;

  const refreshList = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: listQueryKey });
  }, [listQueryKey, queryClient]);

  function resetCreateState() {
    setCreateForm(EMPTY_FORM);
    setCreateError("");
    setCategory("");
    setAgentSystemPrompt("");
    setAgentModelRoute("");
    setToolPrompt("");
    setToolImplementationType("internal");
    setSkillInstructions("");
    setGuardrailMode("deterministic");
    setGuardrailRuleType("forbidden_patterns");
    setGuardrailSeverity("warning");
    setGuardrailConfigJson('{"patterns": []}');
    setGuardrailPromptText("");
    setGuardrailAgentIds([]);
    setGuardrailAgentSearch("");
  }

  const detailRouteForEntity = useCallback(
    (entityId: string): string => `${listPath}/${encodeURIComponent(entityId)}`,
    [listPath],
  );

  /* Ported with the merge: cloning asks for the new identifier instead of
     inventing one, and validates it per catalog. */
  const [cloneSourceId, setCloneSourceId] = useState("");
  const [cloneId, setCloneId] = useState("");
  const [cloneError, setCloneError] = useState("");
  const rowTriggerLabelRef = useRef("");

  async function submitClone() {
    const sourceId = normalizeString(cloneSourceId);
    if (!sourceId) {
      return;
    }
    try {
      const nextId = cloneIdentifier(endpoint, cloneId, t);
      setCloneError("");
      setMessage("");
      setSaving(true);
      await cloneEntity(sourceId, nextId);
      setCloneSourceId("");
      setCloneId("");
    } catch (error) {
      setCloneError(error instanceof Error ? error.message : t("react.registry.failed_to_clone_entity", "Failed to clone entity."));
    } finally {
      setSaving(false);
    }
  }

  function focusRowTrigger() {
    const label = rowTriggerLabelRef.current;
    rowTriggerLabelRef.current = "";
    globalThis.setTimeout(() => {
      globalThis.document?.querySelector<HTMLElement>(`[aria-label="${label}"]`)?.focus();
    }, 0);
  }

  function cancelClone() {
    setCloneSourceId("");
    setCloneId("");
    setCloneError("");
    focusRowTrigger();
  }

  const cloneEntity = useCallback(async (
    entityId: string,
    nextId: string,
  ): Promise<void> => {
    const baseId = normalizeString(entityId);
    if (!baseId) {
      return;
    }
    const resolvedCloneBodyKey = cloneBodyKey(endpoint, idKey);
    const cloneResponse = await fetchJson<RegistryEntity>(
      `${endpoint}/${encodeURIComponent(baseId)}/clone`,
      {
        method: "POST",
        body: JSON.stringify({ [resolvedCloneBodyKey]: nextId }),
      },
    );
    const resolvedId = normalizeString(cloneResponse[resolvedCloneBodyKey] || nextId);
    if (resolvedId) {
      ignoreAsyncResult(navigate(detailRouteForEntity(resolvedId)));
      return;
    }
    await refreshList();
  }, [detailRouteForEntity, endpoint, idKey, navigate, refreshList]);

  // Agents and skills get their contract column filled in after the list
  // lands, so the rendered rows are state rather than the query result.
  useEffect(() => {
    let disposed = false;
    setDisplayRows(rows);
    if (rows.length && (endpoint === "/api/agents" || endpoint === "/api/skills")) {
      ignoreAsyncResult((async () => {
        const validatedRows = await applyContractValidation(endpoint, rows);
        if (!disposed) {
          setDisplayRows(validatedRows);
        }
      })());
    }
    return () => {
      disposed = true;
    };
  }, [endpoint, rows]);

  function openEntity(row: Record<string, unknown>) {
    const entityId = normalizeString(row[idKey] || row._entity_id || row.id);
    if (entityId) {
      ignoreAsyncResult(navigate(detailRouteForEntity(entityId)));
    }
  }

  function handleRowAction(actionId: string, row: Record<string, unknown>) {
    const entityId = normalizeString(row[idKey] || row._entity_id || row.id);
    if (!entityId || !actionId) {
      return;
    }

    if (actionId === "edit" || actionId === "view") {
      ignoreAsyncResult(navigate(detailRouteForEntity(entityId)));
      return;
    }

    if (actionId === "clone") {
      rowTriggerLabelRef.current = `Actions for ${entityId}`;
      setCloneSourceId(entityId);
      setCloneId("");
      setCloneError("");
      return;
    }

    if (actionId === "toggle" || actionId === "toggle-on" || actionId === "toggle-off") {
      ignoreAsyncResult((async () => {
        setMessage("");
        setSaving(true);
        try {
          await toggleRegistryEntity(endpoint, entityId);
          setMessage(t("react.registry.entity_value0_toggled", "Entity {value0} toggled.", { value0: entityId }));
          await refreshList();
        } catch (error) {
          setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_toggle_entity", "Failed to toggle entity."));
        } finally {
          setSaving(false);
        }
      })());
      return;
    }

    if (actionId === "delete") {
      rowTriggerLabelRef.current = `Actions for ${entityId}`;
      const implementation = objectOrEmpty(row.implementation);
      const implementationType = normalizeString(implementation.type || implementation.kind);
      if (endpoint === "/api/tools" && implementationType === "mcp") {
        rowTriggerLabelRef.current = `Actions for ${entityId}`;
        ignoreAsyncResult((async () => {
          setSaving(true);
          setMcpDeleteError("");
          try {
            setMcpDeletePlan(await fetchMcpProjectedToolDeletePlan(entityId));
          } catch (error) {
            setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_load_mcp_tool_impact", "Failed to load MCP tool impact."));
          } finally {
            setSaving(false);
          }
        })());
        return;
      }
      setDeleteTarget(entityId);
    }
  }

  async function confirmDeleteEntity() {
    if (!deleteTarget) {
      return;
    }
    setMessage("");
    setSaving(true);
    try {
      await deleteRegistryEntity(endpoint, deleteTarget);
      setMessage(t("react.registry.entity_value0_deleted", "Entity {value0} deleted.", { value0: deleteTarget }));
      setDeleteTarget(null);
      await refreshList();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : t("react.registry.failed_to_delete_entity", "Failed to delete entity."));
    } finally {
      setSaving(false);
    }
  }

  const visibleRows = useMemo(() => {
    const needle = deferredSearch.trim().toLowerCase();
    return displayRows.filter((row) => {
      if (statusFilter) {
        const label = normalizeString(row._enabled_label).toLowerCase();
        const enabled = label ? label.startsWith("enabled") : Boolean(row.enabled);
        if (statusFilter === "enabled" ? !enabled : enabled) {
          return false;
        }
      }
      for (const field of catalogFilterFields(endpoint, t)) {
        const selected = activeFilters[field.key];
        if (selected && catalogFilterOption(field.key, catalogFilterValue(row, field.key), t).value !== selected) {
          return false;
        }
      }
      if (!needle) {
        return true;
      }
      // Every string the row already carries is searchable: the catalogs differ
      // in which columns they show, and a per-endpoint field list would drift
      // from `buildGridRows` the first time a column is added.
      return Object.values(row).some(
        (value) => typeof value === "string" && value.toLowerCase().includes(needle),
      );
    });
  }, [activeFilters, deferredSearch, displayRows, endpoint, statusFilter, t]);

  const catalogFilters = useMemo(
    () => [
      ...catalogFilterFields(endpoint, t).map((field) => {
        const values = new Set(displayRows.map((row) => catalogFilterValue(row, field.key)));
        const selected = activeFilters[field.key];
        // Keep an active selection intelligible if a refresh removes its last row.
        if (selected) values.add(selected === "missing" ? "" : selected.slice("value:".length));
        return {
          ...field,
          options: Array.from(values)
            .map((value) => catalogFilterOption(field.key, value, t))
            .sort((left, right) => left.label.localeCompare(right.label)),
        };
      }),
      {
        key: "status",
        label: t("extensions.runtime.status", "Status"),
        options: [
          { label: t("settings.mcp.status.enabled", "Enabled"), value: "enabled" },
          { label: t("react.registry.disabled", "Disabled"), value: "disabled" },
        ],
      },
    ],
    [activeFilters, displayRows, endpoint, t],
  );

  const gridConfig = useMemo(
    () => buildGridConfig(endpoint, idKey, displayRows, listHref, readOnly, t),
    [displayRows, endpoint, idKey, listHref, readOnly, t],
  );

  const tableColumns = useMemo(
    () =>
      columnsFromGridConfig(gridConfig, (row) => {
        const entityId = normalizeString(row[idKey] || row._entity_id || row.id);
        return (
          <RowActionMenu
            label={t("react.registry.actions_for_value0", "Actions for {value0}", { value0: entityId })}
            onOpenChange={(open) => setRowMenu(open ? entityId : null)}
            open={rowMenu === entityId}
          >
            {visibleRowActions(gridConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setRowMenu(null);
                    handleRowAction(action.action_id, row);
                  }}
                  role="menuitem"
                  type="button"
                >
                  {action.label}
                </button>
              </div>
            ))}
          </RowActionMenu>
        );
      }),
    // handleRowAction closes over the latest state on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [gridConfig, idKey, rowMenu, t],
  );

  async function handleCreate(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError("");
    setMessage("");
    setSaving(true);
    try {
      const basePayload = buildPayload(createForm, idKey, undefined, endpoint);
      const payload = buildCreatePayload(basePayload, {
        agentModelOptions,
        agentModelRoute,
        agentSystemPrompt,
        category,
        createForm,
        endpoint,
        guardrailAgentIds,
        guardrailConfigJson,
        guardrailMode,
        guardrailPromptText,
        guardrailRuleType,
        guardrailSeverity,
        idKey,
        skillInstructions,
        toolImplementationType,
        toolPrompt,
      }, t);

      if (endpoint === "/api/guardrails" && !guardrailScopeValid) {
        throw new Error(t("react.registry.guardrail_scope_choices_required", "Load the agent choices and select at least one available agent before creating the guardrail."));
      }
      const createResponse = await createRegistryEntity(endpoint, payload);
      const responseRecord = objectOrEmpty(createResponse);
      const createdId =
        normalizeString(
          responseRecord[idKey] ||
            responseRecord.agent_id ||
            responseRecord.tool_id ||
            responseRecord.skill_id ||
            responseRecord.guardrail_id ||
            responseRecord.id,
        ) || normalizeString(payload[idKey] || payload.agent_id || payload.tool_id || payload.skill_id || payload.id);

      resetCreateState();
      setCreateFormOpen(false);
      setMessage(t("react.registry.entity_created", "Entity created."));
      await refreshList();
      if (createdId) {
        ignoreAsyncResult(navigate(detailRouteForEntity(createdId)));
      }
    } catch (error) {
      const text = error instanceof Error ? error.message : t("react.registry.display_failed_create_entity", "Failed to create entity.");
      setCreateError(text);
      setMessage(text);
    } finally {
      setSaving(false);
    }
  }

  const createButton = renderWhen(!readOnly, (
    <button
      className="fs-button primary"
      onClick={() => {
        setCreateError("");
        setCreateFormOpen(true);
      }}
      type="button"
    >
      <Plus aria-hidden="true" />
      {t("react.registry.new_value0", "New {value0}", { value0: createLabel })}
    </button>
  ));
  const headerAction = renderWhen(Boolean(secondaryLinks.length || createButton), (
      <div className="flex flex-wrap items-center justify-end gap-2">
        {secondaryLinks.map((link) => (
          <Link className="fs-button secondary" key={`${link.to}:${link.label}`} to={link.to}>
            {link.label}
          </Link>
        ))}
        {createButton}
      </div>
    ));

  return (
    <PageContainer width="wide">
      <section className="fs-panel-stack">
        <DsPageHeader
          action={headerAction}
          description={description}
          eyebrow={t("trash.category.registry", "Registry")}
          title={title}
        />

        <article className="fs-card fs-table-card">
          <div className="fs-table-toolbar">
            <AdvancedTableFilters
              disabled={listQuery.isLoading}
              fields={catalogFilters}
              onChange={(key, value) => setFiltersByEndpoint((current) => ({
                ...current,
                [endpoint]: { ...current[endpoint], [key]: value },
              }))}
              onClear={() => setFiltersByEndpoint((current) => ({
                ...current,
                [endpoint]: {},
              }))}
              values={activeFilters}
            />
            <label className="fs-search">
              <Search aria-hidden="true" />
              <input
                aria-label={t("react.registry.search_value0_entries", "Search {value0} entries", { value0: createLabel.toLowerCase() })}
                onChange={(event) => setSearch(event.target.value)}
                placeholder={t("react.registry.search_value0", "Search {value0}…", { value0: createLabel.toLowerCase() })}
                type="search"
                value={search}
              />
            </label>
            <span aria-live="polite" className="fs-management-result-announcement">
              {visibleRows.length} {" "}{t("react.registry.of", "of")}{" "}{displayRows.length}
            </span>
          </div>
          {renderWhen(Boolean(listGridId), (
            <DsTable
                ariaLabel={t("react.registry.value0_inventory", "{value0} inventory", { value0: createLabel })}
                columns={tableColumns}
                onRowClick={openEntity}
                rowKey={(row) => normalizeString(row[idKey] || row._entity_id || row.id)}
                rows={visibleRows}
                state={renderWhen(!visibleRows.length, (
                    <div className="fs-table-state" data-testid={listGridId}>
                      <strong>{normalizeString((gridConfig.empty_state as Record<string, unknown>)?.title) || t("react.registry.nothing_here_yet", "Nothing here yet")}</strong>
                      <span>{normalizeString((gridConfig.empty_state as Record<string, unknown>)?.description)}</span>
                    </div>
                  ))}
            />
          ))}
        </article>

        <DsDialog
          busy={saving}
          closeLabel={t("react.registry.close_delete_confirmation", "Close delete confirmation")}
          description={t("react.registry.the_entry_is_removed_from_this_registry_anything_already_referencing_it_keeps_its_recorded_copy", "The entry is removed from this registry. Anything already referencing it keeps its recorded copy.")}
          eyebrow={t("trash.category.registry", "Registry")}
          footer={
            <>
              <button
                className="fs-button"
                disabled={saving}
                onClick={() => {
                  setDeleteTarget(null);
                  focusRowTrigger();
                }}
                type="button"
              >
                {t("extensions.repeater.cancel", "Cancel")}</button>
              <button className="fs-button danger" disabled={saving} onClick={() => ignoreAsyncResult(confirmDeleteEntity())} type="button">
                {renderChoice(saving, t("react.workflows.deleting_variant_43b5894c", "Deleting…"), t("settings.model_providers.delete", "Delete"))}
              </button>
            </>
          }
          onClose={() => {
            setDeleteTarget(null);
            focusRowTrigger();
          }}
          open={Boolean(deleteTarget)}
          title={t("react.registry.delete_value0", "Delete {value0}", { value0: deleteTarget || "entry" })}
          variant="confirm"
        >
          <DsAlert tone="danger" title={t("react.registry.this_moves_value0_to_trash", "This moves {value0} to Trash.", { value0: deleteTarget || t("react.registry.display_the_entry", "the entry") })} />
        </DsDialog>

        {renderWhen(effectiveReadOnly, (
          <div data-testid="registry-read-only-notice">
            <DsAlert title={readOnlyNotice || t("react.registry.display_read_only_catalog", "This catalog is read-only in the current React cutover surface.")} />
          </div>
        ))}

        <RegistryCreateDialog
          {...{
            saving,
            createLabel,
            endpoint,
            guardrailAgentIds,
            modelOptionsQuery,
            createModalId,
            setCreateFormOpen,
            createFormOpen,
            readOnly,
            handleCreate,
            showsIdentifierField,
            idKey,
            requiresIdentifier,
            setCreateForm,
            createForm,
            requiresDisplayName,
            setCategory,
            category,
            categoryOptions: categories.options,
            setAgentSystemPrompt,
            agentSystemPrompt,
            setAgentModelRoute,
            agentModelOptions,
            agentModelRoute,
            setToolImplementationType,
            toolImplementationType,
            setToolPrompt,
            toolPrompt,
            setSkillInstructions,
            skillInstructions,
            setGuardrailMode,
            guardrailMode,
            guardrailPromptText,
            setGuardrailPromptText,
            setGuardrailSeverity,
            guardrailSeverity,
            setGuardrailRuleType,
            guardrailRuleType,
            setGuardrailConfigJson,
            guardrailConfigJson,
            guardrailAgentsQuery,
            guardrailScopeValid,
            setGuardrailAgentSearch,
            setGuardrailAgentIds,
            guardrailAgentRows: guardrailAssignmentRows,
            guardrailAgentSearch,
            guardrailAgentOptions,
            createError,
          }}
        />

        {renderWhen(listQuery.isError, (
          <DsAlert
            title={renderChoice(
              listQuery.error instanceof Error,
              listQuery.error instanceof Error ? listQuery.error.message : "",
              t("react.registry.display_failed_load_catalog", "Failed to load registry catalog."),
            ) as string}
            tone="danger"
          />
        ))}

        {renderWhen(Boolean(message), <DsAlert title={message} />)}
        {renderWhen(Boolean(cloneSourceId), (
          <DsDialog
            busy={saving}
            closeLabel={t("react.registry.close_clone_dialog", "Close clone dialog")}
            description={t("react.registry.choose_a_new_identifier_for_the_cloned_registry_entry", "Choose a new identifier for the cloned registry entry.")}
            eyebrow={t("trash.category.registry", "Registry")}
            footer={
              <>
                <button className="fs-button" onClick={cancelClone} type="button">
                  {t("extensions.repeater.cancel", "Cancel")}</button>
                <button
                  className="fs-button primary"
                  disabled={saving}
                  onClick={() => ignoreAsyncResult(submitClone())}
                  type="button"
                >
                  {renderChoice(saving, t("react.workflows.cloning", "Cloning…"), t("react.registry.clone", "Clone"))}
                </button>
              </>
            }
            onClose={cancelClone}
            open
            title={t("react.registry.clone_value0", "Clone {value0}", { value0: cloneSourceId })}
          >
            <label className="fs-field" htmlFor="registry-clone-id">
              <span className="fs-field-label">{t("react.registry.new_id", "New ID")}</span>
              <input
                className="fs-input"
                id="registry-clone-id"
                onChange={(event) => setCloneId(event.target.value)}
                value={cloneId}
              />
            </label>
            {renderWhen(Boolean(cloneError), <DsAlert title={cloneError} tone="danger" />)}
          </DsDialog>
        ))}

        {renderWhen(Boolean(mcpDeletePlan), (
          <McpToolDeletePlanModal
            busy={saving}
            error={mcpDeleteError}
            onClose={() => {
              setMcpDeletePlan(null);
              setMcpDeleteError("");
              focusRowTrigger();
            }}
            onConfirm={() => ignoreAsyncResult((async () => {
              setSaving(true);
              setMcpDeleteError("");
              try {
                await deleteMcpProjectedToolConfirmed(mcpDeletePlan!.tool_id, mcpDeletePlan!.revision);
                setMcpDeletePlan(null);
                setMessage(t("react.registry.entity_value0_moved_to_trash", "Entity {value0} moved to Trash.", { value0: mcpDeletePlan!.tool_id }));
                await refreshList();
              } catch (error) {
                setMcpDeleteError(error instanceof Error ? error.message : t("react.registry.failed_to_move_mcp_tool_to_trash", "Failed to move MCP tool to Trash."));
              } finally {
                setSaving(false);
              }
            })())}
            plan={mcpDeletePlan!}
          />
        ))}

      </section>
    </PageContainer>
  );
}
