import { useI18n } from "@/lib/i18n/useI18n";
import type { RefObject } from "react";

import { DsAlert } from "@/components/shared/design-system";
import { type CreateWorkflowForm, WORKFLOW_ID_RULE } from "@/pages/workflows/list/create-workflow-state";

/**
 * The start-from-scratch form: Display name first and required, the Workflow
 * ID made from it until the user writes their own, and a description.
 */
export function CreateWorkflowFields({
  form,
  idRef,
}: Readonly<{ form: CreateWorkflowForm; idRef?: RefObject<HTMLInputElement | null> }>) {
  const { t } = useI18n();
  return (
    <>
      {/* A field-level rule belongs under its field; anything the server said
          is an alert, because it is not about one input. */}
      {form.notice && !form.idInvalid && !form.nameInvalid ? (
        <DsAlert title={t("react.workflows.workflow_was_not_created", "Workflow was not created")} tone="danger">
          {form.notice}
        </DsAlert>
      ) : null}
      <div className="fs-form-grid">
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="new_workflow_display_name">
            {t("react.workflows.display_name", "Display name")}
            <span aria-hidden="true" className="fs-required"> *</span></label>
          <input
            aria-invalid={form.nameInvalid}
            className="fs-input"
            id="new_workflow_display_name"
            name="new_workflow_display_name"
            onChange={(event) => form.setDisplayName(event.target.value)}
            placeholder={t("react.workflows.custom_workflow", "Custom workflow")}
            ref={idRef}
            required
            type="text"
            value={form.displayName}
          />
          {form.nameInvalid ? <span className="fs-field-helper error">{form.notice}</span> : null}
        </div>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="new_workflow_id">
            {t("react.workflows.workflow_id", "Workflow ID")}
            <span aria-hidden="true" className="fs-required"> *</span></label>
          <input
            aria-invalid={form.idInvalid}
            className="fs-input"
            id="new_workflow_id"
            name="new_workflow_id"
            onChange={(event) => form.setWorkflowId(event.target.value)}
            placeholder="custom_workflow"
            required
            type="text"
            value={form.workflowId}
          />
          {form.idInvalid ? (
            <span className="fs-field-helper error">{form.notice}</span>
          ) : (
            <span className="fs-field-helper">{t("react.workflows.display_workflow_id_rule", WORKFLOW_ID_RULE)}</span>
          )}
        </div>
        <div className="fs-field full">
          <label className="fs-field-label" htmlFor="new_workflow_description">
            {t("registry.workflows.simulation.description", "Description")}</label>
          <textarea
            className="fs-textarea"
            id="new_workflow_description"
            name="new_workflow_description"
            onChange={(event) => form.setDescription(event.target.value)}
            value={form.description}
          />
        </div>
      </div>
    </>
  );
}
