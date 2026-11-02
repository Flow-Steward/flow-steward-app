import { useI18n } from "@/lib/i18n/useI18n";
import { useState, type ClipboardEvent } from "react";

import { DsAlert, DsSelect } from "@/components/shared/design-system";

import {
  McpJsonImportError,
  parseMcpJson,
  type McpJsonImport,
  type McpJsonServer,
} from "./mcp-json-import";

type Notice = { tone: "success" | "warning"; text: string };

/** The paste box at the top of the New MCP form: a server's JSON config fills the fields below. */
export function McpJsonImportField({ onApply }: Readonly<{ onApply: (server: McpJsonServer) => void }>) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [servers, setServers] = useState<McpJsonServer[]>([]);
  const [chosen, setChosen] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);

  function filled(server: McpJsonServer): Notice {
    const base = t("react.registry.mcp_json.filled", "Filled from the JSON: {name}. Check the fields before saving.", { name: server.name || server.url });
    if (!server.headersToFill.length) return { tone: "success", text: base };
    return {
      tone: "warning",
      text: `${base} ${t("react.registry.mcp_json.headers_to_fill", "Enter the value of: {headers}.", { headers: server.headersToFill.join(", ") })}`,
    };
  }

  function skippedText(skipped: McpJsonImport["skipped"]): string {
    return skipped.map(({ name, reason }) => {
      if (reason === "stdio") return t("react.registry.mcp_json.skipped_stdio", "{name} runs as a local program (command). Only remote MCP servers reached over HTTP can be connected.", { name });
      if (reason === "sse") return t("react.registry.mcp_json.skipped_sse", "{name} uses the SSE transport. Use the server's streamable HTTP URL instead (it often ends in /mcp).", { name });
      return t("react.registry.mcp_json.skipped_no_url", "{name} has no URL.", { name });
    }).join(" ");
  }

  function choose(server: McpJsonServer) {
    setChosen(server.name);
    onApply(server);
    setNotice(filled(server));
  }

  function apply(source: string) {
    if (!source.trim()) return;
    let result: McpJsonImport;
    try {
      result = parseMcpJson(source);
    } catch (error) {
      const code = error instanceof McpJsonImportError ? error.code : "invalid_json";
      setServers([]);
      setNotice({
        tone: "warning",
        text: code === "no_servers"
          ? t("react.registry.mcp_json.no_servers", "No MCP server was found in this JSON.")
          : t("react.registry.mcp_json.invalid", "This text is not valid JSON."),
      });
      return;
    }
    setServers(result.servers);
    if (!result.servers.length) {
      setNotice({ tone: "warning", text: skippedText(result.skipped) });
      return;
    }
    choose(result.servers[0]);
  }

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    // Pasting into the empty box is the whole gesture; typed edits use the button.
    if (text.trim()) return;
    const pasted = event.clipboardData.getData("text");
    queueMicrotask(() => apply(pasted));
  }

  return (
    <div className="fs-field full">
      <label className="fs-field-label" htmlFor="mcp-create-json">{t("react.registry.mcp_json.label", "Paste JSON configuration")}</label>
      <textarea
        className="fs-textarea font-mono"
        id="mcp-create-json"
        onChange={(event) => setText(event.target.value)}
        onPaste={onPaste}
        placeholder={'{"mcpServers": {"github": {"url": "https://api.githubcopilot.com/mcp/"}}}'}
        spellCheck={false}
        value={text}
      />
      <span className="fs-field-helper">{t("react.registry.mcp_json.helper", "Optional. The config from a server's docs or from Claude, Cursor or VS Code fills the fields below.")}</span>
      <div className="flex flex-wrap items-center gap-2">
        <button className="fs-button secondary" disabled={!text.trim()} onClick={() => apply(text)} type="button">
          {t("react.registry.mcp_json.fill", "Fill the form")}
        </button>
        {servers.length > 1 ? (
          <DsSelect
            ariaLabel={t("react.registry.mcp_json.server", "Server from the JSON")}
            onChange={(value) => {
              const server = servers.find((item) => item.name === value);
              if (server) choose(server);
            }}
            options={servers.map((server) => ({ value: server.name, label: server.name }))}
            value={chosen}
          />
        ) : null}
      </div>
      {notice ? <DsAlert title={notice.text} tone={notice.tone} /> : null}
    </div>
  );
}
