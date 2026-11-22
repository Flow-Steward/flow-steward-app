import type { FormFieldView } from "@/components/workflow-inputs/form-field";

import { prepareOperatorRuns, runBreaksUploadLimits } from "./submission";

function field(name: string, kind: string, extra: Partial<FormFieldView> = {}): FormFieldView {
  return { name, kind, widget: "", label: name, ...extra };
}

const fields = [
  field("doc_type", "select", { options: ["invoice"], required: true }),
  field("pages", "file_list", { required: true }),
  field("cover", "file"),
  field("ocr_model", "string", { hidden: true }),
];

const pages = [new File(["one"], "p1.jpg"), new File(["two"], "p2.jpg"), new File(["three"], "p3.jpg"), new File(["four"], "p4.jpg")];
const cover = new File(["c"], "cover.pdf");
const values = { doc_type: "invoice", ocr_model: "never sent" };

describe("prepareOperatorRuns", () => {
  it("sends Bulk as one run with every file, one entry per file in upload order", () => {
    const { runs, problems } = prepareOperatorRuns({
      keyPrefix: "p1:wf",
      submission: { mode: "bulk" },
      fields,
      values,
      files: { pages, cover: [cover] },
      submissionId: "s1",
    });
    expect(problems).toEqual([]);
    expect(runs).toHaveLength(1);
    expect(runs[0].inputs).toEqual({ doc_type: "invoice" });
    expect(runs[0].files.map((entry) => [entry.inputName, entry.file.name])).toEqual([
      ["pages", "p1.jpg"],
      ["pages", "p2.jpg"],
      ["pages", "p3.jpg"],
      ["pages", "p4.jpg"],
      ["cover", "cover.pdf"],
    ]);
    expect(runs[0].idempotencyKey).toBe("operator:p1:wf:s1:all");
  });

  it("sends For each over files as one run per file, other values and files repeated", () => {
    const { runs } = prepareOperatorRuns({
      keyPrefix: "p1:wf",
      submission: { mode: "foreach", target_input: "pages" },
      fields,
      values,
      files: { pages, cover: [cover] },
      submissionId: "s1",
    });
    expect(runs).toHaveLength(4);
    runs.forEach((run, index) => {
      expect(run.inputs).toEqual({ doc_type: "invoice" });
      expect(run.files.map((entry) => [entry.inputName, entry.file.name])).toEqual([
        ["cover", "cover.pdf"],
        ["pages", pages[index].name],
      ]);
    });
    expect(new Set(runs.map((run) => run.idempotencyKey)).size).toBe(4);
  });

  it("sends For each over a list as one-item lists", () => {
    const companies = field("companies", "object_list", { sub_fields: [field("name", "string")], min_items: 2 });
    const { runs, problems } = prepareOperatorRuns({
      keyPrefix: "p1:wf",
      submission: { mode: "foreach", target_input: "companies" },
      fields: [companies],
      values: { companies: [{ name: "Acme" }, { name: "Beta" }, {}] },
      files: {},
      submissionId: "s2",
    });
    expect(problems).toEqual([]);
    expect(runs.map((run) => run.inputs)).toEqual([{ companies: [{ name: "Acme" }] }, { companies: [{ name: "Beta" }] }]);
  });

  it("refuses a form with missing required values or nothing to send", () => {
    expect(
      prepareOperatorRuns({ keyPrefix: "k", submission: { mode: "bulk" }, fields, values: {}, files: {}, submissionId: "s" }).problems,
    ).toEqual([
      { field: "doc_type", code: "required" },
      { field: "pages", code: "required" },
    ]);
    const optionalTarget = [field("receipts", "file_list")];
    const result = prepareOperatorRuns({ keyPrefix: "k", submission: { mode: "foreach", target_input: "receipts" }, fields: optionalTarget, values: {}, files: {}, submissionId: "s" });
    expect(result.runs).toEqual([]);
    expect(result.problems).toEqual([{ field: "receipts", code: "nothing_to_send" }]);
  });

  it("never downgrades For each to Bulk when its target is hidden, fixed, condition-hidden or missing", () => {
    const send = (target: string, targetFields: FormFieldView[], targetValues: Record<string, unknown> = {}) =>
      prepareOperatorRuns({
        keyPrefix: "k",
        submission: { mode: "foreach", target_input: target },
        fields: targetFields,
        values: targetValues,
        files: { pages },
        submissionId: "s",
      });
    const hidden = send("pages", [field("pages", "file_list", { hidden: true })]);
    const fixed = send("pages", [field("pages", "file_list", { fixed: true })]);
    const missing = send("ghost", fields, values);
    const empty = send("", fields, values);
    const conditional = send(
      "pages",
      [field("split", "boolean"), field("pages", "file_list", { conditional_logic: [[{ field: "split", operator: "==", value: true }]] })],
      { split: false },
    );
    for (const result of [hidden, fixed, missing, conditional]) {
      expect(result.runs).toEqual([]);
      expect(result.problems).toEqual([{ field: result === missing ? "ghost" : "pages", code: "foreach_target_unavailable" }]);
    }
    expect(empty.runs).toEqual([]);
    expect(empty.problems).toEqual([{ field: "", code: "foreach_target_unavailable" }]);
  });
});

describe("runBreaksUploadLimits", () => {
  const limits = { max_files: 3, max_file_bytes: 4, max_total_bytes: 12 };
  it("refuses too many files, a file too large and an empty file", () => {
    const files = (sizes: number[]) => sizes.map((size, index) => ({ inputName: "pages", file: new File(["x".repeat(size)], `${index}`) }));
    expect(runBreaksUploadLimits({ files: files([1, 2, 3]) }, limits)).toBe(false);
    expect(runBreaksUploadLimits({ files: files([1, 1, 1, 1]) }, limits)).toBe(true);
    expect(runBreaksUploadLimits({ files: files([5]) }, limits)).toBe(true);
    expect(runBreaksUploadLimits({ files: files([0]) }, limits)).toBe(true);
    expect(runBreaksUploadLimits({ files: files([1, 2]) }, undefined)).toBe(false);
  });
});
