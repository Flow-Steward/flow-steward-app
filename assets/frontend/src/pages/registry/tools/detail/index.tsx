import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { type Dispatch, type SetStateAction, useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { Link } from "react-router";

import { DsAlert, DsCard, DsPageHeader, DsTabs, PageContainer } from "@/components/shared/design-system";
import { fetchJson } from "@/lib/http/client";
import { isHttpError } from "@/lib/http/errors";
import { useKeyedState } from "@/lib/react/useKeyedState";
import { BuilderMetadataEditor } from "@/pages/registry/shared/builder-metadata-editor";
import {
  builderMetadataDraftFromUnknown,
  builderMetadataPayload,
} from "@/pages/registry/shared/builder-metadata";
import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import {
  firstObject,
  firstStringArray,
  normalizePrimitive,
  objectOrEmpty,
  useRegistryEntityDetail,
} from "@/pages/registry/shared/detail-helpers";
import { SaveErrorAlert, SaveStatusBadge } from "@/pages/registry/shared/save-feedback";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

/**
 * Canon's four value families. Identifier is the default because these values
 * get copied verbatim — `mcp`, `workflow_ops`, `builtin_workflow_ops` — and the code
 * face is what canon's own preview shows for exactly those pairs.
 */
function valueFamily(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): Readonly<{ className?: string; text: string }> {
  if (value === null || value === undefined || value === "") {
    // Never a blank cell and never a bare dash: the row says what is missing.
    // The shared normalizePrimitive returns "—" here, which is why the family is
    // decided from the raw value instead of from its normalized string.
    return { className: "is-empty", text: t("react.registry.not_set", "Not set") };
  }
  if (typeof value === "number") {
    return { className: "is-numeric", text: normalizePrimitive(value) };
  }
  if (typeof value === "boolean" || typeof value === "object") {
    // Booleans and the "N items" / "object" summaries are human phrasing, so the
    // body face rather than the code face.
    return { className: "is-plain", text: normalizePrimitive(value) };
  }
  return { text: normalizePrimitive(value) };
}

/**
 * The disclosure summary: chevron at rest, label, and the count of what is
 * folded away. A count of zero is not printed — canon marks it optional, and
 * `0` reads as a broken counter rather than as an empty body.
 */
function DisclosureSummary({ count, label }: Readonly<{ count: number; label: string }>) {
  return (
    <summary>
      <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
      {label}
      {count ? <span className="fs-disclosure-count">{count}</span> : null}
    </summary>
  );
}

/** Pairs the body will reveal, so the summary can say what is folded away. */
function pairCount(value: unknown): number {
  if (Array.isArray(value)) {
    return value.filter((item) => Boolean(item) && typeof item === "object").length;
  }
  return value && typeof value === "object" ? Object.keys(value).length : 0;
}

/**
 * A read-only key-value list. `title` is no longer painted: every call site sits
 * inside a disclosure whose summary is the heading for the region, and canon
 * allows exactly one. It stays as the list's accessible name so the label is not
 * lost for assistive technology.
 */
function JsonPanel({ title, value }: Readonly<{ title: string; value: unknown }>) {
  const { t } = useI18n();
  const rows = Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
  const record =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  let blocks = rows;
  if (!blocks.length && Object.keys(record).length) {
    blocks = [record];
  }

  return (
    <div className="fs-panel-stack">
      {blocks.map((block, index) => (
        <dl
          aria-label={blocks.length > 1 ? `${title} ${index + 1}` : title}
          className="fs-kv is-bare"
          key={`${title}:${index}`}
        >
          {Object.entries(block).map(([key, candidate]) => {
            const family = valueFamily(candidate, t);
            return (
              <div className="fs-kv-row" key={key}>
                <dt>{key}</dt>
                <dd className={family.className}>{family.text}</dd>
              </div>
            );
          })}
        </dl>
      ))}
    </div>
  );
}

function localizedTOOL_TABS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { key: "config", label: t("react.registry.config", "Config") },
  { key: "builder", label: t("react.registry.builder", "Builder") },
  { key: "actions", label: t("extensions.table.actions", "Actions") },
  { key: "prompt", label: t("registry.tool_detail.tab.prompt", "Prompt") },
  { key: "agents", label: t("react.registry.agents_using", "Agents Using") },
  { key: "changelog", label: t("react.registry.changelog", "Changelog") },
] as const;
}

