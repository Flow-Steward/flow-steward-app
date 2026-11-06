/**
 * The Microsoft 365 Copilot agent package of the AI agents page (ADR 0063), built in the browser.
 *
 * One zip a Microsoft 365 administrator uploads: the app manifest (schema 1.30) at the
 * root with its two icons, a declarative agent (schema 1.9) with the Operator
 * instructions and skills, and an MCP plugin (schema 2.4) that names this
 * installation's MCP address and the OAuth registration the administrator made in
 * the Teams Developer Portal. Built from Microsoft's documentation; not yet uploaded
 * to a tenant.
 *
 * The package holds no secret: the registration ID only tells Microsoft which of the
 * tenant's own registrations to sign in with.
 */
import { createZip, type ZipEntry } from "@/lib/utils/zip";

// Microsoft's two icons, rendered once from the canonical brand mark
// (docs/agent-skills/flow-steward-operator/assets/icon.svg): 192×192 colour on
// white, and 32×32 white on transparent.
import colorIconUrl from "@/design-system/assets/brand/microsoft-365-color-192.png";
import outlineIconUrl from "@/design-system/assets/brand/microsoft-365-outline-32.png";
import { packageFiles } from "@/pages/settings/account/agent-package-archive";

const OPERATOR_PACKAGE_ID = "flow-steward-operator";
const REPOSITORY_URL = "https://github.com/Flow-Steward/flow-steward-app";
const ACCENT_COLOR = "#1268C9";
/** Microsoft's limit on a declarative agent's instructions. */
export const MAX_INSTRUCTIONS = 8000;
/** Microsoft's limit on skills per declarative agent. */
const MAX_SKILLS = 8;
/** The migration skill runs an offline converter, which needs a terminal Copilot does not have. */
const SKILLS_WITHOUT_TERMINAL = new Set(["flow-steward-migrate-workflow"]);
const PLATFORM_CLIENT_ID = /^fs-platform-[a-z0-9]{2,16}-([0-9a-f]{32})$/;
const REFERENCE_ID = /^[A-Za-z0-9+/=._-]{8,512}$/;

export const M365_INSTRUCTIONS = `You are the Flow Steward operator for this organisation. Flow Steward runs business automations ("workflows") in projects. You work only through the Flow Steward tools of this agent, with the access the signed-in person approved.

How to work:
- Start by listing the projects and workflows the person can see; never invent a project, workflow, run or job ID. Look IDs up with a tool first.
- Before you run a workflow, tell the person which workflow, in which project, with which inputs, and what it changes outside Flow Steward (sent messages, updated records, files). Run it only after they agree.
- After a run, report its status and its result. When a tool returns a report, it is shown to the person as a report; summarise its key figures in one or two sentences and do not repeat the whole table.
- A run that waits for an approval shows the approval request: say who must approve and where.
- Report a permission error, a missing project or a failed run plainly as an error with the message the tool gave. Do not retry with other IDs and do not work around it.
- When the person asks to build or change a workflow, follow the workflow authoring skill: describe the steps first, then create or update the workflow, then validate it before running it.
- Never ask for or repeat passwords, tokens or API keys. Connections and secrets are set up in Flow Steward itself.
- Answer in the person's language. Keep answers short and concrete.`;

export const M365_CONVERSATION_STARTERS = [
  { title: "My workflows", text: "List my Flow Steward projects and their enabled workflows." },
  { title: "Recent runs", text: "Which workflow runs failed in the last 7 days, and why?" },
  { title: "Run a workflow", text: "Run a workflow for me. Ask me which one and what inputs it needs first." },
  { title: "Build a workflow", text: "Help me build a new workflow. Ask me what it should do." },
];

export type M365PackageOptions = Readonly<{
  /** This installation's public MCP address. */
  mcpUrl: string;
  /** The platform client the administrator created on this page (`fs-platform-m365-…`). */
  clientId: string;
  /** The OAuth client registration ID the Teams Developer Portal gave back. */
  referenceId: string;
}>;

