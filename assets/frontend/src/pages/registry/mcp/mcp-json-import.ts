/* Reads the MCP server configuration people copy from a server's README or
   another client, so the New MCP form fills itself. Accepted shapes:
   - `{"mcpServers": {name: server}}` (Claude Desktop, Claude Code, Cursor, Windsurf)
   - `{"servers": {name: server}}` and `{"mcp": {"servers": …}}` (VS Code)
   - `{name: server}` or one bare server object, also without the outer braces
   - an MCP Registry `server.json` with `remotes`, or a registry list entry
   A server is `url`/`serverUrl`/`httpUrl` with optional `headers`, or a stdio
   `command` that runs `mcp-remote <url>`, which only bridges to a remote URL.
   Only remote streamable HTTP servers can be connected; the others come back
   in `skipped` with the reason, so the form can say why nothing was filled. */

import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type McpJsonServer = {
  name: string;
  url: string;
  headers: Record<string, string>;
  /** Headers whose value was a placeholder (`${input:token}`) the operator must enter. */
  headersToFill: string[];
  description: string;
  authMode: "none" | "bearer" | "api_key";
};

export type McpJsonSkipReason = "stdio" | "sse" | "no_url";

export type McpJsonImport = {
  servers: McpJsonServer[];
  skipped: { name: string; reason: McpJsonSkipReason }[];
};

export type McpJsonImportErrorCode = "invalid_json" | "no_servers";

export class McpJsonImportError extends Error {
  constructor(readonly code: McpJsonImportErrorCode) {
    super(code);
  }
}

type JsonObject = Record<string, unknown>;

const URL_KEYS = ["url", "serverUrl", "httpUrl"] as const;
const PLACEHOLDER = /\$\{([^}]+)\}/g;

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function removeTrailingCommas(text: string): string {
  let result = "";
  let inString = false;
  let escaped = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      result += char;
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
    } else if (char === '"') {
      inString = true;
      result += char;
    } else if (char === ",") {
      let next = index + 1;
      while (next < text.length && /\s/u.test(text[next])) next += 1;
      if (next < text.length && text[next] !== "}" && text[next] !== "]") result += char;
    } else {
      result += char;
    }
  }
  return result;
}

function parseLenient(text: string): unknown {
  const trimmed = text.trim();
  // Snippets in READMEs often carry a trailing comma or omit the outer braces
  // (`"github": {…}`); try the text as is, then with those repairs.
  const withoutTrailingCommas = removeTrailingCommas(trimmed);
  for (const candidate of [trimmed, withoutTrailingCommas, `{${withoutTrailingCommas}}`]) {
    try {
      return JSON.parse(candidate);
    } catch {
      // try the next repair
    }
  }
  throw new McpJsonImportError("invalid_json");
}

// Not a type guard on purpose: narrowing an object that is not a server to
// `never` would hide its `mcpServers`/`servers` containers from the checks below.
function looksLikeServer(value: unknown): boolean {
  return isObject(value)
    && (URL_KEYS.some((key) => key in value) || "command" in value || Array.isArray(value.remotes));
}

function serverEntries(root: unknown): [string, JsonObject][] {
  if (Array.isArray(root)) {
    // A registry list (`{"servers": [{"server": {…}}]}`) or a plain array.
    return root
      .map((item) => (isObject(item) && isObject(item.server) ? item.server : item))
      .filter((item): item is JsonObject => looksLikeServer(item))
      .map((item): [string, JsonObject] => ["", item]);
  }
  if (!isObject(root)) return [];
  if (looksLikeServer(root.server)) return serverEntries([root]);
  if (looksLikeServer(root)) return [["", root]];
  for (const container of [root.mcpServers, root.servers, isObject(root.mcp) ? root.mcp.servers : undefined]) {
    if (Array.isArray(container) || isObject(container)) return serverEntries(container);
  }
  return Object.entries(root).filter((entry): entry is [string, JsonObject] => looksLikeServer(entry[1]));
}

function stringRecord(value: unknown): Record<string, string> {
  if (!isObject(value)) return {};
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, stringFromUnknownValue(item)]),
  );
}

/** `${VAR}` and `${env:VAR}` resolve from the server's own `env`; anything else stays a placeholder. */
function substitute(value: string, env: Record<string, string>): string {
  return value.replaceAll(PLACEHOLDER, (match, name: string) => {
    const key = name.startsWith("env:") ? name.slice(4) : name;
    return env[key] ?? match;
  });
}

function headerLine(line: string): [string, string] | null {
  const colon = line.indexOf(":");
  if (colon <= 0) return null;
  return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()];
}

