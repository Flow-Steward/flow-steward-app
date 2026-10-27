import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

async function listFilesRecursive(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFilesRecursive(fullPath)));
      continue;
    }
    files.push(fullPath);
  }
  return files;
}

const TEMPLATE_PREVIEW_FILE = path.join("workflows", "detail", "detail-template-preview.tsx");

describe("retired surface cleanup guard", () => {
  it("does not keep retired bridge markers in active React routes", async () => {
    const roots = [
      "src/pages",
      "src/app",
      "src/components/layout",
      "src/components/shared",
      "../platform-renderer.js",
    ];
    const files = (
      await Promise.all(
        roots.map(async (root) => {
          const resolved = path.resolve(process.cwd(), root);
          return root.endsWith(".js") ? [resolved] : listFilesRecursive(resolved);
        }),
      )
    )
      .flat()
      .filter(
        (filePath) =>
          filePath.endsWith(".tsx") || filePath.endsWith(".ts") || filePath.endsWith(".js"),
      );
    files.push(path.resolve(process.cwd(), "index.html"));
    files.push(path.resolve(process.cwd(), "src/main.tsx"));

    for (const filePath of files) {
      const content = await readFile(filePath, "utf-8");
      expect(content).not.toContain("Open legacy");
      expect(content).not.toContain("Open retired");
      expect(content).not.toContain("legacy-surface");
      expect(content).not.toContain("LegacySurface");
      expect(content).not.toContain("retired-surface");
      expect(content).not.toContain("Legacy dashboard");
      expect(content).not.toContain("Retired dashboard");
      expect(content).not.toContain("Legacy projects");
      expect(content).not.toContain("Retired projects");
      expect(content).not.toContain("Legacy jobs");
      expect(content).not.toContain("Retired jobs");
      // One sandboxed frame shows a Template step's HTML/Markdown result in Test Run.
      // It is the only frame allowed: it loads only the server's preview page, which
      // runs its own pinned script and nothing else, and it never gets this origin.
      if (filePath.endsWith(TEMPLATE_PREVIEW_FILE)) {
        expect(content.match(/<iframe/g)).toHaveLength(1);
        // Its sandbox is the page's own response header (tests/unit/test_frontend_shell_routes.py).
        expect(content).toContain('"/app/template-preview-frame"');
        expect(content).not.toMatch(/allow-(same-origin|forms|popups|top-navigation|modals)/);
        expect(content).not.toContain("srcDoc");
        expect(content).not.toContain("dangerouslySetInnerHTML");
      } else {
        expect(content).not.toContain("<iframe");
      }
      const platformKitAllowed =
        filePath.endsWith("platform-renderer.js") || filePath.endsWith("src/main.tsx");
      if (!platformKitAllowed) {
        expect(content).not.toContain("platform-kit");
      }
      expect(content).not.toContain("htmx");
      expect(content).not.toContain("React + FastAPI foundation");
      expect(content).not.toContain("Platform-grid");
      expect(content).not.toContain("normalized summary contract");
      expect(content).not.toContain("migrated surfaces");
      expect(content).not.toContain("FastAPI cookie session");
      expect(content).not.toContain("configuration workspace");
      expect(content).not.toContain("contract drawers");
      expect(content).not.toContain("Selection-aware node contract");
      expect(content).not.toContain("retired builder layout");
      expect(content).not.toContain("Contract validation payload");
      expect(content).not.toContain("FastAPI reset flow");
      expect(content).not.toContain("platform-grid filters");
      expect(content).not.toContain("System monitoring workspace");
      expect(content).not.toContain("Workspace snapshot");
      expect(content).not.toContain("Quick parity summary");
      expect(content).not.toContain("Project workspace with");
      expect(content).not.toContain("Agent workspace with");
      expect(content).not.toContain("Tool workspace with");
      expect(content).not.toContain("Skill workspace with");
      expect(content).not.toContain("Guardrail workspace with");
      expect(content).not.toContain("Prompt workspace");
      expect(content).not.toContain("FastAPI shell mount");
      expect(content).not.toContain("Conversation workspace");
      expect(content).not.toContain("Contextual assistant surface");
      expect(content).not.toContain("React shell");
      expect(content).not.toContain("page-group route owns");
      expect(content).not.toContain("route-contract-preload");
      expect(content).not.toContain("/^\\/events");
    }
  });
});
