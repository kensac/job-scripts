import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  use: {
    baseURL: "http://127.0.0.1:8769",
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE },
  },
  webServer: {
    command: "python3 -m http.server 8769 --bind 127.0.0.1 --directory ..",
    url: "http://127.0.0.1:8769/tests/extension/panel-preview.html",
    reuseExistingServer: false,
  },
});
