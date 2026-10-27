import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const REGISTRY_DETAIL = [
  "agents", "mcp", "tools", "skills", "guardrails",
  "datasets", "workspaces", "data-contracts", "fields", "published-blocks",
].map((name) => `src/pages/registry/${name}/detail/index.tsx`);

describe("registry detail pages", () => {
  for (const path of REGISTRY_DETAIL) {
    it(`${path} declares the standard mode and keeps no legacy ceiling`, () => {
      const body = readFileSync(path, "utf8");
      expect(body).toMatch(/<PageContainer[^>]*width="standard"/);
      expect(/(?<![\w-])fs-settings(?![\w-])/.test(body)).toBe(false);
      expect(/(?<![\w-])fs-settings-detail(?![\w-])/.test(body)).toBe(false);
    });
  }
});

// History is a tab panel of the project page, not a page of its own.
const PROJECT_DETAIL_TABS = ["detail"].map((name) => `src/pages/projects/${name}/index.tsx`);

describe("project page", () => {
  for (const path of PROJECT_DETAIL_TABS) {
    it(`${path} declares the standard mode and keeps no legacy ceiling`, () => {
      const body = readFileSync(path, "utf8");
      expect(body).toMatch(/<PageContainer[^>]*width="standard"/);
      expect(/(?<![\w-])fs-settings(?![\w-])/.test(body)).toBe(false);
      expect(/(?<![\w-])fs-settings-detail(?![\w-])/.test(body)).toBe(false);
    });
  }
});

// Canon page-layout.md: the 880px measure belongs to the form block, not the
// page. The project page's header, tabs and panel keep the project's 1280px
// frame; only its field blocks carry the measure.
describe("project page", () => {
  it("keeps the form measure on its field blocks, not on the page", () => {
    const page = readFileSync("src/pages/projects/detail/index.tsx", "utf8");
    const form = readFileSync("src/pages/projects/detail/project-edit-form.tsx", "utf8");
    expect(page).not.toMatch(/(?<![\w-])fs-form-block(?![\w-])/);
    expect(form).toMatch(/(?<![\w-])fs-form-block(?![\w-])/);
  });
});

const JOBS_AND_WORKFLOWS_PAGES = [
  "jobs/detail",
  "workflows/overview",
  "workflows/projects",
  "workflows/import",
].map((name) => `src/pages/${name}/index.tsx`);

describe("jobs and workflows pages", () => {
  for (const path of JOBS_AND_WORKFLOWS_PAGES) {
    it(`${path} declares the standard mode and keeps no legacy ceiling`, () => {
      const body = readFileSync(path, "utf8");
      expect(body).toMatch(/<PageContainer[^>]*width="standard"/);
      expect(/(?<![\w-])fs-settings(?![\w-])/.test(body)).toBe(false);
      expect(/(?<![\w-])fs-settings-detail(?![\w-])/.test(body)).toBe(false);
    });
  }

  // A page whose early-return states sit above the container would still
  // satisfy "<PageContainer appears somewhere in the file", so the contract
  // pins the shape too: workflows/overview must funnel all three
  // pageBody branches (access denied, load error, main tree) through
  // exactly one PageContainer, not wrap only the happy path.
  it("workflows/overview declares exactly one PageContainer and no pageBody branch sits outside it", () => {
    const body = readFileSync("src/pages/workflows/overview/index.tsx", "utf8");
    const pageContainerOpens = body.match(/<PageContainer[^>]*>/g) || [];
    expect(pageContainerOpens).toHaveLength(1);
    // WorkflowsOverviewPage itself must return only via `pageBody`, never a
    // literal JSX return of its own (that would be a return outside the
    // container reintroduced above it).
    const pageComponentBody = body.slice(body.indexOf("export function WorkflowsOverviewPage"));
    const bareReturns = pageComponentBody.match(/return <Workflow(AccessDenied|LoadError)/g) || [];
    expect(bareReturns).toHaveLength(0);
  });
});
