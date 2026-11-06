import { describe, expect, it } from "vitest";

import { crc32 } from "@/lib/utils/zip";
import {
  M365_CONVERSATION_STARTERS,
  M365_INSTRUCTIONS,
  MAX_INSTRUCTIONS,
  appIdFor,
  buildM365Package,
  encodePng,
  iconPixels,
  m365PackageEntries,
  normalizedReferenceId,
  zlibStored,
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

function bytesOf(entries: ReturnType<typeof m365PackageEntries>, path: string): Uint8Array {
  const entry = entries.find((item) => item.path === path);
  if (!entry || typeof entry.content === "string") throw new Error(`no ${path}`);
  return entry.content;
}

/** Decode the PNGs this module writes: one IDAT of stored deflate blocks, RGBA, filter 0. */
function decodePng(png: Uint8Array): { width: number; height: number; pixels: Uint8Array } {
  expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
  const chunks = new Map<string, Uint8Array>();
  let position = 8;
  while (position < png.length) {
    const length = view.getUint32(position);
    const body = png.subarray(position + 4, position + 8 + length);
    expect(view.getUint32(position + 8 + length)).toBe(crc32(body));
    chunks.set(new TextDecoder().decode(body.subarray(0, 4)), body.subarray(4));
    position += 12 + length;
  }
  const header = chunks.get("IHDR")!;
  const headerView = new DataView(header.buffer, header.byteOffset, header.byteLength);
  const [width, height] = [headerView.getUint32(0), headerView.getUint32(4)];
  expect([...header.subarray(8)]).toEqual([8, 6, 0, 0, 0]);
  expect(chunks.has("IEND")).toBe(true);
  const stream = chunks.get("IDAT")!;
  expect([stream[0], stream[1]]).toEqual([0x78, 0x01]);
  const parts: Uint8Array[] = [];
  let offset = 2;
  for (;;) {
    const last = stream[offset];
    const size = stream[offset + 1] | (stream[offset + 2] << 8);
    expect(stream[offset + 3] | (stream[offset + 4] << 8)).toBe(~size & 0xffff);
    parts.push(stream.subarray(offset + 5, offset + 5 + size));
    offset += 5 + size;
    if (last) break;
  }
  const raw = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let at = 0;
  for (const part of parts) {
    raw.set(part, at);
    at += part.length;
  }
  let a = 1;
  let b = 0;
  for (const byte of raw) {
    a = (a + byte) % 65521;
    b = (b + a) % 65521;
  }
  expect(new DataView(stream.buffer, stream.byteOffset + offset, 4).getUint32(0)).toBe(((b << 16) | a) >>> 0);
  const stride = width * 4;
  const pixels = new Uint8Array(width * height * 4);
  for (let row = 0; row < height; row += 1) {
    expect(raw[row * (stride + 1)]).toBe(0);
    pixels.set(raw.subarray(row * (stride + 1) + 1, (row + 1) * (stride + 1)), row * stride);
  }
  return { width, height, pixels };
}

describe("Microsoft 365 agent package", () => {
  it("lays the app manifest, icons, agent and plugin at the root", () => {
    const entries = m365PackageEntries(OPTIONS, "1.0.2");
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
    const manifest = textOf(m365PackageEntries(OPTIONS, "1.0.2"), "manifest.json") as Json & {
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
    const entries = m365PackageEntries(OPTIONS, "1.0.2");
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
    const plugin = textOf(m365PackageEntries(OPTIONS, "1.0.2"), "ai-plugin.json");
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
    expect(() => m365PackageEntries({ ...OPTIONS, referenceId: "x" }, "1.0.2")).toThrow(/registration ID/);
    expect(() => m365PackageEntries({ ...OPTIONS, mcpUrl: "http://fs.example.com/mcp" }, "1.0.2")).toThrow(/HTTPS/);
  });

  it("draws a 192 px opaque colour icon and a 32 px white outline icon", () => {
    const entries = m365PackageEntries(OPTIONS, "1.0.2");
    const color = decodePng(bytesOf(entries, "color.png"));
    expect([color.width, color.height]).toEqual([192, 192]);
    const alphas = new Set<number>();
    for (let index = 3; index < color.pixels.length; index += 4) alphas.add(color.pixels[index]);
    expect([...alphas]).toEqual([255]);
    const outline = decodePng(bytesOf(entries, "outline.png"));
    expect([outline.width, outline.height]).toEqual([32, 32]);
    let marked = 0;
    for (let index = 0; index < outline.pixels.length; index += 4) {
      expect([...outline.pixels.subarray(index, index + 3)]).toEqual([255, 255, 255]);
      if (outline.pixels[index + 3] > 0) marked += 1;
    }
    expect(outline.pixels[3]).toBe(0);
    expect(marked).toBeGreaterThan(32 * 32 * 0.1);
    expect(marked).toBeLessThan(32 * 32 * 0.6);
  });

  it("splits large images into stored blocks and round-trips the pixels", () => {
    const pixels = iconPixels(192, false);
    expect(decodePng(encodePng(192, pixels)).pixels).toEqual(pixels);
    const data = new Uint8Array(0xffff * 2 + 3).map((_, index) => index & 0xff);
    const stream = zlibStored(data);
    expect(stream.length).toBe(2 + 3 * 5 + data.length + 4);
    expect(zlibStored(new Uint8Array()).length).toBe(2 + 5 + 4);
  });

  it("builds a zip a stored-zip reader opens", async () => {
    const files = readStoredZip(await blobBytes(buildM365Package(OPTIONS, "1.0.2")));
    expect(files.has("manifest.json")).toBe(true);
    expect(files.has("color.png")).toBe(true);
    expect(JSON.parse(files.get("ai-plugin.json") ?? "{}").namespace).toBe("flowsteward");
  });
});
