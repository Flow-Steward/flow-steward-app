import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { marketplaceHref } from "@/pages/workflows/catalog/catalog-model";
import type { MissingExtensionDetail } from "@/pages/workflows/shared/api";

type Translate = I18nContextValue["t"];

/**
 * Why an extension workflow cannot run, as the person reading it needs it:
 * which extensions are missing by name, whether this viewer can fix it, and
 * the backend's own sentence kept for the Details disclosure.
 */
export type MissingExtensionNoticeModel = {
  details: MissingExtensionDetail[];
  viewerCanManageExtensions: boolean;
  technicalReason: string;
};

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function detailsOf(value: unknown): MissingExtensionDetail[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(recordOf)
    .filter((item) => textOf(item.extension_id))
    .map((item) => ({
      extension_id: textOf(item.extension_id),
      display_name: textOf(item.display_name),
      state: textOf(item.state),
      min_version: textOf(item.min_version),
      max_version_exclusive: textOf(item.max_version_exclusive),
    }));
}

/**
 * The structured reason from the first source that is unavailable and names
 * the extensions it misses, or null. Sources are the workflow payloads the
 * page already holds (its record, its portability, the export policy).
 * `canAdministerPlatform` is the Extensions pages' own check; the backend's
 * `viewer_can_manage_extensions` says the same for the request it answered.
 */
export function missingExtensionNotice(
  sources: readonly unknown[],
  canAdministerPlatform: boolean,
): MissingExtensionNoticeModel | null {
  for (const source of sources) {
    const record = recordOf(source);
    const status = textOf(record.availability_status).toLowerCase();
    if (status === "available") continue;
    const details = detailsOf(record.missing_extension_details);
    if (!details.length) continue;
    return {
      details,
      viewerCanManageExtensions: canAdministerPlatform || record.viewer_can_manage_extensions === true,
      technicalReason: textOf(record.availability_reason_text),
    };
  }
  return null;
}

export function missingExtensionName(detail: MissingExtensionDetail): string {
  return textOf(detail.display_name) || detail.extension_id;
}

function namesOf(details: readonly MissingExtensionDetail[]): string {
  return details.map(missingExtensionName).join(", ");
}

/** "This workflow can't run yet: it needs PostgreSQL (Google MCP Toolbox)." */
export function missingExtensionHeadline(model: MissingExtensionNoticeModel, t: Translate = fallbackContext.t): string {
  return t("react.workflows.missing_extension_headline", "This workflow can't run yet: it needs {value0}.", {
    value0: namesOf(model.details),
  });
}

/** The list's one line: "Can't run yet: needs PostgreSQL (Google MCP Toolbox)." */
export function missingExtensionShortLine(model: MissingExtensionNoticeModel, t: Translate = fallbackContext.t): string {
  return t("react.workflows.missing_extension_short_line", "Can't run yet: needs {value0}.", {
    value0: namesOf(model.details),
  });
}

/** What a viewer who cannot install extensions does instead. */
export function missingExtensionAskAdministrator(model: MissingExtensionNoticeModel, t: Translate = fallbackContext.t): string {
  return model.details.length > 1
    ? t("react.workflows.missing_extension_ask_admin_many", "Ask your administrator to turn them on.")
    : t("react.workflows.missing_extension_ask_admin", "Ask your administrator to turn it on.");
}

/**
 * Where an administrator fixes it: the catalog entry for one never installed
 * (or not known to be), the installed extension's own page for one that is
 * installed but off or at a version this workflow does not accept.
 */
export function missingExtensionHref(detail: MissingExtensionDetail): string {
  if (detail.state === "installed_disabled" || detail.state === "installed_incompatible") {
    return `/extensions/${encodeURIComponent(detail.extension_id)}`;
  }
  return marketplaceHref(detail.extension_id);
}

/** Install, Turn on or Update; named when there is more than one to act on. */
export function missingExtensionActionLabel(
  detail: MissingExtensionDetail,
  named: boolean,
  t: Translate = fallbackContext.t,
): string {
  const value0 = missingExtensionName(detail);
  if (detail.state === "installed_disabled") {
    return named
      ? t("react.workflows.missing_extension_turn_on_named", "Turn on {value0}", { value0 })
      : t("react.workflows.missing_extension_turn_on", "Turn on");
  }
  if (detail.state === "installed_incompatible") {
    return named
      ? t("react.workflows.missing_extension_update_named", "Update {value0}", { value0 })
      : t("react.workflows.missing_extension_update", "Update");
  }
  return named
    ? t("react.workflows.missing_extension_install_named", "Install {value0}", { value0 })
    : t("react.workflows.missing_extension_install", "Install");
}
