import { describe, expect, it } from "vitest";

import {
  effectiveReturnedFiles,
  newReturnedFileKey,
  returnedFileEntries,
  stepFileResults,
  toggledReturnedFiles,
} from "./detail-returned-files";

describe("a step's file results", () => {
  it("lists the file each producing step kind saves", () => {
    expect(stepFileResults("file_from_base64", { file_from_base64: { result_target: "state.pdf" } })).toEqual([{ result: "file", target: "state.pdf" }]);
    expect(stepFileResults("http_request", { http_request: { response_mode: "artifact", response_target: "state.download" } })).toEqual([{ result: "response", target: "state.download" }]);
    expect(stepFileResults("http_request", { http_request: { response_mode: "inline", response_target: "state.page" } })).toEqual([]);
    expect(stepFileResults("invoke_workflow", { invoke_workflow: { artifact_mapping: { report: "state.child_report", note: "plain" } } })).toEqual([{ result: "report", target: "state.child_report" }]);
  });

  it("offers only connector outputs whose declared type is a file", () => {
    const override = { connector: { output_mapping: { attachments: "state.attachments", subject: "state.subject" } } };
    const outputs = [{ name: "attachments", type: "file" }, { name: "subject", type: "string" }];
    expect(stepFileResults("connector", override, outputs)).toEqual([{ result: "attachments", target: "state.attachments" }]);
  });
});

describe("Return from workflow entries", () => {
  const pdf = { result: "file", target: "state.reports.invoice-pdf" };

  it("makes a key from the variable, never from a file name, and keeps it unique", () => {
    expect(newReturnedFileKey(pdf, [])).toBe("invoice_pdf");
    expect(newReturnedFileKey(pdf, ["invoice_pdf", "invoice_pdf_2"])).toBe("invoice_pdf_3");
    expect(newReturnedFileKey({ result: "file", target: "state.files" }, [])).toBe("files_file");
  });

  it("ticks once, keeps the key when ticked again, and unticks to nothing", () => {
    const ticked = toggledReturnedFiles([], pdf, true, []);
    expect(ticked).toEqual([{ result: "file", key: "invoice_pdf" }]);
    expect(toggledReturnedFiles(ticked, pdf, true, ["invoice_pdf"])).toBe(ticked);
    expect(toggledReturnedFiles(ticked, pdf, false, [])).toEqual([]);
  });

  it("distinguishes a step never set from one set to return nothing", () => {
    expect(returnedFileEntries({})).toBeNull();
    expect(returnedFileEntries({ returned_files: [] })).toEqual([]);
    expect(returnedFileEntries({ returned_files: [{ result: "file", key: "a", required: true }, { key: "orphan" }] })).toEqual([{ result: "file", key: "a", required: true }]);
  });

  it("reads a never-set step's returned files from the contract rows whose source it is", () => {
    const rows = [
      { name: "invoice", required: true, source: { step_id: "render", result: "file" } },
      { name: "other", source: { step_id: "elsewhere", result: "file" } },
    ];
    expect(effectiveReturnedFiles({}, "render", rows)).toEqual([{ result: "file", key: "invoice", required: true }]);
    expect(effectiveReturnedFiles({ returned_files: [] }, "render", rows)).toEqual([]);
  });
});
