import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

test("an isolated content-script world receives page-world save receipts", async ({ page }) => {
  await page.goto("/tests/extension/autosave.html?receipts=1");
  await expect(page.locator("#name")).toBeVisible();
  const client = await page.context().newCDPSession(page);
  const { frameTree } = await client.send("Page.getFrameTree");
  const { executionContextId } = await client.send("Page.createIsolatedWorld", { frameId: frameTree.frame.id, worldName: "extension-save-fixture" });
  await client.send("Runtime.evaluate", { contextId: executionContextId, expression: readFileSync(new URL("../../.output/isolated-fill.js", import.meta.url), "utf8") });
  const result = await client.send("Runtime.evaluate", { contextId: executionContextId, expression: "fillIsolatedFixture()", awaitPromise: true, returnByValue: true });
  expect(result.exceptionDetails).toBeUndefined();
  expect(result.result.value).toBe(true);
  await expect(page.locator("#name-saved")).toHaveText("Isolated Person");
  await client.detach();
});

test("visible values with rejected saves are shown as unconfirmed", async ({ page }) => {
  await page.goto("/tests/extension/autosave.html?receipts=1&refused=1");
  await page.getByRole("button", { name: "Autofill this page" }).click();
  await expect(page.getByText("Some fields could not be confirmed. Review these before submitting:")).toBeVisible({ timeout: 20000 });
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await expect(page.locator("#name-saved")).toBeEmpty();
});

test("Ashby serializes field saves using response receipts", async ({ page }) => {
  await page.goto("/tests/extension/autosave.html?receipts=1");
  await page.getByRole("button", { name: "Autofill this page" }).click();
  await expect(page.locator("#linkedin-saved")).toHaveText("https://linkedin.com/in/example");
  await expect(page.locator("body")).toHaveAttribute("data-operation", "idle", { timeout: 20000 });
  await expect(page.locator("body")).toHaveAttribute("data-max-pending-saves", "1");
  await expect(page.locator("#name-saved")).toHaveText("Alex Morgan");
  await expect(page.locator("#email-saved")).toHaveText("alex@example.com");
});

test("a complete fill never clears asynchronously saved fields after a resume upload", async ({ page }) => {
  await page.goto("/tests/extension/autosave.html");
  await page.getByRole("button", { name: "Autofill this page" }).click();
  await expect(page.locator("#name-saved")).toHaveText("Alex Morgan");
  await expect(page.locator("body")).toHaveAttribute("data-operation", "idle", { timeout: 20000 });
  await expect(page.locator("#name")).toHaveValue("Alex Morgan");
  await expect(page.locator("#name-saved")).toHaveText("Alex Morgan");
  await expect(page.locator("#email-saved")).toHaveText("alex@example.com");
  await expect(page.locator("#linkedin-saved")).toHaveText("https://linkedin.com/in/example");
  expect(await page.locator("#resume").evaluate((input: HTMLInputElement) => input.files?.[0]?.name)).toBe("fixture.pdf");
  expect(await page.locator("body").getAttribute("data-blank-writes")).toBeNull();
});