type ToolTabKey = ReturnType<typeof localizedTOOL_TABS>[number]["key"];

type ToolDraft = {
  antiUseCases: string;
  category: string;
  compositionRole: string;
  description: string;
  determinismLevel: string;
  displayName: string;
  prompt: string;
  purpose: string;
  safeForAutoUse: boolean;
  useCases: string;
};

function textOrEmpty(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

function objectFromMaybeJson(value: unknown): Record<string, unknown> {
  if (typeof value === "string") {
    try {
      return objectOrEmpty(JSON.parse(value));
    } catch {
      return {};
    }
  }
  return objectOrEmpty(value);
}

function recordList(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => textOrEmpty(item)).filter(isTruthy);
}

function contractRows(value: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(value)) {
    return recordList(value);
  }
  const row = objectFromMaybeJson(value);
  const properties = objectOrEmpty(row.properties);
  if (Object.keys(properties).length) {
    const required = new Set(stringList(row.required));
    return Object.entries(properties).map(([name, schema]) => ({
      ...objectOrEmpty(schema),
      name,
      required: required.has(name),
    }));
  }
  return textOrEmpty(row.name || row.key || row.id || row.field) ? [row] : [];
}

function toolOperations(entity: Record<string, unknown>): Array<Record<string, unknown>> {
  const directOperations = recordList(entity.operations).length
    ? recordList(entity.operations)
    : recordList(entity.contract_operations);
  if (directOperations.length) {
    return directOperations;
  }
  const toolInterface = objectFromMaybeJson(entity.interface);
  const interfaceOperations = recordList(toolInterface.operations);
  if (interfaceOperations.length) {
    return interfaceOperations;
  }
  const functions = recordList(toolInterface.functions);
  if (functions.length) {
    return functions;
  }
  if (
    textOrEmpty(toolInterface.function_name) ||
    contractRows(toolInterface.parameters).length ||
    contractRows(toolInterface.returns).length
  ) {
    return [
      {
        name: textOrEmpty(toolInterface.function_name) || "invoke",
        parameters: toolInterface.parameters,
        returns: toolInterface.returns,
        input_schema: toolInterface.input_schema,
        output_schema: toolInterface.output_schema,
      },
    ];
  }
  return [];
}

function contractFieldName(row: Record<string, unknown>): string {
  return textOrEmpty(row.name || row.key || row.id || row.field) || "value";
}

function contractFieldType(row: Record<string, unknown>): string {
  return textOrEmpty(row.type || row.value_type || row.schema_type) || "value";
}

/**
 * Canon's switch, from parameter-chip.md's decision logic: a chip carries a
 * compact signature with no prose, and the row variant takes over the moment a
 * parameter needs "a sentence, a default or a constraint". A default counts
 * because the chip may not carry one, so with chips selected it would vanish
 * from the page entirely.
 */
function fieldNeedsProse(row: Record<string, unknown>): boolean {
  return Boolean(
    textOrEmpty(row.description || row.help || row.summary) || textOrEmpty(row.default),
  );
}

/** The marker, with an accessible name so requiredness is announced. */
function RequiredMark() {
  const { t } = useI18n();
  return (
    <span aria-label={t("registry.workflow_detail.outputs.required_badge", "required")} className="fs-param-required">
      *
    </span>
  );
}

/**
 * The third column. A bare default is rendered as prose in the code face, the
 * way canon's own preview states it, because a default is prose and the chip has
 * no room for it.
 */
function SignatureDescription({ row }: Readonly<{ row: Record<string, unknown> }>) {
  const { t } = useI18n();
  const description = textOrEmpty(row.description || row.help || row.summary);
  const defaultValue = textOrEmpty(row.default);
  if (!description && !defaultValue) {
    return <span />;
  }
  return (
    <span className="fs-signature-description">
      {description}
      {description && defaultValue ? " " : null}
      {defaultValue ? (
        <>
          {t("react.registry.defaults_to", "Defaults to")}{" "}<code>{defaultValue}</code>.
        </>
      ) : null}
    </span>
  );
}

