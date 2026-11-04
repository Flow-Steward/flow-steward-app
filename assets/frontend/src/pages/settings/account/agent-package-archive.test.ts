import { archiveEntries, buildArchive, packageFiles } from "@/pages/settings/account/agent-package-archive";
import { blobBytes, readStoredZip } from "@/lib/utils/zip.test-helpers";

const OPERATOR = "flow-steward-operator";
const SERVERS = JSON.stringify({
  "flow-steward": { type: "http", url: "https://fs.example.com/mcp", headers: { Authorization: "Bearer fs_pat_personal" } }, // pragma: allowlist secret
});

describe("personal agent archives", () => {
  it("bundles the whole Operator package, manifests and skills", () => {
    const files = Object.keys(packageFiles(OPERATOR));
    expect(files).toEqual(expect.arrayContaining([
      ".claude-plugin/plugin.json",
      ".cursor-plugin/plugin.json",
      "plugin.json",
      "README.md",
      "skills/flow-steward-workflow-authoring/SKILL.md",
      "skills/flow-steward-report-issue/SKILL.md",
    ]));
    expect(packageFiles("flow-steward-unknown")).toEqual({});
  });

  it("a Claude marketplace whose plugin already holds the address and the token", async () => {
    const files = readStoredZip(await blobBytes(buildArchive("claude_marketplace", OPERATOR, SERVERS)));
    const marketplace = JSON.parse(files.get("flow-steward/.claude-plugin/marketplace.json") || "{}");
    expect(marketplace.name).toBe("flow-steward");
    expect(marketplace.plugins.map((plugin: { name: string; source: string }) => [plugin.name, plugin.source])).toEqual([[OPERATOR, `./${OPERATOR}`]]);
    const manifest = JSON.parse(files.get(`flow-steward/${OPERATOR}/.claude-plugin/plugin.json`) || "{}");
    expect(manifest.name).toBe(OPERATOR);
    expect(manifest.userConfig).toBeUndefined();
    expect(manifest.mcpServers).toEqual(JSON.parse(SERVERS));
    expect(files.get(`flow-steward/${OPERATOR}/skills/flow-steward-workflow-authoring/SKILL.md`)).toContain("name: flow-steward-workflow-authoring");
    expect(files.size).toBe(Object.keys(packageFiles(OPERATOR)).length + 1);
  });

  it("refuses a Claude plugin without its server entry", () => {
    expect(() => archiveEntries("claude_marketplace", OPERATOR, "")).toThrow(/server entry/);
  });

  it("a plugin folder is the package as it is, without any token", () => {
    const entries = archiveEntries("plugin_folder", OPERATOR);
    expect(entries.every((entry) => entry.path.startsWith(`${OPERATOR}/`))).toBe(true);
    expect(entries.find((entry) => entry.path === `${OPERATOR}/.cursor-plugin/plugin.json`)).toBeTruthy();
    expect(entries.some((entry) => String(entry.content).includes("Bearer fs_pat"))).toBe(false);
  });

  it("a skills folder holds one folder per skill at the archive root", () => {
    const roots = new Set(archiveEntries("skills_folder", OPERATOR).map((entry) => entry.path.split("/")[0]));
    expect([...roots].sort((left, right) => left.localeCompare(right))).toEqual([
      "flow-steward-migrate-workflow",
      "flow-steward-publish-workflow",
      "flow-steward-report-issue",
      "flow-steward-workflow-authoring",
    ]);
  });
});
