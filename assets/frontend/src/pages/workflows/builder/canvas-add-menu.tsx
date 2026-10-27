import { useI18n } from "@/lib/i18n/useI18n";

type CanvasAddMenuProps = {
  scope?: "hook";
  onStep: () => void;
  onGroup: () => void;
};

/** The ordinary structure menu, shared by the workflow and a hook's child root. For each is a step (Flow), not offered here. */
export function CanvasAddMenu({ scope, onStep, onGroup }: Readonly<CanvasAddMenuProps>) {
  const { t } = useI18n();
  const options = [
    { onClick: onStep, label: t("react.workflows.add_menu_step", "Step"), hint: scope === "hook" ? t("react.workflows.add_menu_hook_step_hint", "A step in this hook.") : t("react.workflows.add_menu_step_hint", "A step in the workflow root.") },
    { onClick: onGroup, label: t("react.workflows.add_menu_group", "Group"), hint: t("react.workflows.palette_desc_group", "Steps that belong together. Can become a child workflow.") },
  ];
  return options.map((option) => (
    <button className="block w-full rounded-xl px-3 py-2 text-left text-sm text-[var(--fs-text-primary)] hover:bg-[var(--fs-bg-surface-muted)]" key={option.label} onClick={(event) => { event.stopPropagation(); option.onClick(); }} type="button">
      {option.label}
      <span className="mt-0.5 block text-[11px] text-[var(--fs-text-tertiary)]">{option.hint}</span>
    </button>
  ));
}
