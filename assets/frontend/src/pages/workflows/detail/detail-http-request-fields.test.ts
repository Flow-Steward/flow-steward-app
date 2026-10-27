import { parseHttpValue, parseHttpStatuses, httpIssueField, httpReadinessDisplay } from "./detail-http-request-fields";

it.each(["false", "001", "null", '{"a":1}', '"quoted"', "", "  "])("preserves exact text %s", (text) => {
  expect(parseHttpValue("text", text)).toEqual({ ok: true, value: text });
});
it.each([
  ["false", false], ["0", 0], ["null", null], ['"001"', "001"],
  ['{"source":"state.inputs.body"}', { source: "state.inputs.body" }], ['[false,0,null]', [false,0,null]],
])("retains explicit JSON type %s", (text, value) => {
  expect(parseHttpValue("json", String(text))).toEqual({ ok: true, value });
});
it.each(["{", "1e309", '{"n":1e309}'])("retains invalid/unrepresentable JSON as an error %s", (text) => {
  expect(parseHttpValue("json", text).ok).toBe(false);
});
it("separates integer CSV representation from HTTP readiness", () => {
  expect(parseHttpStatuses("")).toEqual({ ok: true, value: [] });
  expect(parseHttpStatuses("201, 201,600")).toEqual({ ok: true, value: [201,201,600] });
  for (const text of ["201,", "201foo", "201.5", "1e2", "9007199254740993", "999999999999999999999999999999999"]) {
    expect(parseHttpStatuses(text).ok).toBe(false);
  }
  expect(httpReadinessDisplay({ method: " pOsT ", url: "", response_target: "", expected_statuses: [], auth_mode: "bearer", auth_ref: "" })).toMatchObject({ url: expect.any(String), response_target: expect.any(String), expected_statuses: expect.any(String), auth_ref: expect.any(String) });
});
it("matches captured whole step prefix without granting foreign issues", () => {
  expect(httpIssueField("workflow.steps.a.b.override.http_request.headers.x.source", "a.b")).toBe("headers");
  expect(httpIssueField("workflow.steps.a.b2.override.http_request.url", "a.b")).toBeUndefined();
  expect(httpIssueField("operations[1].patch.override.http_request.url", "a.b")).toBeUndefined();
  expect(httpIssueField("operations[0].patch.override.http_request.unknown", "a.b")).toBeUndefined();
});
