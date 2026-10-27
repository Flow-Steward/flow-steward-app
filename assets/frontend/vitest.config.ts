import path from "node:path";

import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

const frontendExtensionManifest = resolveOptionalManifest(
  process.env.VITE_FRONTEND_EXTENSION_MANIFEST,
);

function resolveOptionalManifest(value: string | undefined) {
  const candidate = String(value || "").trim();
  if (!candidate) {
    return path.resolve(__dirname, "./src/app/extension-manifest-empty.tsx");
  }
  return path.isAbsolute(candidate) ? candidate : path.resolve(__dirname, candidate);
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    fs: {
      allow: [path.resolve(__dirname), path.resolve(__dirname, "../..")],
    },
  },
  resolve: {
    alias: [
      {
        find: "@flow-steward/frontend-extension-manifest",
        replacement: frontendExtensionManifest,
      },
      { find: "@", replacement: path.resolve(__dirname, "./src") },
      {
        find: /^@testing-library\/react$/,
        replacement: path.resolve(__dirname, "./node_modules/@testing-library/react/dist/index.js"),
      },
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
  test: {
    // The quality gate runs frontend-tests and backend-tests one after the other
    // (verify.py keeps Tier 3 serial), so the suite has the audit container to
    // itself. Four jsdom workers stay well inside its memory and cut the run
    // roughly in three; a fixed count keeps the gate the same on any host.
    maxWorkers: 4,
    environment: "jsdom",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["**/node_modules/**", "**/.git/**", "tests/e2e/**"],
    environmentOptions: {
      jsdom: {
        url: "http://localhost:3000/",
      },
    },
    globals: true,
    css: true,
    setupFiles: path.resolve(__dirname, "./src/test/setup.ts"),
  },
});
