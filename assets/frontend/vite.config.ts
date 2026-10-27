import path from "node:path";
import fs from "node:fs";

import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";

function parsePort(value: string | undefined, fallback: number) {
  const parsed = Number.parseInt(value || "", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

const backendTarget = process.env.VITE_BACKEND_TARGET || "https://localhost:8099";
const backendUrl = new URL(backendTarget);
const websocketTarget = `${backendUrl.protocol === "https:" ? "wss" : "ws"}://${backendUrl.host}`;
const devPort = parsePort(process.env.VITE_PORT, 5173);
const pollingInterval = parsePort(process.env.VITE_POLL_INTERVAL, 150);
const frontendExtensionManifest = resolveOptionalManifest(
  process.env.VITE_FRONTEND_EXTENSION_MANIFEST,
);
const platformKitRoot = path.resolve(__dirname, "../platform-kit");
const platformKitStaticPrefix = "/static/platform-kit/";

function resolveOptionalManifest(value: string | undefined) {
  const candidate = String(value || "").trim();
  if (!candidate) {
    return path.resolve(__dirname, "./src/app/extension-manifest-empty.tsx");
  }
  return path.isAbsolute(candidate) ? candidate : path.resolve(__dirname, candidate);
}

function platformKitContentType(filePath: string) {
  if (filePath.endsWith(".js")) {
    return "text/javascript; charset=utf-8";
  }
  if (filePath.endsWith(".css")) {
    return "text/css; charset=utf-8";
  }
  return "application/octet-stream";
}

function platformKitStaticPlugin(): Plugin {
  return {
    name: "flow-steward-platform-kit-static",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const requestPath = new URL(req.url || "/", "http://localhost").pathname;
        if (!requestPath.startsWith(platformKitStaticPrefix)) {
          next();
          return;
        }
        const relativePath = decodeURIComponent(requestPath.slice(platformKitStaticPrefix.length));
        const filePath = path.resolve(platformKitRoot, relativePath);
        const rootRelativePath = path.relative(platformKitRoot, filePath);
        if (rootRelativePath.startsWith("..") || path.isAbsolute(rootRelativePath)) {
          res.statusCode = 403;
          res.end("Forbidden");
          return;
        }
        fs.readFile(filePath, (error, body) => {
          if (error) {
            next();
            return;
          }
          res.setHeader("Content-Type", platformKitContentType(filePath));
          res.end(body);
        });
      });
    },
  };
}

export default defineConfig({
  base: "/app/",
  plugins: [platformKitStaticPlugin(), react(), tailwindcss()],
  server: {
    host: process.env.VITE_HOST || "0.0.0.0",
    port: devPort,
    strictPort: true,
    fs: {
      allow: [path.resolve(__dirname), path.resolve(__dirname, "../..")],
    },
    watch: {
      usePolling: process.env.VITE_USE_POLLING !== "0",
      interval: pollingInterval,
    },
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: websocketTarget,
        changeOrigin: true,
        secure: false,
        ws: true,
      },
    },
  },
  resolve: {
    alias: [
      {
        find: "@flow-steward/frontend-extension-manifest",
        replacement: frontendExtensionManifest,
      },
      { find: "@", replacement: path.resolve(__dirname, "./src") },
      { find: /^react$/, replacement: path.resolve(__dirname, "./node_modules/react/index.js") },
      {
        find: /^react\/jsx-dev-runtime$/,
        replacement: path.resolve(__dirname, "./node_modules/react/jsx-dev-runtime.js"),
      },
      {
        find: /^react\/jsx-runtime$/,
        replacement: path.resolve(__dirname, "./node_modules/react/jsx-runtime.js"),
      },
      {
        find: /^react-dom$/,
        replacement: path.resolve(__dirname, "./node_modules/react-dom/index.js"),
      },
      {
        find: /^react-dom\/client$/,
        replacement: path.resolve(__dirname, "./node_modules/react-dom/client.js"),
      },
    ],
  },
  build: {
    chunkSizeWarningLimit: 700,
    outDir: "dist",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          if (id.includes("/react/") || id.includes("/react-dom/") || id.includes("/scheduler/")) {
            return "react-vendor";
          }
          if (id.includes("/@tanstack/")) {
            return "query-vendor";
          }
          if (id.includes("/@xyflow/")) {
            return "flow-vendor";
          }
          if (id.includes("/lucide-react/")) {
            return "icons-vendor";
          }
          return "vendor";
        },
      },
    },
    sourcemap: false,
  },
});
