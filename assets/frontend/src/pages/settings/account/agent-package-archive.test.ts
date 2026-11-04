import { archiveEntries, buildArchive, packageFiles } from "@/pages/settings/account/agent-package-archive";
import { blobBytes, readStoredZip } from "@/lib/utils/zip.test-helpers";

const OPERATOR = "flow-steward-operator";
const DEVELOPER = "flow-steward-developer";
const SERVER = { type: "http", url: "https://fs.example.com/mcp", headers: { Authorization: "Bearer fs_pat_personal" } }; // pragma: allowlist secret
const SERVERS = JSON.stringify({ mcpServers: { "flow-steward": SERVER } });
const MANIFEST_DIR = ".example-plugin";

function roots(paths: string[]): string[] {
  return [...new Set(paths.map((path) => path.split("/")[0]))].sort((left, right) => left.localeCompare(right));
}

function paths(entries: ReturnType<typeof archiveEntries>): string[] {
  return entries.map((entry) => entry.path);
}

describe("personal agent archives", () => {
  it("bundles both packages, manifests and skills", () => {
    expect(Object.keys(packageFiles(OPERATOR))).toEqual(expect.arrayContaining([
      ".claude-plugin/plugin.json",
      ".cursor-plugin/plugin.json",
      "plugin.json",
      "README.md",
      "skills/flow-steward-workflow-authoring/SKILL.md",
      "skills/flow-steward-report-issue/SKILL.md",
    ]));
    expect(Object.keys(packageFiles(DEVELOPER))).toEqual(expect.arrayContaining([
      ".claude-plugin/plugin.json",
      "plugin.json",
      "skills/flow-steward-extension-authoring/SKILL.md",
    ]));
    expect(packageFiles("flow-steward-unknown")).toEqual({});
  });

  it("a Claude marketplace named after the package whose plugin already holds the address and the token", async () => {
    const files = readStoredZip(await blobBytes(buildArchive("claude_marketplace", OPERATOR, { servers: SERVERS })));
    // Several entries at the root: unpacking makes one folder named after the archive.
    expect(roots([...files.keys()])).toEqual([".claude-plugin", OPERATOR]);
    const marketplace = JSON.parse(files.get(".claude-plugin/marketplace.json") || "{}");
    expect(marketplace.name).toBe(OPERATOR);
    expect(marketplace.plugins.map((plugin: { name: string; source: string }) => [plugin.name, plugin.source])).toEqual([[OPERATOR, `./${OPERATOR}`]]);
    const manifest = JSON.parse(files.get(`${OPERATOR}/.claude-plugin/plugin.json`) || "{}");
    expect(manifest.name).toBe(OPERATOR);
    expect(manifest.userConfig).toBeUndefined();
    expect(manifest.mcpServers).toEqual({ "flow-steward": SERVER });
    expect(files.get(`${OPERATOR}/skills/flow-steward-workflow-authoring/SKILL.md`)).toContain("name: flow-steward-workflow-authoring");
    expect(files.size).toBe(Object.keys(packageFiles(OPERATOR)).length + 1);
  });

  it("a Developer Claude plugin carries no server and no token", () => {
    const entries = archiveEntries("claude_marketplace", DEVELOPER);
    const manifest = JSON.parse(String(entries.find((entry) => entry.path === `${DEVELOPER}/.claude-plugin/plugin.json`)?.content));
    expect(manifest.mcpServers).toBeUndefined();
    expect(JSON.parse(String(entries[0].content)).name).toBe(DEVELOPER);
    expect(entries.some((entry) => String(entry.content).includes("Bearer"))).toBe(false);
  });

  it("an .agents marketplace whose plugin manifest points at the server file", () => {
    const entries = archiveEntries("agents_marketplace", OPERATOR, { servers: SERVERS, manifestDir: MANIFEST_DIR });
    const byPath = new Map(entries.map((entry) => [entry.path, String(entry.content)]));
    expect(roots(paths(entries))).toEqual([".agents", "plugins"]);
    const marketplace = JSON.parse(byPath.get(".agents/plugins/marketplace.json") || "{}");
    expect(marketplace.name).toBe(OPERATOR);
    expect(marketplace.plugins).toEqual([expect.objectContaining({ name: OPERATOR, source: { source: "local", path: `./plugins/${OPERATOR}` } })]);
    const manifest = JSON.parse(byPath.get(`plugins/${OPERATOR}/${MANIFEST_DIR}/plugin.json`) || "{}");
    expect(manifest).toEqual(expect.objectContaining({ name: OPERATOR, skills: "./skills/", mcpServers: "./.mcp.json" }));
    expect(manifest.interface.displayName).toBe("Flow Steward Operator");
    expect(JSON.parse(byPath.get(`plugins/${OPERATOR}/.mcp.json`) || "{}")).toEqual(JSON.parse(SERVERS));
    // A root plugin.json would win over the client's manifest and hide the server.
    expect(byPath.has(`plugins/${OPERATOR}/plugin.json`)).toBe(false);
    expect(byPath.get(`plugins/${OPERATOR}/skills/flow-steward-report-issue/SKILL.md`)).toBeTruthy();
  });

  it("an .agents marketplace without a server has no server file", () => {
    const entries = archiveEntries("agents_marketplace", DEVELOPER, { manifestDir: MANIFEST_DIR });
    const manifest = JSON.parse(String(entries.find((entry) => entry.path === `plugins/${DEVELOPER}/${MANIFEST_DIR}/plugin.json`)?.content));
    expect(manifest.mcpServers).toBeUndefined();
    expect(paths(entries)).not.toContain(`plugins/${DEVELOPER}/.mcp.json`);
  });

  it("refuses an .agents marketplace without a safe manifest folder, and a server entry that is not mcp.json", () => {
    expect(() => archiveEntries("agents_marketplace", OPERATOR, { servers: SERVERS })).toThrow(/manifest folder/);
    expect(() => archiveEntries("agents_marketplace", OPERATOR, { manifestDir: "../x" })).toThrow(/manifest folder/);
    expect(() => archiveEntries("claude_marketplace", OPERATOR, { servers: '{"flow-steward": {}}' })).toThrow(/mcpServers/);
  });

  it("a plugin folder is the package at the archive root, with mcp.json when it carries the server", () => {
    const entries = archiveEntries("plugin_folder", OPERATOR, { servers: SERVERS });
    expect(paths(entries)).toEqual(expect.arrayContaining([".cursor-plugin/plugin.json", "mcp.json", "skills/flow-steward-report-issue/SKILL.md"]));
    expect(JSON.parse(String(entries.find((entry) => entry.path === "mcp.json")?.content))).toEqual(JSON.parse(SERVERS));
    expect(paths(archiveEntries("plugin_folder", DEVELOPER))).not.toContain("mcp.json");
  });

  it("a skills folder holds one folder per skill at the archive root", () => {
    expect(roots(paths(archiveEntries("skills_folder", OPERATOR)))).toEqual([
      "flow-steward-migrate-workflow",
      "flow-steward-publish-workflow",
      "flow-steward-report-issue",
      "flow-steward-workflow-authoring",
    ]);
  });
});
