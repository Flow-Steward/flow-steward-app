import { useI18n } from "@/lib/i18n/useI18n";
import { cn } from "@/lib/utils/cn";
import { CatalogIcon } from "@/components/shared/catalog-browse";
import { extensionIconUrl } from "@/pages/extensions/extension-icon";
import { stepVisual } from "@/pages/workflows/builder/canvas-core";

/** Shared type appearance for canvas, menus, overview and the step editor. */
export function WorkflowStepIcon({ kind, toolCategory, className }: Readonly<{ kind: string; toolCategory?: string; className?: string }>) {
  const { Icon, iconClass } = stepVisual(kind, toolCategory);
  return (
    <span aria-hidden="true" className={cn("inline-flex h-6 w-6 shrink-0 items-center justify-center rounded", iconClass, className)}>
      <Icon className="h-4 w-4" />
    </span>
  );
}

/** The logo of the extension a step comes from, named for assistive technology. */
export function WorkflowStepExtensionMark({ extensionId, extensionName }: Readonly<{ extensionId: string; extensionName?: string }>) {
  const { t } = useI18n();
  if (!extensionId) return null;
  const name = extensionName?.trim();
  const label = name && name !== extensionId
    ? t("react.workflows.step_from_extension", "From extension {value0}", { value0: name })
    : t("extension_page.extension_fallback", "Extension");
  return (
    <span aria-label={label} className="inline-flex shrink-0" data-step-extension={extensionId} role="img" title={label}>
      <CatalogIcon size="inline" src={extensionIconUrl(extensionId)} />
    </span>
  );
}