function ContractFields({
  emptyLabel,
  rows,
  title,
}: Readonly<{
  emptyLabel: string;
  rows: Array<Record<string, unknown>>;
  title: string;
}>) {
  const { t } = useI18n();
  const asRows = rows.some(fieldNeedsProse);
  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-field p-3">
      <span className="fs-field-label">{title}</span>
      {rows.length ? (
        <>
          {asRows ? (
            <div className="fs-signature">
              <div className="fs-signature-head">
                <span>{t("react.registry.parameter", "Parameter")}</span>
                <span>{t("trash.columns.type", "Type")}</span>
                <span>{t("registry.workflows.simulation.description", "Description")}</span>
              </div>
              {rows.map((row, index) => (
                <div className="fs-signature-row" key={`${title}:${contractFieldName(row)}:${index}`}>
                  <span className="fs-signature-name">
                    <span>{contractFieldName(row)}</span>
                    {isTruthy(row.required) ? <RequiredMark /> : null}
                  </span>
                  <span className="fs-signature-type">{contractFieldType(row)}</span>
                  <SignatureDescription row={row} />
                </div>
              ))}
            </div>
          ) : (
            <div className="fs-param-group">
              {rows.map((row, index) => (
                <span className="fs-param-chip" key={`${title}:${contractFieldName(row)}:${index}`}>
                  <span className="fs-param-name">
                    <span>{contractFieldName(row)}</span>
                    {isTruthy(row.required) ? <RequiredMark /> : null}
                  </span>
                  <span className="fs-param-type">{contractFieldType(row)}</span>
                </span>
              ))}
            </div>
          )}
          {/* One legend per group, unconditionally. Gating it on "something is
              marked" was the first attempt and it was wrong: in a group with no
              asterisks the second clause is the informative half — it tells the
              reader every parameter is optional, rather than that requiredness
              was never recorded. */}
          <p className="fs-param-legend">
            <span className="fs-param-required">*</span> {" "}{t("react.registry.required_unmarked_parameters_are_optional", "required · unmarked parameters are optional")}</p>
        </>
      ) : (
        <p className="fs-empty-copy">{emptyLabel}</p>
      )}
    </div>
  );
}

async function fetchToolUsingAgents(entityId: string): Promise<string[]> {
  const payload = await fetchJson<unknown>(
    `/api/tools/${encodeURIComponent(entityId)}/agents`,
  );
  if (!Array.isArray(payload)) return [];
  return payload
    .map((row) => {
      if (typeof row === "string") return row;
      if (!row || typeof row !== "object") return "";
      return firstStringFromUnknownValue([
        (row as Record<string, unknown>).agent_id,
        "",
      ]).trim();
    })
    .filter(isTruthy);
}

function toolIsMissing(
  entity: Record<string, unknown>,
  error: unknown,
  isError: boolean,
): boolean {
  return (isHttpError(error) && error.status === 404)
    || (!isError && !Object.keys(entity).length);
}

function toolDetailNeedsLoadState(
  isLoading: boolean,
  isError: boolean,
  missing: boolean,
): boolean {
  return isLoading || isError || missing;
}

function ToolDetailLoadState({
  isError,
  isLoading,
  missing,
  onRetry,
}: Readonly<{
  isError: boolean;
  isLoading: boolean;
  missing: boolean;
  onRetry: () => void;
}>) {
  const { t } = useI18n();
  if (isLoading) {
    return (
      <section className="fs-page-sections">
        <Link className="fs-link" to="/registry/tools">{t("react.registry.back_to_tools", "Back to tools")}</Link>
        <output className="block">
          <DsCard description={t("react.registry.retrieving_the_current_tool_configuration", "Retrieving the current tool configuration.")} title={t("react.registry.loading_tool_detail", "Loading tool detail")} />
        </output>
      </section>
    );
  }
  if (missing) {
    return (
      <section className="fs-page-sections">
        <Link className="fs-link" to="/registry/tools">{t("react.registry.back_to_tools", "Back to tools")}</Link>
        <div role="alert">
          <DsCard description={t("react.registry.this_tool_may_have_been_deleted_or_purged_or_you_may_not_have_access_to_it", "This tool may have been deleted or purged, or you may not have access to it.")} title={t("react.registry.tool_not_found", "Tool not found")} />
        </div>
      </section>
    );
  }
  if (!isError) return null;
  return (
    <section className="fs-page-sections">
      <Link className="fs-link" to="/registry/tools">{t("react.registry.back_to_tools", "Back to tools")}</Link>
      <div role="alert">
        <DsCard
          className="fs-card-danger"
          description={t("react.registry.the_tool_detail_could_not_be_loaded_check_the_connection_and_try_again", "The tool detail could not be loaded. Check the connection and try again.")}
          footer={<button className="fs-button" onClick={onRetry} type="button">{t("settings.watchdog.retry", "Retry")}</button>}
          title={t("react.registry.tool_unavailable", "Tool unavailable")}
        />
      </div>
    </section>
  );
}

