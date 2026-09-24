import { test, expect } from "@playwright/test";

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
