import { breadcrumbsForPath } from "@/components/layout/app-shell-breadcrumbs";
import { fallbackContext } from "@/lib/i18n/context";

const labels = new Map([
  ["/projects", "Projects"],
  ["/jobs", "Jobs"],
  ["/registry/workflows", "Workflows"],
  ["/settings", "Settings"],
]);

describe("breadcrumbsForPath", () => {
  it.each([["projects", "shell.nav.projects", "Projektai"], ["builder", "react.workflows.builder_title", "Kūrimo priemonė"]])("localizes the workflow %s child without relabeling author IDs", (segment, key, expected) => {
    const t: typeof fallbackContext.t = (candidate, text, params) => candidate === key ? expected : fallbackContext.t(candidate, text, params);
    expect(breadcrumbsForPath(`/registry/workflows/Owner%20Projects/${segment}`, labels, t)).toEqual([{ label: "Workflows", to: "/registry/workflows" }, { label: expected }]);
    expect(breadcrumbsForPath("/registry/workflows/Owner%20Projects", labels, t).at(-1)).toEqual({ label: "Owner Projects" });
    expect(breadcrumbsForPath(`/registry/workflows/${segment}`, labels, t).at(-1)).toEqual({ label: segment === "projects" ? "Projects" : "Builder" });
    expect(breadcrumbsForPath(`/custom/${segment}`, labels, t).at(-1)).toEqual({ label: segment === "projects" ? "Projects" : "Builder" });
  });
  it("names an extension's workflow by its workflow id, not by the install uuid around it", () => {
    expect(breadcrumbsForPath("/registry/workflows/extension.a074bd88-61cd-4d9f-937e-2d14a11097b4.demo_dataset_open_orders", labels)).toEqual([
      { label: "Workflows", to: "/registry/workflows" }, { label: "Demo dataset open orders" },
    ]);
    expect(breadcrumbsForPath("/registry/workflows/extension.a074bd88-61cd-4d9f-937e-2d14a11097b4.demo_dataset_open_orders/builder", labels).at(-1)).toEqual({ label: "Builder" });
    expect(breadcrumbsForPath("/extensions/acme.reports", new Map([["/extensions", "Extensions"]])).at(-1)).toEqual({ label: "Acme.reports" });
  });
  it("localizes only the exact workflow import breadcrumb", () => {
    const t: typeof fallbackContext.t = (key, text, params) => key === "react.workflows.import_breadcrumb" ? "Importas" : fallbackContext.t(key, text, params);
    expect(breadcrumbsForPath("/registry/workflows/import", labels, t)).toEqual([
      { label: "Workflows", to: "/registry/workflows" }, { label: "Importas" },
    ]);
    expect(breadcrumbsForPath("/registry/workflows/import", labels).at(-1)).toEqual({ label: "Import" });
    expect(breadcrumbsForPath("/custom/import", labels, t)).toEqual([{ label: "Import" }]);
  });
  it("uses the registered label for an exact route", () => {
    expect(breadcrumbsForPath("/registry/workflows", labels)).toEqual([
      { label: "Workflows" },
    ]);
  });

  it("uses a registered parent and a safe detail label for entity routes", () => {
    expect(
      breadcrumbsForPath(
        "/projects/ce42aa88-9a3f-46fd-be1f-720b5e12a7d8",
        labels,
      ),
    ).toEqual([
      { label: "Projects", to: "/projects" },
      { label: "Project details" },
    ]);
  });

  it("names no project sub-screen: every project section is a tab of the project page", () => {
    // `/projects/:id/edit`, `/history`, `/scheduled-jobs` and `/queue` were
    // removed, so their segments carry no special label any more.
    expect(breadcrumbsForPath("/registry/workflows/wf-1/edit", labels)[1]).toEqual({
      label: "Edit",
    });
  });

  it("humanizes an unknown route without displaying opaque identifiers", () => {
    expect(breadcrumbsForPath("/custom-audit", labels)).toEqual([
      { label: "Custom audit" },
    ]);
    expect(breadcrumbsForPath("/jobs/12345", labels)[1]).toEqual({
      label: "Job details",
    });
  });

  it("falls back safely for a malformed percent-encoded route", () => {
    expect(breadcrumbsForPath("/reports/%E0%A4%A", labels)).toEqual([
      { label: "Current page" },
    ]);
  });

  it.each([
    ["/settings/providers", "Model Providers"],
  ])("uses the exact registered route label for %s", (path, currentLabel) => {
    const breadcrumbs = breadcrumbsForPath(path, labels);

    expect(breadcrumbs.at(-1)).toEqual({ label: currentLabel });
  });
});
