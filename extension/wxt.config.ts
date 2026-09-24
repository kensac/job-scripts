import { defineConfig } from "wxt";

export default defineConfig({
  imports: false,
  manifest: {
    name: "Job Tracker Apply",
    description: "Fill application forms from your Job Tracker profile and saved answers.",
    permissions: ["storage", "scripting"],
    host_permissions: ["https://*/*", "https://www.kanishksachdev.com/*", "https://boards-api.greenhouse.io/*", "https://boards-api.eu.greenhouse.io/*"],
    action: { default_title: "Job Tracker Apply" },
    web_accessible_resources: [{ resources: ["panel.css"], matches: ["https://*/*"], use_dynamic_url: true }],
  },
});
