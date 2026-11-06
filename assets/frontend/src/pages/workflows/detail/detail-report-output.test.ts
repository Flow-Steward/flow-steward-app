import { describe, expect, it } from "vitest";

import type { WorkflowInterfaceFieldDraft } from "@/pages/workflows/detail/detail-interface";

import { reportOutputCandidates, staleReportOutput } from "./detail-report-output";

function row(name: string, type: string, required: boolean): WorkflowInterfaceFieldDraft {
  return { draftId: name, description: "", label: "", name, options: "", required, type } as WorkflowInterfaceFieldDraft;
}

const rows = [
  row("sales_report", "object", true),
  row("notes", "string", true),
  row("optional_report", "object", false),
  row("raw", "json", true),
];

describe("report output", () => {
  it("offers only required outputs of type object", () => {
    expect(reportOutputCandidates(rows)).toEqual(["sales_report", "raw"]);
  });

  it("calls a selection stale once its output was renamed, retyped or made optional", () => {
    expect(staleReportOutput("sales_report", rows)).toBe(false);
    expect(staleReportOutput("", rows)).toBe(false);
    expect(staleReportOutput("sales", rows)).toBe(true);
    expect(staleReportOutput("notes", rows)).toBe(true);
    expect(staleReportOutput("optional_report", rows)).toBe(true);
  });
});
