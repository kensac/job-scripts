import { test, expect } from "@playwright/test";

test("existing submission is visible before filling", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1&applied=1");
  const notice = page.getByRole("status", { name: "Existing application" });
  await expect(notice).toContainText("You already submitted this application.");
  await expect(notice).toContainText("Application Submitted · 2026-09-01");
  await expect(page.locator("#name")).toHaveValue("");
});

test("completed autofill saves to the board without submitting", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByText("Saved to your board. Existing status and application date preserved.", { exact: true })).toBeVisible();
  await expect(page.locator("body")).toHaveAttribute("data-track-requests", "1");
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await expect(page.locator("#demo-submit")).toBeVisible();
});

test("a failed board save keeps the form and offers retry", async ({ page }) => {
  await page.goto("/tests/extension/panel-preview.html?reset=1&track-error=1");
  await page.locator("#jt-autofill").click();
  await expect(page.getByRole("button", { name: "Retry saving to board" })).toBeVisible();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await page.getByRole("button", { name: "Retry saving to board" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-track-requests", "2");
});
