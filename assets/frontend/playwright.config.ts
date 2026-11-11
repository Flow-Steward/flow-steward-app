import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: path.join(__dirname, "../../logs/test-runs/playwright/test-results"),
  reporter: "list",
  // One timeout policy for every browser journey, as vitest.config.ts keeps for
  // the unit suite: specs set no timeouts of their own. A gate host under load
  // took longer than Playwright's 5 s default to render an extension page, and
  // a journey makes several such waits, so the test bound leaves room for them.
  timeout: 120_000,
  expect: { timeout: 30_000 },
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:4173",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
          : {}),
      },
    },
  ],
});
