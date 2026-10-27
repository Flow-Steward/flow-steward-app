import { describe, expect, it } from "vitest";
import { HttpError } from "@/lib/http/errors";
import { interpolate } from "@/lib/i18n/interpolate";
import { workflowValidationErrorMessage } from "@/pages/workflows/detail/detail-webhook";

describe("workflow validation save errors", () => {
  it.each(["lt", "en"])("renders the %s transform source error from a rejected save", (locale) => {
    const source = "state.inputs.owner_payload";
    const message = `transform input.source '${source}' is not produced by prior workflow inputs or earlier steps`;
    const issue = { message, message_key: "react.workflows.transform_input_source_unavailable", message_params: { source } };
    const error = new HttpError({ status: 400, url: "/steps", payload: { detail: { validation: { errors: [issue] } } } });
    const t = (key: string, fallback = "", params?: Parameters<typeof interpolate>[1]) => interpolate(locale === "lt" && key === issue.message_key ? "Transformavimo įvesties šaltinis '{source}' nėra sukurtas ankstesnių darbo eigos įvesčių ar žingsnių." : fallback, params);
    expect(workflowValidationErrorMessage(error, "Fallback", t)).toBe(locale === "lt" ? `Transformavimo įvesties šaltinis '${source}' nėra sukurtas ankstesnių darbo eigos įvesčių ar žingsnių.` : message);
    expect(issue.message_params.source).toBe(source);
  });

  it("translates keyed validation errors in the HTTP envelope without translating owner text", () => {
    const issue = { message: "Expected input source unavailable", message_key: "source_unavailable", message_params: { input_name: "task_summary", source: "phase.start.task_summary" } };
    const error = new HttpError({ status: 422, url: "/phase", payload: { error: { message: issue.message, details: { validation: { errors: [issue] } } } } });
    const t = (key: string, fallback = "", params?: Parameters<typeof interpolate>[1]) => interpolate(key === "source_unavailable" ? "Įvesties {input_name} šaltinis {source} nepasiekiamas." : fallback, params);
    expect(workflowValidationErrorMessage(error, "Fallback", t)).toBe("Įvesties task_summary šaltinis phase.start.task_summary nepasiekiamas.");
    expect(issue.message).toBe("Expected input source unavailable");
    expect(workflowValidationErrorMessage(new Error("Owner message"), "Fallback", t)).toBe("Owner message");
    const direct = new HttpError({ status: 400, url: "/phase", payload: { detail: { validation: { errors: [issue, { message: "Owner diagnostic" }], warnings: [{ message: "Owner warning" }] } } } });
    expect(workflowValidationErrorMessage(direct, "Fallback", t)).toBe("Įvesties task_summary šaltinis phase.start.task_summary nepasiekiamas.\nOwner diagnostic");
  });
});
