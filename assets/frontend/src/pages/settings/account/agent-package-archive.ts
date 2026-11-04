/**
 * The personal archives of the AI agents page, built in the browser.
 *
 * The package files come with the web UI: `infra/tooling/agent-skills/sync_packages.py`
 * renders them from `docs/agent-skills` into the JSON below, and this module is
 * loaded only when someone downloads. The archive, and the token a Claude plugin
 * carries, never travel to the server. The server's catalog says which layout a
 * step wants; this module only arranges the files.
 */
import { createZip, type ZipEntry } from "@/lib/utils/zip";

import bundle from "@/pages/settings/account/agent-skill-packages.generated.json";

export type ArchiveLayout = "claude_marketplace" | "plugin_folder" | "skills_folder";

/** The marketplace folder name and the marketplace name in its manifest. */
const MARKETPLACE = "flow-steward";

const packages: Record<string, Record<string, string>> = bundle.packages;

/** `{relative path: text}` of one bundled package. */
export function packageFiles(packageId: string): Record<string, string> {
  return Object.hasOwn(packages, packageId) ? packages[packageId] : {};
}

function marketplaceManifest(packageId: string): string {
  const manifest = JSON.parse(bundle.marketplace) as { plugins?: Array<{ name?: string }> };
  manifest.plugins = (manifest.plugins || []).filter((plugin) => plugin.name === packageId);
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

/** The Claude plugin manifest with a fixed server entry instead of the options it asks for. */
function personalClaudeManifest(source: string, servers: string): string {
  const manifest = JSON.parse(source) as Record<string, unknown>;
  delete manifest.userConfig;
  manifest.mcpServers = JSON.parse(servers);
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

/**
 * The entries of one archive. `servers` is the `mcpServers` JSON with the address
 * and the token filled in; only the Claude marketplace layout uses it.
 */
export function archiveEntries(layout: ArchiveLayout, packageId: string, servers = ""): ZipEntry[] {
  const files = packageFiles(packageId);
  if (!Object.keys(files).length) throw new Error(`The package ${packageId} is not bundled.`);
  const entries = Object.entries(files);
  if (layout === "skills_folder") {
    return entries.filter(([path]) => path.startsWith("skills/")).map(([path, content]) => ({ path: path.slice("skills/".length), content }));
  }
  if (layout === "plugin_folder") {
    return entries.map(([path, content]) => ({ path: `${packageId}/${path}`, content }));
  }
  if (!servers.trim()) throw new Error("The plugin needs its server entry.");
  return [
    { path: `${MARKETPLACE}/.claude-plugin/marketplace.json`, content: marketplaceManifest(packageId) },
    ...entries.map(([path, content]) => ({
      path: `${MARKETPLACE}/${packageId}/${path}`,
      content: path === ".claude-plugin/plugin.json" ? personalClaudeManifest(content, servers) : content,
    })),
  ];
}

export function buildArchive(layout: ArchiveLayout, packageId: string, servers = ""): Blob {
  const bytes = createZip(archiveEntries(layout, packageId, servers));
  return new Blob([bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer], { type: "application/zip" });
}
