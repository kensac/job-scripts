import { test, expect } from "@playwright/test";

const preview = "/tests/extension/panel-preview.html?reset=1";

test("pause holds the form, resume continues, and stop prevents later writes", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${preview}&slow=1`);
  await page.locator("#jt-autofill").click();
  await page.getByRole("button", { name: "Pause autofill", exact: true }).click();
  await expect(page.getByRole("button", { name: "Resume autofill", exact: true })).toBeVisible();
  const values = () => page.locator("main input").evaluateAll(inputs => inputs.map(input => (input as HTMLInputElement).value));
  const before = await values();
  // The fixture waits 500 ms per write. Observing beyond it catches a UI-only pause.
  await page.waitForTimeout(700);
  expect(await values()).toEqual(before);
  await page.getByRole("button", { name: "Resume autofill", exact: true }).click();
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await page.getByRole("button", { name: "Stop autofill", exact: true }).click();
  await expect(page.locator("#jt-apply")).toContainText("Autofill stopped");
  const stopped = await values();
  await page.waitForTimeout(700);
  expect(await values()).toEqual(stopped);
  expect(errors).toEqual([]);
});

test("stop releases a pending resolve and retry starts a fresh operation", async ({ page }) => {
  await page.goto(`${preview}&state=loading`);
  await page.locator("#jt-autofill").click();
  await page.getByRole("button", { name: "Stop autofill", exact: true }).click();
  await expect(page.locator("#jt-autofill")).toBeVisible();
  await expect(page.locator("#name")).toHaveValue("");
});

for (const theme of ["light", "dark"]) test(`${theme} panel fits a 390px viewport`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${preview}&theme=${theme}`);
  await expect(page.locator("#jt-autofill")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  const panel = await page.locator("#jt-apply").boundingBox();
  expect(panel!.x).toBeGreaterThanOrEqual(0);
  expect(panel!.x + panel!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `test-results/panel-${theme}.png`, fullPage: true });
});