function ToolActionsPanel({ operations }: Readonly<{ operations: Array<Record<string, unknown>> }>) {
  const { t } = useI18n();
  if (!operations.length) {
    return (
      <div className="fs-panel-group">
        <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("extensions.table.actions", "Actions")}</h2><p>{t("react.registry.runtime_functions_exposed_by_this_tool_including_accepted_parameters_and_returned_fields", "Runtime functions exposed by this tool, including accepted parameters and returned fields.")}</p></div></div>
        <div className="fs-table-state"><strong>{t("react.registry.no_action_contracts_registered_for_this_tool", "No action contracts registered for this tool.")}</strong></div>
      </div>
    );
  }
  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("extensions.table.actions", "Actions")}</h2><p>{t("react.registry.runtime_functions_exposed_by_this_tool_including_accepted_parameters_and_returned_fields", "Runtime functions exposed by this tool, including accepted parameters and returned fields.")}</p></div></div>
      {operations.map((operation, index) => {
        const operationName = textOrEmpty(operation.display_name || operation.label || operation.name || operation.operation_id || operation.id || operation.key) || `Action ${index + 1}`;
        const operationId = textOrEmpty(operation.operation_id || operation.id || operation.key || operation.name);
        const description = textOrEmpty(operation.description || operation.summary);
        return (
          <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4" key={`${operationId || operationName}:${index}`}>
            <div className="fs-panel-heading"><div><h3 className="fs-panel-title">{operationName}</h3>{description ? <p>{description}</p> : null}</div></div>
            <div className="fs-form-grid">
              <ContractFields emptyLabel={t("react.registry.no_parameters_declared", "No parameters declared.")} rows={contractRows(operation.parameters || operation.inputs)} title={t("react.registry.input_parameters", "Input parameters")} />
              <ContractFields emptyLabel={t("react.registry.no_returned_fields_declared", "No returned fields declared.")} rows={contractRows(operation.returns || operation.outputs)} title={t("react.registry.returned_fields", "Returned fields")} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ToolConfigPanel({
  config,
  draft,
  draftSeed,
  implementation,
  onSave,
  saving,
  setDraft,
  setSaveStatus,
}: Readonly<{
  config: Record<string, unknown>;
  draft: ToolDraft;
  draftSeed: ToolDraft;
  implementation: Record<string, unknown>;
  onSave: (patch: Record<string, unknown>) => Promise<void>;
  saving: boolean;
  setDraft: Dispatch<SetStateAction<ToolDraft>>;
  setSaveStatus: (status: string) => void;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("react.registry.tool_config", "Tool Config")}</h2></div></div>
      <div className="fs-form-grid">
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="tool-display-name">{t("users.modal.display_name", "Display Name")}</label>
          <input className="fs-input" id="tool-display-name" name="display_name" onChange={(event) => setDraft((current) => ({ ...current, displayName: event.target.value }))} placeholder={t("react.registry.display_name", "Display name")} type="text" value={draft.displayName} />
        </div>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="tool-category">{t("trash.columns.category", "Category")}</label>
          <input className="fs-input" id="tool-category" name="category" onChange={(event) => setDraft((current) => ({ ...current, category: event.target.value }))} placeholder={t("trash.columns.category", "Category")} type="text" value={draft.category} />
        </div>
        <div className="fs-field full">
          <label className="fs-field-label" htmlFor="tool-description">{t("registry.workflows.simulation.description", "Description")}</label>
          <textarea className="fs-textarea" id="tool-description" name="description" onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} placeholder={t("registry.workflows.simulation.description", "Description")} value={draft.description} />
        </div>
      </div>
      {Object.keys(implementation).length ? (
        <details className="fs-disclosure">
          <DisclosureSummary count={pairCount(implementation)} label={t("react.registry.advanced_runtime_details", "Advanced runtime details")} />
          <div className="fs-disclosure-body"><JsonPanel title={t("react.registry.advanced_runtime_details", "Advanced runtime details")} value={implementation} /></div>
        </details>
      ) : null}
      {Object.keys(config).length ? (
        <details className="fs-disclosure">
          <DisclosureSummary count={pairCount(config)} label={t("react.registry.runtime_settings", "Runtime settings")} />
          <div className="fs-disclosure-body"><JsonPanel title={t("react.registry.runtime_settings", "Runtime settings")} value={config} /></div>
        </details>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <button className="fs-button primary" disabled={saving} onClick={() => ignoreAsyncResult(onSave({ display_name: draft.displayName.trim(), name: draft.displayName.trim(), category: draft.category.trim(), description: draft.description.trim() }))} type="button">{t("settings.extensions.save_config", "Save Config")}</button>
        <button className="fs-button" onClick={() => { setDraft(draftSeed); setSaveStatus(""); }} type="button">{t("trash.reset", "Reset")}</button>
      </div>
    </div>
  );
}

function ToolAgentsPanel({ usingAgents }: Readonly<{ usingAgents: string[] }>) {
  const { t } = useI18n();
  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("registry.tool_detail.agents.title", "Agents Using This Tool")}</h2></div></div>
      {usingAgents.length ? (
        <div className="fs-detail-rows">
          {usingAgents.map((agentId) => <div className="fs-detail-row" key={agentId}><span className="fs-detail-value mono">{agentId}</span><Link className="fs-link" to={`/registry/agents/${encodeURIComponent(agentId)}`}>{t("settings.common.open", "Open")}</Link></div>)}
        </div>
      ) : <div className="fs-table-state"><strong>{t("react.registry.no_linked_agents_found", "No linked agents found.")}</strong></div>}
    </div>
  );
}

function ToolAvailabilityAlert({
  available,
  reasonCode,
  reasonText,
}: Readonly<{
  available: boolean;
  reasonCode: string;
  reasonText: string;
}>) {
  const { t } = useI18n();
  if (available) return null;
  return (
    <DsAlert title={t("monitoring.health.unavailable", "Unavailable")} tone="warning">
      {reasonText}
      {reasonCode ? <code className="fs-code-value"> {reasonCode}</code> : null}
    </DsAlert>
  );
}

export function RegistryToolsDetailPage() {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<ToolTabKey>("config");
  const {
    detailQuery,
    entity,
    entityName,
    historyQuery,
    resolvedEntityId,
    routeEntityId,
    saveEntityPatch,
    saveError,
    saveStatus,
    saving,
    setSaveStatus,
    toggleEntityEnabled,
  } = useRegistryEntityDetail({
    endpoint: "/api/tools",
    idKey: "tool_id",
    idParam: "toolId",
    title: t("react.registry.tool", "Tool"),
  });

  const implementation = firstObject(entity, ["implementation"]);
  const config = firstObject(entity, ["config"]);
  const usingAgentsQuery = useQuery({
    queryKey: ["/api/tools", "agents", resolvedEntityId || routeEntityId] as const,
    queryFn: () => fetchToolUsingAgents(resolvedEntityId || routeEntityId),
    enabled: detailQuery.isSuccess && Boolean(resolvedEntityId || routeEntityId),
    retry: false,
  });
  const usingAgentsFromEntity = firstStringArray(entity, ["using_agents", "_agents", "agents"]);
  const usingAgents = (usingAgentsQuery.data || []).length ? usingAgentsQuery.data || [] : usingAgentsFromEntity;
  const operations = useMemo(() => toolOperations(entity), [entity]);
  const availabilityStatus = textOrEmpty(entity.availability_status).toLowerCase() || "available";
  const availabilityReasonCode = textOrEmpty(entity.availability_reason_code);
  const availabilityReasonText = textOrEmpty(entity.availability_reason_text).slice(0, 240);
  const isAvailable = availabilityStatus === "available";
  const builderMetadataDraft = useMemo(
    () =>
      builderMetadataDraftFromUnknown(
        entity.builder_metadata_jsonb || entity.builder_metadata,
      ),
    [entity.builder_metadata, entity.builder_metadata_jsonb],
  );
  const draftSeed = useMemo<ToolDraft>(
    () => ({
      antiUseCases: builderMetadataDraft.antiUseCases,
      category: firstStringFromUnknownValue([entity.category, ""]).trim(),
      compositionRole: builderMetadataDraft.compositionRole,
      description: firstStringFromUnknownValue([entity.description, ""]).trim(),
      determinismLevel: builderMetadataDraft.determinismLevel,
      displayName: firstStringFromUnknownValue([entity.display_name, entity.name, ""]).trim(),
      prompt: firstStringFromUnknownValue([entity.prompt, ""]).trim(),
      purpose: builderMetadataDraft.purpose,
      safeForAutoUse: builderMetadataDraft.safeForAutoUse,
      useCases: builderMetadataDraft.useCases,
    }),
    [
      builderMetadataDraft.antiUseCases,
      builderMetadataDraft.compositionRole,
      builderMetadataDraft.determinismLevel,
      builderMetadataDraft.purpose,
      builderMetadataDraft.safeForAutoUse,
      builderMetadataDraft.useCases,
      entity.category,
      entity.description,
      entity.display_name,
      entity.name,
      entity.prompt,
    ],
  );
  const draftStateKey = useMemo(
    () => `${resolvedEntityId || routeEntityId || "tool"}:${JSON.stringify(draftSeed)}`,
    [draftSeed, resolvedEntityId, routeEntityId],
  );
  const [draft, setDraft] = useKeyedState<ToolDraft>(draftStateKey, draftSeed);

  const missing = toolIsMissing(entity, detailQuery.error, detailQuery.isError);
  const loadState = (
    <ToolDetailLoadState
      isError={detailQuery.isError}
      isLoading={detailQuery.isLoading}
      missing={missing}
      onRetry={() => ignoreAsyncResult(detailQuery.refetch())}
    />
  );
  const needsLoadState = toolDetailNeedsLoadState(detailQuery.isLoading, detailQuery.isError, missing);

  const configPanel = (
    <ToolConfigPanel
      config={config}
      draft={draft}
      draftSeed={draftSeed}
      implementation={implementation}
      onSave={saveEntityPatch}
      saving={saving}
      setDraft={setDraft}
      setSaveStatus={setSaveStatus}
    />
  );

  const builderPanel = (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("react.registry.builder_metadata", "Builder Metadata")}</h2>
          <p>{t("react.registry.technical_authoring_metadata_used_for_workflow_authoring_to_decide_when_and_how_this_tool_should_surface", "Technical authoring metadata used for workflow authoring to decide when and how this tool should surface.")}</p>
        </div>
      </div>
      <div className="fs-panel-group">
        <BuilderMetadataEditor
          draft={{
            antiUseCases: draft.antiUseCases,
            compositionRole: draft.compositionRole,
            determinismLevel: draft.determinismLevel,
            purpose: draft.purpose,
            safeForAutoUse: draft.safeForAutoUse,
            useCases: draft.useCases,
          }}
          onChange={(next) =>
            setDraft((current) => ({
              ...current,
              antiUseCases: next.antiUseCases,
              compositionRole: next.compositionRole,
              determinismLevel: next.determinismLevel,
              purpose: next.purpose,
              safeForAutoUse: next.safeForAutoUse,
              useCases: next.useCases,
            }))
          }
        />
        <div className="flex flex-wrap gap-2">
          <button
            className="fs-button primary"
            disabled={saving}
            onClick={() => {
              ignoreAsyncResult(saveEntityPatch({
                builder_metadata_jsonb: builderMetadataPayload({
                  antiUseCases: draft.antiUseCases,
                  compositionRole: draft.compositionRole,
                  determinismLevel: draft.determinismLevel,
                  purpose: draft.purpose,
                  safeForAutoUse: draft.safeForAutoUse,
                  useCases: draft.useCases,
                }),
              }));
            }}
            type="button"
          >
            {t("react.registry.save_builder_metadata", "Save Builder metadata")}</button>
          <button
            className="fs-button"
            onClick={() => {
              setDraft(draftSeed);
              setSaveStatus("");
            }}
            type="button"
          >
            {t("trash.reset", "Reset")}</button>
        </div>
      </div>
    </div>
  );

  const actionsPanel = <ToolActionsPanel operations={operations} />;

  const promptPanel = (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("registry.tools.field.prompt", "Tool Prompt")}</h2>
          <p>{t("react.registry.prompt_contract_injected_when_this_tool_is_selected_in_runtime_context", "Prompt contract injected when this tool is selected in runtime context.")}</p>
        </div>
      </div>
      <div className="fs-field full">
        <label className="fs-field-label" htmlFor="tool-prompt">{t("registry.tool_detail.tab.prompt", "Prompt")}</label>
        <textarea
          className="fs-textarea fs-code-value"
          id="tool-prompt"
          name="prompt"
          onChange={(event) =>
            setDraft((current) => ({ ...current, prompt: event.target.value }))
          }
          placeholder={t("react.registry.prompt", "Prompt...")}
          value={draft.prompt}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          className="fs-button primary"
          disabled={saving}
          onClick={() => {
            ignoreAsyncResult(saveEntityPatch({ prompt: draft.prompt.trim() }));
          }}
          type="button"
        >
          {t("react.registry.save_prompt", "Save prompt")}</button>
        <button
          className="fs-button"
          onClick={() => {
            setDraft(draftSeed);
            setSaveStatus("");
          }}
          type="button"
        >
          {t("trash.reset", "Reset")}</button>
      </div>
      <details className="fs-disclosure">
        <DisclosureSummary count={operations.length} label={t("react.registry.advanced_tool_operations", "Advanced tool operations")} />
        <div className="fs-disclosure-body">
          <div className="fs-panel-stack">
            <p className="fs-empty-copy">
              {t("react.registry.operation_metadata_used_by_runtime_integrations_and_tool_authors", "Operation metadata used by runtime integrations and tool authors.")}</p>
            <JsonPanel title={t("registry.tool_detail.operations.title", "Operations")} value={operations} />
          </div>
        </div>
      </details>
    </div>
  );

  const agentsPanel = <ToolAgentsPanel usingAgents={usingAgents} />;

  const changelogPanel = (
    <RegistryEntityChangelogPanel
      entityId={resolvedEntityId || routeEntityId}
      entityType="tool"
      loading={historyQuery.isLoading}
      onRestored={async () => {
        await historyQuery.refetch();
        await detailQuery.refetch();
      }}
      rows={historyQuery.data || []}
    />
  );

  const pageBody = needsLoadState ? loadState : (
    <section className="fs-page-sections">
      <DsPageHeader
        action={
          <>
            <Link className="fs-button" to="/registry/tools">
              {t("react.registry.back_to_tools", "Back to tools")}</Link>
            {/* No identifier chip in the header. #40 took the UUID off
                Guardrails detail; the decision that followed was to drop the
                identifier everywhere, readable or not. The id is in the URL,
                which is where anyone who needs it looks. */}
            {entity.enabled || isAvailable ? (
              <button
                className="fs-button"
                disabled={saving}
                onClick={() => {
                  ignoreAsyncResult(toggleEntityEnabled());
                }}
                type="button"
              >
                {entity.enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
              </button>
            ) : null}
            <SaveStatusBadge status={saveStatus} />
          </>
        }
        description={t("react.registry.review_configuration_prompts_agent_dependencies_and_change_history_for_this_tool", "Review configuration, prompts, agent dependencies, and change history for this tool.")}
        eyebrow={t("trash.category.registry", "Registry")}
        title={entityName || t("react.registry.tool_detail", "Tool detail")}
      />
      <SaveErrorAlert error={saveError} />
      <ToolAvailabilityAlert
        available={isAvailable}
        reasonCode={availabilityReasonCode}
        reasonText={availabilityReasonText}
      />

      <section aria-label={t("react.registry.tool_detail", "Tool detail")} className="fs-settings-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.registry.tool_detail_sections", "Tool detail sections")}
          idPrefix="tool"
          items={localizedTOOL_TABS(t)}
          onSelect={(key) => setActiveTab(key as ToolTabKey)}
        />
        <div
          aria-labelledby={`tool-tab-${activeTab}`}
          className="fs-tabs-panel"
          id={`tool-panel-${activeTab}`}
          role="tabpanel"
        >
          <div className="fs-panel-stack">
            {activeTab === "config" ? configPanel : null}
            {activeTab === "builder" ? builderPanel : null}
            {activeTab === "actions" ? actionsPanel : null}
            {activeTab === "prompt" ? promptPanel : null}
            {activeTab === "agents" ? agentsPanel : null}
            {activeTab === "changelog" ? changelogPanel : null}
          </div>
        </div>
      </section>
    </section>
  );

  return <PageContainer width="standard">{pageBody}</PageContainer>;
}
