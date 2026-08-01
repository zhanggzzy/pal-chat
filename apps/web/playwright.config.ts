import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "@playwright/test";

const apiPort = 18000;
const webPort = 18173;
const currentDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(currentDir, "..", "..");
const dataDir = path.join(repoRoot, ".tmp", "playwright-data");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "./scripts/run-playwright-backend.sh",
      cwd: repoRoot,
      env: {
        ...process.env,
        PAL_CHAT_API_PORT: String(apiPort),
        PAL_CHAT_WEB_PORT: String(webPort),
        PAL_CHAT_PLAYWRIGHT_DATA_DIR: dataDir,
      },
      url: `http://127.0.0.1:${apiPort}/health/ready`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      cwd: currentDir,
      env: {
        ...process.env,
        VITE_API_BASE: `http://127.0.0.1:${apiPort}`,
      },
      url: `http://127.0.0.1:${webPort}`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
