
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { trimRepeatedEnd } from "@/lib/utils/string-normalization";

export type ShellBreadcrumb = { label: string; to?: string };

const OPAQUE_SEGMENT = /^(?:\d+|[0-9a-f]{8}-[0-9a-f-]{27,})$/i;



function getExactChildRouteLabels(t: I18nContextValue["t"] = fallbackContext.t) {
  return new Map([
  ["/registry/workflows/import", t("react.workflows.import_breadcrumb", "Import")],
  ["/settings/account", t("auth.login.account_label", "Account")],
  ["/settings/general", t("registry.workflow_detail.settings.general", "General")],
  ["/settings/notifications", t("settings.tab.notifications", "Notifications")],
  ["/settings/providers", t("settings.tab.model_providers", "Model Providers")],
  ["/settings/retention", t("settings.retention.title", "Retention")],
  ["/settings/security", t("account.security.title", "Security")],
  ["/settings/service-accounts", t("react.shared.service_accounts", "Service Accounts")],
  ["/settings/users", t("react.shared.users", "Users")],
]);
}

// A segment whose name only means something inside one section.


function getSectionSegmentLabels(t: I18nContextValue["t"] = fallbackContext.t) {
  return new Map([
  ["/registry/workflows", new Map([
    ["projects", t("shell.nav.projects", "Projects")],
    ["builder", t("react.workflows.builder_title", "Builder")],
  ])],
]);
}
/** An extension's workflow id, `extension.<install uuid>.<workflow id>`: the workflow id names it. */
const EXTENSION_WORKFLOW_ID = /^extension\.[0-9a-f]{8}-[0-9a-f-]{27,}\.(.+)$/i;

function humanize(value: string, t: I18nContextValue["t"]) {
  let decoded: string;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    return t("react.shared.current_page", "Current page");
  }
  decoded = EXTENSION_WORKFLOW_ID.exec(decoded)?.[1] ?? decoded;
  const text = decoded.replaceAll(/[-_]+/g, " ").trim();
  return text
    ? `${text[0]?.toUpperCase() ?? ""}${text.slice(1)}`
    : t("react.shared.current_page", "Current page");
}

function singular(label: string) {
  if (label.endsWith("ies")) {
    return `${label.slice(0, -3)}y`;
  }
  return label.endsWith("s") ? label.slice(0, -1) : label;
}

export function breadcrumbsForPath(
  pathname: string,
  routeLabels: ReadonlyMap<string, string>, t: I18nContextValue["t"] = fallbackContext.t,
): ShellBreadcrumb[] {
  const normalized = trimRepeatedEnd(pathname, "/") || "/";
  const exact = routeLabels.get(normalized);
  if (exact) return [{ label: exact }];

  const parent = [...routeLabels.keys()]
    .filter((route) => normalized.startsWith(`${route}/`))
    .sort((left, right) => right.length - left.length)[0];
  if (parent) {
    const label =
      routeLabels.get(parent) ?? humanize(parent.split("/").at(-1) ?? "", t);
    const tail = normalized.slice(parent.length + 1).split("/").filter(Boolean);
    const last = tail.at(-1) ?? "";
    const detailLabel =
      getExactChildRouteLabels(t).get(normalized) ??
      ((parent !== "/registry/workflows" || tail.length === 2)
        ? getSectionSegmentLabels(t).get(parent)?.get(last)
        : undefined) ??
      (OPAQUE_SEGMENT.test(last)
        ? t("react.shared.value1_details", "{value1} details", { value1: String(singular(label)) })
        : humanize(last, t));
    return [{ label, to: parent }, { label: detailLabel }];
  }

  const last = normalized.split("/").findLast(Boolean) ?? "current-page";
  return [
    { label: OPAQUE_SEGMENT.test(last) ? t("react.shared.current_page", "Current page") : humanize(last, t) },
  ];
}
