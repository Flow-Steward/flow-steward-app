import { savedHttpTestRunSummary } from "./detail-test-run";

it("shows a saved method and URL without changing the configuration", () => {
  const config = { method: "post", url: "https://example.test/orders" };
  expect(savedHttpTestRunSummary(config, "Uses saved configuration")).toEqual({
    method: "POST",
    url: "https://example.test/orders",
  });
  expect(config.method).toBe("post");
});

it("uses the saved GET default", () => {
  expect(savedHttpTestRunSummary({ url: "https://example.test/orders" }, "Uses saved configuration")).toEqual({
    method: "GET",
    url: "https://example.test/orders",
  });
});

it.each(["{{ state.target_url }}", "https://example.test/{{ state.id }}"])(
  "labels a templated destination as saved configuration: %s",
  (url) => {
    expect(savedHttpTestRunSummary({ method: "{{ state.verb }}", url }, "Uses saved configuration")).toEqual({
      method: "Uses saved configuration",
      url: "Uses saved configuration",
    });
  },
);
