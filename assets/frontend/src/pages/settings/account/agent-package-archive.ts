/**
 * The personal archives of the AI agents page, built in the browser.
 *
 * The package files come with the web UI: `infra/tooling/agent-skills/sync_packages.py`
 * renders them from `docs/agent-skills` into the JSON below, and this module is
 * loaded only when someone downloads. The archive, and the token a plugin carries,
 * never travel to the server. The server's catalog says which layout a step wants
 * and names every client-specific folder; this module only arranges the files.
 *
 * Every layout but the skills folder puts several entries at the archive root, so
 * unpacking it on any system makes one folder named after the archive.
 */
import { createZip, type ZipEntry } from "@/lib/utils/zip";

import bundle from "@/pages/settings/account/agent-skill-packages.generated.json";

export type ArchiveLayout = "claude_marketplace" | "agents_marketplace" | "plugin_folder" | "skills_folder";

export type ArchiveOptions = Readonly<{
  /** The `mcp.json` text with the address and the token filled in; a plugin without it carries no server. */
  servers?: string;
  /** `agents_marketplace`: the folder of the client's plugin manifest. */
  manifestDir?: string;
}>;

type JsonObject = Record<string, unknown>;

const packages: Record<string, Record<string, string>> = bundle.packages;

/** `{relative path: text}` of one bundled package. */
export function packageFiles(packageId: string): Record<string, string> {
  return Object.hasOwn(packages, packageId) ? packages[packageId] : {};
}

function json(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function serverEntries(servers: string): JsonObject {
  const parsed = JSON.parse(servers) as { mcpServers?: JsonObject };
  if (!parsed.mcpServers || typeof parsed.mcpServers !== "object") throw new Error("The server entry has no mcpServers.");
  return parsed.mcpServers;
}

/** A `.claude-plugin` marketplace named after the package, listing it alone. */
function claudeMarketplace(packageId: string): string {
  const manifest = JSON.parse(bundle.marketplace) as JsonObject & { plugins?: Array<{ name?: string }> };
  manifest.name = packageId;
  manifest.plugins = (manifest.plugins || []).filter((plugin) => plugin.name === packageId);
  return json(manifest);
}

/** The Claude plugin manifest: no options to ask for, and the server when there is one. */
function claudeManifest(source: string, servers: string): string {
  const manifest = JSON.parse(source) as JsonObject;
  delete manifest.userConfig;
  if (servers) manifest.mcpServers = serverEntries(servers);
  return json(manifest);
}

function openaiInterface(manifest: JsonObject): JsonObject {
  const extensions = (manifest.extensions || {}) as Record<string, { interface?: JsonObject }>;
  return extensions["com.openai"]?.interface || {};
}

/** A `.agents/plugins` marketplace named after the package, listing it alone. */
function agentsMarketplace(packageId: string, manifest: JsonObject): string {
  const ui = openaiInterface(manifest);
  return json({
    name: packageId,
    interface: { displayName: ui.displayName || packageId },
    plugins: [
      {
        name: packageId,
        source: { source: "local", path: `./plugins/${packageId}` },
        policy: { installation: "AVAILABLE", authentication: "ON_INSTALL" },
        category: ui.category || "Productivity",
      },
    ],
  });
}

/** The client's plugin manifest from the package's Agent Plugins `plugin.json`. */
function agentsManifest(manifest: JsonObject, servers: string): string {
  const fields = ["name", "version", "description", "author", "homepage", "repository", "license", "keywords"];
  const out: JsonObject = Object.fromEntries(fields.filter((field) => field in manifest).map((field) => [field, manifest[field]]));
  out.skills = "./skills/";
  if (servers) out.mcpServers = "./.mcp.json";
  out.interface = openaiInterface(manifest);
  return json(out);
}

/** The entries of one archive. */
export function archiveEntries(layout: ArchiveLayout, packageId: string, options: ArchiveOptions = {}): ZipEntry[] {
  const files = packageFiles(packageId);
  if (!Object.keys(files).length) throw new Error(`The package ${packageId} is not bundled.`);
  const servers = (options.servers || "").trim();
  if (servers) serverEntries(servers);
  const entries = Object.entries(files);
  if (layout === "skills_folder") {
    return entries.filter(([path]) => path.startsWith("skills/")).map(([path, content]) => ({ path: path.slice("skills/".length), content }));
  }
  if (layout === "plugin_folder") {
    return [...entries.map(([path, content]) => ({ path, content })), ...(servers ? [{ path: "mcp.json", content: `${servers}\n` }] : [])];
  }
  if (layout === "agents_marketplace") {
    const manifestDir = (options.manifestDir || "").trim();
    if (!/^\.[a-z0-9-]+$/.test(manifestDir)) throw new Error("The plugin needs its manifest folder.");
    const root = `plugins/${packageId}`;
    const manifest = JSON.parse(files["plugin.json"] || "{}") as JsonObject;
    return [
      { path: ".agents/plugins/marketplace.json", content: agentsMarketplace(packageId, manifest) },
      // The client's manifest replaces the Agent Plugins one: a client that finds
      // `plugin.json` at the plugin root reads it first and never sees the server.
      ...entries.filter(([path]) => path !== "plugin.json").map(([path, content]) => ({ path: `${root}/${path}`, content })),
      { path: `${root}/${manifestDir}/plugin.json`, content: agentsManifest(manifest, servers) },
      ...(servers ? [{ path: `${root}/.mcp.json`, content: `${servers}\n` }] : []),
    ];
  }
  return [
    { path: ".claude-plugin/marketplace.json", content: claudeMarketplace(packageId) },
    ...entries.map(([path, content]) => ({
      path: `${packageId}/${path}`,
      content: path === ".claude-plugin/plugin.json" ? claudeManifest(content, servers) : content,
    })),
  ];
}

export function buildArchive(layout: ArchiveLayout, packageId: string, options: ArchiveOptions = {}): Blob {
  const bytes = createZip(archiveEntries(layout, packageId, options));
  return new Blob([bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer], { type: "application/zip" });
}
