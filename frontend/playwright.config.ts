import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results/browser",
  fullyParallel: true,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:15174",
    channel:
      process.env.PW_CHANNEL ??
      (process.platform === "win32" ? "msedge" : undefined),
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 15174 --strictPort",
    url: "http://127.0.0.1:15174",
    reuseExistingServer: false,
  },
});
