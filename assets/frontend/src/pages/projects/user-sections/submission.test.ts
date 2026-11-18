import { buildInputFields } from "./schema";
import { prepareSectionRuns, runWithConcurrency } from "./submission";
import type { JsonSchema } from "./types";

const schema: JsonSchema = {
  type: "object",
  properties: {
    receipt: { type: "object", "x-fs-type": "file" },
    company: { type: "string" },
    tags: { type: "array", items: { type: "string" } },
  },
  required: ["receipt", "company"],
};
const fields = buildInputFields(schema);

function file(name: string, size = 10, lastModified = 1): File {
  return new File([new Uint8Array(size)], name, { lastModified, type: "image/jpeg" });
}

const foreach = { id: "receipts", submission: { mode: "foreach" as const, target_input: "receipt" } };

describe("prepareSectionRuns", () => {
  it("sends one run per file with the other values repeated", () => {
    const files = { receipt: Array.from({ length: 10 }, (_, index) => file(`r${index}.jpg`, 10 + index)) };
    const { runs, problems } = prepareSectionRuns({ section: foreach, fields, values: { company: "ACME" }, files, submissionId: "s1" });
    expect(problems).toEqual([]);
    expect(runs).toHaveLength(10);
    expect(runs.every((run) => run.inputs.company === "ACME")).toBe(true);
    expect(runs[3].files).toEqual([{ inputName: "receipt", file: files.receipt[3] }]);
  });

  it("keeps two receipts with the same name as two items with their own keys", () => {
    const { runs } = prepareSectionRuns({
      section: foreach,
      fields,
      values: { company: "ACME" },
      files: { receipt: [file("scan.jpg", 10, 1), file("scan.jpg", 10, 1)] },
      submissionId: "s1",
    });
    expect(new Set(runs.map((run) => run.idempotencyKey)).size).toBe(2);
  });

  it("gives a retry the same keys and a new submission new ones", () => {
    const files = { receipt: [file("a.jpg")] };
    const first = prepareSectionRuns({ section: foreach, fields, values: { company: "A" }, files, submissionId: "s1" });
    const again = prepareSectionRuns({ section: foreach, fields, values: { company: "A" }, files, submissionId: "s1" });
    const next = prepareSectionRuns({ section: foreach, fields, values: { company: "A" }, files, submissionId: "s2" });
    expect(again.runs[0].idempotencyKey).toBe(first.runs[0].idempotencyKey);
    expect(next.runs[0].idempotencyKey).not.toBe(first.runs[0].idempotencyKey);
  });

  it("splits a described list into one-item lists", () => {
    const { runs } = prepareSectionRuns({
      section: { id: "tags", submission: { mode: "foreach", target_input: "tags" } },
      fields,
      values: { company: "A", tags: ["x", "", "y"] },
      files: { receipt: [file("r.jpg")] },
      submissionId: "s1",
    });
    expect(runs.map((run) => run.inputs.tags)).toEqual([["x"], ["y"]]);
  });

  it("sends bulk as one run", () => {
    const { runs } = prepareSectionRuns({
      section: { id: "bulk", submission: { mode: "bulk" } },
      fields,
      values: { company: "A" },
      files: { receipt: [file("r.jpg")] },
      submissionId: "s1",
    });
    expect(runs).toHaveLength(1);
  });

  it("refuses before sending anything when a required value is missing", () => {
    const { runs, problems } = prepareSectionRuns({
      section: foreach,
      fields,
      values: {},
      files: { receipt: [file("r.jpg")] },
      submissionId: "s1",
    });
    expect(runs).toEqual([]);
    expect(problems).toEqual([{ code: "required", field: "company" }]);
  });
});

describe("runWithConcurrency", () => {
  it("keeps at most three requests in flight and goes on after a failure", async () => {
    let inFlight = 0;
    let peak = 0;
    const done: number[] = [];
    const tasks = Array.from({ length: 8 }, (_, index) => async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      if (index === 2) throw new Error("boom");
      done.push(index);
    });
    await runWithConcurrency(tasks, 3);
    expect(peak).toBeLessThanOrEqual(3);
    expect(done).toHaveLength(7);
  });

  it("stops starting new requests once cancelled", async () => {
    let started = 0;
    let cancelled = false;
    const tasks = Array.from({ length: 6 }, () => async () => {
      started += 1;
      cancelled = true;
    });
    await runWithConcurrency(tasks, 1, () => cancelled);
    expect(started).toBe(1);
  });
});
