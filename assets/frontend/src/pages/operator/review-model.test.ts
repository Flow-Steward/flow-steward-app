import { HttpError } from "@/lib/http/errors";

import { rowContext, listContext, operatorJobPath, sectionListPath, isTypingTarget } from "./queue";
import { AWAITING_REVIEW_STATUS, jobStatusRequest, normalizeStatusList } from "@/pages/jobs/list/job-filters";

import { reviewEdits, reviewFieldError, reviewFormField, reviewPageDecides, type ReviewField } from "./review-model";

describe("review fields", () => {
  it("map each type to its default widget", () => {
    const widget = (field: ReviewField) => {
      const form = reviewFormField(field);
      return form ? `${form.kind}:${form.widget}` : null;
    };
    expect(widget({ name: "a", type: "string" })).toBe("string:text");
    expect(widget({ name: "a", type: "string", format: "date" })).toBe("string:date");
    expect(widget({ name: "a", type: "string", format: "date-time" })).toBe("string:date_time");
    expect(widget({ name: "a" })).toBe("string:textarea");
    expect(widget({ name: "a", type: "number" })).toBe("number:number");
    expect(widget({ name: "a", type: "boolean" })).toBe("boolean:true_false");
    expect(widget({ name: "a", type: "select", options: ["x"] })).toBe("select:select");
    expect(widget({ name: "a", type: "array", items: { type: "select", options: ["x", "y"] } })).toBe("select_list:multiselect");
    expect(widget({ name: "a", type: "array", items: { type: "object", properties: [{ name: "qty", type: "number" }] } })).toBe("object_list:repeater");
    expect(widget({ name: "a", type: "object", properties: { city: { type: "string" } } })).toBe("object:group");
    expect(widget({ name: "a", type: "file" })).toBeNull();
    expect(widget({ name: "a", type: "array", items: { type: "file" } })).toBeNull();
  });

  it("send only edited editable fields, numbers typed in the locale as numbers", () => {
    const fields: ReviewField[] = [
      { name: "total", type: "number", editable: true },
      { name: "supplier", type: "string", editable: true },
      { name: "confidence", type: "number", editable: false },
      { name: "lines", type: "array", editable: true },
    ];
    const initial = { total: 31.85, supplier: "ACME", confidence: 0.6, lines: [{ qty: 2, item: "Paper" }] };
    expect(reviewEdits(fields, initial, { ...initial })).toEqual({});
    expect(reviewEdits(fields, initial, { ...initial, lines: [{ item: "Paper", qty: 2 }] })).toEqual({});
    expect(reviewEdits(fields, initial, { ...initial, total: "318,50", confidence: 1 })).toEqual({ total: 318.5 });
    expect(reviewEdits(fields, initial, { ...initial, total: "" })).toEqual({ total: null });
    expect(reviewEdits(fields, initial, { ...initial, total: "318,5О" })).toEqual({ total: "318,5О" });
  });

  it("read the field a 422 names", () => {
    const refused = new HttpError({ status: 422, url: "/api/approvals/t", payload: { code: "approval_review_field_invalid", field: "total", message: "Not a number" } });
    expect(reviewFieldError(refused)).toEqual({ field: "total", message: "Not a number" });
    expect(reviewFieldError(new HttpError({ status: 409, url: "/x", payload: { code: "approval_already_resolved" } }))).toBeNull();
  });
});

describe("queue addresses", () => {
  it("keep only the list's context and its page", () => {
    const params = new URLSearchParams("f.state=awaiting_review&search=acme&tab=x&page=9");
    expect(listContext(params).toString()).toBe("f.state=awaiting_review&search=acme&page=9");
    expect(rowContext(params, 3, 50).toString()).toBe("f.state=awaiting_review&search=acme&page=3&per_page=50");
    expect(operatorJobPath("p 1", "wf", "j1", rowContext(params, 1, 25))).toBe("/projects/p%201/workflows/wf/work/jobs/j1?f.state=awaiting_review&search=acme");
    expect(sectionListPath("p1", "wf", new URLSearchParams())).toBe("/projects/p1/workflows/wf/work");
  });

  it("know when someone is typing", () => {
    const text = document.createElement("input");
    const box = document.createElement("input");
    box.type = "checkbox";
    expect(isTypingTarget(text)).toBe(true);
    expect(isTypingTarget(document.createElement("textarea"))).toBe(true);
    expect(isTypingTarget(box)).toBe(false);
    expect(isTypingTarget(document.body)).toBe(false);
  });
});

describe("review decisions and the built-in queue status", () => {
  it("keep the review page to approve and reject", () => {
    expect(reviewPageDecides({ job_id: "c", allowed_actions: ["approve", "reject"] })).toBe(true);
    expect(reviewPageDecides({ job_id: "c", allowed_actions: ["Approve"] })).toBe(true);
    expect(reviewPageDecides({ job_id: "c", allowed_actions: ["approve", "override"] })).toBe(false);
    expect(reviewPageDecides({ job_id: "c", allowed_actions: ["abort"] })).toBe(false);
  });

  it("keep awaiting review in the status filter and send it with the group statuses", () => {
    expect(normalizeStatusList(["failed", AWAITING_REVIEW_STATUS])).toBe("awaiting_review,failed");
    expect(normalizeStatusList(["bogus"])).toBe("");
    expect(jobStatusRequest("awaiting_review,failed")).toEqual(["awaiting_review", "failed", "max_iterations", "triage_needed"]);
    expect(jobStatusRequest("completed")).toEqual(["completed"]);
  });
});

