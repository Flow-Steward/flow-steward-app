import { describe, expect, it } from "vitest";
import { interpolate } from "@/lib/i18n/interpolate";
import { workflowIOIssueMessage } from "@/pages/workflows/detail/detail-dom-bridge";

describe("phase I/O validation display", () => {
  it("translates explicitly keyed diagnostics and preserves owner messages", () => {
    const issue = { message: "Owner diagnostic", message_key: "input_ambiguous", message_params: { input_name: "task_summary" } };
    const t = (key: string, fallback = "", params?: Parameters<typeof interpolate>[1]) =>
      interpolate(key === "input_ambiguous" ? "Įvestis {input_name} yra dviprasmiška." : fallback, params);
    expect(workflowIOIssueMessage(issue, t)).toBe("Įvestis task_summary yra dviprasmiška.");
    expect(workflowIOIssueMessage({ message: "Owner diagnostic" }, t)).toBe("Owner diagnostic");
    expect(issue.message).toBe("Owner diagnostic");
  });
});