function json(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

/** The registration ID as Microsoft shows it, or "" when it cannot be one. */
export function normalizedReferenceId(value: string): string {
  const trimmed = value.trim();
  return REFERENCE_ID.test(trimmed) ? trimmed : "";
}

/**
 * A stable app ID per platform client: uploading a newer download of the same
 * registration updates the agent instead of adding a second one.
 */
export function appIdFor(clientId: string): string {
  const match = PLATFORM_CLIENT_ID.exec(clientId);
  if (!match) throw new Error("The Microsoft 365 client is not a platform client.");
  const hex = match[1];
  const variant = ((Number.parseInt(hex[16], 16) & 0x3) | 0x8).toString(16);
  return [hex.slice(0, 8), hex.slice(8, 12), `4${hex.slice(13, 16)}`, `${variant}${hex.slice(17, 20)}`, hex.slice(20, 32)].join("-");
}

function httpsUrl(value: string): URL {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("The MCP address must use HTTPS.");
  return url;
}

/** The Operator skills Copilot can use, at most Microsoft's limit, under `skills/`. */
export function skillEntries(): { entries: ZipEntry[]; folders: string[] } {
  const files = packageFiles(OPERATOR_PACKAGE_ID);
  const folders = [...new Set(Object.keys(files).filter((path) => path.startsWith("skills/") && path.endsWith("/SKILL.md")).map((path) => path.split("/")[1]))]
    .filter((name) => !SKILLS_WITHOUT_TERMINAL.has(name))
    .sort()
    .slice(0, MAX_SKILLS);
  const entries = Object.entries(files)
    .filter(([path]) => folders.includes(path.split("/")[1] ?? "") && path.startsWith("skills/"))
    .map(([path, content]) => ({ path, content }));
  return { entries, folders };
}

function appManifest(appId: string, origin: string, version: string): string {
  return json({
    $schema: "https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json",
    manifestVersion: "1.30",
    version,
    id: appId,
    developer: {
      name: "Flow Steward",
      websiteUrl: origin,
      privacyUrl: REPOSITORY_URL,
      termsOfUseUrl: REPOSITORY_URL,
    },
    icons: { color: "color.png", outline: "outline.png" },
    name: { short: "Flow Steward", full: "Flow Steward Operator" },
    description: {
      short: "Run and build Flow Steward workflows from Copilot.",
      full: `Run, check and build the business automations of the Flow Steward installation at ${origin}. People sign in to Flow Steward and approve the access the first time they use the agent.`,
    },
    accentColor: ACCENT_COLOR,
    copilotAgents: { declarativeAgents: [{ id: "flowStewardOperator", file: "declarativeAgent.json" }] },
    validDomains: [new URL(origin).hostname],
  });
}

function declarativeAgent(folders: string[]): string {
  if (M365_INSTRUCTIONS.length > MAX_INSTRUCTIONS) throw new Error("The agent instructions are too long.");
  return json({
    $schema: "https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.9/schema.json",
    version: "v1.9",
    name: "Flow Steward",
    description: "Runs, checks and builds Flow Steward workflows with the access you approve.",
    instructions: M365_INSTRUCTIONS,
    conversation_starters: M365_CONVERSATION_STARTERS,
    actions: [{ id: "flowSteward", file: "ai-plugin.json" }],
    ...(folders.length ? { agent_skills: folders.map((name) => ({ folder: `./skills/${name}` })) } : {}),
  });
}

function mcpPlugin(mcpUrl: string, referenceId: string): string {
  return json({
    $schema: "https://developer.microsoft.com/json-schemas/copilot/plugin/v2.4/schema.json",
    schema_version: "v2.4",
    name_for_human: "Flow Steward",
    description_for_human: "Flow Steward projects, workflows, runs and reports.",
    namespace: "flowsteward",
    functions: [],
    runtimes: [
      {
        type: "RemoteMCPServer",
        spec: { url: mcpUrl },
        run_for_functions: ["*"],
        auth: { type: "OAuthPluginVault", reference_id: referenceId },
      },
    ],
  });
}

export type M365Icons = Readonly<{ color: Uint8Array; outline: Uint8Array }>;

export function m365PackageEntries(options: M365PackageOptions, version: string, icons: M365Icons): ZipEntry[] {
  const mcp = httpsUrl(options.mcpUrl);
  const referenceId = normalizedReferenceId(options.referenceId);
  if (!referenceId) throw new Error("The OAuth client registration ID is missing or malformed.");
  const { entries, folders } = skillEntries();
  return [
    { path: "manifest.json", content: appManifest(appIdFor(options.clientId), mcp.origin, version) },
    { path: "color.png", content: icons.color },
    { path: "outline.png", content: icons.outline },
    { path: "declarativeAgent.json", content: declarativeAgent(folders) },
    { path: "ai-plugin.json", content: mcpPlugin(mcp.href, referenceId) },
    ...entries,
  ];
}

async function assetBytes(url: string): Promise<Uint8Array> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`The icon ${url} could not be loaded.`);
  return new Uint8Array(await response.arrayBuffer());
}

/** The two icons, from the web UI's own assets. */
export async function loadM365Icons(): Promise<M365Icons> {
  const [color, outline] = await Promise.all([assetBytes(colorIconUrl), assetBytes(outlineIconUrl)]);
  return { color, outline };
}

export async function buildM365Package(options: M365PackageOptions, version: string): Promise<Blob> {
  const bytes = createZip(m365PackageEntries(options, version, await loadM365Icons()));
  return new Blob([bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer], { type: "application/zip" });
}
