import { afterEach, describe, expect, it, vi } from "vitest";

import {
  M365_CONVERSATION_STARTERS,
  M365_INSTRUCTIONS,
  MAX_INSTRUCTIONS,
  appIdFor,
  buildM365Package,
  m365PackageEntries,
  normalizedReferenceId,
} from "@/pages/settings/account/m365-agent-package";
import { blobBytes, readStoredZip } from "@/test/zip-helpers";

const CLIENT_ID = "fs-platform-m365-0123456789abcdef0123456789abcdef";
const OPTIONS = { mcpUrl: "https://fs.example.com/mcp", clientId: CLIENT_ID, referenceId: "  OTVkNjMyZTEtYmU2Ny00NjYy  " };

type Json = Record<string, unknown>;

function textOf(entries: ReturnType<typeof m365PackageEntries>, path: string): Json {
  const entry = entries.find((item) => item.path === path);
  if (!entry || typeof entry.content !== "string") throw new Error(`no ${path}`);
  return JSON.parse(entry.content) as Json;
}

const ICONS = { color: new Uint8Array([1, 2, 3]), outline: new Uint8Array([4, 5]) };

describe("Microsoft 365 agent package", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lays the app manifest, icons, agent and plugin at the root", () => {
    const entries = m365PackageEntries(OPTIONS, "1.0.2", ICONS);
    expect(entries.slice(0, 5).map((entry) => entry.path)).toEqual([
      "manifest.json",
      "color.png",
      "outline.png",
      "declarativeAgent.json",
      "ai-plugin.json",
    ]);
    expect(entries.slice(5).every((entry) => entry.path.startsWith("skills/"))).toBe(true);
  });

  it("writes an app manifest within Microsoft's field limits", () => {
    const manifest = textOf(m365PackageEntries(OPTIONS, "1.0.2", ICONS), "manifest.json") as Json & {
      name: { short: string; full: string };
      description: { short: string; full: string };
      developer: { name: string; websiteUrl: string };
      copilotAgents: { declarativeAgents: Array<{ id: string; file: string }> };
    };
    expect(manifest.$schema).toBe("https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json");
    expect(manifest.manifestVersion).toBe("1.30");
    expect(manifest.version).toBe("1.0.2");
    expect(manifest.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(manifest.name.short.length).toBeLessThanOrEqual(30);
    expect(manifest.name.full.length).toBeLessThanOrEqual(100);
    expect(manifest.description.short.length).toBeLessThanOrEqual(80);
    expect(manifest.description.full.length).toBeLessThanOrEqual(4000);
    expect(manifest.developer.name.length).toBeLessThanOrEqual(32);
    expect(manifest.developer.websiteUrl).toBe("https://fs.example.com");
    expect(manifest.accentColor).toMatch(/^#[0-9A-Fa-f]{6}$/);
    expect(manifest.icons).toEqual({ color: "color.png", outline: "outline.png" });
    expect(manifest.validDomains).toEqual(["fs.example.com"]);
    expect(manifest.copilotAgents.declarativeAgents).toEqual([{ id: "flowStewardOperator", file: "declarativeAgent.json" }]);
  });

  it("keeps one app ID per platform client, so a newer download updates the agent", () => {
    expect(appIdFor(CLIENT_ID)).toBe("01234567-89ab-4def-8123-456789abcdef");
    expect(appIdFor(CLIENT_ID)).toBe(appIdFor(CLIENT_ID));
    expect(appIdFor("fs-platform-m365-" + "f".repeat(32))).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
    expect(() => appIdFor("https://claude.ai/oauth/client")).toThrow();
  });

  it("writes a declarative agent with the Operator instructions, starters and skills", () => {
    const entries = m365PackageEntries(OPTIONS, "1.0.2", ICONS);
    const agent = textOf(entries, "declarativeAgent.json") as Json & {
      instructions: string;
      conversation_starters: Array<{ title: string; text: string }>;
      agent_skills: Array<{ folder: string }>;
    };
    expect(agent.$schema).toBe("https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.9/schema.json");
    expect(agent.version).toBe("v1.9");
    expect(Object.keys(agent).sort()).toEqual(["$schema", "actions", "agent_skills", "conversation_starters", "description", "instructions", "name", "version"]);
    expect(agent.instructions).toBe(M365_INSTRUCTIONS);
    expect(M365_INSTRUCTIONS.length).toBeLessThanOrEqual(MAX_INSTRUCTIONS);
    expect(String(agent.description).length).toBeLessThanOrEqual(1000);
    expect(agent.conversation_starters).toEqual(M365_CONVERSATION_STARTERS);
    expect(agent.conversation_starters.length).toBeLessThanOrEqual(12);
    expect(agent.actions).toEqual([{ id: "flowSteward", file: "ai-plugin.json" }]);
    expect(agent.agent_skills.length).toBeGreaterThan(0);
    expect(agent.agent_skills.length).toBeLessThanOrEqual(8);
    for (const skill of agent.agent_skills) {
      const folder = skill.folder.replace(/^\.\//, "");
      expect(entries.some((entry) => entry.path === `${folder}/SKILL.md`)).toBe(true);
    }
    // The migration skill's converter needs a terminal; none of its files are packed.
    expect(agent.agent_skills.map((skill) => skill.folder)).not.toContain("./skills/flow-steward-migrate-workflow");
    expect(entries.some((entry) => entry.path.startsWith("skills/flow-steward-migrate-workflow/"))).toBe(false);
  });

  it("points the MCP plugin at this installation with the pasted registration", () => {
    const plugin = textOf(m365PackageEntries(OPTIONS, "1.0.2", ICONS), "ai-plugin.json");
    expect(plugin).toEqual({
      $schema: "https://developer.microsoft.com/json-schemas/copilot/plugin/v2.4/schema.json",
      schema_version: "v2.4",
      name_for_human: "Flow Steward",
      description_for_human: "Flow Steward projects, workflows, runs and reports.",
      namespace: "flowsteward",
      functions: [],
      runtimes: [
        {
          type: "RemoteMCPServer",
          spec: { url: "https://fs.example.com/mcp" },
          run_for_functions: ["*"],
          auth: { type: "OAuthPluginVault", reference_id: "OTVkNjMyZTEtYmU2Ny00NjYy" },
        },
      ],
    });
  });

  it.each([
    ["", ""],
    ["short", ""],
    ["has space inside", ""],
    ["<script>alert(1)</script>", ""],
    ["  abcDEF123+/=_-.x  ", "abcDEF123+/=_-.x"],
  ])("reads the registration ID %j as %j", (value, expected) => {
    expect(normalizedReferenceId(value)).toBe(expected);
  });

  it("refuses to build without a registration ID or with a plain-HTTP address", () => {
    expect(() => m365PackageEntries({ ...OPTIONS, referenceId: "x" }, "1.0.2", ICONS)).toThrow(/registration ID/);
    expect(() => m365PackageEntries({ ...OPTIONS, mcpUrl: "http://fs.example.com/mcp" }, "1.0.2", ICONS)).toThrow(/HTTPS/);
  });

  it("packs the icons it is given, as they are", () => {
    const entries = m365PackageEntries(OPTIONS, "1.0.2", ICONS);
    expect(entries.find((entry) => entry.path === "color.png")?.content).toBe(ICONS.color);
    expect(entries.find((entry) => entry.path === "outline.png")?.content).toBe(ICONS.outline);
  });

  it("loads the web UI's two icon assets and builds a zip a stored-zip reader opens", async () => {
    const fetched: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      fetched.push(String(input));
      return new Response(new Uint8Array([137, 80, 78, 71]));
    });
    const files = readStoredZip(await blobBytes(await buildM365Package(OPTIONS, "1.0.2")));
    expect(fetched).toHaveLength(2);
    expect(fetched[0]).toMatch(/microsoft-365-color-192\.png/);
    expect(fetched[1]).toMatch(/microsoft-365-outline-32\.png/);
    expect(files.has("manifest.json")).toBe(true);
    expect(files.has("color.png")).toBe(true);
    expect(JSON.parse(files.get("ai-plugin.json") ?? "{}").namespace).toBe("flowsteward");
  });
});
