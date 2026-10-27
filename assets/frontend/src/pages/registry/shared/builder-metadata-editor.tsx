import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useId } from "react";

import { DsCheckbox, DsSelect } from "@/components/shared/design-system";

import type { BuilderMetadataDraft } from "./builder-metadata";

function localizedDETERMINISM_OPTIONS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "deterministic", label: t("react.registry.deterministic_variant_0badac3c", "deterministic") },
  { value: "hybrid", label: t("react.registry.hybrid", "hybrid") },
  { value: "agentic", label: t("react.registry.agentic", "agentic") },
];
}

export function BuilderMetadataEditor({
  draft,
  onChange,
}: Readonly<{
  draft: BuilderMetadataDraft;
  onChange: (next: BuilderMetadataDraft) => void;
}>) {
  const { t } = useI18n();
  const controlIdPrefix = useId().replaceAll(":", "");

  return (
    <div className="fs-panel-group">
      <p className="fs-empty-copy">
        {t("react.registry.workflow_discovery_uses_this_metadata_for_retrieval_and_ranking_these_fields_stay_technical_by_default_and_are", "Workflow discovery uses this metadata for retrieval and ranking. These fields stay technical by default and are meant to improve workflow authoring quality rather than end-user presentation.")}</p>
      <div className="fs-field full">
        <label
          className="fs-field-label"
          htmlFor={`${controlIdPrefix}-builder-metadata-purpose`}
        >
          {t("react.registry.purpose", "Purpose")}</label>
        <textarea
          className="fs-textarea"
          id={`${controlIdPrefix}-builder-metadata-purpose`}
          name="builder_metadata_purpose"
          onChange={(event) => onChange({ ...draft, purpose: event.target.value })}
          placeholder={t("react.registry.what_this_primitive_is_for_in_workflow_authoring", "What this primitive is for in workflow authoring.")}
          value={draft.purpose}
        />
      </div>
      <div className="fs-form-grid">
        <div className="fs-field">
          <label
            className="fs-field-label"
            htmlFor={`${controlIdPrefix}-builder-metadata-composition-role`}
          >
            {t("react.registry.composition_role", "Composition Role")}</label>
          <input
            className="fs-input"
            id={`${controlIdPrefix}-builder-metadata-composition-role`}
            name="builder_metadata_composition_role"
            onChange={(event) =>
              onChange({ ...draft, compositionRole: event.target.value })
            }
            placeholder="reasoning_step"
            type="text"
            value={draft.compositionRole}
          />
        </div>
        <div className="fs-field">
          <span className="fs-field-label">{t("react.registry.determinism", "Determinism")}</span>
          <DsSelect
            ariaLabel={t("react.registry.determinism", "Determinism")}
            onChange={(value) => onChange({ ...draft, determinismLevel: value })}
            options={localizedDETERMINISM_OPTIONS(t)}
            value={draft.determinismLevel}
          />
        </div>
      </div>
      <div className="fs-field full">
        <label
          className="fs-field-label"
          htmlFor={`${controlIdPrefix}-builder-metadata-use-cases`}
        >
          {t("react.registry.use_cases", "Use Cases")}</label>
        <textarea
          className="fs-textarea"
          id={`${controlIdPrefix}-builder-metadata-use-cases`}
          name="builder_metadata_use_cases"
          onChange={(event) => onChange({ ...draft, useCases: event.target.value })}
          placeholder={t("react.registry.one_use_case_per_line", "One use case per line")}
          value={draft.useCases}
        />
      </div>
      <div className="fs-field full">
        <label
          className="fs-field-label"
          htmlFor={`${controlIdPrefix}-builder-metadata-anti-use-cases`}
        >
          {t("react.registry.anti_use_cases", "Anti Use Cases")}</label>
        <textarea
          className="fs-textarea"
          id={`${controlIdPrefix}-builder-metadata-anti-use-cases`}
          name="builder_metadata_anti_use_cases"
          onChange={(event) =>
            onChange({ ...draft, antiUseCases: event.target.value })
          }
          placeholder={t("react.registry.when_workflow_authoring_should_avoid_using_this_primitive", "When workflow authoring should avoid using this primitive")}
          value={draft.antiUseCases}
        />
      </div>
      {/* No surface around this row. Canon lists helper text in the checkbox
          anatomy and draws no panel around it, and its own live option row is
          `inline-flex` with an explicit `width: fit-content` — the narrow target
          is the published intent, not an app artefact. The surface was ours, and
          it was the only thing implying the whole row toggles: the label is
          559×44 inside a 1081px row, so the target was never too small, the
          surface was too wide for what responded inside it. This flag removes a
          human review step, so a full-width toggle would let a stray click flip
          it silently. */}
      <DsCheckbox
        checked={draft.safeForAutoUse}
        name="builder_metadata_safe_for_auto_use"
        onChange={(checked) => onChange({ ...draft, safeForAutoUse: checked })}
      >
        {/* One wrapper, because DsCheckbox's children span is an inline-flex
            row: two children sit side by side and `display` on a flex item is
            blockified away. `.fs-field`'s grid does the stacking. */}
        <span className="fs-field">
          <span>{t("react.registry.safe_for_automatic_use", "Safe for automatic use")}</span>
          <span className="fs-field-helper">
            {t("react.registry.marks_this_primitive_as_suitable_for_automatic_selection_during_workflow_authoring_execution_permissions_still", "Marks this primitive as suitable for automatic selection during workflow authoring. Execution permissions still apply.")}</span>
        </span>
      </DsCheckbox>
    </div>
  );
}
