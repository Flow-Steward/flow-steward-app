/**
 * The MCP Apps report viewer as one self-contained HTML file (task 3).
 *
 * Hosts load a View from a `ui://` resource in a sandboxed frame with no
 * network by default, so everything it needs is inside the file: the shared
 * report renderer, React, the chart and table engines, the design tokens and
 * the third-party notices. Web fonts are left out (system fonts are used), and
 * any file the inliner cannot place fails the build.
 */
import fs from "node:fs";
import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const VIEWER_HTML = "report-viewer.html";
const NOTICES = path.resolve(__dirname, "public/third-party-notices.txt");

/** Inline code must not close its own script element or open an HTML comment state. */
function escapeForScript(code: string): string {
  return code.replace(/<\/(script)/gi, "<\\/$1").replace(/<!--/g, "<\\!--");
}

function escapeForStyle(code: string): string {
  return code.replace(/<\/(style)/gi, "<\\/$1");
}

/** Removes @font-face rules after imports are inlined, before URLs are resolved. */
const dropWebFonts = {
  postcssPlugin: "flow-steward-viewer-drop-web-fonts",
  AtRule: {
    "font-face": (rule: { remove: () => void }) => {
      rule.remove();
    },
  },
};

function singleFileViewer(): Plugin {
  return {
    name: "flow-steward-viewer-single-file",
    enforce: "post",
    generateBundle(_options, bundle) {
      const page = bundle[VIEWER_HTML];
      if (!page || page.type !== "asset") {
        this.error(`${VIEWER_HTML} was not emitted`);
      }
      let html = String(page.source);
      for (const [fileName, item] of Object.entries(bundle)) {
        if (fileName === VIEWER_HTML) continue;
        const name = fileName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        if (item.type === "chunk" && item.isEntry) {
          const tag = new RegExp(`<script[^>]*src="[^"]*${name}"[^>]*></script>`);
          if (!tag.test(html)) this.error(`no script tag for ${fileName}`);
          html = html.replace(tag, () => `<script type="module">${escapeForScript(item.code)}</script>`);
          delete bundle[fileName];
        } else if (item.type === "asset" && fileName.endsWith(".css")) {
          const tag = new RegExp(`<link[^>]*href="[^"]*${name}"[^>]*>`);
          if (!tag.test(html)) this.error(`no stylesheet link for ${fileName}`);
          html = html.replace(tag, () => `<style>${escapeForStyle(String(item.source))}</style>`);
          delete bundle[fileName];
        }
      }
      const leftovers = Object.keys(bundle).filter((fileName) => fileName !== VIEWER_HTML);
      if (leftovers.length) {
        this.error(`the viewer must be one file; also emitted: ${leftovers.join(", ")}`);
      }
      const notices = fs.readFileSync(NOTICES, "utf-8");
      html = html.replace(
        "</head>",
        () =>
          `<script type="text/plain" id="fs-third-party-notices">${escapeForScript(notices)}</script>\n</head>`,
      );
      page.source = html;
    },
  };
}

export default defineConfig({
  base: "./",
  publicDir: false,
  plugins: [react(), singleFileViewer()],
  resolve: {
    alias: [{ find: "@", replacement: path.resolve(__dirname, "./src") }],
  },
  css: {
    postcss: { plugins: [dropWebFonts] },
  },
  build: {
    outDir: "dist-report-viewer",
    emptyOutDir: true,
    // Small images in the tokens (the brand loader) become data URIs.
    assetsInlineLimit: 100_000,
    cssCodeSplit: false,
    modulePreload: false,
    rollupOptions: {
      input: path.resolve(__dirname, VIEWER_HTML),
      output: { inlineDynamicImports: true },
    },
  },
});