function mcpRemoteTarget(server: JsonObject): { url: string; headers: Record<string, string> } | null {
  const command = stringFromUnknownValue(server.command);
  const args = Array.isArray(server.args) ? server.args.map((arg) => stringFromUnknownValue(arg)) : [];
  const words = [command, ...args];
  if (!words.some((word) => /(^|\/|@)mcp-remote(@|$)/.test(word))) return null;
  const url = args.find((arg) => /^https?:\/\//i.test(arg));
  if (!url) return null;
  const headers: Record<string, string> = {};
  for (const [index, arg] of args.entries()) {
    if (arg !== "--header") continue;
    const parsed = headerLine(args[index + 1] ?? "");
    if (parsed) headers[parsed[0]] = parsed[1];
  }
  return { url, headers };
}

function registryRemote(server: JsonObject): { url: string; headers: Record<string, string>; sse: boolean } | null {
  const remotes = Array.isArray(server.remotes) ? server.remotes.filter(isObject) : [];
  const remote = remotes.find((item) => stringFromUnknownValue(item.type).toLowerCase() === "streamable-http");
  if (!remote) {
    return remotes.length ? { url: "", headers: {}, sse: true } : null;
  }
  const headers: Record<string, string> = {};
  for (const header of Array.isArray(remote.headers) ? remote.headers.filter(isObject) : []) {
    const name = stringFromUnknownValue(header.name).trim();
    if (name) headers[name] = stringFromUnknownValue(header.value) || "${input}";
  }
  return { url: stringFromUnknownValue(remote.url), headers, sse: false };
}

function registryName(server: JsonObject): string {
  const title = stringFromUnknownValue(server.title).trim();
  if (title) return title;
  // Registry names are reverse-DNS (`io.github.owner/server`); the last part reads best.
  const name = stringFromUnknownValue(server.name).trim();
  return name.split("/").pop() || name;
}

function isSseTransport(server: JsonObject, url: string): boolean {
  const transport = stringFromUnknownValue(server.type || server.transport).toLowerCase();
  if (transport) return transport === "sse";
  try {
    return new URL(url).pathname.replace(/\/+$/, "").endsWith("/sse");
  } catch {
    return false;
  }
}

function authModeFor(headers: Record<string, string>): McpJsonServer["authMode"] {
  const names = Object.keys(headers).map((name) => name.toLowerCase());
  if (names.includes("authorization")) return "bearer";
  return names.length ? "api_key" : "none";
}

function readServer(key: string, server: JsonObject): McpJsonServer | McpJsonSkipReason {
  const env = stringRecord(server.env);
  let url = "";
  let rawHeaders: Record<string, string> = {};
  const remote = registryRemote(server);
  if (remote?.sse) return "sse";
  if (remote) {
    url = remote.url;
    rawHeaders = remote.headers;
  } else if (URL_KEYS.some((name) => name in server)) {
    url = URL_KEYS.map((name) => stringFromUnknownValue(server[name])).find(Boolean) ?? "";
    rawHeaders = stringRecord(server.headers);
  } else {
    const target = mcpRemoteTarget(server);
    if (!target) return "stdio";
    url = target.url;
    rawHeaders = target.headers;
  }
  url = substitute(url.trim(), env);
  if (!url) return "no_url";
  if (!remote && isSseTransport(server, url)) return "sse";

  const headers: Record<string, string> = {};
  const headersToFill: string[] = [];
  for (const [name, rawValue] of Object.entries(rawHeaders)) {
    const value = substitute(rawValue, env).trim();
    // A value still holding a placeholder is not a credential: leave it empty
    // so the form asks for it instead of saving `${input:token}`.
    PLACEHOLDER.lastIndex = 0;
    const unresolved = !value || PLACEHOLDER.test(value);
    headers[name] = unresolved ? "" : value;
    if (unresolved) headersToFill.push(name);
  }
  return {
    name: key || registryName(server),
    url,
    headers,
    headersToFill,
    description: stringFromUnknownValue(server.description).trim(),
    authMode: authModeFor(headers),
  };
}

export function parseMcpJson(text: string): McpJsonImport {
  const entries = serverEntries(parseLenient(text));
  if (!entries.length) throw new McpJsonImportError("no_servers");
  const result: McpJsonImport = { servers: [], skipped: [] };
  for (const [key, server] of entries) {
    const read = readServer(key, server);
    if (typeof read === "string") {
      result.skipped.push({ name: key || registryName(server), reason: read });
    } else {
      result.servers.push(read);
    }
  }
  return result;
}
