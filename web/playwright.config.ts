import { defineConfig, devices } from "@playwright/test";

const python = process.env.FASTCLAW_E2E_PYTHON || "python";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  outputDir: "/tmp/fastclaw-playwright-results",
  use: {
    baseURL: "http://127.0.0.1:19000",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: `${python} ../tests/e2e/fake_provider.py`,
      url: "http://127.0.0.1:19001",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `${python} ../tests/e2e/gateway_server.py`,
      url: "http://127.0.0.1:19000/healthz",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
